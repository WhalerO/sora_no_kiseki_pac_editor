from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from retext.model3d import (
    ModelAnimationClip,
    ModelAnimationKeyframe,
    ModelAnimationPlayer,
    ModelAnimationTrack,
    ModelGeometry,
    ModelSkeleton,
)
from retext.model3d import animation as animation_module


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


def _key(time: float, value: tuple[float, ...]) -> ModelAnimationKeyframe:
    return ModelAnimationKeyframe(
        time=time,
        value=value,
        mode=0,
        extra_vector_a=(0.0, 0.0),
        extra_vector_b=(0.0, 0.0),
    )


def _translation_track(
    target: str,
    *keys: ModelAnimationKeyframe,
) -> ModelAnimationTrack:
    return ModelAnimationTrack(
        name=f"{target}_translation",
        bone_name=target,
        track_type=9,
        unknown_a=0,
        unknown_b=0,
        keyframes=tuple(keys),
    )


def _scale_track(
    target: str,
    *keys: ModelAnimationKeyframe,
) -> ModelAnimationTrack:
    return ModelAnimationTrack(
        name=f"{target}_scale",
        bone_name=target,
        track_type=11,
        unknown_a=0,
        unknown_b=0,
        keyframes=tuple(keys),
    )


def _clip(*tracks: ModelAnimationTrack) -> ModelAnimationClip:
    return ModelAnimationClip(
        name="rigid_motion.mdl",
        source_path=Path("rigid_motion.mdl"),
        version=4,
        start=0.0,
        end=1.0,
        tracks=tuple(tracks),
    )


def _rigid_triangle(
    *,
    owner: int = 0,
    names: tuple[str, ...] = ("root",),
    parents: tuple[int, ...] = (-1,),
) -> ModelGeometry:
    skeleton = ModelSkeleton(
        names=names,
        parent_indices=parents,
        local_translations=((0.0, 0.0, 0.0),) * len(names),
        local_rotations=((0.0, 0.0, 0.0, 1.0),) * len(names),
        local_scales=((1.0, 1.0, 1.0),) * len(names),
        inverse_bind_matrices=(_IDENTITY_MATRIX,) * len(names),
        node_types=(0,) * len(names),
        mesh_group_indices=(-1,) * len(names),
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
        rigid_joint_indices=(owner,) * 3,
        skeleton=skeleton,
    )


class ModelRigidAnimationTests(unittest.TestCase):
    def test_rigid_vertices_follow_their_owner_node(self) -> None:
        player = ModelAnimationPlayer(
            _rigid_triangle(),
            _clip(
                _translation_track(
                    "root",
                    _key(0.0, (0.0, 0.0, 0.0)),
                    _key(1.0, (2.0, 0.0, 0.0)),
                )
            ),
        )

        middle = player.sample(0.5)

        self.assertTrue(player.compatible)
        self.assertEqual(player.skinned_vertex_count, 0)
        self.assertEqual(player.rigid_vertex_count, 3)
        self.assertEqual(player.affected_vertex_count, 3)
        self.assertEqual(middle.vertices[0], (2.0, 0.0, 0.0))
        self.assertEqual(middle.vertices[1], (1.0, 1.0, 0.0))

    def test_parent_track_affects_a_child_owned_rigid_mesh(self) -> None:
        player = ModelAnimationPlayer(
            _rigid_triangle(
                owner=1,
                names=("root", "mesh"),
                parents=(-1, 0),
            ),
            _clip(
                _translation_track(
                    "root",
                    _key(0.0, (0.0, 0.0, 0.0)),
                    _key(1.0, (0.0, 2.0, 0.0)),
                )
            ),
        )

        sampled = player.sample(1.0)

        self.assertTrue(player.compatible)
        self.assertEqual(sampled.vertices[0], (1.0, 2.0, 0.0))

    def test_unrelated_track_does_not_claim_a_playable_geometry(self) -> None:
        player = ModelAnimationPlayer(
            _rigid_triangle(
                owner=1,
                names=("controller", "mesh"),
                parents=(-1, -1),
            ),
            _clip(
                _translation_track(
                    "controller",
                    _key(0.0, (0.0, 0.0, 0.0)),
                    _key(1.0, (3.0, 0.0, 0.0)),
                )
            ),
        )

        self.assertFalse(player.compatible)
        self.assertEqual(player.affected_vertex_count, 0)
        self.assertIn("没有影响任何可渲染顶点", player.warning)

    def test_numpy_and_scalar_rigid_transforms_are_equivalent(self) -> None:
        geometry = _rigid_triangle()
        clip = _clip(
            _translation_track(
                "root",
                _key(0.0, (0.0, 0.0, 0.0)),
                _key(1.0, (1.5, -0.5, 0.25)),
            )
        )
        accelerated = ModelAnimationPlayer(geometry, clip).sample(0.4)
        with patch.object(animation_module, "_np", None):
            scalar = ModelAnimationPlayer(geometry, clip).sample(0.4)

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
                    places=9,
                )

    def test_zero_scale_bind_node_can_expand_during_animation(self) -> None:
        geometry = _rigid_triangle()
        assert geometry.skeleton is not None
        local_vertices = geometry.vertices
        zero_matrix = (
            0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        skeleton = replace(
            geometry.skeleton,
            local_scales=((0.0, 0.0, 0.0),),
            bind_world_matrices=(zero_matrix,),
            noninvertible_bind_indices=(0,),
        )
        geometry = replace(
            geometry,
            vertices=((0.0, 0.0, 0.0),) * 3,
            rigid_local_vertices=local_vertices,
            skeleton=skeleton,
        )
        player = ModelAnimationPlayer(
            geometry,
            _clip(
                _scale_track(
                    "root",
                    _key(0.0, (0.0, 0.0, 0.0)),
                    _key(1.0, (1.0, 1.0, 1.0)),
                )
            ),
        )

        hidden = player.sample(0.0)
        visible = player.sample(1.0)

        self.assertTrue(player.compatible)
        self.assertEqual(hidden.vertices, ((0.0, 0.0, 0.0),) * 3)
        self.assertEqual(visible.vertices, local_vertices)
        self.assertEqual(hidden.bounds_min, visible.bounds_min)
        self.assertEqual(hidden.bounds_max, visible.bounds_max)
        self.assertGreater(
            max(
                high - low
                for low, high in zip(
                    hidden.bounds_min,
                    hidden.bounds_max,
                )
            ),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
