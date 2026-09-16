"""Read-only image decoding shared by previews, font atlases and model textures."""
from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import lz4.frame
from PIL import Image

LZ4_MAGIC = b"\x04\x22\x4d\x18"
MAX_IMAGE_PAYLOAD_BYTES = 256 * 1024 * 1024


def decode_lz4_image(payload: bytes, *, limit: int = MAX_IMAGE_PAYLOAD_BYTES) -> bytes:
    """Decode one complete frame, enforcing the bound even without a size field."""
    if limit <= 0:
        raise ValueError("Image payload limit must be positive.")
    try:
        info = lz4.frame.get_frame_info(payload)
        if info["content_size"] > limit:
            raise ValueError("LZ4 图片解压大小超过预览限制。")
        with lz4.frame.LZ4FrameDecompressor() as decoder:
            decoded = decoder.decompress(payload, max_length=limit + 1)
            if len(decoded) > limit:
                raise ValueError("LZ4 图片解压大小超过预览限制。")
            if not decoder.eof:
                raise ValueError("LZ4 图片压缩帧不完整。")
            if decoder.unused_data:
                raise ValueError("LZ4 图片压缩帧后存在额外数据。")
    except RuntimeError as exc:
        raise ValueError(f"LZ4 图片解压失败：{exc}") from exc
    return decoded


@contextmanager
def open_asset_image(path: str | Path) -> Iterator[Image.Image]:
    """Open plain PNG/DDS or an LZ4-wrapped image without rewriting its file."""
    with Path(path).open("rb") as stream:
        magic = stream.read(4)
        stream.seek(0)
        if magic == LZ4_MAGIC:
            payload = stream.read(MAX_IMAGE_PAYLOAD_BYTES + 1)
            if len(payload) > MAX_IMAGE_PAYLOAD_BYTES:
                raise ValueError("LZ4 图片文件大小超过预览限制。")
            with io.BytesIO(decode_lz4_image(payload)) as decoded:
                with Image.open(decoded) as image:
                    image.info["asset_wrapper"] = "LZ4"
                    yield image
        else:
            with Image.open(stream) as image:
                yield image
