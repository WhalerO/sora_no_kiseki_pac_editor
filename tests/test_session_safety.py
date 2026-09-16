from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from retext.domain import DocumentKind, TextDocument, TextUnit
from retext.session import DocumentSession, SessionOptions
from tests.test_regressions import minimal_exact_dat


class SessionSafetyTests(unittest.TestCase):
    def test_verification_failure_does_not_publish_or_mutate_document(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.tbl"
            source.write_bytes(b"old")
            document = TextDocument(source, DocumentKind.TBL, "test",
                [TextUnit(0, "old", "new", "0")], state=SimpleNamespace(original_bytes=b"old"))
            def save(candidate, *, output_path, **kwargs):
                candidate.source_path = Path(output_path)
                candidate.units[0].original_text = "new"
                candidate.source_path.write_bytes(b"invalid")
                return candidate.source_path
            service = SimpleNamespace(save=save, load=lambda path, **kwargs:
                TextDocument(Path(path), DocumentKind.TBL, "test", [TextUnit(0, "wrong", "wrong", "0")]))
            session = DocumentSession(service)
            session.document = document
            with self.assertRaisesRegex(RuntimeError, "roundtrip"):
                session.save()
            self.assertIs(session.document, document)
            self.assertEqual(source.read_bytes(), b"old")
            self.assertEqual(document.units[0].original_text, "old")
            self.assertEqual(document.units[0].current_text, "new")
            self.assertFalse(Path(f"{source}.bak").exists())
            self.assertEqual(list(Path(folder).iterdir()), [source])

    def test_real_dat_repeated_save_as_and_stale_source_detection(self):
        for engine in ("legacy", "kuro_dat"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source.dat"
                source.write_bytes(minimal_exact_dat())
                session = DocumentSession()
                session.open_document(source, options=SessionOptions(engine_override=engine))
                unit = next(unit for unit in session.document.units if unit.current_text == "阵")
                session.update_unit(unit.index, "阵型测试")
                target = session.save_as(root / "renamed.dat")
                self.assertEqual(source.read_bytes(), minimal_exact_dat())
                session.update_unit(unit.index, "阵型第二次测试")
                session.save()
                self.assertEqual(session.document.get_unit(unit.index).current_text, "阵型第二次测试")
                self.assertEqual(session.changed_count(), 0)
                self.assertTrue(Path(f"{target}.bak").is_file())
                target.write_bytes(b"external update")
                session.update_unit(unit.index, "不得覆盖")
                with self.assertRaisesRegex(RuntimeError, "changed on disk"):
                    session.save()
                self.assertEqual(target.read_bytes(), b"external update")


if __name__ == "__main__":
    unittest.main()
