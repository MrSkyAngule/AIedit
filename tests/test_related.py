import tempfile
import unittest
from pathlib import Path

from aiedit import related
from aiedit.policy import Rules


class RelatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("pkg", "secrets", ".git", "node_modules"):
            (self.root / d).mkdir(parents=True)
        files = {
            "main.py": "import pkg.util\nfrom pkg import helper\nfrom secrets import keys\nimport os, json\nfrom . import sibling\n",
            "pkg/__init__.py": "", "pkg/util.py": "TOKEN_FIELD = 1\n", "pkg/helper.py": "x = 1\n",
            "secrets/keys.py": "TOKEN = 'abc'\n", "sibling.py": "y = 2\n",
            ".git/config": "TOKEN", "node_modules/m.js": "TOKEN", "notes.txt": "token here\nTOKEN again\n",
            "bin.dat": "",
        }
        for n, t in files.items():
            (self.root / n).write_text(t)
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00TOKEN")
        self.rules = Rules(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, paths):
        return sorted(str(p.relative_to(self.root)) for p in paths)

    def test_imports_resolved_inside_project(self):
        got = related.python_imports((self.root / "main.py").read_text(), self.root / "main.py", self.root)
        self.assertEqual(self.names(got), ["pkg/__init__.py", "pkg/helper.py", "pkg/util.py", "secrets/keys.py", "sibling.py"])

    def test_denied_imports_are_skipped(self):
        self.rules.set_denied(self.root / "secrets", True)
        got = related.python_imports((self.root / "main.py").read_text(), self.root / "main.py", self.root)
        self.assertNotIn("secrets/keys.py", self.names(got))

    def test_bad_syntax_gives_nothing(self):
        self.assertEqual(related.python_imports("def (:", self.root / "main.py", self.root), [])

    def test_search_skips_service_dirs_binary_and_denied(self):
        hits = related.search(self.root, "token", case=False)
        files = {str(h.path.relative_to(self.root)) for h in hits}
        self.assertEqual(files, {"pkg/util.py", "secrets/keys.py", "notes.txt"})
        self.rules.set_denied(self.root / "secrets", True)
        files = {str(h.path.relative_to(self.root)) for h in related.search(self.root, "token")}
        self.assertEqual(files, {"pkg/util.py", "notes.txt"})

    def test_search_case_and_line_numbers(self):
        hits = related.search(self.root, "TOKEN", case=True)
        notes = [h for h in hits if h.path.name == "notes.txt"]
        self.assertEqual([(h.line, h.text) for h in notes], [(2, "TOKEN again")])

    def test_search_limit_and_empty_query(self):
        self.assertEqual(related.search(self.root, ""), [])
        self.assertEqual(len(related.search(self.root, "t", limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
