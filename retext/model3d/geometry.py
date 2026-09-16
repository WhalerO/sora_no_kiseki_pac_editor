from __future__ import annotations

import colorsys
import math
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw

from ..image_decode import open_asset_image


MAX_GEOMETRY_BUFFER_BYTES = 64 * 1024 * 1024
MAX_PRIMITIVE_RECORDS = 100_000
MAX_MESH_GROUPS = 100_000
DEFAULT_MAX_MODEL_TRIANGLES = 500_000
DEFAULT_MAX_MODEL_VERTICES = 1_000_000
MAX_ESTIMATED_GEOMETRY_BYTES = 512 * 1024 * 1024

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]
Color = tuple[int, int, int]
Face = tuple[int, int, int, int]
Quaternion = tuple[float, float, float, float]
Matrix4 = tuple[float, ...]
JointIndices = tuple[int, int, int, int]
JointWeights = tuple[float, float, float, float]
_IDENTITY_MATRIX: Matrix4 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


@dataclass(slots=True, frozen=True)
class ModelMaterialRenderInfo:
    material_index: int
    base_texture_name: str = ""
    uv_channel: int = 0
    wrap_s: int = 1
    wrap_t: int = 1
    blend_mode: str = "opaque"
    cull_mode: int = 0
    shadow_only: bool = False
    alpha_cutoff: float = 0.5


@dataclass(slots=True, frozen=True)
class ModelMaterialSurface:
    material_index: int
    texture: Image.Image | None = None
    uv_channel: int = 0
    wrap_s: int = 1
    wrap_t: int = 1
    blend_mode: str = "opaque"
    cull_mode: int = 0
    shadow_only: bool = False
    alpha_cutoff: float = 0.5


@dataclass(slots=True, frozen=True)
class ModelSkeleton:
    names: tuple[str, ...]
    parent_indices: tuple[int, ...]
    local_translations: tuple[Vec3, ...]
    local_rotations: tuple[Quaternion, ...]
    local_scales: tuple[Vec3, ...]
    inverse_bind_matrices: tuple[Matrix4, ...]
    node_types: tuple[int, ...] = ()
    mesh_group_indices: tuple[int, ...] = ()
    bind_world_matrices: tuple[Matrix4, ...] = ()
    noninvertible_bind_indices: tuple[int, ...] = ()


@dataclass(slots=True, frozen=True)
class ModelGeometry:
    vertices: tuple[Vec3, ...]
    faces: tuple[Face, ...]
    source_vertex_count: int
    source_triangle_count: int
    mesh_group_count: int
    primitive_count: int
    bounds_min: Vec3
    bounds_max: Vec3
    sampled: bool
    warnings: tuple[str, ...] = ()
    uvs: tuple[Vec2 | None, ...] = ()
    uv_channels: tuple[tuple[Vec2 | None, ...], ...] = ()
    vertex_normals: tuple[Vec3 | None, ...] = ()
    face_normals: tuple[Vec3, ...] = ()
    material_textures: tuple[tuple[int, Image.Image], ...] = ()
    material_surfaces: tuple[ModelMaterialSurface, ...] = ()
    textured_material_count: int = 0
    joint_indices: tuple[JointIndices, ...] = ()
    joint_weights: tuple[JointWeights, ...] = ()
    rigid_joint_indices: tuple[int, ...] = ()
    rigid_local_vertices: tuple[Vec3 | None, ...] = ()
    rigid_local_normals: tuple[Vec3 | None, ...] = ()
    skeleton: ModelSkeleton | None = None
    applied_pose_name: str = ""


@dataclass(slots=True, frozen=True)
class _Section:
    section_type: int
    payload_offset: int
    stored_size: int
    item_count: int

    @property
    def end_offset(self) -> int:
        return self.payload_offset + self.stored_size


@dataclass(slots=True, frozen=True)
class _PrimitiveBuffer:
    element_type: int
    size: int
    stride: int
    mesh_index: int
    submesh_index: int
    data_offset: int


@dataclass(slots=True, frozen=True)
class _MeshSkin:
    joint_names: tuple[str, ...]


class ModelRenderCancelled(RuntimeError):
    """Raised when a newer viewport request supersedes an active render."""


def load_model_geometry(
    path: str | Path,
    *,
    max_triangles: int | None = None,
) -> ModelGeometry | None:
    source_path = Path(path)
    triangle_limit = (
        DEFAULT_MAX_MODEL_TRIANGLES
        if max_triangles is None
        else max_triangles
    )
    if triangle_limit <= 0:
        raise ValueError("MDL 三维预览的三角形上限必须大于 0。")
    version, sections = _scan_sections(source_path)
    mesh_section = next(
        (section for section in sections if section.section_type == 1),
        None,
    )
    primitive_section = next(
        (section for section in sections if section.section_type == 4),
        None,
    )
    if mesh_section is None or primitive_section is None:
        return None
    if version != 4:
        raise ValueError("当前三维预览仅支持 Steam 资源使用的 MDL v4。")

    warnings: list[str] = []
    assignments, mesh_skins, mesh_group_count = _read_mesh_metadata(
        source_path,
        mesh_section,
    )
    try:
        skeleton = _read_skeleton(source_path, sections)
    except Exception as exc:
        skeleton = None
        warnings.append(f"节点蒙皮信息无法解析，已保留绑定网格：{exc}")
    skeleton_indices = (
        {name: index for index, name in enumerate(skeleton.names)}
        if skeleton is not None
        else {}
    )
    mesh_node_indices: dict[int, int] = {}
    bind_world_matrices: tuple[Matrix4, ...] = ()
    if skeleton is not None:
        if len(skeleton.bind_world_matrices) == len(skeleton.names):
            bind_world_matrices = skeleton.bind_world_matrices
        else:
            bind_world_matrices = tuple(
                _invert_affine_matrix(matrix)
                for matrix in skeleton.inverse_bind_matrices
            )
        if skeleton.noninvertible_bind_indices:
            warnings.append(
                f"{len(skeleton.noninvertible_bind_indices):,} 个零缩放或"
                "奇异绑定节点无法用于骨骼蒙皮；"
                "其刚性网格仍会按节点动画预览。"
            )
        if len(skeleton.mesh_group_indices) == len(skeleton.names):
            duplicate_mesh_groups: set[int] = set()
            for node_index, mesh_group_index in enumerate(
                skeleton.mesh_group_indices
            ):
                if mesh_group_index < 0:
                    continue
                if mesh_group_index in mesh_node_indices:
                    duplicate_mesh_groups.add(mesh_group_index)
                    continue
                mesh_node_indices[mesh_group_index] = node_index
            for mesh_group_index in duplicate_mesh_groups:
                mesh_node_indices.pop(mesh_group_index, None)
            if duplicate_mesh_groups:
                warnings.append(
                    "部分刚性网格同时关联到多个节点，"
                    "已保留其原始顶点坐标。"
                )
    buffers = _read_primitive_headers(source_path, primitive_section)
    grouped: dict[tuple[int, int], list[_PrimitiveBuffer]] = {}
    for buffer in buffers:
        if buffer.element_type not in {0, 1, 4, 5, 6, 7}:
            continue
        key = (buffer.mesh_index, buffer.submesh_index)
        grouped.setdefault(key, []).append(buffer)

    renderable: list[
        tuple[
            tuple[int, int],
            _PrimitiveBuffer,
            _PrimitiveBuffer,
            _PrimitiveBuffer | None,
            tuple[_PrimitiveBuffer, ...],
            _PrimitiveBuffer | None,
            _PrimitiveBuffer | None,
            int,
        ]
    ] = []
    source_vertex_count = 0
    source_triangle_count = 0
    maximum_uv_channel_count = 0
    for key, elements in grouped.items():
        positions = next(
            (element for element in elements if element.element_type == 0),
            None,
        )
        normals = next(
            (element for element in elements if element.element_type == 1),
            None,
        )
        uv_buffers = tuple(
            element for element in elements if element.element_type == 4
        )
        weights = next(
            (element for element in elements if element.element_type == 5),
            None,
        )
        joints = next(
            (element for element in elements if element.element_type == 6),
            None,
        )
        indices = next(
            (element for element in elements if element.element_type == 7),
            None,
        )
        if positions is None or indices is None:
            continue
        if positions.stride < 12 or positions.size % positions.stride:
            warnings.append(
                f"网格 {key[0]}/{key[1]} 的顶点缓冲步长无效，已跳过。"
            )
            continue
        if indices.stride != 4 or indices.size % indices.stride:
            warnings.append(
                f"网格 {key[0]}/{key[1]} 的索引缓冲格式无效，已跳过。"
            )
            continue
        vertex_count = positions.size // positions.stride
        if normals is not None and (
            normals.stride != 4
            or normals.size % normals.stride
            or normals.size // normals.stride < vertex_count
        ):
            warnings.append(
                f"网格 {key[0]}/{key[1]} 的法线缓冲格式无效，"
                "将使用无逐面明暗的基础材质。"
            )
            normals = None
        valid_uv_buffers: list[_PrimitiveBuffer] = []
        for uv_channel, uv_buffer in enumerate(uv_buffers):
            if (
                uv_buffer.stride != 8
                or uv_buffer.size % uv_buffer.stride
                or uv_buffer.size // uv_buffer.stride < vertex_count
            ):
                warnings.append(
                    f"网格 {key[0]}/{key[1]} 的 UV{uv_channel} "
                    "缓冲格式无效，已忽略该通道。"
                )
                continue
            valid_uv_buffers.append(uv_buffer)
        if (weights is None) != (joints is None):
            warnings.append(
                f"网格 {key[0]}/{key[1]} 的蒙皮权重或关节索引缺失，"
                "该子网格将保留绑定姿势。"
            )
            weights = None
            joints = None
        elif weights is not None and joints is not None:
            if (
                weights.stride != 16
                or weights.size % weights.stride
                or weights.size // weights.stride < vertex_count
                or joints.stride != 16
                or joints.size % joints.stride
                or joints.size // joints.stride < vertex_count
            ):
                warnings.append(
                    f"网格 {key[0]}/{key[1]} 的蒙皮缓冲格式无效，"
                    "该子网格将保留绑定姿势。"
                )
                weights = None
                joints = None
        maximum_uv_channel_count = max(
            maximum_uv_channel_count,
            len(valid_uv_buffers),
        )
        triangle_count = (indices.size // indices.stride) // 3
        if vertex_count <= 0 or triangle_count <= 0:
            continue
        source_vertex_count += vertex_count
        source_triangle_count += triangle_count
        renderable.append(
            (
                key,
                positions,
                indices,
                normals,
                tuple(valid_uv_buffers),
                weights,
                joints,
                assignments.get(key, -1),
            )
        )
    if not renderable or source_triangle_count <= 0:
        return None
    if source_vertex_count > DEFAULT_MAX_MODEL_VERTICES:
        raise ValueError(
            f"模型声明 {source_vertex_count:,} 个顶点，超过完整拓扑上限 "
            f"{DEFAULT_MAX_MODEL_VERTICES:,}；已在创建大型 Python 几何对象前"
            "停止载入。"
        )
    if source_triangle_count > triangle_limit:
        raise ValueError(
            f"模型包含 {source_triangle_count:,} 个三角形，超过完整拓扑"
            f"上限 {triangle_limit:,}；为避免通过丢弃面元产生破损、孔洞或"
            "面片化预览，已停止载入几何。"
        )
    estimated_bytes = (
        source_vertex_count * (240 + maximum_uv_channel_count * 48)
        + source_triangle_count * 160
    )
    if estimated_bytes > MAX_ESTIMATED_GEOMETRY_BYTES:
        raise ValueError(
            "模型完整拓扑的预计内存占用超过 512 MiB 安全预算；"
            "已停止载入，而不会抽样丢弃三角形。"
        )

    # Do not implement a triangle budget by taking every Nth triangle.
    # Character meshes are continuous indexed surfaces; dropping otherwise
    # valid faces punches visible holes into the skin and turns a moderately
    # dense model into scattered triangular patches.  Rendering quality is
    # reduced through framebuffer resolution and GPU reuse instead, while the
    # topology loaded here remains complete.
    selected_by_key: dict[
        tuple[int, int],
        tuple[list[tuple[int, int, int, int]], set[int]],
    ] = {}
    with source_path.open("rb") as stream:
        for (
            key,
            _positions,
            indices,
            _normals,
            _uv_buffers,
            _weights,
            _joints,
            material_index,
        ) in renderable:
            raw = _read_buffer(stream, indices, label="索引")
            triangle_count = len(raw) // 12
            selected: list[tuple[int, int, int, int]] = []
            needed_vertices: set[int] = set()
            for triangle_index in range(triangle_count):
                offset = triangle_index * 12
                a, b, c = struct.unpack_from("<III", raw, offset)
                selected.append((a, b, c, material_index))
                needed_vertices.update((a, b, c))
            if selected:
                selected_by_key[key] = selected, needed_vertices

        vertices: list[Vec3] = []
        vertex_uv_channels: list[list[Vec2 | None]] = [
            [] for _index in range(maximum_uv_channel_count)
        ]
        vertex_normals: list[Vec3 | None] = []
        vertex_joint_indices: list[JointIndices] = []
        vertex_joint_weights: list[JointWeights] = []
        vertex_rigid_joint_indices: list[int] = []
        vertex_rigid_local_vertices: list[Vec3 | None] = []
        vertex_rigid_local_normals: list[Vec3 | None] = []
        topology_vertices: list[Vec3] = []
        faces: list[Face] = []
        face_normals: list[Vec3] = []
        for (
            key,
            positions,
            _indices,
            normals,
            uv_buffers,
            weights,
            joints,
            _material_index,
        ) in renderable:
            selected_payload = selected_by_key.get(key)
            if selected_payload is None:
                continue
            selected, needed_vertices = selected_payload
            position_raw = _read_buffer(stream, positions, label="顶点")
            uv_raw_channels = tuple(
                _read_buffer(stream, uv_buffer, label=f"UV{channel_index}")
                for channel_index, uv_buffer in enumerate(uv_buffers)
            )
            normal_raw = (
                _read_buffer(stream, normals, label="法线")
                if normals is not None
                else None
            )
            weight_raw = (
                _read_buffer(stream, weights, label="蒙皮权重")
                if weights is not None
                else None
            )
            joint_raw = (
                _read_buffer(stream, joints, label="关节索引")
                if joints is not None
                else None
            )
            mesh_skin = mesh_skins.get(key[0])
            joint_palette = (
                tuple(
                    skeleton_indices.get(name, -1)
                    for name in mesh_skin.joint_names
                )
                if mesh_skin is not None
                else ()
            )
            vertex_count = positions.size // positions.stride
            remap: dict[int, int] = {}
            for source_index in sorted(needed_vertices):
                if source_index >= vertex_count:
                    continue
                offset = source_index * positions.stride
                point = struct.unpack_from("<3f", position_raw, offset)
                if not all(math.isfinite(value) for value in point):
                    continue
                if any(abs(value) > 100_000_000 for value in point):
                    continue
                remap[source_index] = len(vertices)
                vertices.append(point)
                for channel_index in range(maximum_uv_channel_count):
                    uv: Vec2 | None = None
                    if channel_index < len(uv_buffers):
                        uv_buffer = uv_buffers[channel_index]
                        uv_raw = uv_raw_channels[channel_index]
                        uv_offset = source_index * uv_buffer.stride
                        candidate = struct.unpack_from(
                            "<2f",
                            uv_raw,
                            uv_offset,
                        )
                        if all(math.isfinite(value) for value in candidate):
                            uv = candidate
                    vertex_uv_channels[channel_index].append(uv)
                normal: Vec3 | None = None
                if normal_raw is not None and normals is not None:
                    normal_offset = source_index * normals.stride
                    packed = struct.unpack_from(
                        "<3b",
                        normal_raw,
                        normal_offset,
                    )
                    candidate_normal = tuple(
                        max(-1.0, value / 127.0)
                        for value in packed
                    )
                    normal_length = math.sqrt(
                        sum(value * value for value in candidate_normal)
                    )
                    if normal_length > 1e-8:
                        normal = tuple(
                            value / normal_length
                            for value in candidate_normal
                        )
                vertex_normals.append(normal)
                resolved_joints: JointIndices = (-1, -1, -1, -1)
                resolved_weights: JointWeights = (0.0, 0.0, 0.0, 0.0)
                if (
                    weight_raw is not None
                    and joint_raw is not None
                    and weights is not None
                    and joints is not None
                    and joint_palette
                ):
                    source_weights = struct.unpack_from(
                        "<4f",
                        weight_raw,
                        source_index * weights.stride,
                    )
                    source_joints = struct.unpack_from(
                        "<4I",
                        joint_raw,
                        source_index * joints.stride,
                    )
                    resolved_joints = tuple(
                        joint_palette[index]
                        if index < len(joint_palette)
                        else -1
                        for index in source_joints
                    )
                    resolved_weights = tuple(
                        min(1.0, max(0.0, value))
                        if math.isfinite(value)
                        else 0.0
                        for value in source_weights
                    )
                vertex_joint_indices.append(resolved_joints)
                vertex_joint_weights.append(resolved_weights)
                has_valid_skin = any(
                    skeleton is not None
                    and 0 <= joint < len(skeleton.names)
                    and weight > 1e-8
                    for joint, weight in zip(
                        resolved_joints,
                        resolved_weights,
                    )
                )
                rigid_joint_index = (
                    mesh_node_indices.get(key[0], -1)
                    if not has_valid_skin
                    else -1
                )
                if (
                    rigid_joint_index >= 0
                    and rigid_joint_index < len(bind_world_matrices)
                ):
                    bind_world = bind_world_matrices[rigid_joint_index]
                    vertices[-1] = _transform_point(bind_world, point)
                    if vertex_normals[-1] is not None:
                        vertex_normals[-1] = _transform_normal(
                            bind_world,
                            vertex_normals[-1],
                        )
                vertex_rigid_joint_indices.append(rigid_joint_index)
                vertex_rigid_local_vertices.append(
                    point if rigid_joint_index >= 0 else None
                )
                vertex_rigid_local_normals.append(
                    normal if rigid_joint_index >= 0 else None
                )
                topology_vertices.append(
                    point
                    if rigid_joint_index >= 0
                    else vertices[-1]
                )
            for a, b, c, material_index in selected:
                if a == b or b == c or a == c:
                    continue
                if a not in remap or b not in remap or c not in remap:
                    continue
                face = (
                    remap[a],
                    remap[b],
                    remap[c],
                    material_index,
                )
                normal = _face_normal(vertices, face)
                if normal is None:
                    normal = _face_normal(topology_vertices, face)
                if normal is None:
                    continue
                faces.append(face)
                face_normals.append(normal)

    if not vertices or not faces:
        return None
    bounds_min = tuple(min(point[axis] for point in vertices) for axis in range(3))
    bounds_max = tuple(max(point[axis] for point in vertices) for axis in range(3))
    uv_channels = tuple(
        tuple(channel_values)
        for channel_values in vertex_uv_channels
    )
    return ModelGeometry(
        vertices=tuple(vertices),
        faces=tuple(faces),
        source_vertex_count=source_vertex_count,
        source_triangle_count=source_triangle_count,
        mesh_group_count=mesh_group_count,
        primitive_count=len(renderable),
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        sampled=False,
        warnings=tuple(dict.fromkeys(warnings)),
        uvs=uv_channels[0] if uv_channels else (),
        uv_channels=uv_channels,
        vertex_normals=tuple(vertex_normals),
        face_normals=tuple(face_normals),
        joint_indices=tuple(vertex_joint_indices),
        joint_weights=tuple(vertex_joint_weights),
        rigid_joint_indices=tuple(vertex_rigid_joint_indices),
        rigid_local_vertices=tuple(vertex_rigid_local_vertices),
        rigid_local_normals=tuple(vertex_rigid_local_normals),
        skeleton=skeleton,
    )


def apply_model_pose(
    geometry: ModelGeometry,
    animation_path: str | Path,
) -> tuple[ModelGeometry, tuple[str, ...]]:
    """Apply the first key of each companion animation track to a skinned mesh.

    Facial companion MDLs store independent controller ranges on one timeline,
    so a single global timestamp is not a meaningful neutral pose.  The first
    key of every track is the controller's rest value.
    """

    skeleton = geometry.skeleton
    if skeleton is None:
        return geometry, ("MDL 不包含可用于伴随动画的节点层级。",)
    if (
        len(geometry.joint_indices) != len(geometry.vertices)
        or len(geometry.joint_weights) != len(geometry.vertices)
    ):
        return geometry, ("MDL 蒙皮缓冲不完整，无法应用伴随动画。",)
    source_path = Path(animation_path)
    try:
        translations, rotations, scales = _read_first_animation_keys(
            source_path
        )
    except Exception as exc:
        return geometry, (f"伴随动画 {source_path.name} 无法解析：{exc}",)

    matched_names = (
        set(translations) | set(rotations) | set(scales)
    ) & set(skeleton.names)
    if not matched_names:
        return geometry, (
            f"伴随动画 {source_path.name} 没有匹配当前模型的骨骼。",
        )

    return _apply_skeleton_transforms(
        geometry,
        translations=translations,
        rotations=rotations,
        scales=scales,
        pose_name=source_path.name,
    ), ()


def _apply_skeleton_transforms(
    geometry: ModelGeometry,
    *,
    translations: Mapping[str, Vec3],
    rotations: Mapping[str, Quaternion],
    scales: Mapping[str, Vec3],
    pose_name: str,
) -> ModelGeometry:
    skeleton = geometry.skeleton
    if skeleton is None:
        return geometry
    local_matrices: list[Matrix4] = []
    for index, name in enumerate(skeleton.names):
        local_matrices.append(
            _compose_matrix(
                translations.get(name, skeleton.local_translations[index]),
                (
                    _multiply_quaternion(
                        skeleton.local_rotations[index],
                        rotations[name],
                    )
                    if name in rotations
                    else skeleton.local_rotations[index]
                ),
                scales.get(name, skeleton.local_scales[index]),
            )
        )
    world_matrices = _world_matrices(
        tuple(local_matrices),
        skeleton.parent_indices,
    )
    skin_matrices = tuple(
        _multiply_matrix(
            world_matrices[index],
            skeleton.inverse_bind_matrices[index],
        )
        for index in range(len(skeleton.names))
    )

    posed_vertices: list[Vec3] = []
    posed_normals: list[Vec3 | None] = []
    source_normals = (
        geometry.vertex_normals
        if len(geometry.vertex_normals) == len(geometry.vertices)
        else (None,) * len(geometry.vertices)
    )
    for point, normal, joints, weights in zip(
        geometry.vertices,
        source_normals,
        geometry.joint_indices,
        geometry.joint_weights,
    ):
        influences = tuple(
            (joint, weight)
            for joint, weight in zip(joints, weights)
            if (
                0 <= joint < len(skin_matrices)
                and weight > 1e-8
            )
        )
        total_weight = sum(weight for _joint, weight in influences)
        if total_weight <= 1e-8:
            posed_vertices.append(point)
            posed_normals.append(normal)
            continue
        inverse_total = 1.0 / total_weight
        transformed_points = tuple(
            (
                _transform_point(skin_matrices[joint], point),
                weight * inverse_total,
                joint,
            )
            for joint, weight in influences
        )
        posed_vertices.append(
            tuple(
                sum(candidate[axis] * weight for candidate, weight, _ in transformed_points)
                for axis in range(3)
            )
        )
        if normal is None:
            posed_normals.append(None)
        else:
            transformed_normal = tuple(
                sum(
                    _transform_vector(skin_matrices[joint], normal)[axis]
                    * weight
                    * inverse_total
                    for joint, weight in influences
                )
                for axis in range(3)
            )
            normal_length = math.sqrt(
                sum(value * value for value in transformed_normal)
            )
            posed_normals.append(
                tuple(value / normal_length for value in transformed_normal)
                if normal_length > 1e-8
                else normal
            )

    bounds_min = tuple(
        min(point[axis] for point in posed_vertices)
        for axis in range(3)
    )
    bounds_max = tuple(
        max(point[axis] for point in posed_vertices)
        for axis in range(3)
    )
    face_normals = tuple(
        normal
        for face in geometry.faces
        if (normal := _face_normal(posed_vertices, face)) is not None
    )
    return replace(
        geometry,
        vertices=tuple(posed_vertices),
        vertex_normals=tuple(posed_normals),
        face_normals=face_normals,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        applied_pose_name=pose_name,
    )


def apply_model_textures(
    geometry: ModelGeometry,
    texture_paths: Mapping[int, str | Path],
    *,
    material_infos: Mapping[int, ModelMaterialRenderInfo] | None = None,
    maximum_texture_size: int = 512,
) -> tuple[ModelGeometry, tuple[str, ...]]:
    if maximum_texture_size <= 0:
        raise ValueError("MDL 贴图采样尺寸必须大于 0。")
    available_uv_channels = geometry.uv_channels or (
        (geometry.uvs,) if geometry.uvs else ()
    )
    if (
        texture_paths
        and not any(
            len(channel) == len(geometry.vertices)
            for channel in available_uv_channels
        )
    ):
        return geometry, ("MDL 没有可对应到顶点的 UV，无法加载贴图着色。",)
    used_materials = {face[3] for face in geometry.faces}
    requested = {
        material_index: Path(texture_path)
        for material_index, texture_path in texture_paths.items()
        if material_index in used_materials
    }
    infos = {
        material_index: info
        for material_index, info in (material_infos or {}).items()
        if material_index in used_materials
    }

    warnings: list[str] = []
    loaded_by_path: dict[Path, Image.Image] = {}
    material_textures: list[tuple[int, Image.Image]] = []
    loaded_by_material: dict[int, Image.Image] = {}
    for material_index, texture_path in requested.items():
        try:
            texture = loaded_by_path.get(texture_path)
            if texture is None:
                with open_asset_image(texture_path) as source:
                    texture = source.convert("RGBA")
                texture = texture.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                texture = _thumbnail_rgba_preserving_hidden_rgb(
                    texture,
                    maximum_texture_size,
                )
                loaded_by_path[texture_path] = texture
            material_textures.append((material_index, texture))
            loaded_by_material[material_index] = texture
        except Exception as exc:
            warnings.append(f"贴图 {texture_path.name} 无法读取：{exc}")

    material_surfaces: list[ModelMaterialSurface] = []
    for material_index in sorted(used_materials):
        info = infos.get(material_index)
        if info is None:
            material_surfaces.append(
                ModelMaterialSurface(
                    material_index=material_index,
                    texture=loaded_by_material.get(material_index),
                )
            )
            continue
        uv_channel = info.uv_channel
        if (
            loaded_by_material.get(material_index) is not None
            and (
                uv_channel < 0
                or uv_channel >= len(available_uv_channels)
                or len(available_uv_channels[uv_channel])
                != len(geometry.vertices)
            )
        ):
            warnings.append(
                f"材质 {material_index + 1} 请求 UV{uv_channel}，"
                "该通道不存在；已回退到 UV0。"
            )
            uv_channel = 0
        material_surfaces.append(
            ModelMaterialSurface(
                material_index=material_index,
                texture=loaded_by_material.get(material_index),
                uv_channel=uv_channel,
                wrap_s=info.wrap_s,
                wrap_t=info.wrap_t,
                blend_mode=info.blend_mode,
                cull_mode=info.cull_mode,
                shadow_only=info.shadow_only,
                alpha_cutoff=info.alpha_cutoff,
            )
        )

    return (
        replace(
            geometry,
            material_textures=tuple(material_textures),
            material_surfaces=tuple(material_surfaces),
            textured_material_count=len(material_textures),
        ),
        tuple(warnings),
    )


def _thumbnail_rgba_preserving_hidden_rgb(
    image: Image.Image,
    maximum_size: int,
) -> Image.Image:
    """Resize an RGBA game texture without discarding RGB below alpha zero.

    Falcom face textures use alpha as shader data rather than ordinary
    compositing transparency.  Pillow's direct RGBA resize premultiplies the
    color channels, which turns those still-meaningful hidden RGB pixels black.
    """

    if image.width <= maximum_size and image.height <= maximum_size:
        return image.copy()
    scale = min(maximum_size / image.width, maximum_size / image.height)
    target_size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    channels = tuple(
        channel.resize(target_size, Image.Resampling.BILINEAR)
        for channel in image.split()
    )
    return Image.merge("RGBA", channels)


def render_model_geometry(
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
    width = max(int(width), 64)
    height = max(int(height), 64)
    zoom = min(max(float(zoom), 0.2), 8.0)
    center = tuple(
        (geometry.bounds_min[axis] + geometry.bounds_max[axis]) / 2
        for axis in range(3)
    )
    extent = max(
        geometry.bounds_max[axis] - geometry.bounds_min[axis]
        for axis in range(3)
    )
    if not math.isfinite(extent) or extent <= 1e-8:
        extent = 1.0
    scale = min(width, height) * 0.82 * zoom / extent
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    cos_pitch = math.cos(pitch)
    sin_pitch = math.sin(pitch)
    origin_x = width / 2 + pan_x
    origin_y = height / 2 + pan_y
    transformed: list[tuple[float, float, float]] = []
    for vertex_index, (x, y, z) in enumerate(geometry.vertices):
        if (
            cancelled is not None
            and vertex_index % 2048 == 0
            and cancelled()
        ):
            raise ModelRenderCancelled
        x -= center[0]
        y -= center[1]
        z -= center[2]
        rotated_x = cos_yaw * x + sin_yaw * z
        yaw_z = -sin_yaw * x + cos_yaw * z
        rotated_y = cos_pitch * y - sin_pitch * yaw_z
        rotated_z = sin_pitch * y + cos_pitch * yaw_z
        transformed.append(
            (
                rotated_z,
                origin_x + rotated_x * scale,
                origin_y - rotated_y * scale,
            )
        )

    if wireframe:
        image = Image.new("RGB", (width, height), (32, 28, 24))
        draw = ImageDraw.Draw(image)
        material_colors: dict[int, Color] = {}
        surfaces = {
            surface.material_index: surface
            for surface in geometry.material_surfaces
        }
        for a, b, c, material_index in geometry.faces:
            surface = surfaces.get(material_index)
            if surface is not None and surface.shadow_only:
                continue
            color = material_colors.setdefault(
                material_index,
                _material_color(material_index),
            )
            draw.line(
                (
                    (transformed[a][1], transformed[a][2]),
                    (transformed[b][1], transformed[b][2]),
                    (transformed[c][1], transformed[c][2]),
                    (transformed[a][1], transformed[a][2]),
                ),
                fill=color,
                width=1,
            )
        return image

    surfaces = {
        surface.material_index: surface
        for surface in geometry.material_surfaces
    }
    available_uv_channels = geometry.uv_channels or (
        (geometry.uvs,) if geometry.uvs else ()
    )
    complete_uv_channels = {
        channel_index
        for channel_index, channel in enumerate(available_uv_channels)
        if (
            len(channel) == len(geometry.vertices)
            and all(uv is not None for uv in channel)
        )
    }
    fully_textured_materials = {
        material_index
        for material_index, surface in surfaces.items()
        if (
            surface.texture is not None
            and surface.uv_channel in complete_uv_channels
        )
    }
    needs_vertex_lighting = any(
        face[3] not in fully_textured_materials
        for face in geometry.faces
        if not (
            (surface := surfaces.get(face[3])) is not None
            and surface.shadow_only
        )
    )
    vertex_lighting = (
        _vertex_lighting(
            geometry,
            cos_yaw=cos_yaw,
            sin_yaw=sin_yaw,
            cos_pitch=cos_pitch,
            sin_pitch=sin_pitch,
        )
        if needs_vertex_lighting
        else ()
    )
    return _rasterize_model_surface(
        geometry,
        transformed,
        vertex_lighting,
        width,
        height,
        cancelled=cancelled,
    )


def _vertex_lighting(
    geometry: ModelGeometry,
    *,
    cos_yaw: float,
    sin_yaw: float,
    cos_pitch: float,
    sin_pitch: float,
) -> tuple[float, ...]:
    if len(geometry.vertex_normals) != len(geometry.vertices):
        return (0.86,) * len(geometry.vertices)
    light = (0.35, 0.72, 0.60)
    values: list[float] = []
    for normal in geometry.vertex_normals:
        if normal is None:
            values.append(0.86)
            continue
        normal_x, normal_y, normal_z = normal
        rotated_x = cos_yaw * normal_x + sin_yaw * normal_z
        yaw_z = -sin_yaw * normal_x + cos_yaw * normal_z
        rotated_y = cos_pitch * normal_y - sin_pitch * yaw_z
        rotated_z = sin_pitch * normal_y + cos_pitch * yaw_z
        strength = abs(
            rotated_x * light[0]
            + rotated_y * light[1]
            + rotated_z * light[2]
        )
        values.append(0.42 + min(1.0, strength) * 0.58)
    return tuple(values)


def _rasterize_model_surface(
    geometry: ModelGeometry,
    transformed: list[tuple[float, float, float]],
    vertex_lighting: tuple[float, ...],
    width: int,
    height: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> Image.Image:
    background = (32, 28, 24)
    color_buffer = bytearray(background) * (width * height)
    depth_buffer = [-math.inf] * (width * height)
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
    texture_infos = {
        material_index: (
            surface.texture.load(),
            surface.texture.width,
            surface.texture.height,
        )
        for material_index, surface in surfaces.items()
        if surface.texture is not None
    }
    uv_channels = geometry.uv_channels or (
        (geometry.uvs,) if geometry.uvs else ()
    )
    material_colors: dict[int, Color] = {}

    opaque_faces: list[Face] = []
    transparent_faces: list[Face] = []
    for face in geometry.faces:
        surface = surfaces.get(face[3])
        if surface is not None and surface.shadow_only:
            continue
        if (
            surface is not None
            and surface.blend_mode in {"blend", "additive"}
        ):
            transparent_faces.append(face)
        else:
            opaque_faces.append(face)
    transparent_faces.sort(
        key=lambda face: (
            transformed[face[0]][0]
            + transformed[face[1]][0]
            + transformed[face[2]][0]
        )
        / 3.0
    )

    rendered_face_count = 0

    def rasterize_face(face: Face) -> None:
        nonlocal rendered_face_count
        rendered_face_count += 1
        if (
            cancelled is not None
            and rendered_face_count % 128 == 0
            and cancelled()
        ):
            raise ModelRenderCancelled
        a, b, c, material_index = face
        surface = surfaces.get(material_index)
        if surface is None:
            surface = ModelMaterialSurface(material_index=material_index)
        z0, x0, y0 = transformed[a]
        z1, x1, y1 = transformed[b]
        z2, x2, y2 = transformed[c]
        area = _edge_value(x0, y0, x1, y1, x2, y2)
        if abs(area) < 0.25:
            return
        if surface.cull_mode == 2 and area < 0.0:
            return
        if surface.cull_mode == 1 and area > 0.0:
            return
        minimum_x = max(0, math.floor(min(x0, x1, x2)))
        maximum_x = min(width - 1, math.ceil(max(x0, x1, x2)))
        minimum_y = max(0, math.floor(min(y0, y1, y2)))
        maximum_y = min(height - 1, math.ceil(max(y0, y1, y2)))
        if minimum_x > maximum_x or minimum_y > maximum_y:
            return

        texture_info = texture_infos.get(material_index)
        uv_values = (
            uv_channels[surface.uv_channel]
            if (
                0 <= surface.uv_channel < len(uv_channels)
                and len(uv_channels[surface.uv_channel])
                == len(geometry.vertices)
            )
            else ()
        )
        uv0 = uv_values[a] if uv_values else None
        uv1 = uv_values[b] if uv_values else None
        uv2 = uv_values[c] if uv_values else None
        use_texture = bool(
            texture_info is not None
            and uv0 is not None
            and uv1 is not None
            and uv2 is not None
        )
        base_color = material_colors.setdefault(
            material_index,
            _material_color(material_index),
        )
        inverse_area = 1.0 / area
        start_x = minimum_x + 0.5
        start_y = minimum_y + 0.5
        row_b0 = (
            _edge_value(x1, y1, x2, y2, start_x, start_y)
            * inverse_area
        )
        row_b1 = (
            _edge_value(x2, y2, x0, y0, start_x, start_y)
            * inverse_area
        )
        row_b2 = (
            _edge_value(x0, y0, x1, y1, start_x, start_y)
            * inverse_area
        )
        b0_step_x = (y2 - y1) * inverse_area
        b1_step_x = (y0 - y2) * inverse_area
        b2_step_x = (y1 - y0) * inverse_area
        b0_step_y = -(x2 - x1) * inverse_area
        b1_step_y = -(x0 - x2) * inverse_area
        b2_step_y = -(x1 - x0) * inverse_area

        for y in range(minimum_y, maximum_y + 1):
            if (
                cancelled is not None
                and y % 16 == 0
                and cancelled()
            ):
                raise ModelRenderCancelled
            b0 = row_b0
            b1 = row_b1
            b2 = row_b2
            row_offset = y * width
            for x in range(minimum_x, maximum_x + 1):
                if b0 >= -1e-7 and b1 >= -1e-7 and b2 >= -1e-7:
                    pixel_index = row_offset + x
                    depth = b0 * z0 + b1 * z1 + b2 * z2
                    transparent = surface.blend_mode in {
                        "blend",
                        "additive",
                    }
                    depth_visible = (
                        depth + 1e-6 >= depth_buffer[pixel_index]
                        if transparent
                        else depth > depth_buffer[pixel_index]
                    )
                    if depth_visible:
                        alpha = 255
                        if use_texture:
                            assert texture_info is not None
                            assert uv0 is not None and uv1 is not None
                            assert uv2 is not None
                            texture_pixels, texture_width, texture_height = (
                                texture_info
                            )
                            u = _wrap_coordinate(
                                b0 * uv0[0] + b1 * uv1[0] + b2 * uv2[0],
                                surface.wrap_s,
                            )
                            v = _wrap_coordinate(
                                b0 * uv0[1] + b1 * uv1[1] + b2 * uv2[1],
                                surface.wrap_t,
                            )
                            texture_x = min(
                                texture_width - 1,
                                max(0, round(u * (texture_width - 1))),
                            )
                            texture_y = min(
                                texture_height - 1,
                                max(0, round(v * (texture_height - 1))),
                            )
                            red, green, blue, alpha = texture_pixels[
                                texture_x,
                                texture_y,
                            ]
                        else:
                            lighting = (
                                b0 * vertex_lighting[a]
                                + b1 * vertex_lighting[b]
                                + b2 * vertex_lighting[c]
                            )
                            red = round(base_color[0] * lighting)
                            green = round(base_color[1] * lighting)
                            blue = round(base_color[2] * lighting)
                        if (
                            surface.blend_mode == "mask"
                            and alpha / 255.0 < surface.alpha_cutoff
                        ):
                            b0 += b0_step_x
                            b1 += b1_step_x
                            b2 += b2_step_x
                            continue
                        color_offset = pixel_index * 3
                        if surface.blend_mode == "blend":
                            source_alpha = alpha / 255.0
                            inverse_alpha = 1.0 - source_alpha
                            color_buffer[color_offset] = round(
                                red * source_alpha
                                + color_buffer[color_offset] * inverse_alpha
                            )
                            color_buffer[color_offset + 1] = round(
                                green * source_alpha
                                + color_buffer[color_offset + 1] * inverse_alpha
                            )
                            color_buffer[color_offset + 2] = round(
                                blue * source_alpha
                                + color_buffer[color_offset + 2] * inverse_alpha
                            )
                        elif surface.blend_mode == "additive":
                            source_alpha = alpha / 255.0
                            color_buffer[color_offset] = min(
                                255,
                                round(
                                    color_buffer[color_offset]
                                    + red * source_alpha
                                ),
                            )
                            color_buffer[color_offset + 1] = min(
                                255,
                                round(
                                    color_buffer[color_offset + 1]
                                    + green * source_alpha
                                ),
                            )
                            color_buffer[color_offset + 2] = min(
                                255,
                                round(
                                    color_buffer[color_offset + 2]
                                    + blue * source_alpha
                                ),
                            )
                        else:
                            depth_buffer[pixel_index] = depth
                            color_buffer[color_offset] = red
                            color_buffer[color_offset + 1] = green
                            color_buffer[color_offset + 2] = blue
                b0 += b0_step_x
                b1 += b1_step_x
                b2 += b2_step_x
            row_b0 += b0_step_y
            row_b1 += b1_step_y
            row_b2 += b2_step_y

    for face in opaque_faces:
        rasterize_face(face)
    for face in transparent_faces:
        rasterize_face(face)
    return Image.frombytes("RGB", (width, height), bytes(color_buffer))


def _edge_value(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    x: float,
    y: float,
) -> float:
    return (x - x0) * (y1 - y0) - (y - y0) * (x1 - x0)


def _face_normal(
    vertices: tuple[Vec3, ...] | list[Vec3],
    face: Face,
) -> Vec3 | None:
    a, b, c, _material_index = face
    va = vertices[a]
    vb = vertices[b]
    vc = vertices[c]
    edge1 = tuple(vb[index] - va[index] for index in range(3))
    edge2 = tuple(vc[index] - va[index] for index in range(3))
    normal = (
        edge1[1] * edge2[2] - edge1[2] * edge2[1],
        edge1[2] * edge2[0] - edge1[0] * edge2[2],
        edge1[0] * edge2[1] - edge1[1] * edge2[0],
    )
    length = math.sqrt(sum(value * value for value in normal))
    if length <= 1e-12:
        return None
    return tuple(value / length for value in normal)


def _mirror_coordinate(value: float) -> float:
    wrapped = value % 2.0
    return 2.0 - wrapped if wrapped > 1.0 else wrapped


def _wrap_coordinate(value: float, mode: int) -> float:
    if mode == 0:
        return value % 1.0
    if mode == 2:
        return min(1.0, max(0.0, value))
    return _mirror_coordinate(value)


def _scan_sections(path: Path) -> tuple[int, tuple[_Section, ...]]:
    file_size = path.stat().st_size
    if file_size < 16:
        raise ValueError("MDL 文件过短。")
    sections: list[_Section] = []
    with path.open("rb") as stream:
        magic, version, _header_value = struct.unpack("<4sII", stream.read(12))
        if magic != b"MDL ":
            raise ValueError("不是支持的 MDL 文件。")
        offset = 12
        while True:
            if offset + 4 > file_size:
                raise ValueError("MDL 缺少结束标记。")
            stream.seek(offset)
            section_type = struct.unpack("<I", stream.read(4))[0]
            if section_type == 0xFFFFFFFF:
                break
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("MDL 区段头不完整。")
            stored_size, item_count = struct.unpack("<II", raw)
            if stored_size < 4:
                raise ValueError("MDL 区段长度无效。")
            payload_offset = offset + 8
            end_offset = payload_offset + stored_size
            if end_offset > file_size:
                raise ValueError("MDL 区段越过文件末尾。")
            sections.append(
                _Section(
                    section_type=section_type,
                    payload_offset=payload_offset,
                    stored_size=stored_size,
                    item_count=item_count,
                )
            )
            offset = end_offset
    return version, tuple(sections)


def _read_skeleton(
    path: Path,
    sections: tuple[_Section, ...],
) -> ModelSkeleton | None:
    section = next(
        (value for value in sections if value.section_type == 2),
        None,
    )
    if section is None:
        return None
    names: list[str] = []
    node_types: list[int] = []
    mesh_group_indices: list[int] = []
    translations: list[Vec3] = []
    rotations: list[Quaternion] = []
    scales: list[Vec3] = []
    child_indices: list[tuple[int, ...]] = []
    with path.open("rb") as stream:
        stream.seek(section.payload_offset)
        node_count = _read_u32(stream, "节点数量")
        if node_count != section.item_count or node_count > MAX_MESH_GROUPS:
            raise ValueError("MDL 节点数量与区段头不一致。")
        for _node_index in range(node_count):
            name_size = _read_exact(stream, 1, "节点名称长度")[0]
            names.append(
                _read_exact(stream, name_size, "节点名称").decode(
                    "utf-8",
                    errors="replace",
                )
            )
            node_type, mesh_group_index = struct.unpack(
                "<II",
                _read_exact(stream, 8, "节点类型和网格索引"),
            )
            node_types.append(node_type)
            mesh_group_indices.append(
                -1 if mesh_group_index == 0xFFFFFFFF else mesh_group_index
            )
            translations.append(
                struct.unpack("<3f", _read_exact(stream, 12, "节点位移"))
            )
            _read_exact(stream, 16, "节点附加四元数")
            _read_exact(stream, 4, "节点蒙皮索引")
            euler = struct.unpack(
                "<3f",
                _read_exact(stream, 12, "节点欧拉旋转"),
            )
            rotations.append(_euler_to_quaternion(euler))
            scales.append(
                struct.unpack("<3f", _read_exact(stream, 12, "节点缩放"))
            )
            _read_exact(stream, 12, "节点附加向量")
            child_count = _read_u32(stream, "子节点数量")
            if child_count > node_count:
                raise ValueError("MDL 子节点数量异常。")
            children = tuple(
                _read_u32(stream, "子节点索引")
                for _index in range(child_count)
            )
            child_indices.append(children)
        if stream.tell() != section.end_offset:
            raise ValueError("MDL 节点层级解析后存在未识别数据。")

    parents = [-1] * len(names)
    for parent_index, children in enumerate(child_indices):
        for child_index in children:
            if child_index >= len(names):
                raise ValueError("MDL 子节点索引越界。")
            if parents[child_index] != -1:
                raise ValueError("MDL 节点同时具有多个父节点。")
            parents[child_index] = parent_index
    local_matrices = tuple(
        _compose_matrix(translation, rotation, scale)
        for translation, rotation, scale in zip(
            translations,
            rotations,
            scales,
        )
    )
    world_matrices = _world_matrices(local_matrices, tuple(parents))
    inverse_bind_matrices: list[Matrix4] = []
    noninvertible_bind_indices: list[int] = []
    for node_index, matrix in enumerate(world_matrices):
        try:
            inverse_bind_matrices.append(_invert_affine_matrix(matrix))
        except ValueError:
            # Zero-scale nodes are common in effects that animate visibility.
            # Rigid meshes use their retained local vertices and the sampled
            # world matrix directly, so one singular bind node must not make
            # the complete hierarchy unavailable.
            inverse_bind_matrices.append(_IDENTITY_MATRIX)
            noninvertible_bind_indices.append(node_index)
    return ModelSkeleton(
        names=tuple(names),
        parent_indices=tuple(parents),
        local_translations=tuple(translations),
        local_rotations=tuple(rotations),
        local_scales=tuple(scales),
        inverse_bind_matrices=tuple(inverse_bind_matrices),
        node_types=tuple(node_types),
        mesh_group_indices=tuple(mesh_group_indices),
        bind_world_matrices=world_matrices,
        noninvertible_bind_indices=tuple(
            noninvertible_bind_indices
        ),
    )


def _read_first_animation_keys(
    path: Path,
) -> tuple[
    dict[str, Vec3],
    dict[str, Quaternion],
    dict[str, Vec3],
]:
    _version, sections = _scan_sections(path)
    section = next(
        (value for value in sections if value.section_type == 3),
        None,
    )
    if section is None:
        raise ValueError("文件不包含动画区段。")
    value_sizes = {9: 12, 10: 16, 11: 12, 12: 4, 13: 8}
    translations: dict[str, Vec3] = {}
    rotations: dict[str, Quaternion] = {}
    scales: dict[str, Vec3] = {}
    with path.open("rb") as stream:
        stream.seek(section.payload_offset)
        track_count = _read_u32(stream, "动画轨道数量")
        if track_count != section.item_count or track_count > 100_000:
            raise ValueError("MDL 动画轨道数量与区段头不一致。")
        for _track_index in range(track_count):
            track_name_size = _read_exact(
                stream,
                1,
                "动画轨道名称长度",
            )[0]
            _read_exact(stream, track_name_size, "动画轨道名称")
            bone_name_size = _read_exact(
                stream,
                1,
                "动画骨骼名称长度",
            )[0]
            bone_name = _read_exact(
                stream,
                bone_name_size,
                "动画骨骼名称",
            ).decode("utf-8", errors="replace")
            animation_type, _unknown0, _unknown1, frame_count = struct.unpack(
                "<4I",
                _read_exact(stream, 16, "动画轨道属性"),
            )
            value_size = value_sizes.get(animation_type)
            if value_size is None:
                raise ValueError(f"未知动画轨道类型 {animation_type}。")
            if frame_count > 1_000_000:
                raise ValueError("MDL 动画关键帧数量异常。")
            first_value: tuple[float, ...] | None = None
            for frame_index in range(frame_count):
                _read_exact(stream, 4, "动画关键帧时间")
                raw_value = _read_exact(
                    stream,
                    value_size,
                    "动画关键帧值",
                )
                if frame_index == 0:
                    first_value = struct.unpack(
                        f"<{value_size // 4}f",
                        raw_value,
                    )
                _read_exact(stream, 20, "动画关键帧附加数据")
            if first_value is None:
                continue
            if animation_type == 9:
                translations[bone_name] = first_value
            elif animation_type == 10:
                rotations[bone_name] = _normalize_quaternion(first_value)
            elif animation_type == 11:
                scales[bone_name] = first_value
        remaining = section.end_offset - stream.tell()
        if remaining not in {0, 8}:
            raise ValueError(
                f"MDL 动画区段解析后剩余 {remaining} 字节。"
            )
    return translations, rotations, scales


def _euler_to_quaternion(euler: Vec3) -> Quaternion:
    x, y, z = euler
    cy = math.cos(z * 0.5)
    sy = math.sin(z * 0.5)
    cp = math.cos(y * 0.5)
    sp = math.sin(y * 0.5)
    cr = math.cos(x * 0.5)
    sr = math.sin(x * 0.5)
    return _normalize_quaternion(
        (
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy,
        )
    )


def _normalize_quaternion(values: tuple[float, ...]) -> Quaternion:
    x, y, z, w = values
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1e-12 or not math.isfinite(length):
        return 0.0, 0.0, 0.0, 1.0
    return x / length, y / length, z / length, w / length


def _multiply_quaternion(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> Quaternion:
    """Apply one MDL delta quaternion after its bind-pose quaternion."""

    lx, ly, lz, lw = _normalize_quaternion(left)
    rx, ry, rz, rw = _normalize_quaternion(right)
    return _normalize_quaternion(
        (
            lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz,
        )
    )


def _compose_matrix(
    translation: Vec3,
    rotation: Quaternion,
    scale: Vec3,
) -> Matrix4:
    x, y, z, w = _normalize_quaternion(rotation)
    sx, sy, sz = scale
    return (
        (1.0 - 2.0 * (y * y + z * z)) * sx,
        (2.0 * (x * y - z * w)) * sy,
        (2.0 * (x * z + y * w)) * sz,
        translation[0],
        (2.0 * (x * y + z * w)) * sx,
        (1.0 - 2.0 * (x * x + z * z)) * sy,
        (2.0 * (y * z - x * w)) * sz,
        translation[1],
        (2.0 * (x * z - y * w)) * sx,
        (2.0 * (y * z + x * w)) * sy,
        (1.0 - 2.0 * (x * x + y * y)) * sz,
        translation[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _multiply_matrix(left: Matrix4, right: Matrix4) -> Matrix4:
    return tuple(
        sum(
            left[row * 4 + index] * right[index * 4 + column]
            for index in range(4)
        )
        for row in range(4)
        for column in range(4)
    )


def _world_matrices(
    local_matrices: tuple[Matrix4, ...],
    parents: tuple[int, ...],
) -> tuple[Matrix4, ...]:
    world: list[Matrix4 | None] = [None] * len(local_matrices)
    visiting: set[int] = set()

    def resolve(index: int) -> Matrix4:
        existing = world[index]
        if existing is not None:
            return existing
        if index in visiting:
            raise ValueError("MDL 节点层级包含循环。")
        visiting.add(index)
        parent = parents[index]
        matrix = local_matrices[index]
        if parent >= 0:
            if parent >= len(local_matrices):
                raise ValueError("MDL 父节点索引越界。")
            matrix = _multiply_matrix(resolve(parent), matrix)
        visiting.remove(index)
        world[index] = matrix
        return matrix

    return tuple(resolve(index) for index in range(len(local_matrices)))


def _invert_affine_matrix(matrix: Matrix4) -> Matrix4:
    a, b, c, tx = matrix[0:4]
    d, e, f, ty = matrix[4:8]
    g, h, i, tz = matrix[8:12]
    determinant = (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )
    if abs(determinant) <= 1e-12:
        raise ValueError("MDL 节点变换矩阵不可逆。")
    inverse = 1.0 / determinant
    r00 = (e * i - f * h) * inverse
    r01 = (c * h - b * i) * inverse
    r02 = (b * f - c * e) * inverse
    r10 = (f * g - d * i) * inverse
    r11 = (a * i - c * g) * inverse
    r12 = (c * d - a * f) * inverse
    r20 = (d * h - e * g) * inverse
    r21 = (b * g - a * h) * inverse
    r22 = (a * e - b * d) * inverse
    return (
        r00,
        r01,
        r02,
        -(r00 * tx + r01 * ty + r02 * tz),
        r10,
        r11,
        r12,
        -(r10 * tx + r11 * ty + r12 * tz),
        r20,
        r21,
        r22,
        -(r20 * tx + r21 * ty + r22 * tz),
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _transform_point(matrix: Matrix4, point: Vec3) -> Vec3:
    x, y, z = point
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _transform_vector(matrix: Matrix4, vector: Vec3) -> Vec3:
    x, y, z = vector
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z,
        matrix[4] * x + matrix[5] * y + matrix[6] * z,
        matrix[8] * x + matrix[9] * y + matrix[10] * z,
    )


def _transform_normal(matrix: Matrix4, normal: Vec3) -> Vec3:
    """Transform a normal with the inverse transpose of an affine matrix."""

    try:
        inverse = _invert_affine_matrix(matrix)
    except ValueError:
        return normal
    x, y, z = normal
    candidate = (
        inverse[0] * x + inverse[4] * y + inverse[8] * z,
        inverse[1] * x + inverse[5] * y + inverse[9] * z,
        inverse[2] * x + inverse[6] * y + inverse[10] * z,
    )
    length = math.sqrt(sum(value * value for value in candidate))
    if length <= 1e-12:
        return normal
    return tuple(value / length for value in candidate)


def _read_mesh_metadata(
    path: Path,
    section: _Section,
) -> tuple[
    dict[tuple[int, int], int],
    dict[int, _MeshSkin],
    int,
]:
    assignments: dict[tuple[int, int], int] = {}
    mesh_skins: dict[int, _MeshSkin] = {}
    with path.open("rb") as stream:
        stream.seek(section.payload_offset)
        mesh_count = _read_u32(stream, "网格组数量")
        if mesh_count != section.item_count or mesh_count > MAX_MESH_GROUPS:
            raise ValueError("MDL 网格组数量与区段头不一致。")
        for mesh_index in range(mesh_count):
            name_size = _read_exact(stream, 1, "网格名称长度")[0]
            _read_exact(stream, name_size, "网格名称")
            block_size = _read_u32(stream, "网格元数据长度")
            block_start = stream.tell()
            block_end = block_start + block_size
            if block_size < 8 or block_end > section.end_offset:
                raise ValueError("MDL 网格元数据块越界。")
            primitive_count = _read_u32(stream, "子网格数量")
            if primitive_count > MAX_PRIMITIVE_RECORDS:
                raise ValueError("MDL 子网格数量异常。")
            if 4 + primitive_count * 12 + 4 > block_size:
                raise ValueError("MDL 子网格记录超过网格元数据块。")
            for submesh_index in range(primitive_count):
                material_index, _index_count, _flags = struct.unpack(
                    "<III",
                    _read_exact(stream, 12, "子网格记录"),
                )
                assignments[(mesh_index, submesh_index)] = material_index
            joint_names: list[str] = []
            if stream.tell() + 4 <= block_end:
                joint_count = _read_u32(stream, "蒙皮关节数量")
                palette_valid = (
                    joint_count
                    <= (block_end - stream.tell()) // 65
                )
                for _joint_index in range(joint_count if palette_valid else 0):
                    joint_name_size = _read_exact(
                        stream,
                        1,
                        "蒙皮关节名称长度",
                    )[0]
                    if stream.tell() + joint_name_size + 64 > block_end:
                        palette_valid = False
                        break
                    joint_name = _read_exact(
                        stream,
                        joint_name_size,
                        "蒙皮关节名称",
                    ).decode("utf-8", errors="replace")
                    _read_exact(stream, 64, "蒙皮绑定矩阵")
                    joint_names.append(joint_name)
                if not palette_valid:
                    joint_names.clear()
            if joint_names:
                mesh_skins[mesh_index] = _MeshSkin(tuple(joint_names))
            stream.seek(block_end)
            collision_size = _read_u32(stream, "碰撞元数据长度")
            collision_end = stream.tell() + collision_size
            if collision_end > section.end_offset:
                raise ValueError("MDL 碰撞元数据块越界。")
            stream.seek(collision_end)
        if stream.tell() != section.end_offset:
            raise ValueError("MDL 网格区段解析后存在未识别数据。")
    return assignments, mesh_skins, mesh_count


def _read_primitive_headers(
    path: Path,
    section: _Section,
) -> tuple[_PrimitiveBuffer, ...]:
    with path.open("rb") as stream:
        stream.seek(section.payload_offset)
        record_count = _read_u32(stream, "图元缓冲数量")
        if (
            record_count != section.item_count
            or record_count > MAX_PRIMITIVE_RECORDS
        ):
            raise ValueError("MDL 图元缓冲数量与区段头不一致。")
        header_bytes = 4 + record_count * 20
        if header_bytes > section.stored_size:
            raise ValueError("MDL 图元缓冲目录越界。")
        records: list[tuple[int, int, int, int, int]] = []
        for _index in range(record_count):
            records.append(
                struct.unpack(
                    "<5I",
                    _read_exact(stream, 20, "图元缓冲记录"),
                )
            )
        data_offset = section.payload_offset + header_bytes
        buffers: list[_PrimitiveBuffer] = []
        for element_type, size, stride, mesh_index, submesh_index in records:
            if stride <= 0 or size % stride:
                raise ValueError("MDL 图元缓冲步长或长度无效。")
            end_offset = data_offset + size
            if end_offset > section.end_offset:
                raise ValueError("MDL 图元缓冲越过区段末尾。")
            buffers.append(
                _PrimitiveBuffer(
                    element_type=element_type,
                    size=size,
                    stride=stride,
                    mesh_index=mesh_index,
                    submesh_index=submesh_index,
                    data_offset=data_offset,
                )
            )
            data_offset = end_offset
        if data_offset != section.end_offset:
            raise ValueError("MDL 图元区段解析后存在未识别数据。")
    return tuple(buffers)


def _read_buffer(
    stream,
    buffer: _PrimitiveBuffer,
    *,
    label: str,
) -> bytes:
    if buffer.size > MAX_GEOMETRY_BUFFER_BYTES:
        raise ValueError(f"MDL {label}缓冲超过 64 MiB，拒绝整体载入。")
    stream.seek(buffer.data_offset)
    return _read_exact(stream, buffer.size, f"{label}缓冲")


def _read_u32(stream, label: str) -> int:
    return struct.unpack("<I", _read_exact(stream, 4, label))[0]


def _read_exact(stream, size: int, label: str) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"MDL {label}不完整。")
    return data


def _material_color(material_index: int) -> tuple[int, int, int]:
    if material_index < 0:
        return 176, 164, 148
    hue = (material_index * 0.618033988749895 + 0.07) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.42, 0.88)
    return round(red * 255), round(green * 255), round(blue * 255)
