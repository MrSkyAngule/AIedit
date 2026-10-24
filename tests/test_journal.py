import unittest

from aiedit.journal import Hunk, attach_remarks, hunks_for, sorted_hunks, total_text


class HunkTest(unittest.TestCase):
    def test_new_file_is_one_hunk_of_all_lines(self):
        h, = hunks_for("a.py", None, "x = 1\ny = 2\nz = 3\n")
        self.assertEqual((h.kind, h.added, h.removed, h.start, h.end, h.size), ("new", 3, 0, 1, 3, 3))
        self.assertEqual(h.preview, "x = 1")

    def test_modification_and_line_numbers_in_the_new_text(self):
        old = "a\nb\nc\nd\ne\nf\ng\nh\n"
        new = "a\nb\nC1\nC2\nd\ne\nf\ng\nh\n"
        h, = hunks_for("a.py", old, new)
        self.assertEqual((h.kind, h.added, h.removed, h.start, h.end), ("changed", 2, 1, 3, 4))
        self.assertEqual(h.preview, "C1")

    def test_pure_insertion_and_pure_deletion(self):
        ins, = hunks_for("a.py", "a\nb\n", "a\nNEW\nb\n")
        self.assertEqual((ins.kind, ins.added, ins.removed, ins.start), ("added", 1, 0, 2))
        dele, = hunks_for("a.py", "a\nb\nc\n", "a\nc\n")
        self.assertEqual((dele.kind, dele.added, dele.removed, dele.start, dele.end), ("removed", 0, 1, 2, 2))
        self.assertEqual(dele.preview, "удалено строк: 1")

    def test_far_apart_edits_are_separate_and_close_ones_are_merged(self):
        base = [f"line{i}" for i in range(30)]
        edited = list(base)
        edited[2], edited[4], edited[25] = "X", "Y", "Z"
        hunks = hunks_for("a.py", "\n".join(base) + "\n", "\n".join(edited) + "\n")
        self.assertEqual([(h.start, h.end) for h in hunks], [(3, 5), (26, 26)])  # строки 3 и 5 склеились
        apart = hunks_for("a.py", "\n".join(base) + "\n", "\n".join(edited) + "\n", merge_gap=0)
        self.assertEqual(len(apart), 3)

    def test_no_change_no_hunks_and_trailing_newline_is_not_a_change(self):
        self.assertEqual(hunks_for("a.py", "a\nb\n", "a\nb\n"), [])
        self.assertEqual(hunks_for("a.py", "", ""), [])

    def test_blank_added_lines_do_not_become_the_preview(self):
        h, = hunks_for("a.py", "a\n", "a\n\n\n   code here   \n")
        self.assertEqual(h.preview, "code here")

    def test_preview_is_capped(self):
        h, = hunks_for("a.py", None, "x" * 500 + "\n")
        self.assertEqual(len(h.preview), 90)


class SortAndRemarksTest(unittest.TestCase):
    def hunks(self):
        return [Hunk("b.py", 5, 6, 2, 0, "added", "b"), Hunk("a.py", 1, 40, 40, 0, "new", "big"),
                Hunk("a.py", 70, 71, 1, 1, "changed", "mid"), Hunk("c.py", 3, 4, 2, 0, "added", "c")]

    def test_biggest_first_and_stable_order_for_ties(self):
        order = [(h.file, h.start) for h in sorted_hunks(self.hunks())]
        self.assertEqual(order, [("a.py", 1), ("a.py", 70), ("b.py", 5), ("c.py", 3)])  # 40, 2, 2, 2 -> по файлу

    def test_remarks_land_on_the_hunk_that_covers_the_line(self):
        hs = self.hunks()
        attach_remarks(hs, [("a.py", 71, "near the small one"), ("a.py", 10, "inside the big one"),
                            ("b.py", None, "no line"), ("a.py", 500, "far away"), ("ghost.py", 1, "unknown file")])
        by = {(h.file, h.start): h.remarks for h in hs}
        self.assertEqual(by[("a.py", 70)], ["near the small one"])
        self.assertEqual(by[("a.py", 1)], ["inside the big one", "far away"])  # мимо правок - на самую большую
        self.assertEqual(by[("b.py", 5)], ["no line"])
        self.assertEqual(by[("c.py", 3)], [])

    def test_summary_text(self):
        self.assertEqual(total_text(self.hunks()), "4 мест в 3 файлах, +45 -1 строк")


if __name__ == "__main__":
    unittest.main()
