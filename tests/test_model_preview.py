from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from retext.model_preview import (
    ModelGeometry,
    ModelMaterialRenderInfo,
    ModelMaterialSurface,
    ModelRenderCancelled,
    apply_model_textures,
    load_model_geometry,
    render_model_geometry,
)
from retext.model3d import calculate_render_dimensions
from retext.preview import AssetPreviewService


def _mdl_section(section_type: int, item_count: int, body: bytes) -> bytes:
    payload = struct.pack("<I", item_count) + body
    return struct.pack("<II", section_type, len(payload)) + payload


def _build_static_mdl(
    path: Path,
    vertices: list[tuple[float, float, float]],
    indices: list[int],
    *,
    material_index: int = 2,
    uvs: list[tuple[float, float]] | None = None,
    texture_name: str | None = None,
    rigid_scale: tuple[float, float, float] | None = None,
) -> None:
    mesh_metadata = (
        struct.pack("<I", 1)
        + struct.pack("<III", material_index, len(indices), 0)
        + struct.pack("<I", 0)
    )
    mesh_body = (
        bytes([4])
        + b"mesh"
        + struct.pack("<I", len(mesh_metadata))
        + mesh_metadata
        + struct.pack("<I", 44)
        + bytes(44)
    )
    position_data = b"".join(struct.pack("<3f", *point) for point in vertices)
    uv_data = (
        b"".join(struct.pack("<2f", *point) for point in uvs)
        if uvs is not None
        else b""
    )
    index_data = b"".join(struct.pack("<I", index) for index in indices)
    primitive_records = [(0, len(position_data), 12, 0, 0)]
    primitive_payloads = [position_data]
    if uvs is not None:
        primitive_records.append((4, len(uv_data), 8, 0, 0))
        primitive_payloads.append(uv_data)
    primitive_records.append((7, len(index_data), 4, 0, 0))
    primitive_payloads.append(index_data)
    primitive_headers = b"".join(
        struct.pack("<5I", *record)
        for record in primitive_records
    )
    primitive_body = primitive_headers + b"".join(primitive_payloads)
    material_section = b""
    if texture_name is not None:
        def text(value: str) -> bytes:
            encoded = value.encode("ascii")
            return bytes([len(encoded)]) + encoded

        material_body = (
            text("material")
            + text("map")
            + text("map")
            + struct.pack("<I", 1)
            + text(texture_name)
            + struct.pack("<5I", 0, 0xFFFFFFFF, 1, 0, 0)
            + struct.pack("<III", 0, 0, 0)
            + struct.pack("<I", 0)
            + bytes(20)
        )
        material_section = _mdl_section(0, 1, material_body)
    node_section = b""
    if rigid_scale is not None:
        node_body = (
            bytes([4])
            + b"mesh"
            + struct.pack("<II", 2, 0)
            + struct.pack("<3f", 0.0, 0.0, 0.0)
            + struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)
            + struct.pack("<I", 0)
            + struct.pack("<3f", 0.0, 0.0, 0.0)
            + struct.pack("<3f", *rigid_scale)
            + struct.pack("<3f", 0.0, 0.0, 0.0)
            + struct.pack("<I", 0)
        )
        node_section = _mdl_section(2, 1, node_body)
    path.write_bytes(
        b"MDL "
        + struct.pack("<II", 4, 0)
        + material_section
        + _mdl_section(1, 1, mesh_body)
        + node_section
        + _mdl_section(4, len(primitive_records), primitive_body)
        + struct.pack("<I", 0xFFFFFFFF)
    )


class ModelPreviewTests(unittest.TestCase):
    def test_static_geometry_is_parsed_and_exposed_by_preview_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "triangle.mdl"
            _build_static_mdl(
                source,
                [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 3.0, 0.0)],
                [0, 1, 2],
                material_index=7,
            )

            geometry = load_model_geometry(source)
            preview = AssetPreviewService().load(source)

            self.assertIsNotNone(geometry)
            assert geometry is not None
            self.assertEqual(geometry.source_vertex_count, 3)
            self.assertEqual(geometry.source_triangle_count, 1)
            self.assertEqual(geometry.mesh_group_count, 1)
            self.assertEqual(geometry.primitive_count, 1)
            self.assertEqual(geometry.bounds_min, (0.0, 0.0, 0.0))
            self.assertEqual(geometry.bounds_max, (2.0, 3.0, 0.0))
            self.assertEqual(geometry.faces, ((0, 1, 2, 7),))
            self.assertFalse(geometry.sampled)
            self.assertIsNotNone(preview.model_geometry)
            self.assertIn(("三角形", "1"), preview.metadata)
            self.assertIn("绑定网格", preview.warning)

    def test_rigid_mesh_uses_its_node_bind_transform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "rigid.mdl"
            _build_static_mdl(
                source,
                [(1.0, 1.0, 1.0), (2.0, 1.0, 1.0), (1.0, 2.0, 1.0)],
                [0, 1, 2],
                rigid_scale=(2.0, 3.0, 4.0),
            )

            geometry = load_model_geometry(source)

            self.assertIsNotNone(geometry)
            assert geometry is not None
            self.assertEqual(
                geometry.vertices,
                (
                    (2.0, 3.0, 4.0),
                    (4.0, 3.0, 4.0),
                    (2.0, 6.0, 4.0),
                ),
            )
            self.assertEqual(geometry.rigid_joint_indices, (0, 0, 0))
            assert geometry.skeleton is not None
            self.assertEqual(geometry.skeleton.node_types, (2,))
            self.assertEqual(
                geometry.skeleton.mesh_group_indices,
                (0,),
            )

    def test_zero_scale_rigid_node_keeps_local_animation_geometry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "hidden_at_bind.mdl"
            local_vertices = [
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            ]
            _build_static_mdl(
                source,
                local_vertices,
                [0, 1, 2],
                rigid_scale=(0.0, 0.0, 0.0),
            )

            geometry = load_model_geometry(source)

            self.assertIsNotNone(geometry)
            assert geometry is not None
            self.assertEqual(
                geometry.vertices,
                ((0.0, 0.0, 0.0),) * 3,
            )
            self.assertEqual(
                geometry.rigid_local_vertices,
                tuple(local_vertices),
            )
            self.assertEqual(len(geometry.faces), 1)
            assert geometry.skeleton is not None
            self.assertEqual(
                geometry.skeleton.noninvertible_bind_indices,
                (0,),
            )

    def test_triangle_limit_rejects_incomplete_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "complete_topology.mdl"
            vertices = [
                (float(index % 3), float(index // 3), float(index % 2))
                for index in range(12)
            ]
            _build_static_mdl(source, vertices, list(range(12)))

            geometry = load_model_geometry(source)

            self.assertIsNotNone(geometry)
            assert geometry is not None
            self.assertEqual(geometry.source_triangle_count, 4)
            self.assertEqual(len(geometry.faces), 4)
            self.assertFalse(geometry.sampled)
            with self.assertRaisesRegex(ValueError, "避免通过丢弃面元"):
                load_model_geometry(source, max_triangles=3)

            with patch(
                "retext.model3d.geometry.DEFAULT_MAX_MODEL_TRIANGLES",
                3,
            ):
                with self.assertRaisesRegex(ValueError, "完整拓扑"):
                    load_model_geometry(source)

    def test_default_vertex_and_memory_budgets_reject_before_geometry_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "budget.mdl"
            _build_static_mdl(
                source,
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
            )

            with patch("retext.model3d.geometry.DEFAULT_MAX_MODEL_VERTICES", 2):
                with self.assertRaisesRegex(ValueError, "顶点.*完整拓扑上限"):
                    load_model_geometry(source)
            with patch("retext.model3d.geometry.MAX_ESTIMATED_GEOMETRY_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "预计内存占用"):
                    load_model_geometry(source)

    def test_software_renderer_supports_filled_and_wireframe_views(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "triangle.mdl"
            _build_static_mdl(
                source,
                [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
            )
            geometry = load_model_geometry(source)
            assert geometry is not None

            filled = render_model_geometry(
                geometry,
                320,
                240,
                yaw=0.35,
                pitch=-0.2,
                zoom=1.0,
            )
            wireframe = render_model_geometry(
                geometry,
                320,
                240,
                yaw=0.35,
                pitch=-0.2,
                zoom=1.0,
                wireframe=True,
            )

            self.assertEqual(filled.size, (320, 240))
            self.assertEqual(wireframe.size, (320, 240))
            self.assertIsNotNone(
                ImageChops.difference(filled, wireframe).getbbox()
            )
            self.assertNotEqual(filled.getpixel((160, 120)), (32, 28, 24))

    def test_uvs_apply_continuous_external_texture_without_face_shading(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "triangle.mdl"
            texture = root / "albedo.dds"
            _build_static_mdl(
                source,
                [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
                material_index=2,
                uvs=[(0.1, 0.1), (0.2, 0.1), (0.1, 0.2)],
            )
            Image.new("RGB", (8, 8), (22, 144, 210)).save(texture)
            geometry = load_model_geometry(source)
            assert geometry is not None

            colored, warnings = apply_model_textures(
                geometry,
                {2: texture},
            )

            self.assertEqual(warnings, ())
            self.assertEqual(colored.textured_material_count, 1)
            self.assertEqual(len(colored.material_textures), 1)
            self.assertEqual(
                colored.material_textures[0][1].getpixel((0, 0)),
                (22, 144, 210, 255),
            )
            rendered = render_model_geometry(
                colored,
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
            )
            self.assertTrue(
                set(rendered.get_flattened_data()).issubset(
                    {(32, 28, 24), (22, 144, 210)}
                )
            )

    def test_depth_buffer_makes_surface_independent_of_face_order(self) -> None:
        vertices = (
            (-1.0, -1.0, -1.0),
            (1.0, -1.0, -1.0),
            (0.0, 1.0, -1.0),
            (-1.0, -1.0, 1.0),
            (1.0, -1.0, 1.0),
            (0.0, 1.0, 1.0),
        )
        far_face = (0, 1, 2, 0)
        near_face = (3, 4, 5, 1)
        geometry = ModelGeometry(
            vertices=vertices,
            faces=(near_face, far_face),
            source_vertex_count=6,
            source_triangle_count=2,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, -1.0),
            bounds_max=(1.0, 1.0, 1.0),
            sampled=False,
        )
        reversed_geometry = ModelGeometry(
            vertices=vertices,
            faces=(far_face, near_face),
            source_vertex_count=6,
            source_triangle_count=2,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, -1.0),
            bounds_max=(1.0, 1.0, 1.0),
            sampled=False,
        )

        first = render_model_geometry(
            geometry,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )
        second = render_model_geometry(
            reversed_geometry,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )

        self.assertEqual(first.tobytes(), second.tobytes())

    def test_material_sampler_flips_dds_and_preserves_render_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "triangle.mdl"
            texture = root / "albedo.dds"
            _build_static_mdl(
                source,
                [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
                material_index=0,
                uvs=[(0.0, 0.0)] * 3,
            )
            image = Image.new("RGBA", (2, 2))
            image.putdata(
                [
                    (255, 0, 0, 255),
                    (255, 0, 0, 255),
                    (0, 0, 255, 255),
                    (0, 0, 255, 255),
                ]
            )
            image.save(texture)
            geometry = load_model_geometry(source)
            assert geometry is not None

            colored, warnings = apply_model_textures(
                geometry,
                {0: texture},
                material_infos={
                    0: ModelMaterialRenderInfo(
                        material_index=0,
                        wrap_s=1,
                        wrap_t=0,
                        cull_mode=2,
                    )
                },
            )

            self.assertEqual(warnings, ())
            self.assertEqual(
                colored.material_textures[0][1].getpixel((0, 0)),
                (0, 0, 255, 255),
            )
            self.assertEqual(colored.material_surfaces[0].wrap_s, 1)
            self.assertEqual(colored.material_surfaces[0].wrap_t, 0)
            self.assertEqual(colored.material_surfaces[0].cull_mode, 2)

    def test_texture_downsampling_preserves_rgb_below_zero_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "triangle.mdl"
            texture = root / "face.png"
            _build_static_mdl(
                source,
                [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
                material_index=0,
                uvs=[(0.5, 0.5)] * 3,
            )
            Image.new("RGBA", (8, 8), (143, 91, 47, 0)).save(texture)
            geometry = load_model_geometry(source)
            assert geometry is not None

            colored, warnings = apply_model_textures(
                geometry,
                {0: texture},
                maximum_texture_size=2,
            )

            self.assertEqual(warnings, ())
            resized = colored.material_textures[0][1]
            self.assertEqual(resized.size, (2, 2))
            self.assertEqual(resized.getpixel((0, 0)), (143, 91, 47, 0))

    def test_interactive_render_size_uses_a_small_fixed_pixel_budget(self) -> None:
        width, height = calculate_render_dimensions(
            900,
            700,
            interactive=True,
        )

        self.assertLessEqual(width * height, 14_000)
        self.assertGreaterEqual(width, 96)
        self.assertGreaterEqual(height, 72)
        self.assertEqual(
            calculate_render_dimensions(900, 700, interactive=False),
            (900, 700),
        )

    def test_renderer_can_cancel_a_superseded_view(self) -> None:
        geometry = ModelGeometry(
            vertices=(
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2, 0),),
            source_vertex_count=3,
            source_triangle_count=1,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, 0.0),
            bounds_max=(1.0, 1.0, 0.0),
            sampled=False,
        )

        with self.assertRaises(ModelRenderCancelled):
            render_model_geometry(
                geometry,
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
                cancelled=lambda: True,
            )

    def test_shadow_only_surface_is_not_drawn(self) -> None:
        geometry = ModelGeometry(
            vertices=(
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2, 0),),
            source_vertex_count=3,
            source_triangle_count=1,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, 0.0),
            bounds_max=(1.0, 1.0, 0.0),
            sampled=False,
            material_surfaces=(
                ModelMaterialSurface(
                    material_index=0,
                    shadow_only=True,
                ),
            ),
        )

        rendered = render_model_geometry(
            geometry,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )

        self.assertEqual(rendered.getbbox(), (0, 0, 160, 120))
        self.assertEqual(set(rendered.get_flattened_data()), {(32, 28, 24)})

    def test_material_can_select_second_uv_channel(self) -> None:
        uv0 = ((0.0, 0.0),) * 3
        uv1 = ((1.0, 0.0),) * 3
        texture = Image.new("RGBA", (2, 1))
        texture.putdata([(255, 0, 0, 255), (0, 0, 255, 255)])
        geometry = ModelGeometry(
            vertices=(
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2, 0),),
            source_vertex_count=3,
            source_triangle_count=1,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, 0.0),
            bounds_max=(1.0, 1.0, 0.0),
            sampled=False,
            uvs=uv0,
            uv_channels=(uv0, uv1),
            material_surfaces=(
                ModelMaterialSurface(
                    material_index=0,
                    texture=texture,
                    uv_channel=1,
                    wrap_s=2,
                    wrap_t=2,
                ),
            ),
        )

        rendered = render_model_geometry(
            geometry,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )

        self.assertEqual(rendered.getpixel((80, 60)), (0, 0, 255))

    def test_transparent_surface_blends_without_writing_opaque_color(
        self,
    ) -> None:
        uv = ((0.0, 0.0),) * 3
        geometry = ModelGeometry(
            vertices=(
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2, 0),),
            source_vertex_count=3,
            source_triangle_count=1,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, 0.0),
            bounds_max=(1.0, 1.0, 0.0),
            sampled=False,
            uvs=uv,
            uv_channels=(uv,),
            material_surfaces=(
                ModelMaterialSurface(
                    material_index=0,
                    texture=Image.new("RGBA", (1, 1), (255, 0, 0, 128)),
                    wrap_s=2,
                    wrap_t=2,
                    blend_mode="blend",
                ),
            ),
        )

        rendered = render_model_geometry(
            geometry,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )

        self.assertEqual(rendered.getpixel((80, 60)), (144, 14, 12))

    def test_back_face_culling_respects_triangle_winding(self) -> None:
        common = dict(
            vertices=(
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            source_vertex_count=3,
            source_triangle_count=1,
            mesh_group_count=1,
            primitive_count=1,
            bounds_min=(-1.0, -1.0, 0.0),
            bounds_max=(1.0, 1.0, 0.0),
            sampled=False,
            material_surfaces=(
                ModelMaterialSurface(
                    material_index=0,
                    cull_mode=2,
                ),
            ),
        )
        front = ModelGeometry(faces=((0, 1, 2, 0),), **common)
        back = ModelGeometry(faces=((0, 2, 1, 0),), **common)

        front_image = render_model_geometry(
            front,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )
        back_image = render_model_geometry(
            back,
            160,
            120,
            yaw=0.0,
            pitch=0.0,
            zoom=1.0,
        )

        self.assertNotEqual(front_image.getpixel((80, 60)), (32, 28, 24))
        self.assertEqual(back_image.getpixel((80, 60)), (32, 28, 24))

    def test_preview_service_resolves_diffuse_dds_without_requiring_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "triangle.mdl"
            texture = root / "albedo.dds"
            _build_static_mdl(
                source,
                [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
                material_index=0,
                uvs=[(0.1, 0.1), (0.2, 0.1), (0.1, 0.2)],
                texture_name="albedo",
            )
            Image.new("RGB", (8, 8), (190, 80, 35)).save(texture)

            plain = AssetPreviewService().load(source)
            textured = AssetPreviewService().load(
                source,
                model_texture_resolver=lambda _name: texture,
            )
            missing = AssetPreviewService().load(
                source,
                model_texture_resolver=lambda _name: None,
            )

            assert plain.model_geometry is not None
            assert textured.model_geometry is not None
            self.assertEqual(plain.model_geometry.textured_material_count, 0)
            self.assertEqual(
                textured.model_geometry.textured_material_count,
                1,
            )
            self.assertIn("只保存贴图引用", plain.warning)
            self.assertIn("外部 DDS", textured.warning)
            self.assertIn("未找到 1 个基础颜色 DDS", missing.warning)
            self.assertIn(
                ("缺失基础颜色 DDS", "1 个唯一引用"),
                missing.metadata,
            )

    def test_out_of_range_indices_do_not_escape_geometry_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "invalid_index.mdl"
            _build_static_mdl(
                source,
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 99],
            )

            self.assertIsNone(load_model_geometry(source))


if __name__ == "__main__":
    unittest.main()
