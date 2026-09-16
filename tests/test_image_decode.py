import io
import struct
import tempfile
import unittest
from pathlib import Path

import lz4.frame
from PIL import Image

from retext.image_decode import decode_lz4_image
from retext.preview import AssetPreviewService
from retext.model3d.geometry import apply_model_textures, load_model_geometry
from tests.test_model_preview import _build_static_mdl


class WrappedImageTests(unittest.TestCase):
    def test_lz4_dds_is_shared_by_image_font_and_model_previews(self):
        stream = io.BytesIO()
        Image.new("RGBA", (8, 8), (22, 144, 210, 192)).save(stream, format="DDS")
        compressed = lz4.frame.compress(stream.getvalue(), content_checksum=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            texture = root / "wrapped.dds"
            texture.write_bytes(compressed)
            service = AssetPreviewService()
            preview = service.load(texture)
            self.assertEqual(preview.image.getpixel((0, 0)), (22, 144, 210, 192))
            self.assertIn(("asset_wrapper", "LZ4"), preview.metadata)

            font = root / "font.fnt"
            glyph = struct.pack("<IIHHHHHhHH", 65, 1, 0, 0, 4, 4, 0x100, 0, 0, 4)
            font.write_bytes(struct.pack("<4sHHIHHHHIII4sI", b"FCV\0", 2, 32,
                                        1, 68, 3, 1, 1, 0, 0, 0, b"FLTI", len(glyph)) + glyph)
            atlas = service.load(font, font_atlas_path=texture)
            self.assertEqual(atlas.warning, "")
            self.assertEqual(atlas.font_atlas_image.getpixel((0, 0)), (22, 144, 210, 192))

            model = root / "model.mdl"
            _build_static_mdl(model, [(-1, -1, 0), (1, -1, 0), (0, 1, 0)],
                              [0, 1, 2], material_index=2, uvs=[(0, 0), (1, 0), (0, 1)])
            geometry, warnings = apply_model_textures(load_model_geometry(model), {2: texture})
            self.assertEqual(warnings, ())
            self.assertEqual(geometry.textured_material_count, 1)
            self.assertEqual(geometry.material_textures[0][1].getpixel((0, 0)), (22, 144, 210, 192))
            self.assertEqual(texture.read_bytes(), compressed)

    def test_truncated_checksum_corrupt_and_trailing_frames_are_rejected(self):
        frame = lz4.frame.compress(b"DDS " + bytes(range(128)), content_checksum=True)
        corrupt = bytearray(frame)
        corrupt[-1] ^= 1
        for invalid in (frame[:-1], bytes(corrupt), frame + b"extra", frame[:6]):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                decode_lz4_image(invalid)

    def test_decoded_size_is_bounded_with_or_without_content_size(self):
        for store_size in (True, False):
            frame = lz4.frame.compress(b"A" * 4096, store_size=store_size)
            with self.subTest(store_size=store_size), self.assertRaisesRegex(ValueError, "大小超过"):
                decode_lz4_image(frame, limit=100)
            self.assertEqual(decode_lz4_image(frame, limit=4096), b"A" * 4096)
