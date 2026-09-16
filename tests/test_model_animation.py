from __future__ import annotations

import math
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from retext.model3d import (
    ModelAnimationClip,
    ModelAnimationKeyframe,
    ModelAnimationNodeTransform,
    ModelAnimationPlayer,
    ModelAnimationTrack,
    ModelGeometry,
    ModelSkeleton,
    load_model_animation,
    model_3d_service,
    sample_animation_track,
)
from retext.model3d import animation as animation_module
from retext.preview import AssetPreviewService


_COMPONENT_COUNTS = {9: 3, 10: 4, 11: 3, 12: 1, 13: 2, 14: 3}
_IDENTITY_MATRIX = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


def _text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return bytes([len(encoded)]) + encoded


def _track_bytes(
    track_type: int,
    frames: list[
        tuple[
            float,
            tuple[float, ...],
            int,
            tuple[float, float, float, float],
        ]
    ],
    *,
    name: str = "track",
    bone_name: str = "root",
    unknown_a: int = 0,
    unknown_b: int = 0,
) -> bytes:
    assert all(
        len(value) == _COMPONENT_COUNTS[track_type]
        for _time, value, _mode, _extra in frames
    )
    payload = (
        _text(name)
        + _text(bone_name)
        + struct.pack(
            "<4I",
            track_type,
            unknown_a,
            unknown_b,
            len(frames),
        )
    )
    for time, value, mode, extra in frames:
        payload += (
            struct.pack("<f", time)
            + struct.pack(f"<{len(value)}f", *value)
            + struct.pack("<I4f", mode, *extra)
        )
    return payload


def _write_animation_mdl(
    path: Path,
    tracks: list[bytes],
    *,
    start: float = 30.0,
    end: float = 31.0,
    version: int = 4,
    include_range: bool = True,
    nodes: list[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ]
    | None = None,
) -> None:
    node_section = _node_section_bytes(nodes) if nodes is not None else b""
    body = b"".join(tracks)
    if include_range:
        body += struct.pack("<2f", start, end)
    payload = struct.pack("<I", len(tracks)) + body
    section = struct.pack("<II", 3, len(payload)) + payload
    path.write_bytes(
        b"MDL "
        + struct.pack("<II", version, 0)
        + node_section
        + section
        + struct.pack("<I", 0xFFFFFFFF)
    )


def _node_section_bytes(
    nodes: list[
        tuple[
            str,
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ]
    ],
) -> bytes:
    node_body = struct.pack("<I", len(nodes))
    for name, translation, euler, scale in nodes:
        node_body += (
            _text(name)
            + struct.pack("<II", 1, 0xFFFFFFFF)
            + struct.pack("<3f", *translation)
            + struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)
            + struct.pack("<I", 0)
            + struct.pack("<3f", *euler)
            + struct.pack("<3f", *scale)
            + struct.pack("<3f", 0.0, 0.0, 0.0)
            + struct.pack("<I", 0)
        )
    return struct.pack("<II", 2, len(node_body)) + node_body


def _write_mesh_stub(
    path: Path,
    *,
    node_names: tuple[str, ...] = (),
) -> None:
    node_section = (
        _node_section_bytes(
            [
                (
                    name,
                    (0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (1.0, 1.0, 1.0),
                )
                for name in node_names
            ]
        )
        if node_names
        else b""
    )
    path.write_bytes(
        b"MDL "
        + struct.pack("<II", 4, 0)
        + struct.pack("<III", 1, 4, 1)
        + node_section
        + struct.pack("<III", 4, 4, 1)
        + struct.pack("<I", 0xFFFFFFFF)
    )


def _key(
    time: float,
    value: tuple[float, ...],
    *,
    mode: int = 0,
) -> ModelAnimationKeyframe:
    return ModelAnimationKeyframe(
        time=time,
        value=value,
        mode=mode,
        extra_vector_a=(0.0, 0.0),
        extra_vector_b=(0.0, 0.0),
    )


def _track(
    track_type: int,
    bone_name: str,
    *keyframes: ModelAnimationKeyframe,
) -> ModelAnimationTrack:
    return ModelAnimationTrack(
        name=f"{bone_name}_{track_type}",
        bone_name=bone_name,
        track_type=track_type,
        unknown_a=0,
        unknown_b=0,
        keyframes=tuple(keyframes),
    )


def _clip(
    tracks: tuple[ModelAnimationTrack, ...],
    *,
    start: float = 30.0,
    end: float = 31.0,
    node_transforms: tuple[ModelAnimationNodeTransform, ...] = (),
    name: str = "motion.mdl",
) -> ModelAnimationClip:
    return ModelAnimationClip(
        name=name,
        source_path=Path(name),
        version=4,
        start=start,
        end=end,
        tracks=tracks,
        node_transforms=node_transforms,
    )


def _skinned_triangle() -> ModelGeometry:
    skeleton = ModelSkeleton(
        names=("root",),
        parent_indices=(-1,),
        local_translations=((0.0, 0.0, 0.0),),
        local_rotations=((0.0, 0.0, 0.0, 1.0),),
        local_scales=((1.0, 1.0, 1.0),),
        inverse_bind_matrices=(_IDENTITY_MATRIX,),
    )
    return ModelGeometry(
        vertices=(
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0),
        ),
        faces=((0, 1, 2, 0),),
        source_vertex_count=3,
        source_triangle_count=1,
        mesh_group_count=1,
        primitive_count=1,
        bounds_min=(0.0, 0.0, 0.0),
        bounds_max=(1.0, 1.0, 0.0),
        sampled=False,
        vertex_normals=((0.0, 0.0, 1.0),) * 3,
        joint_indices=((0, -1, -1, -1),) * 3,
        joint_weights=((1.0, 0.0, 0.0, 0.0),) * 3,
        skeleton=skeleton,
    )


def _skinned_triangle_for_skeleton(
    names: tuple[str, ...],
    *,
    parent_indices: tuple[int, ...] | None = None,
    weighted_bone: str,
) -> ModelGeometry:
    if parent_indices is None:
        parent_indices = tuple(
            -1 if index == 0 else index - 1
            for index in range(len(names))
        )
    weighted_index = names.index(weighted_bone)
    skeleton = ModelSkeleton(
        names=names,
        parent_indices=parent_indices,
        local_translations=((0.0, 0.0, 0.0),) * len(names),
        local_rotations=((0.0, 0.0, 0.0, 1.0),) * len(names),
        local_scales=((1.0, 1.0, 1.0),) * len(names),
        inverse_bind_matrices=(_IDENTITY_MATRIX,) * len(names),
    )
    return replace(
        _skinned_triangle(),
        joint_indices=((weighted_index, -1, -1, -1),) * 3,
        skeleton=skeleton,
    )


class ModelAnimationParserTests(unittest.TestCase):
    def test_parser_preserves_types_9_through_14_and_complete_key_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "000123.mdl"
            tracks = []
            for track_type, component_count in _COMPONENT_COUNTS.items():
                tracks.append(
                    _track_bytes(
                        track_type,
                        [
                            (
                                30.0,
                                tuple(float(index) for index in range(component_count)),
                                1,
                                (1.0, 2.0, 3.0, 4.0),
                            ),
                            (
                                31.0,
                                tuple(
                                    float(index + 10)
                                    for index in range(component_count)
                                ),
                                0,
                                (5.0, 6.0, 7.0, 8.0),
                            ),
                        ],
                        name=f"track_{track_type}",
                        bone_name=f"target_{track_type}",
                        unknown_a=track_type + 100,
                        unknown_b=track_type + 200,
                    )
                )
            _write_animation_mdl(source, tracks, start=30.0, end=31.5)

            clip = load_model_animation(
                source,
                name="asset/common/model/chr0001_m_btl_walk.mdl",
            )

        assert clip is not None
        self.assertEqual(
            clip.name,
            "asset/common/model/chr0001_m_btl_walk.mdl",
        )
        self.assertEqual(clip.version, 4)
        self.assertEqual(clip.start, 30.0)
        self.assertEqual(clip.end, 31.5)
        self.assertEqual(clip.duration, 1.5)
        self.assertEqual(clip.keyframe_count, 12)
        self.assertEqual(
            [track.track_type for track in clip.tracks],
            [9, 10, 11, 12, 13, 14],
        )
        first = clip.tracks[0]
        self.assertEqual(first.unknown_a, 109)
        self.assertEqual(first.unknown_b, 209)
        self.assertEqual(first.keyframes[0].mode, 1)
        self.assertEqual(first.keyframes[0].extra_vector_a, (1.0, 2.0))
        self.assertEqual(first.keyframes[0].extra_vector_b, (3.0, 4.0))
        self.assertEqual(clip.tracks[-1].kind, "vector3")
        self.assertEqual(clip.tracks[-1].component_count, 3)

    def test_zero_track_zero_duration_animation_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "empty.mdl"
            _write_animation_mdl(source, [], start=0.0, end=0.0)

            clip = load_model_animation(source)

        assert clip is not None
        self.assertEqual(clip.tracks, ())
        self.assertEqual(clip.duration, 0.0)

    def test_parser_retains_section_two_kurotools_bind_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "motion.mdl"
            _write_animation_mdl(
                source,
                [],
                nodes=[
                    (
                        "root",
                        (1.0, 2.0, 3.0),
                        (0.0, 0.0, math.pi / 2.0),
                        (2.0, 3.0, 4.0),
                    )
                ],
            )

            clip = load_model_animation(source)

        assert clip is not None
        self.assertEqual(len(clip.node_transforms), 1)
        node = clip.node_transforms[0]
        self.assertEqual(node.name, "root")
        self.assertEqual(node.translation, (1.0, 2.0, 3.0))
        self.assertEqual(node.scale, (2.0, 3.0, 4.0))
        self.assertAlmostEqual(node.bind_rotation[2], math.sqrt(0.5), places=6)
        self.assertAlmostEqual(node.bind_rotation[3], math.sqrt(0.5), places=6)

    def test_parser_rejects_missing_range_and_non_v4_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            missing_range = root / "missing_range.mdl"
            old_version = root / "old.mdl"
            _write_animation_mdl(
                missing_range,
                [],
                include_range=False,
            )
            _write_animation_mdl(old_version, [], version=1)

            with self.assertRaisesRegex(ValueError, "start/end"):
                load_model_animation(missing_range)
            with self.assertRaisesRegex(ValueError, "MDL v4"):
                load_model_animation(old_version)

    def test_non_animation_mdl_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "static.mdl"
            source.write_bytes(
                b"MDL "
                + struct.pack("<II", 4, 0)
                + struct.pack("<I", 0xFFFFFFFF)
            )

            self.assertIsNone(load_model_animation(source))


class ModelAnimationSamplingTests(unittest.TestCase):
    def test_translation_and_scale_are_linear_and_rotation_uses_slerp(
        self,
    ) -> None:
        translation = _track(
            9,
            "root",
            _key(30.0, (0.0, 0.0, 0.0)),
            _key(31.0, (2.0, 4.0, 6.0)),
        )
        scale = _track(
            11,
            "root",
            _key(30.0, (1.0, 1.0, 1.0)),
            _key(31.0, (3.0, 5.0, 7.0)),
        )
        rotation = _track(
            10,
            "root",
            _key(30.0, (0.0, 0.0, 0.0, 1.0)),
            _key(31.0, (0.0, 0.0, 1.0, 0.0)),
        )

        self.assertEqual(
            sample_animation_track(translation, 30.5),
            (1.0, 2.0, 3.0),
        )
        self.assertEqual(
            sample_animation_track(scale, 30.5),
            (2.0, 3.0, 4.0),
        )
        sampled_rotation = sample_animation_track(rotation, 30.5)
        assert sampled_rotation is not None
        self.assertAlmostEqual(sampled_rotation[0], 0.0, places=6)
        self.assertAlmostEqual(sampled_rotation[1], 0.0, places=6)
        self.assertAlmostEqual(
            sampled_rotation[2],
            math.sqrt(0.5),
            places=6,
        )
        self.assertAlmostEqual(
            sampled_rotation[3],
            math.sqrt(0.5),
            places=6,
        )

    def test_mode_one_holds_the_left_key_value(self) -> None:
        stepped = _track(
            12,
            "controller",
            _key(0.0, (2.0,), mode=1),
            _key(1.0, (9.0,)),
        )

        self.assertEqual(sample_animation_track(stepped, 0.5), (2.0,))
        self.assertEqual(sample_animation_track(stepped, 1.0), (9.0,))

    def test_player_samples_relative_seconds_and_loops(self) -> None:
        clip = _clip(
            (
                _track(
                    9,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0)),
                    _key(31.0, (2.0, 0.0, 0.0)),
                ),
            )
        )
        player = ModelAnimationPlayer(_skinned_triangle(), clip)

        middle = player.sample(0.5)
        looped = player.sample(1.5, loop=True)

        self.assertTrue(player.compatible)
        self.assertEqual(player.matched_target_count, 1)
        self.assertEqual(player.skeletal_target_count, 1)
        self.assertEqual(player.compatibility_ratio, 1.0)
        self.assertEqual(player.warning, "")
        self.assertEqual(middle.applied_pose_name, "motion.mdl")
        self.assertAlmostEqual(middle.vertices[0][0], 2.0, places=6)
        self.assertAlmostEqual(middle.vertices[1][0], 1.0, places=6)
        self.assertEqual(middle.vertices, looped.vertices)
        pose = player.sample_pose(0.5)
        self.assertEqual(pose.relative_seconds, 0.5)
        self.assertEqual(pose.absolute_seconds, 30.5)

    def test_rotation_keys_are_deltas_from_the_bind_pose(self) -> None:
        half_turn = math.sqrt(0.5)
        bind_rotation = (0.0, 0.0, half_turn, half_turn)
        inverse_bind = (
            0.0,
            1.0,
            0.0,
            0.0,
            -1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        )
        geometry = replace(
            _skinned_triangle(),
            skeleton=ModelSkeleton(
                names=("root",),
                parent_indices=(-1,),
                local_translations=((0.0, 0.0, 0.0),),
                local_rotations=(bind_rotation,),
                local_scales=((1.0, 1.0, 1.0),),
                inverse_bind_matrices=(inverse_bind,),
            ),
        )
        clip = _clip(
            (
                _track(
                    10,
                    "root",
                    _key(30.0, bind_rotation),
                ),
            )
        )

        player = ModelAnimationPlayer(geometry, clip)
        pose = player.sample_pose(0.0)
        sampled = player.sample(0.0)

        # KuroTools applies bind_rotation * animation_delta, producing a
        # 180-degree local rotation here.  Relative to the 90-degree inverse
        # bind matrix, the skinned vertex therefore rotates another 90
        # degrees instead of remaining unchanged.
        self.assertAlmostEqual(abs(pose.local_rotations[0][2]), 1.0, places=6)
        self.assertAlmostEqual(pose.local_rotations[0][3], 0.0, places=6)
        self.assertAlmostEqual(sampled.vertices[0][0], 0.0, places=6)
        self.assertAlmostEqual(sampled.vertices[0][1], 1.0, places=6)

    def test_animation_file_bind_defaults_take_precedence_when_merging(
        self,
    ) -> None:
        half_turn = math.sqrt(0.5)
        clip = _clip(
            (
                _track(
                    9,
                    "root",
                    _key(31.0, (4.0, 0.0, 0.0)),
                ),
                _track(
                    10,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0, 1.0)),
                ),
            ),
            node_transforms=(
                ModelAnimationNodeTransform(
                    name="root",
                    translation=(2.0, 0.0, 0.0),
                    bind_rotation=(0.0, 0.0, half_turn, half_turn),
                    scale=(1.0, 1.0, 1.0),
                ),
            ),
        )
        player = ModelAnimationPlayer(_skinned_triangle(), clip)

        first_pose = player.sample_pose(0.0)
        middle_pose = player.sample_pose(0.5)
        first = player.sample(0.0)

        # KuroTools extracts the raw delta against section 2 of the animation
        # file before merging the channel into the companion model.
        self.assertAlmostEqual(
            first_pose.local_rotations[0][2],
            half_turn,
            places=6,
        )
        self.assertAlmostEqual(first_pose.local_translations[0][0], 2.0)
        self.assertAlmostEqual(middle_pose.local_translations[0][0], 3.0)
        self.assertAlmostEqual(first.vertices[0][0], 2.0, places=6)
        self.assertAlmostEqual(first.vertices[0][1], 1.0, places=6)

    def test_duplicate_trs_tracks_append_and_sort_like_kurotools(
        self,
    ) -> None:
        clip = _clip(
            (
                _track(
                    9,
                    "root",
                    _key(31.0, (10.0, 0.0, 0.0)),
                ),
                _track(
                    10,
                    "root",
                    _key(31.0, (0.0, 0.0, 1.0, 0.0)),
                ),
                _track(
                    11,
                    "root",
                    _key(31.0, (3.0, 3.0, 3.0)),
                ),
                _track(
                    9,
                    "root",
                    _key(30.0, (2.0, 0.0, 0.0)),
                ),
                _track(
                    10,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0, 1.0)),
                ),
                _track(
                    11,
                    "root",
                    _key(30.0, (1.0, 1.0, 1.0)),
                ),
            )
        )

        pose = ModelAnimationPlayer(
            _skinned_triangle(),
            clip,
        ).sample_pose(0.5)

        self.assertAlmostEqual(
            pose.local_translations[0][0],
            6.0,
            places=6,
        )
        self.assertAlmostEqual(
            pose.local_rotations[0][2],
            math.sqrt(0.5),
            places=6,
        )
        self.assertAlmostEqual(
            pose.local_rotations[0][3],
            math.sqrt(0.5),
            places=6,
        )
        self.assertEqual(pose.local_scales[0], (2.0, 2.0, 2.0))

    def test_low_bone_match_ratio_is_reported_as_incompatible(self) -> None:
        clip = _clip(
            tuple(
                _track(
                    9,
                    bone_name,
                    _key(30.0, (0.0, 0.0, 0.0)),
                )
                for bone_name in ("root", "camera", "camera_aim")
            ),
            name="chr0001_m_event_gs.mdl",
        )

        player = ModelAnimationPlayer(_skinned_triangle(), clip)

        self.assertFalse(player.compatible)
        self.assertEqual(player.matched_target_count, 1)
        self.assertEqual(player.skeletal_target_count, 3)
        self.assertAlmostEqual(player.compatibility_ratio, 1.0 / 3.0)
        self.assertIn("兼容性过低", player.warning)

    def test_low_target_ratio_can_use_full_geometry_coverage(self) -> None:
        clip = _clip(
            tuple(
                _track(
                    9,
                    bone_name,
                    _key(30.0, (0.0, 0.0, 0.0)),
                )
                for bone_name in (
                    "root",
                    "secondary_a",
                    "secondary_b",
                )
            ),
            name="chr5001_c10_m_btl_dash.mdl",
        )

        player = ModelAnimationPlayer(_skinned_triangle(), clip)

        self.assertTrue(player.compatible)
        self.assertEqual(player.matched_target_count, 1)
        self.assertEqual(player.skeletal_target_count, 3)
        self.assertEqual(player.affected_vertex_ratio, 1.0)
        self.assertIn("实际几何影响", player.warning)

    def test_animation_uses_fixed_first_frame_viewport_bounds(self) -> None:
        clip = _clip(
            (
                _track(
                    9,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0)),
                    _key(31.0, (8.0, 0.0, 0.0)),
                ),
            )
        )
        player = ModelAnimationPlayer(_skinned_triangle(), clip)

        first = player.sample(0.0)
        last = player.sample(1.0)

        self.assertNotEqual(first.vertices, last.vertices)
        self.assertEqual(first.bounds_min, last.bounds_min)
        self.assertEqual(first.bounds_max, last.bounds_max)

    def test_empty_clip_is_safe_and_returns_the_bind_geometry(self) -> None:
        geometry = _skinned_triangle()
        player = ModelAnimationPlayer(
            geometry,
            _clip((), start=0.0, end=0.0),
        )

        sampled = player.sample(12.0)

        self.assertFalse(player.compatible)
        self.assertEqual(player.skeletal_target_count, 0)
        self.assertIn("不包含", player.warning)
        self.assertEqual(sampled.vertices, geometry.vertices)

    def test_matching_bones_without_skin_weights_are_not_playable(
        self,
    ) -> None:
        geometry = replace(
            _skinned_triangle(),
            joint_indices=((-1, -1, -1, -1),) * 3,
            joint_weights=((0.0, 0.0, 0.0, 0.0),) * 3,
        )
        player = ModelAnimationPlayer(
            geometry,
            _clip(
                (
                    _track(
                        9,
                        "root",
                        _key(30.0, (1.0, 0.0, 0.0)),
                    ),
                )
            ),
        )

        self.assertFalse(player.compatible)
        self.assertEqual(player.skinned_vertex_count, 0)
        self.assertIn("蒙皮权重", player.warning)

    def test_numpy_and_scalar_skinning_have_equivalent_results(self) -> None:
        clip = _clip(
            (
                _track(
                    9,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0)),
                    _key(31.0, (2.0, 1.0, 0.0)),
                ),
                _track(
                    10,
                    "root",
                    _key(30.0, (0.0, 0.0, 0.0, 1.0)),
                    _key(31.0, (0.0, 0.0, 1.0, 0.0)),
                ),
            )
        )
        geometry = _skinned_triangle()
        accelerated = ModelAnimationPlayer(geometry, clip).sample(0.35)
        with patch.object(animation_module, "_np", None):
            scalar = ModelAnimationPlayer(geometry, clip).sample(0.35)

        for accelerated_point, scalar_point in zip(
            accelerated.vertices,
            scalar.vertices,
        ):
            for accelerated_value, scalar_value in zip(
                accelerated_point,
                scalar_point,
            ):
                self.assertAlmostEqual(
                    accelerated_value,
                    scalar_value,
                    places=8,
                )


class ModelAnimationPreviewIntegrationTests(unittest.TestCase):
    def test_animation_only_model_binds_a_compatible_companion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr0001_m_btl_walk.mdl"
            companion = root / "chr0001.mdl"
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        9,
                        [
                            (30.0, (0.0, 0.0, 0.0), 0, (0.0,) * 4),
                            (31.0, (2.0, 0.0, 0.0), 0, (0.0,) * 4),
                        ],
                        bone_name="root",
                    )
                ],
            )
            _write_mesh_stub(companion)

            with patch.object(
                model_3d_service,
                "load_geometry",
                return_value=_skinned_triangle(),
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda key: (
                        (companion, companion.name)
                        if key == "chr0001"
                        else None
                    ),
                    model_logical_path=(
                        "asset/common/model/"
                        "chr0001_m_btl_walk.mdl"
                    ),
                )

        self.assertEqual(preview.duration_seconds, 1.0)
        self.assertEqual(preview.model_companion_label, "chr0001.mdl")
        self.assertIsNotNone(preview.model_geometry)
        self.assertIsNotNone(preview.model_animation_player)
        assert preview.model_animation_player is not None
        posed = preview.model_animation_player.sample(0.5)
        self.assertAlmostEqual(posed.vertices[0][0], 2.0)
        self.assertIn(("节点匹配", "1 / 1"), preview.metadata)
        self.assertIn("动画已解析并绑定", preview.warning)

    def test_incomplete_bare_model_is_replaced_by_clear_costume_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr0112_m_btl_wait.mdl"
            bare = root / "chr0112.mdl"
            costume = root / "chr0112_c00.mdl"
            node_rows = [
                (
                    name,
                    (0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (1.0, 1.0, 1.0),
                )
                for name in ("root", "arm")
            ]
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        10,
                        [
                            (
                                0.0,
                                (0.0, 0.0, 0.0, 1.0),
                                0,
                                (0.0,) * 4,
                            )
                        ],
                        bone_name=name,
                    )
                    for name in ("root", "arm")
                ],
                start=0.0,
                end=1.0,
                nodes=node_rows,
            )
            _write_mesh_stub(bare, node_names=("root",))
            _write_mesh_stub(costume, node_names=("root", "arm"))

            with patch.object(
                model_3d_service,
                "load_geometry",
                return_value=_skinned_triangle(),
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda key: (
                        (candidate, candidate.name)
                        if (
                            candidate := root / f"{key}.mdl"
                        ).is_file()
                        else None
                    ),
                )

        self.assertEqual(
            preview.model_companion_label,
            "chr0112_c00.mdl",
        )
        self.assertIn("已按兼容度改用", preview.warning)

    def test_tied_best_costumes_are_not_auto_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr5000_m_btl_wait.mdl"
            bare = root / "chr5000.mdl"
            costume_a = root / "chr5000_c00.mdl"
            costume_b = root / "chr5000_c01.mdl"
            bone_names = ("root", "arm", "leg", "hand")
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        10,
                        [
                            (
                                0.0,
                                (0.0, 0.0, 0.0, 1.0),
                                0,
                                (0.0,) * 4,
                            )
                        ],
                        bone_name=name,
                    )
                    for name in bone_names
                ],
                start=0.0,
                end=1.0,
                nodes=[
                    (
                        name,
                        (0.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0),
                        (1.0, 1.0, 1.0),
                    )
                    for name in bone_names
                ],
            )
            _write_mesh_stub(bare, node_names=bone_names[:2])
            _write_mesh_stub(costume_a, node_names=bone_names)
            _write_mesh_stub(costume_b, node_names=bone_names)

            geometries = {
                bare.resolve(): _skinned_triangle_for_skeleton(
                    bone_names[:2],
                    weighted_bone="root",
                ),
                costume_a.resolve(): _skinned_triangle_for_skeleton(
                    bone_names,
                    weighted_bone="root",
                ),
                costume_b.resolve(): _skinned_triangle_for_skeleton(
                    bone_names,
                    weighted_bone="root",
                ),
            }
            with patch.object(
                model_3d_service,
                "load_geometry",
                side_effect=lambda path: geometries[Path(path).resolve()],
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda key: (
                        (candidate, candidate.name)
                        if (
                            candidate := root / f"{key}.mdl"
                        ).is_file()
                        else None
                    ),
                )

        self.assertEqual(preview.model_companion_label, "")
        self.assertIsNone(preview.model_geometry)
        self.assertIsNone(preview.model_animation_player)
        self.assertIn("最佳候选未相对第二名清晰胜出", preview.warning)

    def test_unplayable_primary_allows_a_playable_costume_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr0338_m_btl_wait.mdl"
            bare = root / "chr0338.mdl"
            costume = root / "chr0338_c00.mdl"
            node_names = ("arm", "cloth")
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        10,
                        [
                            (
                                0.0,
                                (0.0, 0.0, 0.0, 1.0),
                                0,
                                (0.0,) * 4,
                            )
                        ],
                        bone_name="arm",
                    )
                ],
                start=0.0,
                end=1.0,
                nodes=[
                    (
                        name,
                        (0.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0),
                        (1.0, 1.0, 1.0),
                    )
                    for name in node_names
                ],
            )
            _write_mesh_stub(bare, node_names=node_names)
            _write_mesh_stub(costume, node_names=node_names)
            geometries = {
                # Mirrors the real chr0338.mdl: it has mesh sections but no
                # usable animation skeleton.
                bare.resolve(): replace(
                    _skinned_triangle_for_skeleton(
                        node_names,
                        parent_indices=(-1, -1),
                        weighted_bone="arm",
                    ),
                    skeleton=None,
                ),
                costume.resolve(): _skinned_triangle_for_skeleton(
                    node_names,
                    parent_indices=(-1, -1),
                    weighted_bone="arm",
                ),
            }
            with patch.object(
                model_3d_service,
                "load_geometry",
                side_effect=lambda path: geometries[Path(path).resolve()],
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda key: (
                        (candidate, candidate.name)
                        if (
                            candidate := root / f"{key}.mdl"
                        ).is_file()
                        else None
                    ),
                )

        self.assertEqual(preview.model_companion_label, "chr0338_c00.mdl")
        self.assertIsNotNone(preview.model_animation_player)
        self.assertIn("没有可用于动画的骨骼层级", preview.warning)

    def test_weights_on_unaffected_bones_do_not_make_a_mesh_playable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr0340_m_btl_wait.mdl"
            companion = root / "chr0340.mdl"
            node_names = ("arm", "cloth")
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        10,
                        [
                            (
                                0.0,
                                (0.0, 0.0, 0.0, 1.0),
                                0,
                                (0.0,) * 4,
                            )
                        ],
                        bone_name="arm",
                    )
                ],
                start=0.0,
                end=1.0,
                nodes=[
                    (
                        name,
                        (0.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0),
                        (1.0, 1.0, 1.0),
                    )
                    for name in node_names
                ],
            )
            _write_mesh_stub(companion, node_names=node_names)
            geometry = _skinned_triangle_for_skeleton(
                node_names,
                parent_indices=(-1, -1),
                weighted_bone="cloth",
            )

            with patch.object(
                model_3d_service,
                "load_geometry",
                return_value=geometry,
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda key: (
                        (companion, companion.name)
                        if key == "chr0340"
                        else None
                    ),
                )

        self.assertEqual(preview.model_companion_label, "")
        self.assertIsNone(preview.model_geometry)
        self.assertIn("没有影响任何可渲染顶点", preview.warning)

    def test_low_compatibility_animation_does_not_force_the_mesh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            animation = root / "chr0001_m_event_gs.mdl"
            companion = root / "chr0001.mdl"
            _write_animation_mdl(
                animation,
                [
                    _track_bytes(
                        9,
                        [
                            (0.0, (0.0, 0.0, 0.0), 0, (0.0,) * 4),
                            (1.0, (1.0, 0.0, 0.0), 0, (0.0,) * 4),
                        ],
                        bone_name="gameCamera",
                    )
                ],
                start=0.0,
                end=1.0,
            )
            _write_mesh_stub(companion)

            with patch.object(
                model_3d_service,
                "load_geometry",
                return_value=_skinned_triangle(),
            ):
                preview = AssetPreviewService().load(
                    animation,
                    model_companion_resolver=lambda _key: (
                        companion,
                        companion.name,
                    ),
                )

        self.assertIsNone(preview.model_geometry)
        self.assertIsNone(preview.model_animation_player)
        self.assertEqual(preview.model_companion_label, "")
        self.assertIn("无法用于预览", preview.warning)
        self.assertIn("没有匹配当前网格", preview.warning)


if __name__ == "__main__":
    unittest.main()
