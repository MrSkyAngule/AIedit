import tempfile
import unittest
from pathlib import Path

from aiedit.marks import Marks
from aiedit.workspace import Workspace

PAY = ('API = "sk-ant-api03-AbCdEf0123456789xyz"\n\n\ndef charge(amount):\n    """SECRET_FORMULA doc"""\n'
       '    return amount * 0.07\n\n\ndef free():\n    return 1\n\n\ndef tail():\n    return 0\n')


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "pay.py").write_text(PAY)
        (self.root / "other.py").write_text("x = 1\n")
        m = Marks()
        m.add("interface", 3, 5, "fee in cents")
        m.save(self.root / "pay.py", PAY)
        self.ws = Workspace(self.root, log_enabled=False)

    def tearDown(self):
        self.tmp.cleanup()


class ModeTest(Case):
    def test_read_only_refuses_every_write_but_allows_reading(self):
        self.ws.read_only = True
        self.assertIn("read-only", self.ws.edit_file("pay.py", "return 1", "return 2"))
        self.assertIn("read-only", self.ws.create_file("new.py", "x"))
        self.assertIn("1|", self.ws.read_file("pay.py"))
        self.assertIn("pay.py", self.ws.search("def free"))
        self.assertEqual(self.ws.changes(), [])

    def test_scope_limits_edits_and_creation(self):
        self.ws.edit_scope = {self.root / "pay.py"}
        self.assertIn("accepted", self.ws.edit_file("pay.py", "return 1", "return 2"))
        self.assertIn("may only edit: pay.py", self.ws.edit_file("other.py", "x = 1", "x = 2"))
        self.assertIn("may only edit", self.ws.create_file("new.py", "x"))
        self.assertEqual([c.path.name for c in self.ws.changes()], ["pay.py"])

    def test_empty_scope_means_nothing_may_change_and_none_means_no_limit(self):
        self.ws.edit_scope = set()
        self.assertIn("may only edit: nothing", self.ws.edit_file("other.py", "x = 1", "x = 2"))
        self.ws.edit_scope = None
        self.assertIn("accepted", self.ws.edit_file("other.py", "x = 1", "x = 2"))

    def test_scope_also_covers_paths_that_do_not_exist(self):
        self.ws.edit_scope = {self.root / "pay.py"}
        self.assertIn("may only edit: pay.py", self.ws.edit_file("ghost.py", "a", "b"))


class ReviewDiffTest(Case):
    def test_diff_shows_the_change_and_no_closed_code(self):
        self.ws.edit_file("pay.py", "def free():\n    return 1", "def free(x=0):\n    return 2")
        diff = self.ws.review_diff()
        self.assertIn("--- a/pay.py", diff)
        self.assertIn("+++ b/pay.py", diff)
        self.assertIn("-    return 1", diff)
        self.assertIn("+    return 2", diff)
        for leak in ("SECRET_FORMULA", "0.07", "sk-ant"):
            self.assertNotIn(leak, diff)
        self.assertIn("note: fee in cents", diff)  # заглушка с заметкой остаётся видна как контекст

    def test_diff_is_clean_when_lines_shift_above_a_closed_zone(self):
        self.ws.edit_file("pay.py", "def free():", "# added line\ndef free():")
        diff = self.ws.review_diff()
        self.assertIn("+# added line", diff)
        for leak in ("SECRET_FORMULA", "0.07", "sk-ant"):
            self.assertNotIn(leak, diff)

    def test_new_file_and_unchanged_files(self):
        self.ws.create_file("new.py", "print('hi')\n")
        self.ws.read_file("other.py")
        diff = self.ws.review_diff()
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+++ b/new.py", diff)
        self.assertIn("+print('hi')", diff)
        self.assertNotIn("other.py", diff)

    def test_no_changes_no_diff_and_truncation(self):
        self.assertEqual(self.ws.review_diff(), "")
        self.ws.create_file("big.py", "x = 1\n" * 20000)
        diff = self.ws.review_diff(limit=1000)
        self.assertLessEqual(len(diff), 1100)
        self.assertIn("diff truncated", diff)


class CoverageTest(Case):
    def test_gaps_name_files_and_hidden_line_counts(self):
        self.ws.read_file("pay.py")
        self.ws.read_file("other.py")
        gaps = dict((name, (hidden, total)) for name, hidden, total in self.ws.coverage_gaps())
        self.assertEqual(list(gaps), ["pay.py"])
        hidden, total = gaps["pay.py"]
        self.assertEqual(total, len(PAY.split("\n")))
        self.assertEqual(hidden, 4)  # interface 3 строки + строка с ключом, скрытая автоматически

    def test_changed_files_count_even_if_not_read(self):
        self.ws.edit_file("pay.py", "def free():\n    return 1", "def free():\n    return 2")
        self.assertEqual([g[0] for g in self.ws.coverage_gaps()], ["pay.py"])

    def test_fully_visible_project_has_no_gaps(self):
        self.ws.read_file("other.py")
        self.assertEqual(self.ws.coverage_gaps(), [])


if __name__ == "__main__":
    unittest.main()
