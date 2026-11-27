"""Bootstrap checks use tiny fixtures instead of rebuilding the real PDF corpus."""

from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from mechanic import prepare


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.root / "manual_search").mkdir()
        (self.root / "manual_search/corpus.py").write_text("# test parser", encoding="utf-8")
        self.manual = SimpleNamespace(slug="test", filename="test.pdf", pages=1)
        for name, value in {
            "ROOT": self.root,
            "MATERIALS": self.root / "artifacts/manuals",
            "MOCKS": self.root / "artifacts/customer",
            "CORPUS": self.root / "artifacts/manual-corpus.json",
            "PREVIEWS": self.root / "artifacts/manual-previews",
            "MANUALS": (self.manual,),
            "BY_SLUG": {self.manual.slug: self.manual},
            "PREVIEW_PAGES": {},
        }.items():
            self.stack.enter_context(patch.object(prepare, name, value))
        self.stack.enter_context(patch.dict("os.environ", {"CUSTOMER_ARCHIVE_PATH": ""}))
        self.stack.enter_context(patch.object(prepare.shutil, "which", return_value="/test/poppler"))
        self.build = self.stack.enter_context(patch.object(prepare, "build_corpus", return_value={
            "fragments": [{"fragment_id": "test-p001", "text": "Проверенная выдержка"}],
            "extraction_issues": [],
        }))

    def materialize_inputs(self):
        prepare.MATERIALS.mkdir(parents=True)
        (prepare.MATERIALS / self.manual.filename).write_bytes(b"%PDF-test")
        for filename in prepare.MOCK_FILES:
            destination = prepare.MOCKS / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("{}", encoding="utf-8")

    def test_missing_archive_explains_where_to_put_materials(self):
        with self.assertRaises(prepare.PreparationError) as raised:
            prepare.ensure_ready()
        message = str(raised.exception).casefold()
        self.assertIn("архив", message)
        self.assertIn("007_", message)
        self.build.assert_not_called()

    def test_damaged_zip_has_actionable_error(self):
        archive = self.root / "007_хакатон_ИИ.zip"
        archive.write_bytes(b"not a zip archive")
        with self.assertRaises(prepare.PreparationError) as raised:
            prepare.ensure_ready()
        self.assertIn("архив", str(raised.exception).casefold())
        self.build.assert_not_called()

    def test_missing_poppler_explains_dependency(self):
        self.materialize_inputs()
        with patch.object(prepare.shutil, "which", return_value=None):
            with self.assertRaises(prepare.PreparationError) as raised:
                prepare.ensure_ready()
        message = str(raised.exception).casefold()
        self.assertIn("poppler", message)
        self.assertIn("pdftotext", message)
        self.build.assert_not_called()

    def test_unchanged_inputs_reuse_index(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        first_index = prepare.CORPUS.read_bytes()
        prepare.ensure_ready()
        self.build.assert_called_once()
        self.assertEqual(prepare.CORPUS.read_bytes(), first_index)

    def test_force_rebuilds_existing_index(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        prepare.ensure_ready(force=True)
        self.assertEqual(self.build.call_count, 2)

    def test_same_materials_reuse_cache_at_a_different_mount_path(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        moved = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "mounted"
        shutil.copytree(self.root, moved)
        with ExitStack() as paths:
            for name in ("ROOT", "MATERIALS", "MOCKS", "CORPUS", "PREVIEWS"):
                current = getattr(prepare, name)
                paths.enter_context(patch.object(prepare, name, moved / current.relative_to(self.root)))
            prepare.ensure_ready()
        self.build.assert_called_once()

    def test_changed_manual_invalidates_cache(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        (prepare.MATERIALS / self.manual.filename).write_bytes(b"%PDF-updated")
        prepare.ensure_ready()
        self.assertEqual(self.build.call_count, 2)

    def test_damaged_index_is_rebuilt(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        prepare.CORPUS.write_text("interrupted write", encoding="utf-8")
        prepare.ensure_ready()
        self.assertEqual(self.build.call_count, 2)
        self.assertTrue(json.loads(prepare.CORPUS.read_text(encoding="utf-8"))["fragments"])

    def test_failed_rebuild_keeps_last_good_index(self):
        self.materialize_inputs()
        prepare.ensure_ready()
        previous = prepare.CORPUS.read_bytes()
        self.build.side_effect = ValueError("test.pdf: expected 1 pages, got 2")
        with self.assertRaises(prepare.PreparationError):
            prepare.ensure_ready(force=True)
        self.assertEqual(prepare.CORPUS.read_bytes(), previous)
        self.assertTrue(json.loads(previous)["fragments"])

    def test_archive_extracts_only_expected_materials(self):
        nested = BytesIO()
        with ZipFile(nested, "w") as archive:
            for filename in prepare.MOCK_FILES:
                archive.writestr("bundle/" + filename, "{}")
            archive.writestr("../unexpected.py", "raise RuntimeError('must not run')")
        archive_path = self.root / "007_хакатон_ИИ.zip"
        with ZipFile(archive_path, "w") as archive:
            archive.writestr("materials/" + prepare.CUSTOMER_ARCHIVE, nested.getvalue())
            archive.writestr("materials/" + self.manual.filename, b"%PDF-test")
            archive.writestr("../../unexpected.py", "raise RuntimeError('must not run')")
        prepare.ensure_ready()
        self.assertTrue((prepare.MATERIALS / self.manual.filename).is_file())
        self.assertTrue((prepare.MOCKS / "mock_ml/app.py").is_file())
        self.assertEqual(list(self.root.rglob("unexpected.py")), [])
        self.build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
