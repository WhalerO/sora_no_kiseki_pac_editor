from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from retext.media_probe import MediaProbeError, probe_webm
from retext.model3d import (
    ModelIdentityMatch,
    ModelIdentityResolution,
)
from retext.preview import (
    AssetPreviewService,
    infer_font_atlas_entry,
    infer_font_atlas_pac_name,
    infer_font_atlas_path,
    infer_model_texture_entry,
    infer_model_texture_path,
    render_font_glyph,
)


def _ebml_size(value: int) -> bytes:
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, "big")
    raise ValueError("synthetic EBML element is too large")


def _ebml_element(element_id: int, payload: bytes) -> bytes:
    width = max(1, (element_id.bit_length() + 7) // 8)
    return element_id.to_bytes(width, "big") + _ebml_size(len(payload)) + payload


def _ebml_uint(element_id: int, value: int) -> bytes:
    width = max(1, (value.bit_length() + 7) // 8)
    return _ebml_element(element_id, value.to_bytes(width, "big"))


def _ebml_float(element_id: int, value: float) -> bytes:
    return _ebml_element(element_id, struct.pack(">d", value))


def _ebml_text(element_id: int, value: str) -> bytes:
    return _ebml_element(element_id, value.encode("utf-8"))


class MediaPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AssetPreviewService()

    def test_png_and_dds_images_are_decoded_for_display(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for suffix in (".png", ".dds"):
                source = root / f"image{suffix}"
                Image.new("RGBA", (32, 16), (20, 40, 60, 128)).save(source)

                preview = self.service.load(source)

                self.assertEqual(preview.kind, "image")
                self.assertEqual(preview.image.size, (32, 16))
                self.assertEqual(preview.image.mode, "RGBA")
                self.assertIn(("尺寸", "32 × 16"), preview.metadata)

    def test_pcm_wav_preview_reports_metadata_and_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "tone.wav"
            sample_rate = 8_000
            frames = 4_000
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                payload = bytearray()
                for index in range(frames):
                    value = round(
                        math.sin(index * math.tau * 440 / sample_rate) * 20_000
                    )
                    payload.extend(struct.pack("<h", value))
                output.writeframes(payload)

            preview = self.service.load(source)

            self.assertEqual(preview.kind, "audio")
            self.assertAlmostEqual(preview.duration_seconds, 0.5)
            self.assertTrue(preview.waveform)
            self.assertGreater(max(preview.waveform), 0.5)
            self.assertIn(("采样率", "8,000 Hz"), preview.metadata)

    def test_webm_preview_uses_bounded_ebml_metadata_without_a_poster(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "movie.webm"
            info = _ebml_element(
                0x1549A966,
                _ebml_uint(0x2AD7B1, 1_000_000)
                + _ebml_float(0x4489, 12_500.0),
            )
            video = _ebml_element(
                0xAE,
                _ebml_uint(0x83, 1)
                + _ebml_text(0x86, "V_VP9")
                + _ebml_uint(0x23E383, round(1_000_000_000 / 60))
                + _ebml_element(
                    0xE0,
                    _ebml_uint(0xB0, 1920) + _ebml_uint(0xBA, 1080),
                ),
            )
            audio = _ebml_element(
                0xAE,
                _ebml_uint(0x83, 2)
                + _ebml_text(0x86, "A_OPUS")
                + _ebml_element(
                    0xE1,
                    _ebml_float(0xB5, 48_000.0) + _ebml_uint(0x9F, 2),
                ),
            )
            tracks = _ebml_element(0x1654AE6B, video + audio)
            source.write_bytes(
                _ebml_element(0x1A45DFA3, b"")
                + _ebml_element(0x18538067, info + tracks)
            )

            preview = self.service.load(source)

            self.assertEqual(preview.kind, "video")
            self.assertEqual(preview.duration_seconds, 12.5)
            self.assertIsNone(preview.image)
            self.assertIn(("视频编码", "VP9"), preview.metadata)
            self.assertIn(("画面尺寸", "1920 × 1080"), preview.metadata)
            self.assertIn(("音频编码", "Opus"), preview.metadata)
            self.assertIn(("音频声道", "2"), preview.metadata)
            self.assertEqual(preview.warning, "")

    def test_malformed_webm_keeps_extract_and_play_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "movie.webm"
            source.write_bytes(b"placeholder")

            preview = self.service.load(source)

            self.assertEqual(preview.kind, "video")
            self.assertEqual(preview.duration_seconds, 0.0)
            self.assertIn(("格式", "WebM"), preview.metadata)
            self.assertIn("仍可尝试在预览页内播放", preview.warning)

    def test_webm_nested_metadata_cannot_escape_the_scan_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "oversized-header.webm"
            tracks = _ebml_element(0x1654AE6B, b"\xEC\x81\x00" * 32)
            source.write_bytes(
                _ebml_element(0x1A45DFA3, b"")
                + _ebml_element(0x18538067, tracks)
            )

            with patch("retext.media_probe._MAX_HEADER_SCAN", 32):
                with self.assertRaisesRegex(MediaProbeError, "安全解析上限"):
                    probe_webm(source)

    def test_fcv_font_preview_parses_glyphs_and_uses_channel_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            font_dir = root / "asset_sc" / "common" / "font"
            atlas_dir = root / "asset_sc" / "dx11" / "image"
            font_dir.mkdir(parents=True)
            atlas_dir.mkdir(parents=True)
            source = font_dir / "font_0.fnt"
            atlas = atlas_dir / "font_0.png"
            glyphs = [
                (0x41, 1, 2, 3, 5, 7, 0x100, -1, 4, 6),
                (0x42, 1, 10, 8, 4, 6, 0x200, 2, 3, 5),
            ]
            payload = b"".join(
                struct.pack("<IIHHHHHhHH", *glyph)
                for glyph in glyphs
            )
            source.write_bytes(
                struct.pack(
                    "<4sHHIHHHHIII4sI",
                    b"FCV\0",
                    2,
                    32,
                    len(glyphs),
                    68,
                    3,
                    1,
                    1,
                    0,
                    0,
                    0,
                    b"FLTI",
                    len(payload),
                )
                + payload
            )
            atlas_image = Image.new("RGBA", (32, 24), (0, 0, 0, 255))
            for y in range(3, 10):
                for x in range(2, 7):
                    atlas_image.putpixel((x, y), (0, 220, 0, 255))
            for y in range(8, 14):
                for x in range(10, 14):
                    atlas_image.putpixel((x, y), (180, 0, 0, 255))
            atlas_image.save(atlas)

            preview = self.service.load(source, font_atlas_path=atlas)

            self.assertTrue(self.service.supports(source))
            self.assertEqual(preview.kind, "font")
            self.assertEqual(len(preview.font_glyphs), 2)
            self.assertEqual(preview.font_glyphs[0].character, "A")
            self.assertEqual(preview.font_glyphs[0].offset_x, -1)
            self.assertEqual(preview.font_glyphs[1].advance, 5)
            self.assertEqual(preview.font_atlas_image.size, (32, 24))
            rendered = render_font_glyph(preview, preview.font_glyphs[0])
            self.assertIsNotNone(rendered)
            self.assertIsNotNone(rendered.getbbox())
            self.assertEqual(preview.warning, "")

    def test_font_atlas_inference_and_missing_atlas_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = (
                Path(temp_name)
                / "asset"
                / "common"
                / "font"
                / "font_0.fnt"
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(
                struct.pack(
                    "<4sHHIHHHHIII4sI",
                    b"FCV\0",
                    2,
                    32,
                    0,
                    68,
                    3,
                    1,
                    1,
                    0,
                    0,
                    0,
                    b"FLTI",
                    0,
                )
            )

            inferred = infer_font_atlas_path(source)
            preview = self.service.load(source)

            self.assertEqual(
                inferred,
                Path(temp_name) / "asset" / "dx11" / "image" / "font_0.dds",
            )
            self.assertEqual(
                infer_font_atlas_entry("asset/common/font/font_0.fnt"),
                "asset/dx11/image/font_0.dds",
            )
            self.assertEqual(
                infer_font_atlas_pac_name("asset_sc/common/font/font_0.fnt"),
                "image_sc.pac",
            )
            self.assertEqual(
                infer_font_atlas_pac_name("asset/common/font/font_0.fnt"),
                "image.pac",
            )
            self.assertIn("可选的 DDS 字体图集", preview.warning)
            self.assertIsNone(preview.image)

    def test_fcv_font_rejects_inconsistent_payload_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "broken.fnt"
            source.write_bytes(
                struct.pack(
                    "<4sHHIHHHHIII4sI",
                    b"FCV\0",
                    2,
                    32,
                    1,
                    68,
                    3,
                    1,
                    1,
                    0,
                    0,
                    0,
                    b"FLTI",
                    0,
                )
            )

            with self.assertRaisesRegex(ValueError, "大小不一致"):
                self.service.load(source)

    def test_mdl_preview_reports_sections_materials_and_textures(self) -> None:
        def mdl_text(value: str) -> bytes:
            encoded = value.encode("utf-8")
            return bytes([len(encoded)]) + encoded

        with tempfile.TemporaryDirectory() as temp_name:
            # PAC preview caches use numeric physical names; the logical PAC
            # path must remain the identity key for table lookups.
            source = Path(temp_name) / "000082.mdl"
            material = (
                mdl_text("body")
                + mdl_text("chr_cloth")
                + mdl_text("default")
                + struct.pack("<I", 1)
                + mdl_text("c0000_body")
                + struct.pack("<IIIII", 0, 0, 0, 0, 0)
                + struct.pack("<I", 1)
                + mdl_text("roughness")
                + struct.pack("<II", 0, 0x3F800000)
                + struct.pack("<I", 1)
                + mdl_text("USE_NORMAL")
                + struct.pack("<I", 1)
                + struct.pack("<I", 1)
                + b"\0"
                + struct.pack("<I", 0)
                + b"\0" * 20
            )
            sections = (
                struct.pack("<III", 0, 4 + len(material), 1)
                + material
                + struct.pack("<III", 1, 4, 2)
                + struct.pack("<III", 2, 4, 3)
                + struct.pack("<III", 4, 4, 5)
            )
            source.write_bytes(
                b"MDL "
                + struct.pack("<II", 4, 123)
                + sections
                + struct.pack("<I", 0xFFFFFFFF)
            )

            preview = self.service.load(source)

            self.assertTrue(self.service.supports(source))
            self.assertEqual(preview.kind, "model")
            self.assertEqual(
                [section.section_type for section in preview.model_sections],
                [0, 1, 2, 4],
            )
            self.assertEqual(preview.model_sections[1].item_count, 2)
            self.assertEqual(preview.model_materials[0].name, "body")
            self.assertEqual(preview.model_materials[0].shader, "chr_cloth")
            self.assertEqual(
                preview.model_materials[0].textures,
                ("c0000_body",),
            )
            self.assertEqual(preview.model_materials[0].texture_slots, (0,))
            self.assertEqual(preview.model_materials[0].texture_wrap_s, (0,))
            self.assertEqual(preview.model_materials[0].texture_wrap_t, (0,))
            self.assertEqual(
                preview.model_materials[0].material_switches,
                (("USE_NORMAL", 1),),
            )
            self.assertEqual(preview.model_materials[0].uv_map_indices, (0,))
            self.assertEqual(
                preview.model_materials[0].diffuse_texture,
                "c0000_body",
            )
            self.assertIn(("模型类型", "网格模型"), preview.metadata)
            self.assertIn(("纹理 1", "c0000_body.dds"), preview.metadata)

            identity_tables = {"t_name": Path(temp_name) / "000084.tbl"}
            resolution = ModelIdentityResolution(
                matches=(
                    ModelIdentityMatch(
                        name="约书亚：DLC：餐厅风格服装",
                        kind="角色 / NPC",
                        model_key="chr0001_c62",
                        source_table="t_name.tbl / NameTableData.model",
                        match_mode="精确模型名",
                    ),
                ),
                searched_tables=("t_name.tbl",),
            )
            with patch(
                "retext.preview.model_identity_service.resolve",
                return_value=resolution,
            ) as resolve:
                traced = self.service.load(
                    source,
                    model_identity_tables=identity_tables,
                    model_logical_path=(
                        r"asset\common\model\chr0001_c62.mdl"
                    ),
                )

            resolve.assert_called_once_with(
                "chr0001_c62",
                identity_tables,
            )
            self.assertIn(
                ("对象名称", "约书亚：DLC：餐厅风格服装"),
                traced.metadata,
            )

    def test_model_texture_paths_are_inferred_from_image_resource_tree(self) -> None:
        logical_path = "asset/common/model/chr0001.mdl"
        self.assertEqual(
            infer_model_texture_entry(logical_path, "chr0001_03_a"),
            "asset/dx11/image/chr0001_03_a.dds",
        )
        source = Path("root") / "asset" / "common" / "model" / "chr0001.mdl"
        self.assertEqual(
            infer_model_texture_path(source, "chr0001_03_a"),
            Path("root")
            / "asset"
            / "dx11"
            / "image"
            / "chr0001_03_a.dds",
        )
        self.assertIsNone(
            infer_model_texture_entry(logical_path, "../outside")
        )
        self.assertIsNone(
            infer_model_texture_path(source, r"..\outside")
        )

    def test_mdl_preview_rejects_section_past_end_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "broken.mdl"
            source.write_bytes(
                b"MDL "
                + struct.pack("<II", 4, 0)
                + struct.pack("<III", 1, 1_000, 1)
                + struct.pack("<I", 0xFFFFFFFF)
            )

            with self.assertRaisesRegex(ValueError, "越过文件末尾"):
                self.service.load(source)

    def test_mi_preview_validates_and_reports_field_dictionary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "model.mi"
            fields = ("Bounding", "DynamicBone", "root")
            dictionary = b"".join(
                struct.pack(
                    "<I",
                    (~zlib.crc32(field.encode("utf-8"))) & 0xFFFFFFFF,
                )
                + field.encode("utf-8")
                + b"\0"
                for field in fields
            )
            payload_offset = 21 + len(dictionary)
            source.write_bytes(
                struct.pack(
                    "<4sIIII",
                    b"JSON",
                    0,
                    payload_offset,
                    0,
                    0xFFFFFFFF,
                )
                + b"\0"
                + dictionary
                + b"\x04\x00"
            )

            preview = self.service.load(source)

            self.assertTrue(self.service.supports(source))
            self.assertEqual(preview.kind, "model_info")
            self.assertEqual(preview.model_info_fields, fields)
            self.assertIn(("字段字典", "3 项"), preview.metadata)
            self.assertIn(("结构名 2", "DynamicBone"), preview.metadata)

    def test_mi_preview_rejects_bad_field_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "broken.mi"
            field = b"Bounding"
            dictionary = struct.pack("<I", 0) + field + b"\0"
            source.write_bytes(
                struct.pack(
                    "<4sIIII",
                    b"JSON",
                    0,
                    21 + len(dictionary),
                    0,
                    0xFFFFFFFF,
                )
                + b"\0"
                + dictionary
                + b"\x04"
            )

            with self.assertRaisesRegex(ValueError, "校验值无效"):
                self.service.load(source)


if __name__ == "__main__":
    unittest.main()
