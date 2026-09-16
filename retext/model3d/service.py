from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from PIL import Image

from .geometry import (
    ModelGeometry,
    ModelMaterialRenderInfo,
    ModelRenderCancelled,
    apply_model_textures,
    load_model_geometry,
    render_model_geometry,
)
from .animation import (
    ModelAnimationClip,
    ModelAnimationPlayer,
    load_model_animation,
    load_model_node_transforms,
)
from .gpu import ModernGLModelRenderer


class Model3DService:
    """Implementation-neutral entry point used by preview and GUI adapters."""

    def __init__(self) -> None:
        self._thread_state = threading.local()
        self._backend_lock = threading.Lock()
        self._last_backend_label = "尚未渲染"
        self._last_gpu_error = ""

    @property
    def backend_preference_label(self) -> str:
        if _gpu_disabled_by_environment():
            return "CPU（已由环境变量禁用 GPU）"
        return "GPU 优先（OpenGL 3.3，失败时自动回退 CPU）"

    @property
    def last_backend_label(self) -> str:
        with self._backend_lock:
            return self._last_backend_label

    @property
    def last_gpu_error(self) -> str:
        with self._backend_lock:
            return self._last_gpu_error

    @property
    def interaction_requires_low_resolution(self) -> bool:
        if _gpu_disabled_by_environment():
            return True
        with self._backend_lock:
            return self._last_backend_label.startswith("CPU")

    def load_geometry(
        self,
        path: str | Path,
        *,
        max_triangles: int | None = None,
    ) -> ModelGeometry | None:
        return load_model_geometry(path, max_triangles=max_triangles)

    def apply_textures(
        self,
        geometry: ModelGeometry,
        texture_paths: Mapping[int, str | Path],
        *,
        material_infos: Mapping[int, ModelMaterialRenderInfo] | None = None,
        maximum_texture_size: int = 512,
    ) -> tuple[ModelGeometry, tuple[str, ...]]:
        return apply_model_textures(
            geometry,
            texture_paths,
            material_infos=material_infos,
            maximum_texture_size=maximum_texture_size,
        )

    def apply_companion_pose(
        self,
        geometry: ModelGeometry,
        animation_path: str | Path,
    ) -> tuple[ModelGeometry, tuple[str, ...]]:
        try:
            clip = self.load_animation(animation_path)
            if clip is None:
                return geometry, ("伴随 MDL 不包含动画区段。",)
            player = self.create_animation_player(geometry, clip)
            warnings = (player.warning,) if player.warning else ()
            return player.sample(0.0), warnings
        except Exception as exc:
            return geometry, (
                f"伴随动画 {Path(animation_path).name} 无法应用：{exc}",
            )

    def load_animation(
        self,
        path: str | Path,
        *,
        name: str = "",
    ) -> ModelAnimationClip | None:
        return load_model_animation(path, name=name)

    @staticmethod
    def load_node_transforms(path: str | Path):
        return load_model_node_transforms(path)

    @staticmethod
    def create_animation_player(
        geometry: ModelGeometry,
        clip: ModelAnimationClip,
    ) -> ModelAnimationPlayer:
        return ModelAnimationPlayer(geometry, clip)

    def render(
        self,
        geometry: ModelGeometry,
        width: int,
        height: int,
        *,
        yaw: float,
        pitch: float,
        zoom: float,
        pan_x: float = 0.0,
        pan_y: float = 0.0,
        wireframe: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> Image.Image:
        if not wireframe and not _gpu_disabled_by_environment():
            state = self._thread_state
            if not getattr(state, "gpu_failed", False):
                renderer = getattr(state, "gpu_renderer", None)
                try:
                    if renderer is None:
                        renderer = ModernGLModelRenderer()
                        state.gpu_renderer = renderer
                    image = renderer.render(
                        geometry,
                        width,
                        height,
                        yaw=yaw,
                        pitch=pitch,
                        zoom=zoom,
                        pan_x=pan_x,
                        pan_y=pan_y,
                        cancelled=cancelled,
                    )
                    self._set_backend(
                        f"GPU · {renderer.description}",
                        gpu_error="",
                    )
                    return image
                except ModelRenderCancelled:
                    raise
                except Exception as exc:
                    # A context creation failure is normally persistent for the
                    # current process.  A model-specific upload/render failure
                    # is not: release that renderer and retry GPU on the next
                    # request instead of permanently degrading all models.
                    if renderer is None:
                        state.gpu_failed = True
                    else:
                        try:
                            renderer.close()
                        except Exception:
                            pass
                        if hasattr(state, "gpu_renderer"):
                            del state.gpu_renderer
                    self._set_backend(
                        (
                            "CPU 软件渲染（GPU 初始化失败）"
                            if renderer is None
                            else "CPU 软件渲染（当前 GPU 帧失败）"
                        ),
                        gpu_error=str(exc),
                    )
        elif wireframe:
            self._set_backend("CPU 线框绘制", gpu_error="")
        else:
            self._set_backend("CPU 软件渲染", gpu_error="")
        return render_model_geometry(
            geometry,
            width,
            height,
            yaw=yaw,
            pitch=pitch,
            zoom=zoom,
            pan_x=pan_x,
            pan_y=pan_y,
            wireframe=wireframe,
            cancelled=cancelled,
        )

    def close_current_thread(self) -> None:
        """Release a renderer owned by the calling render thread."""

        state = self._thread_state
        renderer = getattr(state, "gpu_renderer", None)
        if renderer is not None:
            renderer.close()
            del state.gpu_renderer
        state.gpu_failed = False

    def _set_backend(self, label: str, *, gpu_error: str) -> None:
        with self._backend_lock:
            self._last_backend_label = label
            self._last_gpu_error = gpu_error


model_3d_service = Model3DService()


def _gpu_disabled_by_environment() -> bool:
    return os.environ.get("TIS_RETEXT_DISABLE_GPU", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
