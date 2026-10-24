import unittest

from aiedit.dialogs import diff_html
from aiedit.guard import Region
from aiedit.marks import MAX_SNAPSHOTS, Marks


class SnapshotTest(unittest.TestCase):
    def test_restore_returns_zones_for_seen_text(self):
        m = Marks(); m.add("readonly", 3, 4); m.remember("T1")
        m.items = [Region("readonly", 5, 6)]; m.remember("T2")
        self.assertTrue(m.restore("T1"))
        self.assertEqual(m.items, [Region("readonly", 3, 4)])
        self.assertTrue(m.restore("T2"))
        self.assertEqual(m.items, [Region("readonly", 5, 6)])

    def test_unknown_text_changes_nothing(self):
        m = Marks(); m.add("hidden", 0, 0)
        self.assertFalse(m.restore("never seen"))
        self.assertEqual(m.items, [Region("hidden", 0, 0)])

    def test_latest_marks_win_for_same_text(self):
        m = Marks(); m.add("hidden", 0, 0); m.remember("T")
        m.clear(0, 0); m.remember("T")
        m.add("explain", 1, 1)
        self.assertTrue(m.restore("T"))
        self.assertEqual(m.items, [])

    def test_snapshot_is_a_copy(self):
        m = Marks(); m.add("hidden", 0, 0); m.remember("T")
        m.items.append(Region("explain", 5, 5))
        m.restore("T")
        self.assertEqual(m.items, [Region("hidden", 0, 0)])

    def test_memory_is_bounded(self):
        m = Marks()
        for i in range(MAX_SNAPSHOTS + 50):
            m.remember(f"text {i}")
        self.assertFalse(m.restore("text 0"))
        self.assertTrue(m.restore(f"text {MAX_SNAPSHOTS + 49}"))


class DiffTest(unittest.TestCase):
    def test_marks_added_and_removed_lines(self):
        out = diff_html("a\nb\nc", "a\nB\nc")
        self.assertIn("-b", out)
        self.assertIn("+B", out)
        self.assertIn("#d4f5d4", out)
        self.assertIn("#f9d6d6", out)

    def test_escapes_html(self):
        self.assertIn("&lt;script&gt;", diff_html("x", "<script>"))

    def test_no_changes_no_diff(self):
        self.assertEqual(diff_html("same", "same"), "")


if __name__ == "__main__":
    unittest.main()
