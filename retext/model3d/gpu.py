from __future__ import annotations

import colorsys
import math
import threading
from array import array
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

import numpy as np

from .geometry import (
    ModelGeometry,
    ModelMaterialSurface,
    ModelRenderCancelled,
)


class GpuRenderUnavailable(RuntimeError):
    """Raised when an OpenGL renderer cannot be created or used."""


@dataclass(slots=True)
class _GpuSurface:
    surface: ModelMaterialSurface
    uv_buffer: object
    index_buffer: object
    vertex_array: object
    texture: object | None
    vertex_count: int
    vertex_indices: tuple[int, ...]
    triangle_indices: np.ndarray
    center: tuple[float, float, float]

    @property
    def transparent(self) -> bool:
        return self.surface.blend_mode in {"blend", "additive"}


class ModernGLModelRenderer:
    """Thread-affine, off-screen OpenGL renderer for the Tk image viewport."""

    _VERTEX_SHADER = """
        #version 330
        in vec3 in_position;
        in vec2 in_uv;

        uniform vec3 u_center;
        uniform vec2 u_viewport;
        uniform vec2 u_pan;
        uniform float u_scale;
        uniform float u_depth_extent;
        uniform float u_cos_yaw;
        uniform float u_sin_yaw;
        uniform float u_cos_pitch;
        uniform float u_sin_pitch;

        out vec2 v_uv;

        void main() {
            vec3 p = in_position - u_center;
            float rotated_x = u_cos_yaw * p.x + u_sin_yaw * p.z;
            float yaw_z = -u_sin_yaw * p.x + u_cos_yaw * p.z;
            float rotated_y = u_cos_pitch * p.y - u_sin_pitch * yaw_z;
            float rotated_z = u_sin_pitch * p.y + u_cos_pitch * yaw_z;
            gl_Position = vec4(
                2.0 * (u_pan.x + rotated_x * u_scale) / u_viewport.x,
                2.0 * (-u_pan.y + rotated_y * u_scale) / u_viewport.y,
                -rotated_z / u_depth_extent,
                1.0
            );
            v_uv = in_uv;
        }
    """

    _FRAGMENT_SHADER = """
        #version 330
        in vec2 v_uv;

        uniform sampler2D u_texture;
        uniform int u_use_texture;
        uniform int u_wrap_s;
        uniform int u_wrap_t;
        uniform int u_blend_mode;
        uniform float u_alpha_cutoff;
        uniform vec3 u_flat_color;

        out vec4 f_color;

        float wrap_coordinate(float value, int mode) {
            if (mode == 0) {
                return fract(value);
            }
            if (mode == 2) {
                return clamp(value, 0.0, 1.0);
            }
            return 1.0 - abs(mod(value, 2.0) - 1.0);
        }

        void main() {
            vec4 sampled = vec4(u_flat_color, 1.0);
            if (u_use_texture != 0) {
                vec2 uv = vec2(
                    wrap_coordinate(v_uv.x, u_wrap_s),
                    wrap_coordinate(v_uv.y, u_wrap_t)
                );
                sampled = texture(u_texture, uv);
            }
            if (u_blend_mode == 1 && sampled.a < u_alpha_cutoff) {
                discard;
            }
            if (u_blend_mode <= 1) {
                sampled.a = 1.0;
            }
            f_color = sampled;
        }
    """

    def __init__(self) -> None:
        try:
            import moderngl
        except Exception as exc:  # pragma: no cover - depends on installation
            raise GpuRenderUnavailable(
                "未安装 ModernGL/glcontext。"
            ) from exc

        self._moderngl = moderngl
        self._thread_id = threading.get_ident()
        try:
            self._context = moderngl.create_context(
                standalone=True,
                require=330,
            )
            self._program = self._context.program(
                vertex_shader=self._VERTEX_SHADER,
                fragment_shader=self._FRAGMENT_SHADER,
            )
        except Exception as exc:
            raise GpuRenderUnavailable(
                f"无法创建 OpenGL 3.3 离屏上下文：{exc}"
            ) from exc
        info = self._context.info
        vendor = str(info.get("GL_VENDOR", "")).strip()
        renderer = str(info.get("GL_RENDERER", "")).strip()
        self.description = " / ".join(
            value for value in (vendor, renderer) if value
        ) or "OpenGL 3.3"
        self._geometry: ModelGeometry | None = None
        self._position_buffer = None
        self._gpu_surfaces: list[_GpuSurface] = []
        self._framebuffer = None
        self._framebuffer_size = (0, 0)
        self._closed = False

    def render(
        self,
        geometry: ModelGeometry,
        width: int,
        height: int,
        *,
        yaw: float,
        pitch: float,
        zoom: float,
        pan_x: float,
        pan_y: float,
        cancelled: Callable[[], bool] | None,
    ) -> Image.Image:
        if self._closed:
            raise GpuRenderUnavailable("OpenGL renderer has already been closed.")
        if threading.get_ident() != self._thread_id:
            raise GpuRenderUnavailable(
                "OpenGL 上下文被跨线程调用。"
            )
        if cancelled is not None and cancelled():
            raise ModelRenderCancelled
        width = max(64, int(width))
        height = max(64, int(height))
        zoom = min(max(float(zoom), 0.2), 8.0)
        self._prepare_geometry(geometry, cancelled=cancelled)
        framebuffer = self._prepare_framebuffer(width, height)
        framebuffer.use()
        framebuffer.depth_mask = True
        framebuffer.clear(
            32 / 255.0,
            28 / 255.0,
            24 / 255.0,
            1.0,
            depth=1.0,
        )

        center = tuple(
            (
                geometry.bounds_min[axis]
                + geometry.bounds_max[axis]
            )
            / 2.0
            for axis in range(3)
        )
        extent = max(
            geometry.bounds_max[axis] - geometry.bounds_min[axis]
            for axis in range(3)
        )
        if not math.isfinite(extent) or extent <= 1e-8:
            extent = 1.0
        scale = min(width, height) * 0.82 * zoom / extent
        uniforms = self._program
        uniforms["u_center"].value = center
        uniforms["u_viewport"].value = (float(width), float(height))
        uniforms["u_pan"].value = (float(pan_x), float(pan_y))
        uniforms["u_scale"].value = float(scale)
        uniforms["u_depth_extent"].value = float(extent * 2.0)
        uniforms["u_cos_yaw"].value = math.cos(yaw)
        uniforms["u_sin_yaw"].value = math.sin(yaw)
        uniforms["u_cos_pitch"].value = math.cos(pitch)
        uniforms["u_sin_pitch"].value = math.sin(pitch)
        uniforms["u_texture"].value = 0

        opaque = [
            item for item in self._gpu_surfaces
            if not item.transparent
        ]
        transparent = [
            item for item in self._gpu_surfaces
            if item.transparent
        ]
        vertex_positions = np.asarray(geometry.vertices, dtype=np.float64)
        for item in transparent:
            if item.surface.blend_mode != "blend":
                continue
            sorted_indices = _sorted_transparent_triangle_indices(
                vertex_positions,
                item.triangle_indices,
                yaw=yaw,
                pitch=pitch,
            )
            item.index_buffer.write(sorted_indices.tobytes())
            if cancelled is not None and cancelled():
                raise ModelRenderCancelled
        transparent.sort(
            key=lambda item: _surface_depth(
                item.center,
                yaw=yaw,
                pitch=pitch,
            )
        )
        self._context.enable_only(self._moderngl.DEPTH_TEST)
        self._context.depth_func = "<="
        for item in opaque:
            self._draw_surface(item, transparent=False)
            if cancelled is not None and cancelled():
                raise ModelRenderCancelled
        framebuffer.depth_mask = False
        for item in transparent:
            self._draw_surface(item, transparent=True)
            if cancelled is not None and cancelled():
                raise ModelRenderCancelled
        framebuffer.depth_mask = True
        pixels = framebuffer.read(components=3, alignment=1)
        return Image.frombytes("RGB", (width, height), pixels).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM
        )

    def _prepare_geometry(
        self,
        geometry: ModelGeometry,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if self._geometry is geometry:
            return
        if self._can_update_positions(geometry):
            self._update_positions(geometry, cancelled=cancelled)
            self._geometry = geometry
            return
        self._release_geometry()
        self._position_buffer = self._context.buffer(
            _position_payload(geometry.vertices)
        )
        surfaces = {
            surface.material_index: surface
            for surface in geometry.material_surfaces
        }
        if not surfaces:
            surfaces = {
                material_index: ModelMaterialSurface(
                    material_index=material_index,
                    texture=texture,
                )
                for material_index, texture in geometry.material_textures
            }
        faces_by_material: dict[int, list[tuple[int, int, int]]] = {}
        for a, b, c, material_index in geometry.faces:
            surface = surfaces.get(material_index)
            if surface is not None and surface.shadow_only:
                continue
            faces_by_material.setdefault(material_index, []).append(
                (a, b, c)
            )
        uv_channels = geometry.uv_channels or (
            (geometry.uvs,) if geometry.uvs else ()
        )
        for material_index, faces in faces_by_material.items():
            if cancelled is not None and cancelled():
                raise ModelRenderCancelled
            surface = surfaces.get(
                material_index,
                ModelMaterialSurface(material_index=material_index),
            )
            uv_values = (
                uv_channels[surface.uv_channel]
                if (
                    0 <= surface.uv_channel < len(uv_channels)
                    and len(uv_channels[surface.uv_channel])
                    == len(geometry.vertices)
                )
                else ()
            )
            can_texture = bool(
                surface.texture is not None
                and uv_values
                and all(
                    uv_values[index] is not None
                    for face in faces
                    for index in face
                )
            )
            vertex_indices = tuple(
                vertex_index
                for face in faces
                for vertex_index in face
            )
            triangle_indices = np.asarray(faces, dtype=np.uint32)
            uv_payload = array("f")
            for vertex_index in range(len(geometry.vertices)):
                uv = (
                    uv_values[vertex_index]
                    if can_texture and uv_values[vertex_index] is not None
                    else (0.0, 0.0)
                )
                assert uv is not None
                uv_payload.extend(uv)
            uv_buffer = self._context.buffer(uv_payload.tobytes())
            index_buffer = self._context.buffer(
                np.asarray(vertex_indices, dtype=np.uint32).tobytes()
            )
            vertex_array = self._context.vertex_array(
                self._program,
                [
                    (self._position_buffer, "3f", "in_position"),
                    (uv_buffer, "2f", "in_uv"),
                ],
                index_buffer=index_buffer,
                index_element_size=4,
            )
            texture = None
            if can_texture and surface.texture is not None:
                image = surface.texture.convert("RGBA")
                texture = self._context.texture(
                    image.size,
                    4,
                    image.tobytes(),
                    alignment=1,
                )
                texture.filter = (
                    self._moderngl.LINEAR,
                    self._moderngl.LINEAR,
                )
                texture.repeat_x = False
                texture.repeat_y = False
            self._gpu_surfaces.append(
                _GpuSurface(
                    surface=surface,
                    uv_buffer=uv_buffer,
                    index_buffer=index_buffer,
                    vertex_array=vertex_array,
                    texture=texture,
                    vertex_count=len(vertex_indices),
                    vertex_indices=vertex_indices,
                    triangle_indices=triangle_indices,
                    center=_surface_center(geometry.vertices, vertex_indices),
                )
            )
        self._geometry = geometry

    def _can_update_positions(self, geometry: ModelGeometry) -> bool:
        previous = self._geometry
        return bool(
            previous is not None
            and previous.faces is geometry.faces
            and previous.uvs is geometry.uvs
            and previous.uv_channels is geometry.uv_channels
            and previous.material_textures is geometry.material_textures
            and previous.material_surfaces is geometry.material_surfaces
            and len(previous.vertices) == len(geometry.vertices)
        )

    def _update_positions(
        self,
        geometry: ModelGeometry,
        *,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if cancelled is not None and cancelled():
            raise ModelRenderCancelled
        if self._position_buffer is None:
            raise GpuRenderUnavailable("GPU position buffer is unavailable.")
        self._position_buffer.write(_position_payload(geometry.vertices))
        for item in self._gpu_surfaces:
            if item.transparent:
                item.center = _surface_center(
                    geometry.vertices,
                    item.vertex_indices,
                )

    def _prepare_framebuffer(self, width: int, height: int):
        size = (width, height)
        if self._framebuffer is not None and self._framebuffer_size == size:
            return self._framebuffer
        if self._framebuffer is not None:
            self._framebuffer.release()
        self._framebuffer = self._context.simple_framebuffer(
            size,
            components=3,
        )
        self._framebuffer_size = size
        return self._framebuffer

    def _draw_surface(
        self,
        item: _GpuSurface,
        *,
        transparent: bool,
    ) -> None:
        surface = item.surface
        flags = self._moderngl.DEPTH_TEST
        if surface.cull_mode in {1, 2}:
            flags |= self._moderngl.CULL_FACE
            self._context.front_face = "ccw"
            self._context.cull_face = (
                "front" if surface.cull_mode == 1 else "back"
            )
        if transparent:
            flags |= self._moderngl.BLEND
            if surface.blend_mode == "additive":
                self._context.blend_func = (
                    self._moderngl.SRC_ALPHA,
                    self._moderngl.ONE,
                )
            else:
                self._context.blend_func = (
                    self._moderngl.SRC_ALPHA,
                    self._moderngl.ONE_MINUS_SRC_ALPHA,
                )
        self._context.enable_only(flags)
        self._program["u_use_texture"].value = int(
            item.texture is not None
        )
        self._program["u_wrap_s"].value = surface.wrap_s
        self._program["u_wrap_t"].value = surface.wrap_t
        self._program["u_blend_mode"].value = {
            "opaque": 0,
            "mask": 1,
            "blend": 2,
            "additive": 3,
        }.get(surface.blend_mode, 0)
        self._program["u_alpha_cutoff"].value = surface.alpha_cutoff
        self._program["u_flat_color"].value = tuple(
            value / 255.0
            for value in _material_color(surface.material_index)
        )
        if item.texture is not None:
            item.texture.use(location=0)
        item.vertex_array.render(
            mode=self._moderngl.TRIANGLES,
            vertices=item.vertex_count,
        )

    def _release_geometry(self) -> None:
        for item in self._gpu_surfaces:
            if item.texture is not None:
                item.texture.release()
            item.vertex_array.release()
            item.uv_buffer.release()
            item.index_buffer.release()
        self._gpu_surfaces.clear()
        if self._position_buffer is not None:
            self._position_buffer.release()
            self._position_buffer = None
        self._geometry = None

    def close(self) -> None:
        """Release all thread-affine OpenGL resources exactly once."""

        if self._closed:
            return
        if threading.get_ident() != self._thread_id:
            raise GpuRenderUnavailable(
                "OpenGL renderer must be closed on the thread that created it."
            )
        self._closed = True
        self._release_geometry()
        if self._framebuffer is not None:
            self._framebuffer.release()
            self._framebuffer = None
            self._framebuffer_size = (0, 0)
        self._program.release()
        self._context.release()


def _material_color(material_index: int) -> tuple[int, int, int]:
    hue = (material_index * 0.173 + 0.08) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.45, 0.92)
    return round(red * 255), round(green * 255), round(blue * 255)


def _position_payload(
    vertices: tuple[tuple[float, float, float], ...],
) -> bytes:
    return np.asarray(vertices, dtype=np.float32).tobytes()


def _surface_center(
    vertices: tuple[tuple[float, float, float], ...],
    indices: tuple[int, ...],
) -> tuple[float, float, float]:
    if not indices:
        return 0.0, 0.0, 0.0
    points = np.asarray(vertices, dtype=np.float64)[
        np.asarray(indices, dtype=np.uint32)
    ]
    center = points.mean(axis=0)
    return float(center[0]), float(center[1]), float(center[2])


def _surface_depth(
    center: tuple[float, float, float],
    *,
    yaw: float,
    pitch: float,
) -> float:
    x, y, z = center
    yaw_z = -math.sin(yaw) * x + math.cos(yaw) * z
    return math.sin(pitch) * y + math.cos(pitch) * yaw_z


def _sorted_transparent_triangle_indices(
    vertices: np.ndarray,
    triangles: np.ndarray,
    *,
    yaw: float,
    pitch: float,
) -> np.ndarray:
    """Return stable far-to-near EBO indices for an alpha-blended surface."""

    if triangles.size == 0:
        return np.empty(0, dtype=np.uint32)
    centers = vertices[triangles].mean(axis=1)
    yaw_z = -math.sin(yaw) * centers[:, 0] + math.cos(yaw) * centers[:, 2]
    depth = math.sin(pitch) * centers[:, 1] + math.cos(pitch) * yaw_z
    order = np.argsort(depth, kind="stable")
    return np.ascontiguousarray(triangles[order].reshape(-1), dtype=np.uint32)
