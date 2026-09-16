from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from retext.archive.collection import PacWorkbench
from retext.archive.workspace import PacWorkspaceManager
from retext.model3d import (
    Model3DService,
    ModelGeometry,
    ModelIdentityService,
    animation_model_family,
    companion_model_entry,
)
from retext.preview import infer_model_identity_table_paths
from retext.model3d.mdl import ModelMaterial
from retext.model3d.gpu import _sorted_transparent_triangle_indices
from tests.corpus import CORPUS_ROOT


def _triangle_geometry() -> ModelGeometry:
    return ModelGeometry(
        vertices=((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)),
        faces=((0, 1, 2, 0),),
        source_vertex_count=3,
        source_triangle_count=1,
        mesh_group_count=1,
        primitive_count=1,
        bounds_min=(-1.0, -1.0, 0.0),
        bounds_max=(1.0, 1.0, 0.0),
        sampled=False,
    )


class ModelRendererServiceTests(unittest.TestCase):
    def test_alpha_triangles_are_sorted_far_to_near_for_each_view(self) -> None:
        vertices = np.asarray(
            (
                (-1.0, -1.0, 1.0),
                (1.0, -1.0, 1.0),
                (0.0, 1.0, 1.0),
                (-1.0, -1.0, -1.0),
                (1.0, -1.0, -1.0),
                (0.0, 1.0, -1.0),
            ),
            dtype=np.float64,
        )
        source_order = np.asarray(((0, 1, 2), (3, 4, 5)), dtype=np.uint32)

        forward = _sorted_transparent_triangle_indices(
            vertices,
            source_order,
            yaw=0.0,
            pitch=0.0,
        )
        backward = _sorted_transparent_triangle_indices(
            vertices,
            source_order,
            yaw=np.pi,
            pitch=0.0,
        )

        self.assertEqual(forward.tolist(), [3, 4, 5, 0, 1, 2])
        self.assertEqual(backward.tolist(), [0, 1, 2, 3, 4, 5])

    def test_material_uses_enabled_nonzero_diffuse_slot(self) -> None:
        material = ModelMaterial(
            name="water",
            shader="map",
            variant="",
            textures=("normal.dds", "color.dds"),
            texture_slots=(0, 2),
            material_switches=(("SWITCH_DIFFUSEMAP2", 1),),
            uv_map_indices=(0, 1, 2),
        )

        info = material.render_info(3)

        self.assertEqual(material.base_color_texture, "color.dds")
        self.assertEqual(info.base_texture_name, "color.dds")
        self.assertEqual(info.uv_channel, 2)

    def test_gpu_is_preferred_and_reused_on_the_render_thread(self) -> None:
        created: list[object] = []

        class FakeGpuRenderer:
            description = "Test GPU"

            def __init__(self) -> None:
                created.append(self)

            def render(self, _geometry, width, height, **_kwargs):
                return Image.new("RGB", (width, height), (1, 2, 3))

        with (
            patch.dict(os.environ, {"TIS_RETEXT_DISABLE_GPU": ""}),
            patch(
                "retext.model3d.service.ModernGLModelRenderer",
                FakeGpuRenderer,
            ),
        ):
            service = Model3DService()
            first = service.render(
                _triangle_geometry(),
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
            )
            second = service.render(
                _triangle_geometry(),
                160,
                120,
                yaw=0.2,
                pitch=0.1,
                zoom=1.0,
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(first.getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(second.getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(service.last_backend_label, "GPU · Test GPU")
        self.assertFalse(service.interaction_requires_low_resolution)

    def test_gpu_failure_falls_back_to_cpu_renderer(self) -> None:
        with (
            patch.dict(os.environ, {"TIS_RETEXT_DISABLE_GPU": ""}),
            patch(
                "retext.model3d.service.ModernGLModelRenderer",
                side_effect=RuntimeError("no driver"),
            ),
        ):
            service = Model3DService()
            image = service.render(
                _triangle_geometry(),
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
            )

        self.assertEqual(image.size, (160, 120))
        self.assertTrue(service.last_backend_label.startswith("CPU"))
        self.assertEqual(service.last_gpu_error, "no driver")
        self.assertTrue(service.interaction_requires_low_resolution)

    def test_model_specific_gpu_failure_is_retried_and_renderer_is_closed(self) -> None:
        created: list[object] = []

        class FakeGpuRenderer:
            description = "Retry GPU"

            def __init__(self) -> None:
                self.closed = False
                created.append(self)

            def render(self, _geometry, width, height, **_kwargs):
                if len(created) == 1:
                    raise RuntimeError("bad model upload")
                return Image.new("RGB", (width, height), (4, 5, 6))

            def close(self) -> None:
                self.closed = True

        with (
            patch.dict(os.environ, {"TIS_RETEXT_DISABLE_GPU": ""}),
            patch(
                "retext.model3d.service.ModernGLModelRenderer",
                FakeGpuRenderer,
            ),
        ):
            service = Model3DService()
            service.render(
                _triangle_geometry(),
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
            )
            image = service.render(
                _triangle_geometry(),
                160,
                120,
                yaw=0.0,
                pitch=0.0,
                zoom=1.0,
            )
            service.close_current_thread()

        self.assertEqual(len(created), 2)
        self.assertTrue(all(item.closed for item in created))
        self.assertEqual(image.getpixel((0, 0)), (4, 5, 6))


class ModelIdentityServiceTests(unittest.TestCase):
    def test_animation_companion_names_cover_direct_face_and_table_links(
        self,
    ) -> None:
        self.assertEqual(
            animation_model_family("chr0001_m_btl_walk"),
            "chr0001",
        )
        self.assertEqual(
            animation_model_family("chr0001_face"),
            "chr0001",
        )
        self.assertEqual(
            animation_model_family("etc0032_mot_00"),
            "etc0032",
        )
        self.assertEqual(
            companion_model_entry(
                "asset/common/model/chr0001_m_btl_walk.mdl",
                "chr0001",
            ),
            "asset/common/model/chr0001.mdl",
        )

        service = ModelIdentityService()
        rows = {
            "t_name": (
                {
                    "script": "chrx000",
                    "model": "chr0300",
                    "face": "chr0300_face",
                },
                {
                    "script": "chrx000",
                    "model": "chr0301",
                    "face": "chr0301_face",
                },
            ),
            "t_status": (
                {
                    "file4": "mon5100",
                    "file1": "mon5103",
                },
            ),
        }
        with patch.object(
            service,
            "_load_rows",
            side_effect=lambda key, _path: rows[key],
        ):
            npc = service.resolve_companion_models(
                "chrx000_m_dash",
                {"t_name": "name.tbl", "t_status": "status.tbl"},
            )
            monster = service.resolve_companion_models(
                "mon5100_m_btl_walk",
                {"t_name": "name.tbl", "t_status": "status.tbl"},
            )

        self.assertEqual(npc.model_keys, ("chr0300", "chr0301"))
        self.assertEqual(monster.model_keys, ("mon5103",))

    def test_real_sc_pac_resolves_character_costume_name(self) -> None:
        pac_path = CORPUS_ROOT / "table_sc.pac"
        if not pac_path.is_file():
            self.skipTest("真实 table_sc.pac 测试资源不存在")
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            manager = PacWorkspaceManager(
                workspaces_root=root / "workspaces",
                trash_root=root / "trash",
            )
            workbench = PacWorkbench(manager)
            try:
                project = workbench.open(pac_path)
                entries = {
                    Path(entry.name.replace("\\", "/")).name.casefold():
                        entry.name
                    for entry in project.workspace.archive.entries
                }
                tables = {
                    key: project.workspace.materialize(
                        entries[f"{key}.tbl"]
                    )
                    for key in ("t_name", "t_status")
                }

                result = ModelIdentityService().resolve(
                    "chr0001_c62",
                    tables,
                )
            finally:
                workbench.close_all()

        self.assertEqual(
            [match.name for match in result.matches],
            ["约书亚：DLC：餐厅风格服装"],
        )
        self.assertEqual(result.matches[0].model_key, "chr0001_c62")

    def test_character_costume_uses_exact_name_table_model(self) -> None:
        service = ModelIdentityService()
        rows = {
            "t_name": (
                {
                    "name": "約書亞：ＤＬＣ：餐廳風服裝",
                    "model": "chr0001_c62",
                },
                {"name": "約書亞", "model": "chr0001"},
            ),
            "t_status": (),
        }
        with patch.object(
            service,
            "_load_rows",
            side_effect=lambda key, _path: rows[key],
        ):
            result = service.resolve(
                "chr0001_c62",
                {"t_name": "name.tbl", "t_status": "status.tbl"},
            )

        self.assertEqual(
            [match.name for match in result.matches],
            ["約書亞：ＤＬＣ：餐廳風服裝"],
        )
        self.assertEqual(result.matches[0].match_mode, "精确模型名")

    def test_animation_uses_base_character_and_monster_status_name(self) -> None:
        service = ModelIdentityService()
        rows = {
            "t_name": ({"name": "約書亞", "model": "chr0001"},),
            "t_status": (
                {
                    "ai_file": "mon5000",
                    "unknown": "mon5000",
                    "file1": "mon5000",
                    "name": "骯髒甲鼠",
                },
            ),
        }
        with patch.object(
            service,
            "_load_rows",
            side_effect=lambda key, _path: rows[key],
        ):
            character = service.resolve(
                "chr0001_m_btl_walk",
                {"t_name": "name.tbl", "t_status": "status.tbl"},
            )
            monster = service.resolve(
                "mon5000_m_btl_walk",
                {"t_name": "name.tbl", "t_status": "status.tbl"},
            )

        self.assertEqual(character.matches[0].name, "約書亞")
        self.assertEqual(character.matches[0].match_mode, "基础模型名")
        self.assertEqual(monster.matches[0].name, "骯髒甲鼠")
        self.assertEqual(monster.matches[0].kind, "怪物")

    def test_unpacked_table_inference_prefers_traditional_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name).resolve()
            model = root / "asset" / "common" / "model" / "chr0001.mdl"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"MDL ")
            table_sc = root / "table_sc"
            table_tc = root / "table_tc"
            table_sc.mkdir()
            table_tc.mkdir()
            (table_sc / "t_name.tbl").write_bytes(b"sc")
            (table_tc / "t_name.tbl").write_bytes(b"tc")
            (table_tc / "t_status.tbl").write_bytes(b"status")

            tables = infer_model_identity_table_paths(model)

        self.assertEqual(tables["t_name"], table_tc / "t_name.tbl")
        self.assertEqual(tables["t_status"], table_tc / "t_status.tbl")


if __name__ == "__main__":
    unittest.main()
