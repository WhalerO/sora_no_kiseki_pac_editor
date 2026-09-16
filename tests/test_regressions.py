from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import zstandard

from retext import DocumentKind, RetextService, SavePlan, TextDocument, TextUnit
from retext.business import (
    BatchMapping,
    BusinessFileTarget,
    ServiceBatchHit,
    WorkspaceBusiness,
    apply_batch_behavior,
    load_batch_behavior,
    normalize_batch_mappings,
    serialize_batch_behavior,
)
from retext.engines.kuro.dat import KuroDatEngine, _script_structure_fingerprint
from retext.engines.kuro.processcle import unwrapCLE
from retext.engines.kuro.support import _build_processcle_module, import_kuro_module
from retext.engines.kuro.tbl import HeaderState, KuroTblEngine, KuroTblState
from retext.engines.legacy import models
from retext.engines.legacy.engine import _legacy_modules
from retext.domain import GameVersion
from retext.engines.relocation import (
    StringReference,
    discover_tbl_references,
    parse_dat_references,
    repair_dat_references_from_reference,
    splice_referenced_strings,
)
from retext.io_utils import atomic_copy_file
from retext.paths import REPO_ROOT
from retext.session import DocumentSession, SessionOptions


SAMPLES = REPO_ROOT / "samples"


def sample_path(name: str) -> Path:
    path = SAMPLES / name
    if not path.is_file():
        raise unittest.SkipTest(f"optional real sample is unavailable: {name}")
    return path


def minimal_exact_dat() -> bytes:
    """Build one function with exact function-name and PUSHSTRING pointers."""

    function_start = 56
    strings_start = 63
    function_name = b"func\0"
    text = "阵".encode("utf-8") + b"\0"
    text_offset = strings_start + len(function_name)
    payload = bytearray(text_offset + len(text))
    payload[:4] = b"#scp"
    payload[4:8] = (24).to_bytes(4, "little")
    payload[8:12] = (1).to_bytes(4, "little")
    payload[12:16] = function_start.to_bytes(4, "little")
    payload[24:28] = function_start.to_bytes(4, "little")
    payload[32:36] = function_start.to_bytes(4, "little")
    payload[36:40] = function_start.to_bytes(4, "little")
    payload[44:48] = function_start.to_bytes(4, "little")
    payload[52:56] = (0xC0000000 | strings_start).to_bytes(4, "little")
    payload[function_start : function_start + 2] = b"\0\x04"
    payload[function_start + 2 : function_start + 6] = (
        0xC0000000 | text_offset
    ).to_bytes(4, "little")
    payload[function_start + 6] = 13
    payload[strings_start:text_offset] = function_name
    payload[text_offset:] = text
    return bytes(payload)


def divergent_multi_pointer_dat(*, shift: int = 0, wrong_pointer: bool = False) -> bytes:
    """Build one shifted function with an intentionally ambiguous valid pointer."""

    function_start = 56 + shift
    pointer_count = 12
    code_length = pointer_count * 11 + 1
    strings_start = function_start + code_length
    slots = [b"func"] + [f"text{index}".encode() for index in range(pointer_count)]
    slot_starts: list[int] = []
    cursor = strings_start
    for raw in slots:
        slot_starts.append(cursor)
        cursor += len(raw) + 1
    payload = bytearray(cursor)
    payload[:4] = b"#scp"
    payload[4:8] = (24).to_bytes(4, "little")
    payload[8:12] = (1).to_bytes(4, "little")
    payload[12:16] = function_start.to_bytes(4, "little")
    payload[24:28] = function_start.to_bytes(4, "little")
    payload[32:36] = function_start.to_bytes(4, "little")
    payload[36:40] = function_start.to_bytes(4, "little")
    payload[44:48] = function_start.to_bytes(4, "little")
    payload[52:56] = (0xC0000000 | slot_starts[0]).to_bytes(4, "little")
    position = function_start
    for index in range(pointer_count):
        operand = 0xAAAA0000 + index
        if shift and index in {5, 6}:
            operand = 0xBBBB0000 + index
        payload[position] = 2
        payload[position + 1 : position + 5] = operand.to_bytes(4, "little")
        payload[position + 5 : position + 7] = b"\0\x04"
        slot_index = index + 1
        if wrong_pointer and index == 5:
            slot_index += 1
        payload[position + 7 : position + 11] = (
            0xC0000000 | slot_starts[slot_index]
        ).to_bytes(4, "little")
        position += 11
    payload[position] = 13
    cursor = strings_start
    for raw in slots:
        payload[cursor : cursor + len(raw)] = raw
        cursor += len(raw) + 1
    return bytes(payload)


class LegacyRegressionTests(unittest.TestCase):
    def test_slot_growth_overwrites_padding_without_resizing_file(self) -> None:
        core = _legacy_modules()
        data = b"a\0\0TAIL"
        cluster = models.Cluster(base=0, start=0, end=len(data))
        entries = [models.Entry(0, 0, b"a", "a", "ab")]

        self.assertTrue(core.precheck("TBL", data, cluster, entries, "slot").slot_safe)
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "case.tbl"
            core.save_slot_write(str(target), data, cluster, entries)
            self.assertEqual(target.read_bytes(), b"ab\0TAIL")
            self.assertEqual(target.stat().st_size, len(data))

    def test_slot_growth_reserves_the_string_terminator(self) -> None:
        core = _legacy_modules()
        data = b"abc\0DEF\0"
        cluster = models.Cluster(base=0, start=0, end=len(data))
        entries = [models.Entry(0, 0, b"abc", "abc", "abcd")]

        self.assertFalse(core.precheck("TBL", data, cluster, entries, "slot").slot_safe)
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "case.tbl"
            target.write_bytes(data)
            with self.assertRaises(RuntimeError):
                core.save_slot_write(str(target), data, cluster, entries)
            self.assertEqual(target.read_bytes(), data)

    def test_legacy_raw_equal_batch_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "case.tbl"
            original = b"prefix OLD suffix\0"
            target.write_bytes(original)
            hit = SimpleNamespace(
                checked=True,
                file=str(target),
                offset=7,
                _patch_old="OLD",
                _patch_new="NEW",
            )
            ok, fail, logs = WorkspaceBusiness().execute_equal_batch(
                [hit],
                [("OLD", "NEW")],
                do_backup=True,
            )
            self.assertEqual((ok, fail), (0, 1))
            self.assertFalse(Path(f"{target}.bak").exists())
            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(any("任意字节" in line for line in logs))

    def test_real_tbl_growth_uses_structural_legacy_repack(self) -> None:
        service = RetextService()
        document = service.load(sample_path("t_books.tbl"), engine="legacy")
        document.units[0].current_text += "A"
        plan = service.preview_save(document)
        self.assertEqual(plan.mode, "repack")
        self.assertTrue(plan.safe)

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "t_books.tbl"
            expected = [unit.current_text for unit in document.units]
            service.save(document, output_path=target)
            verified = service.load(target, engine="legacy", schema_hint="t_books")
            self.assertEqual([unit.current_text for unit in verified.units], expected)

    def test_stale_source_is_not_overwritten(self) -> None:
        service = RetextService()
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "t_books.tbl"
            target.write_bytes(sample_path("t_books.tbl").read_bytes())
            document = service.load(target, engine="legacy")
            changed_on_disk = target.read_bytes() + b"external-change"
            target.write_bytes(changed_on_disk)
            with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                service.save(document, output_path=target)
            self.assertEqual(target.read_bytes(), changed_on_disk)

    def test_failed_legacy_staging_never_creates_the_target(self) -> None:
        service = RetextService()
        document = service.load(sample_path("ai_chr0100.dat"), engine="legacy")
        document.units[0].current_text += "X"
        mismatched = TextDocument(Path("verify.dat"), DocumentKind.DAT, "legacy", [])
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "output.dat"
            with patch.object(service.legacy, "load", return_value=mismatched):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "(structured script|roundtrip) verification",
                ):
                    service.save(
                        document,
                        output_path=target,
                        allow_unsafe_repack=True,
                    )
            self.assertFalse(target.exists())

    def test_legacy_dat_growth_uses_structural_pool_relocation(self) -> None:
        service = RetextService()
        document = service.load(sample_path("ai_chr0100.dat"), engine="legacy")
        document.units[0].current_text += "X"

        plan = service.preview_save(document)
        self.assertEqual(plan.mode, "repack")
        self.assertTrue(plan.safe)
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "output.dat"
            expected = [unit.current_text for unit in document.units]
            service.save(document, output_path=target)
            verified = service.load(target, engine="legacy")
            self.assertEqual([unit.current_text for unit in verified.units], expected)

    def test_legacy_save_rejects_reordered_units(self) -> None:
        service = RetextService()
        document = service.load(sample_path("ai_chr0100.dat"), engine="legacy")
        document.units.reverse()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "order or offsets"):
                service.save(document, output_path=Path(temp_dir) / "output.dat")


class KuroRegressionTests(unittest.TestCase):
    def test_manual_game_is_only_a_fallback_for_proven_mixed_tbl_layouts(self) -> None:
        engine = KuroTblEngine()

        resolved, detection = engine._select_game_for_layout(
            GameVersion.SORA2,
            GameVersion.SORA1.value,
            "layout",
        )

        self.assertEqual(resolved, GameVersion.SORA1.value)
        self.assertEqual(detection, "layout-override-Sora2")
        self.assertEqual(
            engine._select_game_for_layout(
                GameVersion.SORA2,
                None,
                "fallback",
            ),
            (GameVersion.SORA2.value, "manual-fallback"),
        )

    def test_manual_sora2_keeps_complete_sora1_layout_tbl_inventory(self) -> None:
        source = sample_path("t_books.tbl")
        service = RetextService()
        automatic = service.load(
            source,
            engine="kuro_tbl",
            game=GameVersion.AUTO.value,
            schema_hint="t_books",
        )
        manual_sora2 = service.load(
            source,
            engine="kuro_tbl",
            game=GameVersion.SORA2.value,
            schema_hint="t_books",
        )

        self.assertEqual(
            [unit.current_text for unit in manual_sora2.units],
            [unit.current_text for unit in automatic.units],
        )
        self.assertEqual(
            manual_sora2.metadata["resolved_game"],
            GameVersion.SORA1.value,
        )

    def test_tbl_exposes_unreferenced_physical_pool_text_in_both_engines(self) -> None:
        hidden = "隐藏阵".encode("utf-8")
        raw = bytearray(112 + len(hidden))
        raw[:4] = b"#TBL"
        raw[4:8] = (1).to_bytes(4, "little")
        raw[8:23] = b"UnknownPhysical"
        raw[76:80] = (88).to_bytes(4, "little")
        raw[80:84] = (8).to_bytes(4, "little")
        raw[84:88] = (1).to_bytes(4, "little")
        raw[96:99] = b"\x01\x02\0"
        raw[100 : 100 + len(hidden) + 1] = hidden + b"\0"

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "unknown.tbl"
            source.write_bytes(raw)
            for engine in ("kuro_tbl", "legacy"):
                document = RetextService().load(
                    source,
                    engine=engine,
                    schema_hint="unknown",
                    game="Sora2",
                )
                matches = [unit for unit in document.units if "阵" in unit.current_text]
                self.assertEqual(
                    [(unit.current_text, unit.metadata.get("text_offset", unit.metadata.get("offset"))) for unit in matches],
                    [("隐藏阵", 100)],
                )

    def test_unaligned_ambiguous_dat_address_is_never_rewritten(self) -> None:
        reference = divergent_multi_pointer_dat()
        damaged = divergent_multi_pointer_dat(shift=6, wrong_pointer=True)
        diagnostics: dict[str, object] = {}

        with patch(
            "retext.engines.relocation._align_dat_reference_fields",
            return_value={},
        ):
            repaired, count = repair_dat_references_from_reference(
                reference,
                damaged,
                strategy="aggressive",
                diagnostics=diagnostics,
            )
            repeated, repeated_count = repair_dat_references_from_reference(
                reference,
                repaired,
                strategy="aggressive",
            )

        self.assertEqual(count, 0)
        self.assertEqual(repeated_count, 0)
        self.assertEqual(repaired, damaged)
        self.assertEqual(repeated, repaired)
        self.assertGreater(diagnostics["ambiguous_pointer_count"], 0)
        self.assertEqual(
            diagnostics["skipped_uncertain_pointer_count"],
            diagnostics["ambiguous_pointer_count"],
        )

    def test_aggressive_dat_reference_repair_is_explicit_and_counted(self) -> None:
        reference = divergent_multi_pointer_dat()
        damaged = divergent_multi_pointer_dat(shift=1, wrong_pointer=True)
        conservative_diagnostics: dict[str, object] = {}
        aggressive_diagnostics: dict[str, object] = {}

        conservative, conservative_count = repair_dat_references_from_reference(
            reference,
            damaged,
            strategy="conservative",
            diagnostics=conservative_diagnostics,
        )
        aggressive, aggressive_count = repair_dat_references_from_reference(
            reference,
            damaged,
            strategy="aggressive",
            diagnostics=aggressive_diagnostics,
        )

        self.assertEqual(conservative_count, 0)
        self.assertEqual(conservative, damaged)
        self.assertEqual(
            conservative_diagnostics["skipped_uncertain_pointer_count"],
            1,
        )
        self.assertEqual(aggressive_count, 1)
        self.assertNotEqual(aggressive, damaged)
        self.assertEqual(aggressive_diagnostics["aggressive_pointer_count"], 1)
        self.assertEqual(aggressive_diagnostics["comparison_mode"], "aggressive")
        parse_dat_references(aggressive)

    def test_kuro_and_legacy_parse_all_text_despite_invalid_dat_pointer(self) -> None:
        payload = bytearray(minimal_exact_dat())
        text_target = int.from_bytes(payload[58:62], "little") & 0x3FFFFFFF
        payload[58:62] = (0xC0000000 | (text_target + 1)).to_bytes(4, "little")
        payload.extend("隐藏文本".encode("utf-8") + b"\0")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "damaged.dat"
            source.write_bytes(payload)
            kuro = RetextService().load(source, engine="kuro_dat")
            legacy = RetextService().load(source, engine="legacy")

        self.assertEqual(kuro.metadata["invalid_pointer_count"], 1)
        self.assertEqual(legacy.metadata["invalid_pointer_count"], 1)
        for document in (kuro, legacy):
            self.assertIn("阵", [unit.current_text for unit in document.units])
            self.assertIn("隐藏文本", [unit.current_text for unit in document.units])

    def test_kuro_and_legacy_edit_damaged_dat_without_dropping_pool_text(self) -> None:
        payload = bytearray(minimal_exact_dat())
        text_target = int.from_bytes(payload[58:62], "little") & 0x3FFFFFFF
        payload[58:62] = (0xC0000000 | (text_target + 1)).to_bytes(4, "little")
        payload.extend("隐藏文本".encode("utf-8") + b"\0")

        for engine in ("kuro_dat", "legacy"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temp_dir:
                source = Path(temp_dir) / "damaged.dat"
                output = Path(temp_dir) / "edited.dat"
                source.write_bytes(payload)
                service = RetextService()
                document = service.load(source, engine=engine)
                edited = next(unit for unit in document.units if unit.current_text == "阵")
                edited.current_text = "阵型扩展"

                service.save(document, output_path=output)
                reopened = service.load(output, engine=engine)

                self.assertIn("阵型扩展", [unit.current_text for unit in reopened.units])
                self.assertIn("隐藏文本", [unit.current_text for unit in reopened.units])
                self.assertEqual(reopened.metadata["invalid_pointer_count"], 1)

    def test_exact_dat_load_does_not_depend_on_disassembler(self) -> None:
        engine = KuroDatEngine()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "exact.dat"
            source.write_bytes(minimal_exact_dat())
            with patch.object(
                engine,
                "_disassemble_snapshot",
                side_effect=AssertionError("exact DAT must not be disassembled"),
            ):
                document = engine.load(source)

        self.assertTrue(document.metadata["binary_layout"])
        self.assertEqual(
            [unit.current_text for unit in document.units],
            ["func", "阵"],
        )

    def test_exact_dat_variable_edit_preserves_non_text_bytes(self) -> None:
        engine = KuroDatEngine()
        original = minimal_exact_dat()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "exact.dat"
            source.write_bytes(original)
            document = engine.load(source)
            document.units[1].current_text = "阵型扩展"
            with patch.object(
                engine,
                "_disassemble_snapshot",
                side_effect=AssertionError("exact DAT save must not use script roundtrip"),
            ):
                engine.save(document)

            candidate = source.read_bytes()
            engine.verify_exact_binary_compatibility(
                source.name,
                original,
                candidate,
            )
            reopened = engine.load(source)

        self.assertEqual(reopened.units[1].current_text, "阵型扩展")

    def test_exact_dat_verifier_rejects_non_text_byte_change(self) -> None:
        engine = KuroDatEngine()
        original = minimal_exact_dat()
        candidate = bytearray(original)
        candidate[48] = 1

        with self.assertRaisesRegex(RuntimeError, "non-text byte"):
            engine.verify_exact_binary_compatibility(
                "exact.dat",
                original,
                bytes(candidate),
            )

    def test_dat_structure_fingerprint_ignores_only_editable_text(self) -> None:
        original = """from disasm.ED9Assembler import *

def script():
    add_function(name="old", input_args=[], output_args=[], b0=0, b1=0)
    add_struct(id=1, nb_sth1=0, array2=["old", INT(3)])
    PUSHSTRING("old")
    PUSHINTEGER(7)

script()
"""
        edited_text = original.replace('"old"', '"new text"')
        changed_instruction = edited_text.replace("PUSHINTEGER(7)", "PUSHINTEGER(8)")

        self.assertEqual(
            _script_structure_fingerprint(original),
            _script_structure_fingerprint(edited_text),
        )
        self.assertNotEqual(
            _script_structure_fingerprint(original),
            _script_structure_fingerprint(changed_instruction),
        )

    def test_dat_payload_verification_rejects_non_text_structure_change(self) -> None:
        expected_source = """from disasm.ED9Assembler import *

def script():
    PUSHSTRING("same text")
    PUSHINTEGER(7)

script()
"""
        rebuilt_source = expected_source.replace("PUSHINTEGER(7)", "PUSHINTEGER(8)")
        verified = TextDocument(
            Path("verified.dat"),
            DocumentKind.DAT,
            "kuro_dat",
            [TextUnit(0, "same text", "same text", "PUSHSTRING[0]")],
            state=SimpleNamespace(script_source=rebuilt_source),
        )
        engine = KuroDatEngine()

        with tempfile.TemporaryDirectory() as temp_dir:
            verify_dir = Path(temp_dir) / "verify"
            verify_dir.mkdir()
            with (
                patch(
                    "retext.engines.kuro.dat.create_runtime_dir",
                    return_value=verify_dir,
                ),
                patch("retext.engines.kuro.dat.cleanup_runtime_dir"),
                patch.object(engine, "load", return_value=verified),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-text script structure"):
                    engine._verify_payload(
                        "candidate.dat",
                        b"#scp-candidate",
                        ["same text"],
                        expected_source,
                    )

    def test_tbl_pool_repack_rewrites_only_concrete_offset_fields(self) -> None:
        raw = bytearray(128)
        raw[:4] = b"#TBL"
        raw[8:16] = (72).to_bytes(8, "little")  # Ordinary numeric field.
        raw[16:24] = (64).to_bytes(8, "little")
        raw[24:32] = (80).to_bytes(8, "little")
        raw[64:69] = b"AAAA\0"
        raw[80:85] = b"BBBB\0"
        header = HeaderState(
            name="Synthetic",
            length=32,
            count=1,
            start=8,
            schema_game="Sora1",
            schema_content={"schema": {}},
            schema_match="exact",
            data_rows=[{}],
            text_storage={},
            external_data_ranges=[],
            external_offset_fields={16: 64, 24: 80},
        )
        state = KuroTblState(
            original_magic=b"#TBL",
            original_bytes=bytes(raw),
            filename_stem="synthetic",
            headers=[header],
            fallback_pool_texts=[],
            trailing_dump=None,
            requested_game="Sora1",
            resolved_game="Sora1",
            game_detection="manual",
            layout_game="Sora1",
        )
        document = TextDocument(
            Path("synthetic.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [
                TextUnit(
                    0,
                    "AAAA",
                    "AAAAAA",
                    "Synthetic[0].first",
                    metadata={
                        "text_offset": 64,
                        "text_encoding": "utf-8",
                        "text_byte_length": 4,
                    },
                ),
                TextUnit(
                    1,
                    "BBBB",
                    "BBBB",
                    "Synthetic[0].second",
                    metadata={
                        "text_offset": 80,
                        "text_encoding": "utf-8",
                        "text_byte_length": 4,
                    },
                ),
            ],
            state=state,
        )

        rebuilt = KuroTblEngine()._build_pool_splice_payload(document)

        self.assertEqual(int.from_bytes(rebuilt[8:16], "little"), 72)
        self.assertEqual(int.from_bytes(rebuilt[16:24], "little"), 64)
        self.assertEqual(int.from_bytes(rebuilt[24:32], "little"), 82)
        self.assertEqual(rebuilt[64:71], b"AAAAAA\0")

    def test_cle_blowfish_compatibility_vector(self) -> None:
        payload = bytes(range(1, 42))
        wrapped = b"F9BA" + len(payload).to_bytes(4, "little") + payload
        self.assertEqual(
            _build_processcle_module().processCLE(wrapped).hex(),
            "9037d3718ea5fad8301071f4385f2b243dd22d9d5e6c99f78dd179f4a54e47bd5597aac167f9203e45",
        )

    def test_cle_rejects_encrypted_payload_before_decrypting_over_limit(self) -> None:
        from retext.engines.kuro import support

        with patch.object(support, "MAX_CLE_OUTPUT_BYTES", 4):
            process_cle = support._build_processcle_module().processCLE
            with self.assertRaisesRegex(ValueError, "Encrypted CLE payload"):
                process_cle(b"F9BA" + (5).to_bytes(4, "little") + b"12345")

    def test_kuro_imports_ignore_conflicting_top_level_modules(self) -> None:
        fake_lib = SimpleNamespace(parser=SimpleNamespace(marker="wrong"))
        with patch.dict(sys.modules, {"lib": fake_lib}):
            parser = import_kuro_module("lib.parser")
        self.assertEqual(
            parser.__name__,
            "retext.engines.kuro.lib.parser",
        )

    def test_dat_load_disassembles_the_retained_byte_snapshot(self) -> None:
        engine = KuroDatEngine()
        original = b"#scp-original"
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "snapshot.dat"
            source.write_bytes(original)

            def disassemble(filename, source_bytes, **_kwargs):
                self.assertEqual(filename, source.name)
                self.assertEqual(source_bytes, original)
                source.write_bytes(b"#scp-external-change")
                return "from disasm.ED9Assembler import *\n\ndef script():\n    PUSHSTRING('text')\n\nscript()\n"

            with patch.object(engine, "_disassemble_snapshot", side_effect=disassemble):
                document = engine.load(source)

            self.assertEqual(document.state.original_bytes, original)
            self.assertEqual(source.read_bytes(), b"#scp-external-change")

    def test_unterminated_text_raises_eof(self) -> None:
        parser = import_kuro_module("lib.parser")
        with self.assertRaises(EOFError):
            parser.readtext(io.BytesIO(b"not-terminated"))

    def test_text_and_array_parser_safety_bounds(self) -> None:
        parser = import_kuro_module("lib.parser")
        with patch.object(parser, "MAX_TEXT_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                parser.readtext(io.BytesIO(b"123456789\0"))

        oversized_count = (
            (0).to_bytes(8, "little")
            + (parser.MAX_ARRAY_ELEMENTS + 1).to_bytes(4, "little")
        )
        with self.assertRaisesRegex(ValueError, "element count"):
            parser.process_data(io.BytesIO(oversized_count), "u32array", 12)

        outside = (100).to_bytes(8, "little") + (1).to_bytes(4, "little")
        with self.assertRaisesRegex(ValueError, "outside"):
            parser.process_data(io.BytesIO(outside), "u32array", 12)

        empty_sentinel = ((1 << 64) - 1).to_bytes(8, "little") + (0).to_bytes(
            4,
            "little",
        )
        self.assertEqual(
            parser.process_data(io.BytesIO(empty_sentinel), "u32array", 12),
            ([], 12),
        )

        stream = io.BytesIO(b"abc")
        stream.seek(1)
        with self.assertRaises(EOFError):
            parser.readintoffset(stream, 2, 4)
        self.assertEqual(stream.tell(), 1)

    def test_dat_save_as_can_be_edited_and_saved_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = DocumentSession()
            session.open_document(
                sample_path("ai_chr0100.dat"),
                options=SessionOptions(
                    engine_override="kuro_dat",
                ),
            )
            target = session.save_as(Path(temp_dir) / "renamed.dat")
            session.update_unit(0, session.document.units[0].current_text + "X")
            session.save()

            self.assertTrue(target.exists())
            self.assertTrue(Path(f"{target}.bak").exists())
            self.assertEqual(session.changed_count(), 0)

    def test_dat_assembler_rejects_non_whitelisted_calls(self) -> None:
        engine = KuroDatEngine()
        assembler = import_kuro_module("disasm.ED9Assembler")
        source = """from disasm.ED9Assembler import *

def script():
    __import__('os')

script()
"""
        with self.assertRaisesRegex(ValueError, "not allowed"):
            engine._execute_assembler_script(source, assembler, "unsafe.py")

    def test_wrapped_tbl_preserves_wrapper_after_variable_length_edit(self) -> None:
        raw = sample_path("t_books.tbl").read_bytes()
        compressed = zstandard.ZstdCompressor().compress(raw)
        wrapped = b"D9BA" + len(compressed).to_bytes(4, "little") + compressed
        service = RetextService()

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "wrapped.tbl"
            output = Path(temp_dir) / "copy.tbl"
            source.write_bytes(wrapped)
            document = service.load(source, engine="kuro_tbl", schema_hint="t_books")
            service.save(document, output_path=output)
            self.assertEqual(output.read_bytes(), wrapped)

            document.units[0].current_text += "X"
            expected = [unit.current_text for unit in document.units]
            changed = Path(temp_dir) / "changed.tbl"
            service.save(document, output_path=changed)
            inner, layers = unwrapCLE(changed.read_bytes())
            self.assertEqual(layers, (b"D9BA",))
            self.assertTrue(inner.startswith(b"#TBL"))
            verified = service.load(changed, engine="kuro_tbl", schema_hint="t_books")
            self.assertEqual([unit.current_text for unit in verified.units], expected)

    def test_tbl_numeric_collision_is_not_relocated_as_a_pointer(self) -> None:
        first_text = "任务A".encode()
        second_text = "调查“震源地”".encode()
        first_target = 120
        second_target = first_target + len(first_text) + 1
        raw = bytearray(second_target + len(second_text) + 1)
        raw[:4] = b"#TBL"
        raw[4:8] = (1).to_bytes(4, "little")
        raw[8:17] = b"QuestTitle"
        raw[76:80] = (88).to_bytes(4, "little")
        raw[80:84] = (16).to_bytes(4, "little")
        raw[84:88] = (2).to_bytes(4, "little")
        for row, target in enumerate((first_target, second_target)):
            start = 88 + row * 16
            raw[start : start + 4] = target.to_bytes(4, "little")
            raw[start + 8 : start + 16] = target.to_bytes(8, "little")
        raw[first_target : first_target + len(first_text) + 1] = first_text + b"\0"
        raw[second_target : second_target + len(second_text) + 1] = second_text + b"\0"
        known = {96: first_target, 112: second_target}

        layout = discover_tbl_references(
            bytes(raw),
            known_fields=known,
            infer_record_ranges=[],
        )
        rebuilt, _mapping = splice_referenced_strings(
            bytes(raw),
            [(first_target, first_text, first_text + b"-extended")],
            [
                StringReference(position, target, 8)
                for position, target in layout.offset_fields.items()
            ],
            immutable_prefix_end=120,
        )

        self.assertEqual(int.from_bytes(rebuilt[88:92], "little"), first_target)
        self.assertEqual(int.from_bytes(rebuilt[104:108], "little"), second_target)
        self.assertEqual(
            int.from_bytes(rebuilt[112:120], "little"),
            second_target + len(b"-extended"),
        )

    def test_string_suffix_alias_keeps_its_internal_boundary(self) -> None:
        raw = bytearray(32)
        parent = b"</GPT"
        raw[0:8] = (16).to_bytes(8, "little")
        raw[8:16] = (18).to_bytes(8, "little")
        raw[16 : 16 + len(parent) + 1] = parent + b"\0"

        rebuilt, _mapping = splice_referenced_strings(
            bytes(raw),
            [(16, parent, parent + b"X")],
            [StringReference(0, 16, 8), StringReference(8, 18, 8)],
            immutable_prefix_end=16,
        )

        self.assertEqual(int.from_bytes(rebuilt[0:8], "little"), 16)
        self.assertEqual(int.from_bytes(rebuilt[8:16], "little"), 18)
        self.assertEqual(rebuilt[16:23], b"</GPTX\0")


class SessionRegressionTests(unittest.TestCase):
    def test_default_engine_preferences_match_the_global_ui_defaults(self) -> None:
        options = SessionOptions()
        self.assertEqual(options.engine_for_path("table/a.tbl"), "kuro_tbl")
        self.assertEqual(options.engine_for_path("script/a.dat"), "legacy")

    def test_tbl_and_dat_engine_preferences_are_independent(self) -> None:
        options = SessionOptions(tbl_engine="legacy", dat_engine="kuro_dat")
        self.assertEqual(options.engine_for_path("table/a.tbl"), "legacy")
        self.assertEqual(options.engine_for_path("script/a.dat"), "kuro_dat")

    def test_case_insensitive_replace_handles_unicode_length_changes(self) -> None:
        session = DocumentSession()
        session.document = TextDocument(
            Path("unicode.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [TextUnit(0, "İX", "İX", "row.text")],
        )

        changed = session.replace_all("x", "Y", case_sensitive=False)

        self.assertEqual(changed, 1)
        self.assertEqual(session.document.units[0].current_text, "İY")


class BusinessRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.business = WorkspaceBusiness()

    def test_service_batch_replacements_do_not_cascade(self) -> None:
        document = TextDocument(
            source_path=Path("case.tbl"),
            kind=DocumentKind.TBL,
            engine="kuro_tbl",
            units=[TextUnit(0, "ab", "ab", "row.text")],
        )
        hits = [
            ServiceBatchHit(True, "case.tbl", "TBL", 0, "row.text", "ab", "bb", "a", "b"),
            ServiceBatchHit(True, "case.tbl", "TBL", 0, "row.text", "ab", "ac", "b", "c"),
        ]
        self.business._apply_service_hits(document, hits)
        self.assertEqual(document.units[0].current_text, "bc")

    def test_preflight_does_not_authorize_unsupported_kuro_tbl_repack(self) -> None:
        document = TextDocument(
            Path("mixed.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [TextUnit(0, "old", "old", "row.text")],
        )
        hit = ServiceBatchHit(
            True,
            "mixed.tbl",
            "TBL",
            0,
            "row.text",
            "old",
            "new",
            "old",
            "new",
        )
        self.business.service = Mock()
        self.business.service.preview_save.return_value = SavePlan(
            engine="kuro_tbl",
            mode="repack",
            safe=False,
            requires_rebuild=True,
            notes=["unknown header"],
        )

        self.business._preflight_service_hits(
            document,
            [hit],
            "table_sc/mixed.tbl",
            options=SessionOptions(allow_risky_repack=True),
        )

        self.assertFalse(hit.writable)
        self.assertFalse(hit.checked)
        self.assertEqual(hit.write_mode, "repack")

    def test_entry_diff_aligns_insertions(self) -> None:
        old = TextDocument(
            Path("old.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [TextUnit(0, "A", "A", "0"), TextUnit(1, "B", "B", "1")],
        )
        new = TextDocument(
            Path("new.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [
                TextUnit(0, "A", "A", "0"),
                TextUnit(1, "X", "X", "1"),
                TextUnit(2, "B", "B", "2"),
            ],
        )
        self.business.service = Mock()
        self.business.service.load.side_effect = [old, new]
        rows = self.business.compute_entry_diff_auto("old.tbl", "new.tbl")
        self.assertEqual([row.status for row in rows], ["same", "added", "same"])

    def test_target_diff_uses_pac_logical_paths_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = root / "000001.tbl"
            new = root / "000002.tbl"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            old_target = BusinessFileTarget(
                str(old),
                "table_sc/items.tbl",
                "pac-old",
            )
            new_target = BusinessFileTarget(
                str(new),
                "table_sc/items.tbl",
                "pac-new",
            )
            rows = self.business.build_target_diff_index(
                [old_target],
                [new_target],
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].rel, "table_sc/items.tbl")
            self.assertEqual(rows[0].status, "modified")

            duplicate = BusinessFileTarget(
                str(new),
                "table_sc/items.tbl",
                "another-pac",
            )
            with self.assertRaisesRegex(ValueError, "重复 PAC 内路径"):
                self.business.build_target_diff_index(
                    [old_target, duplicate],
                    [new_target],
                )

    def test_collect_file_targets_supports_files_and_directory_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            first = root / "one.tbl"
            second = nested / "two.dat"
            ignored = nested / "note.txt"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            ignored.write_bytes(b"ignored")

            targets = self.business.collect_file_targets(
                [str(root), str(first)],
                "*.tbl,*.dat",
            )

            self.assertEqual(
                [(target.logical_path, Path(target.path).name) for target in targets],
                [("nested/two.dat", "two.dat"), ("one.tbl", "one.tbl")],
            )

    def test_batch_file_collection_never_expands_beyond_tbl_and_dat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "safe.tbl").write_bytes(b"table")
            (root / "safe.dat").write_bytes(b"script")
            (root / "image.png").write_bytes(b"not editable")

            targets = self.business.collect_file_targets([temp_dir], "*.*")

            self.assertEqual(
                {Path(target.path).suffix for target in targets},
                {".tbl", ".dat"},
            )

    def test_equal_batch_execution_rejects_non_text_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "image.png"
            original = b"prefix OLD suffix"
            target.write_bytes(original)
            hit = SimpleNamespace(
                checked=True,
                file=str(target),
                offset=7,
                _patch_old="OLD",
                _patch_new="NEW",
            )

            ok, fail, logs = self.business.execute_equal_batch(
                [hit],
                [("OLD", "NEW")],
            )

            self.assertEqual((ok, fail), (0, 1))
            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(any("任意字节" in line for line in logs))

    def test_mixed_batch_rejects_non_text_targets_before_scanning(self) -> None:
        target = BusinessFileTarget(
            "000001.png",
            "asset/image/title.png",
            "pac-a",
        )
        self.business.scan_service_batch_targets = Mock(return_value=[])

        result = self.business.scan_mixed_batch_targets(
            [target],
            "*.*",
            [("OLD", "NEW")],
            use_equal_fast_path=True,
        )

        self.assertEqual(result.hits, [])
        self.business.scan_service_batch_targets.assert_called_once_with([], [("OLD", "NEW")])
        self.assertTrue(any("仅支持 TBL/DAT" in line for line in result.errors))

    def test_mixed_batch_uses_only_the_structural_path(self) -> None:
        targets = [
            BusinessFileTarget("cache-a.tbl", "table_sc/a.tbl", "pac-a"),
            BusinessFileTarget("cache-b.tbl", "table_sc/b.tbl", "pac-a"),
        ]
        structural = ServiceBatchHit(
            True,
            "cache-a.tbl",
            "TBL",
            0,
            "row.text",
            "wide",
            "longer",
            "wide",
            "longer",
            "table_sc/a.tbl",
            "pac-a",
        )
        self.business.scan_service_batch_targets = Mock(return_value=[structural])
        self.business.scan_batch_hits = Mock(
            side_effect=AssertionError("raw scanner must not be called")
        )

        result = self.business.scan_mixed_batch_targets(
            targets,
            "*.tbl",
            [("wide", "longer"), ("aa", "bb")],
            use_equal_fast_path=True,
        )

        self.assertEqual(result.hits, [structural])
        self.assertEqual(result.fast_hit_count, 0)
        self.business.scan_service_batch_targets.assert_called_once_with(
            targets,
            [("wide", "longer"), ("aa", "bb")],
        )

    def test_empty_batch_scope_is_incomplete_instead_of_valid_zero_hits(self) -> None:
        scan = self.business.scan_mixed_batch_targets(
            [],
            "*.tbl,*.dat",
            [("约书亚", "约修亚")],
            use_equal_fast_path=False,
        )

        self.assertFalse(scan.complete)
        self.assertEqual((scan.target_count, scan.parsed_file_count), (0, 0))
        self.assertEqual(scan.hits, [])
        self.assertTrue(any("没有展开出任何" in line for line in scan.errors))

    def test_equal_mapping_is_a_read_only_search_and_location_query(self) -> None:
        target = BusinessFileTarget(
            "cache.tbl",
            "table_sc/test.tbl",
            "pac-a",
        )
        document = TextDocument(
            Path("cache.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [TextUnit(0, "约书亚与约书亚", "约书亚与约书亚", "row.text")],
        )
        self.business._load_service_document = Mock(return_value=document)

        hits = self.business.scan_service_batch_targets(
            [target],
            [("约书亚", "约书亚")],
        )

        self.assertEqual(len(hits), 2)
        self.assertTrue(self.business.last_scan_complete)
        self.assertTrue(all(not hit.checked and not hit.writable for hit in hits))
        self.assertTrue(all(hit.write_mode == "search-only" for hit in hits))
        self.assertEqual([hit.match_start for hit in hits], [0, 4])

    def test_full_match_is_scoped_to_each_mapping_pair(self) -> None:
        target = BusinessFileTarget(
            "cache.tbl",
            "table_sc/test.tbl",
            "pac-a",
        )
        document = TextDocument(
            Path("cache.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [
                TextUnit(0, "雷光", "雷光", "row.0"),
                TextUnit(1, "雷光斩", "雷光斩", "row.1"),
                TextUnit(2, "前文\n雷光\n后文", "前文\n雷光\n后文", "row.2"),
                TextUnit(3, "风花", "风花", "row.3"),
                TextUnit(4, "方术·风花", "方术·风花", "row.4"),
            ],
        )
        self.business._load_service_document = Mock(return_value=document)

        hits = self.business.scan_service_batch_targets(
            [target],
            [
                BatchMapping("雷光", "审判雷光", full_match=True),
                BatchMapping("风花", "春风", full_match=False),
            ],
            preflight=False,
        )

        lightning = [hit for hit in hits if hit.pair_old == "雷光"]
        flowers = [hit for hit in hits if hit.pair_old == "风花"]
        self.assertEqual([(hit.unit_index, hit.match_start) for hit in lightning], [(0, 0)])
        self.assertEqual(
            [(hit.unit_index, hit.match_start) for hit in flowers],
            [(3, 0), (4, 3)],
        )
        self.assertTrue(lightning[0].full_match)
        self.assertTrue(all(not hit.full_match for hit in flowers))

    def test_legacy_and_three_value_mapping_tuples_remain_supported(self) -> None:
        self.assertEqual(
            normalize_batch_mappings(
                [("包含", "替换"), ("完整", "替换", True)]
            ),
            [
                BatchMapping("包含", "替换", False),
                BatchMapping("完整", "替换", True),
            ],
        )

    def test_batch_behavior_keeps_full_match_identity(self) -> None:
        exact = ServiceBatchHit(
            checked=False,
            file="cache.tbl",
            kind="TBL",
            unit_index=0,
            location="row.text",
            original_text="雷光",
            new_text="审判雷光",
            pair_old="雷光",
            pair_new="审判雷光",
            logical_file="table_sc/test.tbl",
            source_id="pac-a",
            match_start=0,
            match_end=2,
            full_match=True,
        )
        operations = load_batch_behavior(serialize_batch_behavior([exact]))
        same = ServiceBatchHit(
            checked=True,
            file=exact.file,
            kind=exact.kind,
            unit_index=exact.unit_index,
            location=exact.location,
            original_text=exact.original_text,
            new_text=exact.new_text,
            pair_old=exact.pair_old,
            pair_new=exact.pair_new,
            logical_file=exact.logical_file,
            source_id=exact.source_id,
            match_start=exact.match_start,
            match_end=exact.match_end,
            full_match=True,
        )
        contains = ServiceBatchHit(
            checked=True,
            file=exact.file,
            kind=exact.kind,
            unit_index=exact.unit_index,
            location=exact.location,
            original_text=exact.original_text,
            new_text=exact.new_text,
            pair_old=exact.pair_old,
            pair_new=exact.pair_new,
            logical_file=exact.logical_file,
            source_id=exact.source_id,
            match_start=exact.match_start,
            match_end=exact.match_end,
            full_match=False,
        )

        self.assertEqual(apply_batch_behavior([same], operations), 1)
        self.assertFalse(same.checked)
        self.assertEqual(apply_batch_behavior([contains], operations), 0)
        self.assertTrue(contains.checked)

    def test_read_only_scan_can_defer_preflight_until_selection_is_frozen(self) -> None:
        target = BusinessFileTarget(
            "cache.dat",
            "script_sc/scena/test.dat",
            "pac-a",
        )
        document = TextDocument(
            Path("cache.dat"),
            DocumentKind.DAT,
            "legacy",
            [TextUnit(0, "阵先生", "阵先生", "0x20")],
        )
        self.business._load_service_document = Mock(return_value=document)
        self.business._preflight_service_hits = Mock()

        hits = self.business.scan_service_batch_targets(
            [target],
            [("阵先生", "金先生"), ("阵", "金")],
            preflight=False,
        )

        self.assertEqual(len(hits), 2)
        self.business._preflight_service_hits.assert_not_called()
        hits[1].checked = False
        self.business.preflight_service_hits([hits[0]])
        self.business._preflight_service_hits.assert_called_once()

    def test_unified_batch_stages_verifies_and_backs_up_tbl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "t_books.tbl"
            original = sample_path("t_books.tbl").read_bytes()
            target.write_bytes(original)
            document = self.business.service.load(target, engine="kuro_tbl", schema_hint="t_books")
            old_text = document.units[0].current_text
            hits = self.business.scan_service_batch_hits(
                [temp_dir],
                "*.tbl",
                [(old_text, old_text + "X")],
            )
            self.assertTrue(hits)
            ok, fail, _ = self.business.execute_service_batch([hits[0]])
            self.assertEqual((ok, fail), (1, 0))
            self.assertEqual(Path(f"{target}.bak").read_bytes(), original)
            reopened = self.business.service.load(target, engine="kuro_tbl", schema_hint="t_books")
            self.assertEqual(reopened.units[0].current_text, old_text + "X")

    def test_unified_scan_reports_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "broken.tbl").write_bytes(b"not-a-table")
            hits = self.business.scan_service_batch_hits([temp_dir], "*.tbl", [("a", "b")])
            self.assertEqual(hits, [])
            self.assertEqual(len(self.business.last_scan_errors), 1)

    def test_incomplete_scan_keeps_partial_hits_but_blocks_execution(self) -> None:
        good = TextDocument(
            Path("good.tbl"),
            DocumentKind.TBL,
            "kuro_tbl",
            [TextUnit(0, "阵", "阵", "row.text")],
        )
        targets = [
            BusinessFileTarget("good.tbl", "table/good.tbl", "pac-a"),
            BusinessFileTarget("broken.tbl", "table/broken.tbl", "pac-a"),
        ]

        def load(path, **_kwargs):
            if Path(path).name == "broken.tbl":
                raise ValueError("broken table")
            return good

        with (
            patch.object(self.business.service, "load", side_effect=load),
            patch.object(
                self.business.service,
                "preview_save",
                return_value=SavePlan("kuro_tbl", "pool-splice", True, True),
            ),
            patch.object(self.business, "execute_service_batch") as execute,
        ):
            scan = self.business.scan_mixed_batch_targets(
                targets,
                "*.tbl",
                [("阵", "阵型")],
                use_equal_fast_path=False,
            )
            ok, fail, logs = self.business.execute_mixed_batch(
                scan.hits,
                [("阵", "阵型")],
            )

        self.assertFalse(scan.complete)
        self.assertEqual((scan.target_count, scan.parsed_file_count), (2, 1))
        self.assertEqual(scan.text_unit_count, 1)
        self.assertEqual(len(scan.hits), 1)
        self.assertTrue(any("[INCOMPLETE]" in line for line in scan.errors))
        self.assertEqual((ok, fail), (0, 1))
        self.assertTrue(any("未完整解析" in line for line in logs))
        execute.assert_not_called()

    def test_atomic_copy_file_streams_and_replaces_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.bin"
            target = root / "target.bin"
            payload = (b"0123456789ABCDEF" * 131_072) + b"tail"
            source.write_bytes(payload)
            target.write_bytes(b"old")

            output = atomic_copy_file(source, target)

            self.assertEqual(output, target.resolve())
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(any(root.glob(".target.bin.*.tmp")))


if __name__ == "__main__":
    unittest.main()
