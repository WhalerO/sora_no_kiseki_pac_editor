from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass, replace
from pathlib import Path

from .geometry import (
    Matrix4,
    ModelGeometry,
    Quaternion,
    Vec3,
)

try:  # NumPy is an optional accelerator, not a runtime requirement.
    import numpy as _np
except Exception:  # pragma: no cover - depends on the local installation
    _np = None


MAX_ANIMATION_TRACKS = 100_000
MAX_TRACK_KEYFRAMES = 1_000_000
MAX_TOTAL_KEYFRAMES = 5_000_000

_TRACK_COMPONENT_COUNTS = {
    9: 3,   # translation
    10: 4,  # quaternion rotation (x, y, z, w)
    11: 3,  # scale
    12: 1,  # scalar controller
    13: 2,  # UV/controller vector
    14: 3,  # vector controller
}


@dataclass(slots=True, frozen=True)
class ModelAnimationKeyframe:
    time: float
    value: tuple[float, ...]
    mode: int
    extra_vector_a: tuple[float, float]
    extra_vector_b: tuple[float, float]


@dataclass(slots=True, frozen=True)
class ModelAnimationTrack:
    name: str
    bone_name: str
    track_type: int
    unknown_a: int
    unknown_b: int
    keyframes: tuple[ModelAnimationKeyframe, ...]

    @property
    def kind(self) -> str:
        return {
            9: "translation",
            10: "rotation",
            11: "scale",
            12: "scalar",
            13: "vector2",
            14: "vector3",
        }[self.track_type]

    @property
    def component_count(self) -> int:
        return _TRACK_COMPONENT_COUNTS[self.track_type]


@dataclass(slots=True, frozen=True)
class ModelAnimationNodeTransform:
    """The section-2 defaults used by KuroTools when importing a clip."""

    name: str
    translation: Vec3
    bind_rotation: Quaternion
    scale: Vec3


@dataclass(slots=True, frozen=True)
class ModelAnimationClip:
    name: str
    source_path: Path
    version: int
    start: float
    end: float
    tracks: tuple[ModelAnimationTrack, ...]
    node_transforms: tuple[ModelAnimationNodeTransform, ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def keyframe_count(self) -> int:
        return sum(len(track.keyframes) for track in self.tracks)

    @property
    def animated_bone_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                track.bone_name
                for track in self.tracks
                if track.track_type in {9, 10, 11} and track.bone_name
            )
        )


@dataclass(slots=True, frozen=True)
class ModelAnimationPose:
    relative_seconds: float
    absolute_seconds: float
    local_translations: tuple[Vec3, ...]
    local_rotations: tuple[Quaternion, ...]
    local_scales: tuple[Vec3, ...]
    joint_matrices: tuple[Matrix4, ...]
    world_matrices: tuple[Matrix4, ...] = ()


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
class _PreparedTrack:
    track_type: int
    times: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]
    modes: tuple[int, ...]


def load_model_animation(
    path: str | Path,
    *,
    name: str = "",
) -> ModelAnimationClip | None:
    """Parse the complete MDL v4 animation section.

    Key times and the section's start/end values are retained in their
    original absolute time domain.  ``ModelAnimationPlayer`` exposes the
    friendlier relative-seconds playback API.
    """

    source_path = Path(path).resolve()
    version, sections = _scan_sections(source_path)
    if version != 4:
        raise ValueError("当前动画预览仅支持 Steam 资源使用的 MDL v4。")
    animation_sections = tuple(
        section for section in sections if section.section_type == 3
    )
    if not animation_sections:
        return None
    if len(animation_sections) != 1:
        raise ValueError(
            f"MDL 包含 {len(animation_sections)} 个动画区段，"
            "当前格式预期至多一个。"
        )
    node_transforms = _read_animation_node_transforms(
        source_path,
        sections,
    )
    section = animation_sections[0]
    tracks: list[ModelAnimationTrack] = []
    total_keyframes = 0
    with source_path.open("rb") as stream:
        stream.seek(section.payload_offset)
        track_count = _read_u32(stream, "动画轨道数量")
        if (
            track_count != section.item_count
            or track_count > MAX_ANIMATION_TRACKS
        ):
            raise ValueError("MDL 动画轨道数量与区段头不一致或超出限制。")
        for track_index in range(track_count):
            prefix = f"动画轨道 {track_index + 1}"
            track_name = _read_text(stream, f"{prefix}名称")
            bone_name = _read_text(stream, f"{prefix}目标名称")
            (
                track_type,
                unknown_a,
                unknown_b,
                frame_count,
            ) = struct.unpack(
                "<4I",
                _read_exact(stream, 16, f"{prefix}属性"),
            )
            component_count = _TRACK_COMPONENT_COUNTS.get(track_type)
            if component_count is None:
                raise ValueError(
                    f"{prefix}使用未知类型 {track_type}；"
                    "当前仅支持类型 9–14。"
                )
            if frame_count > MAX_TRACK_KEYFRAMES:
                raise ValueError(f"{prefix}关键帧数量超出安全限制。")
            total_keyframes += frame_count
            if total_keyframes > MAX_TOTAL_KEYFRAMES:
                raise ValueError("MDL 动画关键帧总数超出安全限制。")
            keyframes: list[ModelAnimationKeyframe] = []
            for frame_index in range(frame_count):
                frame_prefix = f"{prefix}关键帧 {frame_index + 1}"
                time = struct.unpack(
                    "<f",
                    _read_exact(stream, 4, f"{frame_prefix}时间"),
                )[0]
                value = struct.unpack(
                    f"<{component_count}f",
                    _read_exact(
                        stream,
                        component_count * 4,
                        f"{frame_prefix}值",
                    ),
                )
                mode, extra_a0, extra_a1, extra_b0, extra_b1 = struct.unpack(
                    "<I4f",
                    _read_exact(stream, 20, f"{frame_prefix}附加数据"),
                )
                if not math.isfinite(time):
                    raise ValueError(f"{frame_prefix}时间不是有限数。")
                keyframes.append(
                    ModelAnimationKeyframe(
                        time=time,
                        value=tuple(value),
                        mode=mode,
                        extra_vector_a=(extra_a0, extra_a1),
                        extra_vector_b=(extra_b0, extra_b1),
                    )
                )
            tracks.append(
                ModelAnimationTrack(
                    name=track_name,
                    bone_name=bone_name,
                    track_type=track_type,
                    unknown_a=unknown_a,
                    unknown_b=unknown_b,
                    keyframes=tuple(keyframes),
                )
            )
        remaining = section.end_offset - stream.tell()
        if remaining != 8:
            raise ValueError(
                "MDL 动画区段缺少完整的 start/end，"
                f"轨道后剩余 {remaining} 字节。"
            )
        start, end = struct.unpack(
            "<2f",
            _read_exact(stream, 8, "动画起止时间"),
        )
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("MDL 动画起止时间不是有限数。")
        if end < start:
            raise ValueError(
                f"MDL 动画结束时间 {end:g} 早于开始时间 {start:g}。"
            )
        if stream.tell() != section.end_offset:
            raise ValueError("MDL 动画区段解析后仍有未识别数据。")
    return ModelAnimationClip(
        name=name.strip() or source_path.name,
        source_path=source_path,
        version=version,
        start=start,
        end=end,
        tracks=tuple(tracks),
        node_transforms=node_transforms,
    )


def load_model_node_transforms(
    path: str | Path,
) -> tuple[ModelAnimationNodeTransform, ...]:
    """Read section-2 node defaults without loading mesh buffers."""

    source_path = Path(path).resolve()
    version, sections = _scan_sections(source_path)
    if version != 4:
        raise ValueError("当前节点解析仅支持 Steam 资源使用的 MDL v4。")
    return _read_animation_node_transforms(source_path, sections)


def _read_animation_node_transforms(
    path: Path,
    sections: tuple[_Section, ...],
) -> tuple[ModelAnimationNodeTransform, ...]:
    node_sections = tuple(
        section for section in sections if section.section_type == 2
    )
    if not node_sections:
        return ()
    if len(node_sections) != 1:
        raise ValueError(
            f"MDL 包含 {len(node_sections)} 个节点区段，"
            "当前格式预期至多一个。"
        )
    section = node_sections[0]
    transforms: list[ModelAnimationNodeTransform] = []
    with path.open("rb") as stream:
        stream.seek(section.payload_offset)
        node_count = _read_u32(stream, "动画节点数量")
        if (
            node_count != section.item_count
            or node_count > MAX_ANIMATION_TRACKS
        ):
            raise ValueError("MDL 动画节点数量与区段头不一致或超出限制。")
        for node_index in range(node_count):
            prefix = f"动画节点 {node_index + 1}"
            name = _read_text(stream, f"{prefix}名称")
            _read_exact(stream, 8, f"{prefix}类型和网格索引")
            translation = struct.unpack(
                "<3f",
                _read_exact(stream, 12, f"{prefix}位移"),
            )
            # KuroTools names this field ``something`` and does not use it
            # when producing animation channels.  Keep matching that
            # behavior; the Euler field below supplies rotation_bp.
            _read_exact(stream, 16, f"{prefix}附加四元数")
            _read_exact(stream, 4, f"{prefix}蒙皮索引")
            euler = struct.unpack(
                "<3f",
                _read_exact(stream, 12, f"{prefix}欧拉旋转"),
            )
            scale = struct.unpack(
                "<3f",
                _read_exact(stream, 12, f"{prefix}缩放"),
            )
            _read_exact(stream, 12, f"{prefix}附加向量")
            child_count = _read_u32(stream, f"{prefix}子节点数量")
            if child_count > node_count:
                raise ValueError(f"{prefix}子节点数量异常。")
            _read_exact(
                stream,
                child_count * 4,
                f"{prefix}子节点索引",
            )
            transforms.append(
                ModelAnimationNodeTransform(
                    name=name,
                    translation=tuple(translation),
                    bind_rotation=_euler_to_quaternion(
                        tuple(euler)
                    ),
                    scale=tuple(scale),
                )
            )
        if stream.tell() != section.end_offset:
            raise ValueError("MDL 动画节点区段解析后存在未识别数据。")
    return tuple(transforms)


def _match_node_transforms(
    transforms: tuple[ModelAnimationNodeTransform, ...],
    exact_indices: dict[str, int],
    folded_indices: dict[str, int],
) -> dict[int, ModelAnimationNodeTransform]:
    matched: dict[int, ModelAnimationNodeTransform] = {}
    for transform in transforms:
        bone_index = exact_indices.get(transform.name)
        if bone_index is None:
            bone_index = folded_indices.get(transform.name.casefold())
        if bone_index is not None and bone_index not in matched:
            matched[bone_index] = transform
    return matched


def sample_animation_track(
    track: ModelAnimationTrack,
    absolute_seconds: float,
) -> tuple[float, ...] | None:
    """Sample one track in the MDL file's absolute time domain."""

    if not math.isfinite(absolute_seconds):
        raise ValueError("动画采样时间必须是有限数。")
    if not track.keyframes:
        return None
    prepared = _prepare_track(track)
    return _sample_prepared_track(prepared, absolute_seconds)


class ModelAnimationPlayer:
    """Bind a parsed clip to skinned or node-owned rigid geometry."""

    def __init__(
        self,
        geometry: ModelGeometry,
        clip: ModelAnimationClip,
    ) -> None:
        skeleton = geometry.skeleton
        if skeleton is None:
            raise ValueError("MDL 网格不包含可用于动画的骨骼层级。")
        vertex_count = len(geometry.vertices)
        has_skin_buffers = (
            len(geometry.joint_indices) == vertex_count
            and len(geometry.joint_weights) == vertex_count
        )
        has_rigid_nodes = (
            len(geometry.rigid_joint_indices) == vertex_count
        )
        if not has_skin_buffers and not has_rigid_nodes:
            raise ValueError(
                "MDL 网格既没有完整蒙皮缓冲，"
                "也没有逐顶点的刚性节点归属。"
            )
        bone_count = len(skeleton.names)
        skeleton_fields = (
            skeleton.parent_indices,
            skeleton.local_translations,
            skeleton.local_rotations,
            skeleton.local_scales,
            skeleton.inverse_bind_matrices,
        )
        if any(len(field) != bone_count for field in skeleton_fields):
            raise ValueError("MDL 骨骼层级的字段数量不一致。")

        exact_indices = {
            bone_name: index
            for index, bone_name in enumerate(skeleton.names)
        }
        folded_indices: dict[str, int] = {}
        ambiguous: set[str] = set()
        for index, bone_name in enumerate(skeleton.names):
            folded = bone_name.casefold()
            if folded in folded_indices:
                ambiguous.add(folded)
            else:
                folded_indices[folded] = index
        for folded in ambiguous:
            folded_indices.pop(folded, None)

        source_node_transforms = _match_node_transforms(
            clip.node_transforms,
            exact_indices,
            folded_indices,
        )
        prepared_by_bone: dict[tuple[int, int], _PreparedTrack] = {}
        track_templates: dict[
            tuple[int, int],
            ModelAnimationTrack,
        ] = {}
        merged_keyframes: dict[
            tuple[int, int],
            list[ModelAnimationKeyframe],
        ] = {}
        matched_names: list[str] = []
        unmatched_names: list[str] = []
        for track in clip.tracks:
            if track.track_type not in {9, 10, 11}:
                continue
            bone_index = exact_indices.get(track.bone_name)
            if bone_index is None:
                bone_index = folded_indices.get(track.bone_name.casefold())
            if bone_index is None:
                unmatched_names.append(track.bone_name)
                continue
            if track.keyframes:
                track_key = (bone_index, track.track_type)
                track_templates.setdefault(track_key, track)
                merged_keyframes.setdefault(track_key, []).extend(
                    track.keyframes
                )
                matched_names.append(skeleton.names[bone_index])

        # KuroTools appends every section-3 record for the same target and
        # channel to one bone_key_frames vector, then sorts the combined
        # vector in post_process_keys().  A single MDL can therefore split
        # one T/R/S channel over multiple records; treating the final record
        # as an override silently discards the earlier keys.
        for track_key, keyframes in merged_keyframes.items():
            bone_index, track_type = track_key
            source_transform = source_node_transforms.get(bone_index)
            if track_type == 9:
                default_value = (
                    source_transform.translation
                    if source_transform is not None
                    else skeleton.local_translations[bone_index]
                )
            elif track_type == 10:
                # Type-10 values are delta quaternions.  The default rotate
                # channel is identity; section 2 supplies the fixed/bind
                # pre-rotation.
                default_value = (0.0, 0.0, 0.0, 1.0)
            else:
                default_value = (
                    source_transform.scale
                    if source_transform is not None
                    else skeleton.local_scales[bone_index]
                )
            prepared_by_bone[track_key] = _prepare_track(
                replace(
                    track_templates[track_key],
                    keyframes=tuple(keyframes),
                ),
                start=clip.start,
                default_value=default_value,
            )

        self.geometry = geometry
        self.clip = clip
        self._tracks = prepared_by_bone
        self._source_node_transforms = source_node_transforms
        self.matched_bone_names = tuple(dict.fromkeys(matched_names))
        self.unmatched_bone_names = tuple(dict.fromkeys(unmatched_names))
        skeletal_targets = tuple(
            dict.fromkeys(
                track.bone_name
                for track in clip.tracks
                if (
                    track.track_type in {9, 10, 11}
                    and track.keyframes
                    and track.bone_name
                )
            )
        )
        self.skeletal_target_count = len(skeletal_targets)
        self.matched_target_count = len(self.matched_bone_names)
        self.compatibility_ratio = (
            self.matched_target_count / self.skeletal_target_count
            if self.skeletal_target_count
            else 0.0
        )
        animated_node_indices = {
            bone_index
            for bone_index, _track_type in self._tracks
        }
        affected_node_indices: set[int] = set()
        for node_index in range(bone_count):
            current = node_index
            visited: set[int] = set()
            while (
                0 <= current < bone_count
                and current not in visited
            ):
                if current in animated_node_indices:
                    affected_node_indices.add(node_index)
                    break
                visited.add(current)
                current = skeleton.parent_indices[current]

        skin_indices = (
            geometry.joint_indices
            if has_skin_buffers
            else ((-1, -1, -1, -1),) * vertex_count
        )
        skin_weights = (
            geometry.joint_weights
            if has_skin_buffers
            else ((0.0, 0.0, 0.0, 0.0),) * vertex_count
        )
        rigid_indices = (
            geometry.rigid_joint_indices
            if has_rigid_nodes
            else (-1,) * vertex_count
        )
        noninvertible_bind_indices = set(
            skeleton.noninvertible_bind_indices
        )
        skinned_vertex_count = 0
        rigid_vertex_count = 0
        affected_vertex_count = 0
        for joints, weights, rigid_joint in zip(
            skin_indices,
            skin_weights,
            rigid_indices,
        ):
            valid_skin_joints = tuple(
                joint
                for joint, weight in zip(joints, weights)
                if (
                    0 <= joint < bone_count
                    and joint not in noninvertible_bind_indices
                    and math.isfinite(weight)
                    and weight > 1e-8
                )
            )
            if valid_skin_joints:
                skinned_vertex_count += 1
                if any(
                    joint in affected_node_indices
                    for joint in valid_skin_joints
                ):
                    affected_vertex_count += 1
            elif 0 <= rigid_joint < bone_count:
                rigid_vertex_count += 1
                if rigid_joint in affected_node_indices:
                    affected_vertex_count += 1

        self.skinned_vertex_count = skinned_vertex_count
        self.rigid_vertex_count = rigid_vertex_count
        self.affected_vertex_count = affected_vertex_count
        animatable_vertex_count = (
            self.skinned_vertex_count + self.rigid_vertex_count
        )
        self.affected_vertex_ratio = (
            self.affected_vertex_count / animatable_vertex_count
            if animatable_vertex_count
            else 0.0
        )
        clip_stem = clip.source_path.stem.casefold()
        is_gamespace_clip = "_gs" in clip_stem
        geometry_override = bool(
            not is_gamespace_clip
            and self.affected_vertex_ratio >= 0.95
        )
        self.compatible = bool(
            self.skeletal_target_count
            and self.matched_target_count
            and self.affected_vertex_count
            and (
                self.compatibility_ratio >= 0.5
                or geometry_override
            )
        )
        if not self.skeletal_target_count:
            self.warning = "动画不包含可应用到模型节点的 T/R/S 轨道。"
        elif not self.matched_target_count:
            self.warning = "动画没有匹配当前网格的节点目标。"
        elif (
            self.compatibility_ratio < 0.5
            and not geometry_override
        ):
            self.warning = (
                f"动画仅匹配 {self.matched_target_count} / "
                f"{self.skeletal_target_count} 个节点目标，"
                f"仅覆盖 {self.affected_vertex_ratio:.1%} 的可动画顶点，"
                "与当前网格的兼容性过低。"
            )
        elif not (
            self.skinned_vertex_count or self.rigid_vertex_count
        ):
            self.warning = (
                "当前网格不包含可变形的有效蒙皮权重，"
                "也未关联到可动画的刚性节点。"
            )
        elif not self.affected_vertex_count:
            self.warning = (
                "匹配的动画轨道没有影响任何可渲染顶点。"
            )
        elif self.compatibility_ratio < 0.5:
            self.warning = (
                f"动画仅匹配 {self.matched_target_count} / "
                f"{self.skeletal_target_count} 个节点目标，"
                f"但覆盖 {self.affected_vertex_ratio:.1%} 的可动画顶点；"
                "已按实际几何影响判定为可播放。"
            )
        elif self.matched_target_count < self.skeletal_target_count:
            self.warning = (
                f"动画匹配 {self.matched_target_count} / "
                f"{self.skeletal_target_count} 个节点目标；"
                "未匹配轨道将保持绑定姿势。"
            )
        else:
            self.warning = ""
        self._numpy_cache = (
            _build_numpy_cache(geometry)
            if _np is not None
            else None
        )
        self._framing_bounds: tuple[Vec3, Vec3] | None = None

    @property
    def uses_numpy(self) -> bool:
        return self._numpy_cache is not None

    def sample_pose(
        self,
        relative_seconds: float,
        *,
        loop: bool = False,
    ) -> ModelAnimationPose:
        relative = _relative_clip_time(
            relative_seconds,
            self.clip.duration,
            loop=loop,
        )
        absolute = self.clip.start + relative
        skeleton = self.geometry.skeleton
        assert skeleton is not None
        translations = list(skeleton.local_translations)
        rotations = list(skeleton.local_rotations)
        scales = list(skeleton.local_scales)
        animated_bone_indices = {
            bone_index
            for bone_index, _track_type in self._tracks
        }
        for bone_index in animated_bone_indices:
            source_transform = self._source_node_transforms.get(bone_index)
            if source_transform is None:
                continue
            # KuroTools extracts the clip against the section-2 defaults
            # contained in the animation MDL before merging the resulting
            # animation channels into the companion mesh.
            translations[bone_index] = source_transform.translation
            rotations[bone_index] = source_transform.bind_rotation
            scales[bone_index] = source_transform.scale
        for (bone_index, track_type), track in self._tracks.items():
            value = _sample_prepared_track(track, absolute)
            if value is None:
                continue
            if track_type == 9:
                translations[bone_index] = value
            elif track_type == 10:
                # KuroTools' MDL implementation treats type-10 keys as a
                # delta from the node's bind-pose rotation, not as an
                # absolute local rotation:
                #
                #   current_rot = rotation_bp * current_rot
                #
                # See engines/kuro/mdl/model.h::post_process_keys().
                # Replacing the bind rotation directly makes pre-rotated
                # character bones (hips, spine and legs in particular) fold
                # roughly 90 degrees away from their intended pose.
                rotations[bone_index] = _multiply_quaternion(
                    (
                        self._source_node_transforms[
                            bone_index
                        ].bind_rotation
                        if bone_index in self._source_node_transforms
                        else skeleton.local_rotations[bone_index]
                    ),
                    value,
                )
            else:
                scales[bone_index] = value

        local_matrices = tuple(
            _compose_matrix(translation, rotation, scale)
            for translation, rotation, scale in zip(
                translations,
                rotations,
                scales,
            )
        )
        world_matrices = _world_matrices(
            local_matrices,
            skeleton.parent_indices,
        )
        joint_matrices = tuple(
            _multiply_matrix(
                world_matrices[index],
                skeleton.inverse_bind_matrices[index],
            )
            for index in range(len(skeleton.names))
        )
        return ModelAnimationPose(
            relative_seconds=relative,
            absolute_seconds=absolute,
            local_translations=tuple(translations),
            local_rotations=tuple(rotations),
            local_scales=tuple(scales),
            joint_matrices=joint_matrices,
            world_matrices=world_matrices,
        )

    def sample(
        self,
        relative_seconds: float,
        *,
        loop: bool = False,
    ) -> ModelGeometry:
        relative = _relative_clip_time(
            relative_seconds,
            self.clip.duration,
            loop=loop,
        )
        geometry = self._sample_unframed(relative)
        if self._framing_bounds is None:
            first = (
                geometry
                if relative <= 1e-9
                else self._sample_unframed(0.0)
            )
            framing_geometry = first
            if (
                self.clip.duration > 0.0
                and _bounds_max_extent(
                    first.bounds_min,
                    first.bounds_max,
                )
                <= 1e-9
            ):
                # Effects often use a zero-scale bind frame as a visibility
                # switch.  Keep that frame hidden, but derive a useful camera
                # from a later visible pose so playback is not clipped or
                # magnified around a zero-sized box.
                visible_bounds: list[tuple[Vec3, Vec3]] = []
                for fraction in (0.25, 0.5, 0.75, 1.0):
                    candidate_time = self.clip.duration * fraction
                    candidate = self._sample_unframed(candidate_time)
                    if (
                        _bounds_max_extent(
                            candidate.bounds_min,
                            candidate.bounds_max,
                        )
                        > 1e-9
                    ):
                        visible_bounds.append(
                            (
                                candidate.bounds_min,
                                candidate.bounds_max,
                            )
                        )
                if visible_bounds:
                    self._framing_bounds = (
                        tuple(
                            min(bounds[0][axis] for bounds in visible_bounds)
                            for axis in range(3)
                        ),
                        tuple(
                            max(bounds[1][axis] for bounds in visible_bounds)
                            for axis in range(3)
                        ),
                    )
            if self._framing_bounds is None:
                self._framing_bounds = (
                    framing_geometry.bounds_min,
                    framing_geometry.bounds_max,
                )
        return replace(
            geometry,
            bounds_min=self._framing_bounds[0],
            bounds_max=self._framing_bounds[1],
        )

    def _sample_unframed(
        self,
        relative_seconds: float,
    ) -> ModelGeometry:
        pose = self.sample_pose(relative_seconds)
        if self._numpy_cache is not None and _np is not None:
            return _skin_geometry_numpy(
                self.geometry,
                pose,
                self._numpy_cache,
                pose_name=self.clip.name,
            )
        return _skin_geometry_scalar(
            self.geometry,
            pose,
            pose_name=self.clip.name,
        )


def _prepare_track(
    track: ModelAnimationTrack,
    *,
    start: float | None = None,
    default_value: tuple[float, ...] | None = None,
) -> _PreparedTrack:
    ordered = sorted(
        enumerate(track.keyframes),
        key=lambda item: (item[1].time, item[0]),
    )
    times = [frame.time for _index, frame in ordered]
    values = [frame.value for _index, frame in ordered]
    modes = [frame.mode for _index, frame in ordered]
    if (
        start is not None
        and default_value is not None
        and times
        and times[0] > start
    ):
        times.insert(0, start)
        values.insert(0, default_value)
        modes.insert(0, 0)
    return _PreparedTrack(
        track_type=track.track_type,
        times=tuple(times),
        values=tuple(values),
        modes=tuple(modes),
    )


def _sample_prepared_track(
    track: _PreparedTrack,
    absolute_seconds: float,
) -> tuple[float, ...] | None:
    if not track.times:
        return None
    if absolute_seconds <= track.times[0]:
        value = track.values[0]
        return (
            _normalize_quaternion(value)
            if track.track_type == 10
            else value
        )
    if absolute_seconds >= track.times[-1]:
        value = track.values[-1]
        return (
            _normalize_quaternion(value)
            if track.track_type == 10
            else value
        )
    right = bisect.bisect_right(track.times, absolute_seconds)
    left = right - 1
    left_time = track.times[left]
    right_time = track.times[right]
    if right_time <= left_time:
        value = track.values[right]
        return (
            _normalize_quaternion(value)
            if track.track_type == 10
            else value
        )
    if track.modes[left] == 1:
        value = track.values[left]
        return (
            _normalize_quaternion(value)
            if track.track_type == 10
            else value
        )
    amount = (absolute_seconds - left_time) / (right_time - left_time)
    if track.track_type == 10:
        return _slerp(
            track.values[left],
            track.values[right],
            amount,
        )
    return tuple(
        left_value + (right_value - left_value) * amount
        for left_value, right_value in zip(
            track.values[left],
            track.values[right],
        )
    )


def _relative_clip_time(
    seconds: float,
    duration: float,
    *,
    loop: bool,
) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("动画采样时间必须是有效数字。") from exc
    if not math.isfinite(value):
        raise ValueError("动画采样时间必须是有限数。")
    if duration <= 0.0:
        return 0.0
    if loop:
        return value % duration
    return min(duration, max(0.0, value))


def _bounds_max_extent(bounds_min: Vec3, bounds_max: Vec3) -> float:
    return max(
        max(0.0, high - low)
        for low, high in zip(bounds_min, bounds_max)
    )


def _normalize_quaternion(values: tuple[float, ...]) -> Quaternion:
    if len(values) != 4:
        raise ValueError("旋转轨道的四元数必须包含四个分量。")
    x, y, z, w = values
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1e-12 or not math.isfinite(length):
        return 0.0, 0.0, 0.0, 1.0
    return x / length, y / length, z / length, w / length


def _euler_to_quaternion(euler: Vec3) -> Quaternion:
    """Match Assimp's Euler constructor used by KuroTools."""

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


def _multiply_quaternion(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> Quaternion:
    """Return the normalized Hamilton product ``left * right``."""

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


def _slerp(
    left: tuple[float, ...],
    right: tuple[float, ...],
    amount: float,
) -> Quaternion:
    first = _normalize_quaternion(left)
    second = _normalize_quaternion(right)
    dot = sum(a * b for a, b in zip(first, second))
    if dot < 0.0:
        second = tuple(-value for value in second)
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        return _normalize_quaternion(
            tuple(
                a + (b - a) * amount
                for a, b in zip(first, second)
            )
        )
    angle = math.acos(dot)
    sine = math.sin(angle)
    if abs(sine) <= 1e-12:
        return first
    left_weight = math.sin((1.0 - amount) * angle) / sine
    right_weight = math.sin(amount * angle) / sine
    return _normalize_quaternion(
        tuple(
            a * left_weight + b * right_weight
            for a, b in zip(first, second)
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


def _build_numpy_cache(geometry: ModelGeometry):
    assert _np is not None
    try:
        vertices = _np.asarray(geometry.vertices, dtype=_np.float64)
        vertex_count = len(geometry.vertices)
        joints = _np.asarray(
            (
                geometry.joint_indices
                if len(geometry.joint_indices) == vertex_count
                else ((-1, -1, -1, -1),) * vertex_count
            ),
            dtype=_np.intp,
        )
        weights = _np.asarray(
            (
                geometry.joint_weights
                if len(geometry.joint_weights) == vertex_count
                else ((0.0, 0.0, 0.0, 0.0),) * vertex_count
            ),
            dtype=_np.float64,
        )
        rigid_joints = _np.asarray(
            (
                geometry.rigid_joint_indices
                if len(geometry.rigid_joint_indices) == vertex_count
                else (-1,) * vertex_count
            ),
            dtype=_np.intp,
        )
        rigid_local_vertices = _np.asarray(
            (
                tuple(
                    local if local is not None else geometry.vertices[index]
                    for index, local in enumerate(
                        geometry.rigid_local_vertices
                    )
                )
                if len(geometry.rigid_local_vertices) == vertex_count
                else geometry.vertices
            ),
            dtype=_np.float64,
        )
        assert geometry.skeleton is not None
        bone_count = len(geometry.skeleton.names)
        if (
            vertices.shape != (vertex_count, 3)
            or joints.shape != (vertex_count, 4)
            or weights.shape != (vertex_count, 4)
            or rigid_joints.shape != (vertex_count,)
            or rigid_local_vertices.shape != (vertex_count, 3)
        ):
            return None
        valid = (
            (joints >= 0)
            & (joints < bone_count)
            & _np.isfinite(weights)
            & (weights > 1e-8)
        )
        for node_index in geometry.skeleton.noninvertible_bind_indices:
            valid &= joints != node_index
        safe_joints = _np.where(valid, joints, 0)
        effective_weights = _np.where(valid, weights, 0.0)
        totals = effective_weights.sum(axis=1)
        normalized_weights = _np.divide(
            effective_weights,
            totals[:, None],
            out=_np.zeros_like(effective_weights),
            where=totals[:, None] > 1e-8,
        )
        valid_rigid = (
            (rigid_joints >= 0)
            & (rigid_joints < bone_count)
            & (totals <= 1e-8)
        )
        safe_rigid_joints = _np.where(
            valid_rigid,
            rigid_joints,
            0,
        )
        homogeneous = _np.concatenate(
            (
                vertices,
                _np.ones((vertices.shape[0], 1), dtype=_np.float64),
            ),
            axis=1,
        )
        rigid_homogeneous = _np.concatenate(
            (
                rigid_local_vertices,
                _np.ones(
                    (rigid_local_vertices.shape[0], 1),
                    dtype=_np.float64,
                ),
            ),
            axis=1,
        )
        return (
            vertices,
            safe_joints,
            normalized_weights,
            totals > 1e-8,
            safe_rigid_joints,
            valid_rigid,
            homogeneous,
            rigid_homogeneous,
        )
    except Exception:
        return None


def _skin_geometry_numpy(
    geometry: ModelGeometry,
    pose: ModelAnimationPose,
    cache,
    *,
    pose_name: str,
) -> ModelGeometry:
    assert _np is not None
    (
        vertices,
        safe_joints,
        normalized_weights,
        animated_mask,
        safe_rigid_joints,
        rigid_mask,
        homogeneous,
        rigid_homogeneous,
    ) = cache
    matrices = _np.asarray(pose.joint_matrices, dtype=_np.float64).reshape(
        (-1, 4, 4)
    )
    selected = matrices[safe_joints]
    transformed = _np.einsum(
        "vkij,vj->vki",
        selected,
        homogeneous,
        optimize=False,
    )[:, :, :3]
    posed = (
        transformed * normalized_weights[:, :, None]
    ).sum(axis=1)
    posed = _np.where(animated_mask[:, None], posed, vertices)
    world_matrices = _np.asarray(
        pose.world_matrices or pose.joint_matrices,
        dtype=_np.float64,
    ).reshape((-1, 4, 4))
    rigid_transformed = _np.einsum(
        "vij,vj->vi",
        world_matrices[safe_rigid_joints],
        rigid_homogeneous,
        optimize=False,
    )[:, :3]
    posed = _np.where(rigid_mask[:, None], rigid_transformed, posed)

    # The viewport intentionally uses unlit texture/material rendering.  Keep
    # bind-pose normals here so a 40k-vertex character does not spend another
    # full frame converting normals that neither GPU shading nor wireframe
    # consumes.
    posed_vertices = tuple(map(tuple, posed.tolist()))
    if len(posed):
        bounds_min = tuple(float(value) for value in posed.min(axis=0))
        bounds_max = tuple(float(value) for value in posed.max(axis=0))
    else:
        bounds_min = geometry.bounds_min
        bounds_max = geometry.bounds_max
    return replace(
        geometry,
        vertices=posed_vertices,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        applied_pose_name=pose_name,
    )


def _skin_geometry_scalar(
    geometry: ModelGeometry,
    pose: ModelAnimationPose,
    *,
    pose_name: str,
) -> ModelGeometry:
    matrices = pose.joint_matrices
    world_matrices = pose.world_matrices or pose.joint_matrices
    skeleton = geometry.skeleton
    noninvertible_bind_indices = (
        set(skeleton.noninvertible_bind_indices)
        if skeleton is not None
        else set()
    )
    has_normals = (
        len(geometry.vertex_normals) == len(geometry.vertices)
    )
    posed_vertices: list[Vec3] = []
    posed_normals: list[Vec3 | None] = []
    vertex_count = len(geometry.vertices)
    joint_indices = (
        geometry.joint_indices
        if len(geometry.joint_indices) == vertex_count
        else ((-1, -1, -1, -1),) * vertex_count
    )
    joint_weights = (
        geometry.joint_weights
        if len(geometry.joint_weights) == vertex_count
        else ((0.0, 0.0, 0.0, 0.0),) * vertex_count
    )
    rigid_joint_indices = (
        geometry.rigid_joint_indices
        if len(geometry.rigid_joint_indices) == vertex_count
        else (-1,) * vertex_count
    )
    rigid_local_vertices = (
        geometry.rigid_local_vertices
        if len(geometry.rigid_local_vertices) == vertex_count
        else (None,) * vertex_count
    )
    rigid_local_normals = (
        geometry.rigid_local_normals
        if len(geometry.rigid_local_normals) == vertex_count
        else (None,) * vertex_count
    )
    for index, point in enumerate(geometry.vertices):
        joints = joint_indices[index]
        weights = joint_weights[index]
        influences = tuple(
            (joint, weight)
            for joint, weight in zip(joints, weights)
            if (
                0 <= joint < len(matrices)
                and joint not in noninvertible_bind_indices
                and math.isfinite(weight)
                and weight > 1e-8
            )
        )
        total = sum(weight for _joint, weight in influences)
        if total <= 1e-8:
            rigid_joint = rigid_joint_indices[index]
            local_point = rigid_local_vertices[index] or point
            if 0 <= rigid_joint < len(world_matrices):
                posed_vertices.append(
                    _transform_point(
                        world_matrices[rigid_joint],
                        local_point,
                    )
                )
            else:
                posed_vertices.append(point)
            if has_normals:
                normal = (
                    rigid_local_normals[index]
                    or geometry.vertex_normals[index]
                )
                if (
                    normal is not None
                    and 0 <= rigid_joint < len(world_matrices)
                ):
                    candidate = _transform_vector(
                        world_matrices[rigid_joint],
                        normal,
                    )
                    length = math.sqrt(
                        sum(value * value for value in candidate)
                    )
                    normal = (
                        tuple(value / length for value in candidate)
                        if length > 1e-12
                        else normal
                    )
                posed_normals.append(normal)
            continue
        inverse_total = 1.0 / total
        posed_vertices.append(
            tuple(
                sum(
                    _transform_point(matrices[joint], point)[axis]
                    * weight
                    * inverse_total
                    for joint, weight in influences
                )
                for axis in range(3)
            )
        )
        if not has_normals:
            continue
        normal = geometry.vertex_normals[index]
        if normal is None:
            posed_normals.append(None)
            continue
        candidate = tuple(
            sum(
                _transform_vector(matrices[joint], normal)[axis]
                * weight
                * inverse_total
                for joint, weight in influences
            )
            for axis in range(3)
        )
        length = math.sqrt(sum(value * value for value in candidate))
        posed_normals.append(
            tuple(value / length for value in candidate)
            if length > 1e-12
            else normal
        )
    return _replace_posed_geometry(
        geometry,
        tuple(posed_vertices),
        tuple(posed_normals) if has_normals else geometry.vertex_normals,
        pose_name=pose_name,
    )


def _replace_posed_geometry(
    geometry: ModelGeometry,
    vertices: tuple[Vec3, ...],
    normals: tuple[Vec3 | None, ...],
    *,
    pose_name: str,
) -> ModelGeometry:
    if vertices:
        bounds_min = tuple(
            min(point[axis] for point in vertices)
            for axis in range(3)
        )
        bounds_max = tuple(
            max(point[axis] for point in vertices)
            for axis in range(3)
        )
    else:
        bounds_min = geometry.bounds_min
        bounds_max = geometry.bounds_max
    face_normals = tuple(
        _face_normal(vertices, face)
        for face in geometry.faces
    )
    return replace(
        geometry,
        vertices=vertices,
        vertex_normals=normals,
        face_normals=face_normals,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        applied_pose_name=pose_name,
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


def _face_normal(
    vertices: tuple[Vec3, ...],
    face: tuple[int, int, int, int],
) -> Vec3:
    a, b, c, _material = face
    first = vertices[a]
    second = vertices[b]
    third = vertices[c]
    edge1 = tuple(second[index] - first[index] for index in range(3))
    edge2 = tuple(third[index] - first[index] for index in range(3))
    normal = (
        edge1[1] * edge2[2] - edge1[2] * edge2[1],
        edge1[2] * edge2[0] - edge1[0] * edge2[2],
        edge1[0] * edge2[1] - edge1[1] * edge2[0],
    )
    length = math.sqrt(sum(value * value for value in normal))
    if length <= 1e-12:
        return 0.0, 0.0, 0.0
    return tuple(value / length for value in normal)


def _scan_sections(path: Path) -> tuple[int, tuple[_Section, ...]]:
    file_size = path.stat().st_size
    if file_size < 16:
        raise ValueError("MDL 文件过短。")
    sections: list[_Section] = []
    with path.open("rb") as stream:
        magic, version, _header_value = struct.unpack(
            "<4sII",
            _read_exact(stream, 12, "文件头"),
        )
        if magic != b"MDL ":
            raise ValueError("不是支持的 MDL 文件。")
        offset = 12
        while True:
            if offset + 4 > file_size:
                raise ValueError("MDL 缺少结束标记。")
            stream.seek(offset)
            section_type = struct.unpack(
                "<I",
                _read_exact(stream, 4, "区段类型"),
            )[0]
            if section_type == 0xFFFFFFFF:
                if offset + 4 != file_size:
                    raise ValueError("MDL 结束标记后仍包含未解析数据。")
                break
            stored_size, item_count = struct.unpack(
                "<II",
                _read_exact(stream, 8, "区段头"),
            )
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


def _read_text(stream, label: str) -> str:
    size = _read_exact(stream, 1, f"{label}长度")[0]
    return _read_exact(stream, size, label).decode(
        "utf-8",
        errors="replace",
    )


def _read_u32(stream, label: str) -> int:
    return struct.unpack(
        "<I",
        _read_exact(stream, 4, label),
    )[0]


def _read_exact(stream, size: int, label: str) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError(f"MDL {label}不完整。")
    return data
