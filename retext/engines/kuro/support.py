from __future__ import annotations

import importlib
import io
from types import ModuleType

_KEY = b"\x16\x4B\x7D\x0F\x4F\xA7\x4C\xAC\xD3\x7A\x06\xD9\xF8\x6D\x20\x94"
_IV = b"\x9D\x8F\x9D\xA1\x49\x60\xCC\x4C"
MAX_CLE_LAYERS = 8
MAX_CLE_OUTPUT_BYTES = 512 * 1024 * 1024


class MissingOptionalDependency(RuntimeError):
    pass


def _build_processcle_module() -> ModuleType:
    try:
        from Crypto.Cipher import Blowfish
    except ImportError as exc:
        raise MissingOptionalDependency(
            "pycryptodome is required for encrypted CLE payloads."
        ) from exc

    initial_counter = int.from_bytes(_IV, "big")

    def decrypt_ctr(payload: bytes) -> bytes:
        if len(payload) > MAX_CLE_OUTPUT_BYTES:
            raise ValueError("Encrypted CLE payload exceeds the 512 MiB safety limit.")
        cipher = Blowfish.new(
            _KEY,
            Blowfish.MODE_CTR,
            nonce=b"",
            initial_value=initial_counter,
        )
        return cipher.decrypt(payload)

    def decompress_zstandard(payload: bytes) -> bytes:
        try:
            zstandard = importlib.import_module("zstandard")
        except ImportError as exc:
            raise MissingOptionalDependency(
                "zstandard is required for compressed CLE payloads."
            ) from exc
        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(payload)
        ) as reader:
            result = reader.read(MAX_CLE_OUTPUT_BYTES + 1)
        if len(result) > MAX_CLE_OUTPUT_BYTES:
            raise ValueError(
                "CLE decompressed payload exceeds the 512 MiB safety limit."
            )
        return result

    def process_cle(file_content: bytes) -> bytes:
        result = file_content
        magic = file_content[0:4]
        layer_count = 0
        while magic in {b"F9BA", b"C9BA", b"D9BA"}:
            layer_count += 1
            if layer_count > MAX_CLE_LAYERS:
                raise ValueError("CLE payload nesting exceeds the safety limit.")
            if len(file_content) < 8:
                raise ValueError("CLE payload header is incomplete.")
            if magic in {b"F9BA", b"C9BA"}:
                result = decrypt_ctr(file_content[8:])
            else:
                result = decompress_zstandard(file_content[8:])
            if len(result) > MAX_CLE_OUTPUT_BYTES:
                raise ValueError("CLE payload exceeds the 512 MiB safety limit.")
            file_content = result
            magic = file_content[0:4]
        return result

    module = ModuleType("processcle")
    module.processCLE = process_cle
    return module


def import_kuro_module(name: str):
    """Import a bundled KuroTools module without changing global ``sys.path``."""

    qualified = name if name.startswith(f"{__package__}.") else f"{__package__}.{name}"
    return importlib.import_module(qualified)


def clear_disasm_modules() -> None:
    import sys

    package_name = f"{__package__}.disasm"
    prefix = f"{__package__}.disasm."
    for key in list(sys.modules):
        if key.startswith(prefix):
            sys.modules.pop(key, None)

    # ``from package import child`` also stores the imported module as an
    # attribute on the parent package.  Removing only ``sys.modules`` leaves
    # that stale attribute alive, so a later import can silently reuse state
    # (instruction dictionaries, assembler globals, open streams) from the
    # previous DAT.  KuroTools was written as a command-line program and uses
    # module globals extensively; clear both import caches to isolate files.
    package = sys.modules.get(package_name)
    if package is not None:
        for name in (
            "ED9Assembler",
            "ED9Disassembler",
            "ED9InstructionsSet",
            "function",
            "script",
        ):
            package.__dict__.pop(name, None)
