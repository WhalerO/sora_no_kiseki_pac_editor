from __future__ import annotations

import hashlib
import io
import json
import lzma
import ssl
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retext.paths import ensure_runtime_root


def _probe_vlc_pcm(vlc_module, instance, root: Path) -> bool:
    """Decode and seek a generated PCM WAV through the packaged VLC plugins."""

    source = root / "vlc-package-probe.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 16_000)

    player = None
    media = None
    try:
        player = instance.media_player_new()
        media = instance.media_new_path(str(source))
        if player is None or media is None:
            raise RuntimeError("LibVLC could not create the PCM probe player.")
        player.set_media(media)
        player.audio_set_mute(True)
        result = player.play()
        if isinstance(result, int) and result < 0:
            raise RuntimeError("LibVLC rejected the PCM probe playback request.")
        deadline = time.monotonic() + 3.0
        seek_deadline = 0.0
        seek_requested = False
        while time.monotonic() < deadline:
            state = player.get_state()
            if state == vlc_module.State.Error:
                raise RuntimeError("LibVLC reported an error decoding PCM WAV.")
            position = player.get_time()
            if not seek_requested:
                if (
                    player.get_length() > 0
                    and state == vlc_module.State.Playing
                    and position >= 40
                    and player.is_seekable()
                ):
                    seek_result = player.set_time(1_200)
                    if isinstance(seek_result, int) and seek_result < 0:
                        raise RuntimeError("LibVLC rejected the PCM probe seek.")
                    seek_requested = True
                    seek_deadline = time.monotonic() + 0.8
            elif 1_000 <= position <= 1_800:
                return True
            elif time.monotonic() >= seek_deadline:
                raise RuntimeError("LibVLC PCM seek did not reach the target.")
            time.sleep(0.02)
        raise RuntimeError("LibVLC PCM decode probe timed out.")
    finally:
        if player is not None:
            try:
                player.stop()
            finally:
                player.release()
        if media is not None:
            media.release()


def _probe_text_pac() -> bool:
    """Verify the frozen application's text engines and PAC write/read path."""
    from retext import RetextService
    from retext.archive import FpacArchiveService
    from scripts.synthetic_samples import make_dat, make_pac, make_tbl

    service = RetextService()
    archive_service = FpacArchiveService()
    with tempfile.TemporaryDirectory(prefix="tis_retext_text_probe_", dir=ensure_runtime_root()) as folder:
        root = Path(folder)
        fixtures = (("t_books.tbl", make_tbl()), ("demo.dat", make_dat()))
        entries = []
        for name, original in fixtures:
            source = root / name
            source.write_bytes(original)
            engines = ("legacy", "kuro_tbl" if name.endswith(".tbl") else "kuro_dat")
            for engine in engines:
                document = service.load(source, engine=engine)
                if not document.units:
                    raise RuntimeError(f"{engine}: synthetic fixture has no text")
                document.units[-1].current_text += " [frozen roundtrip growth]"
                expected = [unit.current_text for unit in document.units]
                output = root / f"{engine}_{name}"
                service.save(document, output_path=output, do_backup=False)
                actual = service.load(output, engine=engine, schema_hint=source.stem)
                if [unit.current_text for unit in actual.units] != expected:
                    raise RuntimeError(f"{engine}: frozen text roundtrip mismatch")
                if source.read_bytes() != original:
                    raise RuntimeError(f"{engine}: frozen probe changed its input")
                entries.append((f"{engine}/{name}", output.read_bytes()))
        pac = root / "generated.pac"
        pac.write_bytes(make_pac(entries))
        parsed = archive_service.inspect(pac)
        if len(parsed.entries) != len(entries):
            raise RuntimeError("Frozen PAC entry count mismatch")
        for index, (name, payload) in enumerate(entries):
            exported = archive_service.extract_entry(parsed, name, root / f"entry_{index}")
            if exported.read_bytes() != payload:
                raise RuntimeError("Frozen PAC payload readback mismatch")
    return True


def _run_package_probe(output_path: Path) -> int:
    """Exercise native runtime dependencies without opening the GUI."""

    payload: dict[str, object]
    try:
        import tkinter

        import moderngl
        import lz4.frame
        import numpy
        import zstandard
        from PIL import Image, __version__ as pillow_version

        from retext.archive.fallback import PacFallbackTools
        from retext.engines.kuro.support import _build_processcle_module
        from retext.model3d import animation as animation_module
        from retext.vlc_runtime import configure_vlc_runtime
        from retext.image_decode import open_asset_image
        from retext.dpi import current_dpi_awareness

        interpreter = tkinter.Tcl()
        left = numpy.asarray(
            ((1.0, 2.0), (3.0, 4.0)),
            dtype=numpy.float64,
        )
        right = numpy.asarray(
            ((2.0, 0.0), (0.0, 2.0)),
            dtype=numpy.float64,
        )
        product = numpy.einsum("ij,jk->ik", left, right)
        image_buffer = io.BytesIO()
        Image.new("RGBA", (2, 2), (12, 34, 56, 78)).save(
            image_buffer,
            format="PNG",
        )
        image_buffer.seek(0)
        with Image.open(image_buffer) as decoded_image:
            pillow_ok = decoded_image.convert("RGBA").getpixel((0, 0)) == (
                12,
                34,
                56,
                78,
            )

        compressed = zstandard.ZstdCompressor().compress(b"TIS_Retext")
        zstandard_ok = (
            zstandard.ZstdDecompressor().decompress(compressed)
            == b"TIS_Retext"
        )
        cle_payload = bytes(range(1, 42))
        cle_wrapped = (
            b"F9BA"
            + len(cle_payload).to_bytes(4, "little")
            + cle_payload
        )
        blowfish_ok = (
            _build_processcle_module().processCLE(cle_wrapped).hex()
            == "9037d3718ea5fad8301071f4385f2b243dd22d9d5e6c99f7"
            "8dd179f4a54e47bd5597aac167f9203e45"
        )
        with tempfile.TemporaryDirectory(prefix="tis_retext_probe_", dir=ensure_runtime_root()) as temp_name:
            fallback_root = Path(temp_name)
            dds_buffer = io.BytesIO()
            Image.new("RGBA", (4, 4), (12, 34, 56, 78)).save(dds_buffer, format="DDS")
            wrapped_dds = fallback_root / "lz4.dds"
            wrapped_dds.write_bytes(lz4.frame.compress(dds_buffer.getvalue()))
            with open_asset_image(wrapped_dds) as decoded:
                lz4_dds_ok = decoded.convert("RGBA").getpixel((0, 0)) == (12, 34, 56, 78)
            fallback_source = fallback_root / "table_sc"
            fallback_source.mkdir()
            (fallback_source / "probe.tbl").write_bytes(b"probe")
            fallback_tools = PacFallbackTools()
            fallback_pac = fallback_tools.build(
                fallback_source,
                fallback_root / "probe.pac",
            )
            fallback_output = fallback_root / "extracted"
            fallback_output.mkdir()
            fallback_result = fallback_tools.extract(
                fallback_pac,
                fallback_output,
            )
            pac_fallback_ok = (
                (fallback_result / "probe.tbl").read_bytes() == b"probe"
            )

        vlc_runtime = configure_vlc_runtime()
        import vlc

        vlc_instance = vlc.Instance(
            "--intf=dummy",
            "--quiet",
            "--aout=dummy",
            "--vout=dummy",
        )
        if vlc_instance is None:
            raise RuntimeError("LibVLC instance creation returned no instance.")
        try:
            vlc_version = vlc.libvlc_get_version()
            if isinstance(vlc_version, bytes):
                vlc_version = vlc_version.decode("utf-8", errors="replace")
            with tempfile.TemporaryDirectory(
                prefix="tis_retext_vlc_probe_", dir=ensure_runtime_root()
            ) as vlc_temp_name:
                vlc_pcm_ok = _probe_vlc_pcm(
                    vlc,
                    vlc_instance,
                    Path(vlc_temp_name),
                )
        finally:
            vlc_instance.release()

        gpu_probe: dict[str, object]
        try:
            context = moderngl.create_context(standalone=True, require=330)
            try:
                gpu_probe = {
                    "available": True,
                    "version_code": context.version_code,
                    "renderer": str(context.info.get("GL_RENDERER", "")),
                }
            finally:
                context.release()
        except Exception as exc:
            # GPU is optional because the application has a tested CPU path.
            gpu_probe = {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        text_pac_ok = _probe_text_pac()
        required_ok = bool(
            animation_module._np is not None
            and product.tolist() == [[2.0, 4.0], [6.0, 8.0]]
            and pillow_ok
            and zstandard_ok
            and blowfish_ok
            and pac_fallback_ok
            and vlc_runtime is not None
            and vlc_runtime.source == "bundled"
            and bool(vlc_version)
            and vlc_pcm_ok
            and text_pac_ok
            and lz4_dds_ok
        )
        payload = {
            "ok": required_ok,
            "numpy": numpy.__version__,
            "pillow": pillow_version,
            "zstandard": zstandard.__version__,
            "blowfish_cle": blowfish_ok,
            "pac_fallback": pac_fallback_ok,
            "text_pac_roundtrip": text_pac_ok,
            "lz4_dds_decode": lz4_dds_ok,
            "dpi_awareness": current_dpi_awareness(),
            "data_root": str(ensure_runtime_root().parent),
            "vlc": str(vlc_version),
            "vlc_runtime": str(vlc_runtime.root) if vlc_runtime else "",
            "vlc_runtime_source": vlc_runtime.source if vlc_runtime else "",
            "vlc_pcm_decode": vlc_pcm_ok,
            "gpu": gpu_probe,
            "tcl": str(interpreter.call("info", "patchlevel")),
            "openssl": ssl.OPENSSL_VERSION,
            "sha256": hashlib.sha256(b"TIS_Retext").hexdigest(),
            "lzma_bytes": len(lzma.compress(b"TIS_Retext")),
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if payload["ok"] else 2


def main() -> int:
    from retext.dpi import configure_dpi_awareness

    configure_dpi_awareness()
    if len(sys.argv) >= 2 and sys.argv[1] == "--package-probe":
        if len(sys.argv) != 3:
            print(
                "usage: launch_gui.py --package-probe OUTPUT.json",
                file=sys.stderr,
            )
            return 2
        return _run_package_probe(Path(sys.argv[2]).resolve())
    if len(sys.argv) >= 3 and sys.argv[1] == "--pac-fallback-child":
        from retext.archive.fallback import run_fallback_child

        return run_fallback_child(sys.argv[2:])

    from tk_gui import launch

    launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
