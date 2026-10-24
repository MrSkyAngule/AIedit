import os
import tempfile
import unittest
from pathlib import Path

from aiedit import checkpoints
from aiedit.checkpoints import FileBefore


class CheckpointsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        os.environ["AIEDIT_CONFIG_DIR"] = str(self.base / "cfg")
        self.root = self.base / "proj"
        self.root.mkdir()

    def tearDown(self):
        os.environ.pop("AIEDIT_CONFIG_DIR", None)
        self.tmp.cleanup()

    def test_save_and_load_newest_first(self):
        checkpoints.save(self.root, "первая  задача\nс переносом", [FileBefore("/p/a.py", "1", "2")])
        checkpoints.save(self.root, "вторая", [FileBefore("/p/b.py", None, "new"), FileBefore("/p/c.py", "x", None, "[]")])
        points = checkpoints.load(self.root)
        self.assertEqual([p.title for p in points], ["вторая", "первая задача с переносом"])
        self.assertEqual([(f.before, f.after, f.marks_before) for f in points[0].files],
                         [(None, "new", None), ("x", None, "[]")])
        self.assertIn("файлов: 2 (b.py, c.py)", points[0].label())

    def test_stored_outside_the_project_and_private(self):
        checkpoints.save(self.root, "t", [FileBefore(str(self.root / "a.py"), "secret code", "x")])
        self.assertEqual(list(self.root.iterdir()), [])
        d = checkpoints.folder(self.root)
        self.assertTrue(d.is_relative_to(self.base / "cfg"))
        self.assertEqual(os.stat(d.parent).st_mode & 0o777, 0o700)

    def test_projects_are_separate_and_old_points_pruned(self):
        other = self.base / "other"
        other.mkdir()
        for i in range(checkpoints.MAX_POINTS + 5):
            checkpoints.save(self.root, f"t{i}", [FileBefore("/p/a.py", "1", "2")])
        self.assertEqual(len(checkpoints.load(self.root)), checkpoints.MAX_POINTS)
        self.assertEqual(checkpoints.load(self.root)[0].title, f"t{checkpoints.MAX_POINTS + 4}")
        self.assertEqual(checkpoints.load(other), [])

    def test_forget_and_broken_files_are_skipped(self):
        p = checkpoints.save(self.root, "t", [FileBefore("/p/a.py", "1", "2")])
        (checkpoints.folder(self.root) / "broken.json").write_text("{")
        self.assertEqual(len(checkpoints.load(self.root)), 1)
        checkpoints.forget(self.root, p)
        self.assertEqual(checkpoints.load(self.root), [])

    def test_nothing_to_save(self):
        self.assertIsNone(checkpoints.save(self.root, "t", []))
        big = "x" * (checkpoints.MAX_FILE + 1)
        self.assertIsNone(checkpoints.save(self.root, "t", [FileBefore("/p/a.py", big, "y")]))


if __name__ == "__main__":
    unittest.main()
