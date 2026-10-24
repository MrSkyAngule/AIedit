import tempfile
import unittest
from pathlib import Path

from aiedit import fileops, policy
from aiedit.fileops import FileOpError
from aiedit.guard import Region
from aiedit.marks import Marks, sidecar
from aiedit.policy import Rules


class FileOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("x = 1\n")
        (self.root / "secrets" / "k.py").write_text("K = 1\n")
        self.rules = Rules(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_file_and_folder(self):
        f = fileops.create_file(self.root / "src", "b.py")
        d = fileops.create_folder(self.root, "docs")
        self.assertTrue(f.is_file() and d.is_dir())

    def test_bad_names_rejected(self):
        for name in ("", "  ", ".", "..", "a/b", "a\\b"):
            with self.assertRaises(FileOpError):
                fileops.create_file(self.root, name)
        with self.assertRaises(FileOpError):
            fileops.create_file(self.root / "src", "a.py")

    def test_rename_moves_zones_with_file(self):
        a = self.root / "src" / "a.py"
        m = Marks(); m.add("readonly", 0, 0); m.save(a, "x = 1\n")
        b = fileops.rename(a, "b.py", self.rules)
        self.assertTrue(b.exists() and not a.exists())
        self.assertFalse(sidecar(a).exists())
        m2 = Marks()
        self.assertTrue(m2.load(b, "x = 1\n"))
        self.assertEqual(m2.items, [Region("readonly", 0, 0)])

    def test_rename_keeps_file_denied(self):
        self.rules.set_denied(self.root / "src" / "a.py", True)
        b = fileops.rename(self.root / "src" / "a.py", "b.py", self.rules)
        self.assertTrue(policy.is_denied(b))
        self.assertEqual(Rules(self.root).denied, ["src/b.py"])

    def test_rename_denied_folder_keeps_everything_inside_denied(self):
        self.rules.set_denied(self.root / "secrets", True)
        v = fileops.rename(self.root / "secrets", "vault", self.rules)
        self.assertTrue(policy.is_denied(v / "k.py"))
        self.assertFalse(policy.is_denied(self.root / "src" / "a.py"))

    def test_rename_inside_denied_folder_stays_denied(self):
        self.rules.set_denied(self.root / "secrets", True)
        k = fileops.rename(self.root / "secrets" / "k.py", "key.py", self.rules)
        self.assertTrue(policy.is_denied(k))

    def test_rename_cannot_escape_denied_folder(self):
        self.rules.set_denied(self.root / "secrets", True)
        with self.assertRaises(FileOpError):
            fileops.rename(self.root / "secrets" / "k.py", "../src/k.py", self.rules)

    def test_rename_onto_existing_fails(self):
        (self.root / "src" / "b.py").write_text("")
        with self.assertRaises(FileOpError):
            fileops.rename(self.root / "src" / "a.py", "b.py", self.rules)

    def test_delete_removes_sidecar_but_keeps_deny_rule(self):
        env = self.root / ".env"; env.write_text("S=1")
        self.rules.set_denied(env, True)
        m = Marks(); m.add("hidden", 0, 0); m.save(env, "S=1")
        fileops.delete(env)
        self.assertFalse(env.exists() or sidecar(env).exists())
        env.write_text("S=2")  # создали заново
        self.assertTrue(policy.is_denied(env))

    def test_delete_folder_and_count(self):
        self.assertEqual(fileops.count_inside(self.root / "src"), 1)
        fileops.delete(self.root / "src")
        self.assertFalse((self.root / "src").exists())


if __name__ == "__main__":
    unittest.main()
