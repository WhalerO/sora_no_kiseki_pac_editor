"""Compatibility entry points for KuroTools CLE payloads.

The original KuroTools script depended on a bundled GPL Blowfish module and
attempted to install dependencies at runtime.  TIS_Retext supplies the same CTR
operation through PyCryptodome and keeps dependency installation outside the
application process.
"""

from __future__ import annotations

import math

import zstandard
from Crypto.Cipher import Blowfish

from retext.engines.kuro.support import _build_processcle_module


_KEY = b"\x16\x4B\x7D\x0F\x4F\xA7\x4C\xAC\xD3\x7A\x06\xD9\xF8\x6D\x20\x94"
_IV = int.from_bytes(b"\x9D\x8F\x9D\xA1\x49\x60\xCC\x4C", "big")

processCLE = _build_processcle_module().processCLE


def _crypt_ctr(payload: bytes) -> bytes:
    cipher = Blowfish.new(
        _KEY,
        Blowfish.MODE_CTR,
        nonce=b"",
        initial_value=_IV,
    )
    return cipher.encrypt(payload)


def _pad_to_block(payload: bytes) -> bytes:
    padding = 8 * math.ceil(len(payload) / 8) - len(payload)
    return payload + b"0" * padding


def compressCLE(file_content: bytes) -> bytes:
    result = _pad_to_block(
        zstandard.ZstdCompressor(level=9, write_checksum=True).compress(
            file_content
        )
    )
    return b"D9BA" + len(result).to_bytes(4, "little") + result


def encryptCLE(file_content: bytes) -> bytes:
    result = _pad_to_block(_crypt_ctr(file_content))
    return b"F9BA" + len(result).to_bytes(4, "little") + result


def unwrapCLE(file_content: bytes) -> tuple[bytes, tuple[bytes, ...]]:
    """Decode CLE layers while retaining enough information to rebuild them.

    ``processCLE`` intentionally returns only the innermost payload.  Editors
    also need the ordered envelope kinds so a modified TBL/DAT can be written
    with the same compression/encryption stack instead of silently losing its
    wrapper.
    """

    layers: list[bytes] = []
    payload = file_content
    for _ in range(8):
        magic = payload[:4]
        if magic not in {b"F9BA", b"C9BA", b"D9BA"}:
            return payload, tuple(layers)
        if len(payload) < 8:
            raise ValueError("CLE payload header is incomplete.")
        layers.append(magic)
        body = payload[8:]
        if magic in {b"F9BA", b"C9BA"}:
            payload = _crypt_ctr(body)
        else:
            payload = zstandard.ZstdDecompressor().decompress(body)
    raise ValueError("CLE payload nesting exceeds the safety limit.")


def wrapCLE(file_content: bytes, layers: tuple[bytes, ...]) -> bytes:
    """Reapply a wrapper stack returned by :func:`unwrapCLE`."""

    payload = file_content
    for magic in reversed(layers):
        if magic == b"D9BA":
            payload = compressCLE(payload)
        elif magic in {b"F9BA", b"C9BA"}:
            encrypted = _pad_to_block(_crypt_ctr(payload))
            payload = magic + len(encrypted).to_bytes(4, "little") + encrypted
        else:
            raise ValueError(f"Unsupported CLE wrapper: {magic!r}")
    return payload
