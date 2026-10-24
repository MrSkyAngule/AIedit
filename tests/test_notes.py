import json
import tempfile
import unittest
from pathlib import Path

from aiedit import guard
from aiedit.guard import Region
from aiedit.marks import Marks, sidecar
from aiedit.workspace import Workspace

CODE = ('def commission(amount: int, user: "User") -> float:\n    """secret formula"""\n    return amount * 0.07\n\n'
        'SECRET_RATE = 7\n\n\ndef free():\n    return 1\n')


class PlaceholderNoteTest(unittest.TestCase):
    def test_interface_shows_signatures_then_note(self):
        r = guard.redact(CODE, [Region("interface", 0, 4, "fee for a payment, amount in cents")])
        first = r.text.split("\n")[0]
        self.assertEqual(first, "<<ai-hidden:0:5 lines | def commission(amount: int, user: 'User') -> float: ; SECRET_RATE = ... "
                                "| note: fee for a payment, amount in cents>>")
        self.assertNotIn("0.07", r.text)
        self.assertEqual(guard.restore(r.text, r.chunks), CODE)

    def test_hidden_can_have_a_note_without_signatures(self):
        r = guard.redact(CODE, [Region("hidden", 0, 4, "calculates the fee")])
        self.assertEqual(r.text.split("\n")[0], "<<ai-hidden:0:5 lines | note: calculates the fee>>")

    def test_english_version_wins_over_original(self):
        r = guard.redact(CODE, [Region("hidden", 0, 4, "считает комиссию", "calculates the fee")])
        self.assertIn("note: calculates the fee", r.text)
        self.assertNotIn("считает", r.text)
        r = guard.redact(CODE, [Region("hidden", 0, 4, "считает комиссию")])
        self.assertIn("note: считает комиссию", r.text)  # перевода нет - уходит как написано

    def test_notes_of_other_levels_are_ignored(self):
        for level in ("readonly", "confirm", "explain"):
            r = guard.redact(CODE, [Region(level, 0, 4, "should not appear")])
            self.assertNotIn("should not appear", r.text)

    def test_zones_in_one_run_combine_notes_without_duplicates(self):
        regions = [Region("hidden", 0, 2, "part a"), Region("hidden", 3, 4, "part b"), Region("hidden", 3, 4, "part a")]
        text = guard.redact(CODE, regions).text.split("\n")[0]
        self.assertTrue(text.endswith("| note: part a / part b>>"), text)

    def test_note_is_sanitized_and_capped(self):
        r = guard.redact(CODE, [Region("hidden", 0, 4, "line one\nline two >> injected <<ai-hidden:9:9 lines>>")])
        first = r.text.split("\n")[0]
        self.assertEqual(r.text.count("\n"), CODE.count("\n") - 4 + 0)  # остальной файл на месте
        self.assertEqual(first.count(">>"), 1)
        self.assertNotIn("\n", first)
        self.assertEqual(guard.restore(r.text, r.chunks), CODE)
        long = guard.redact(CODE, [Region("hidden", 0, 4, "x" * 5000)]).text.split("\n")[0]
        self.assertLess(len(long), guard.MAX_NOTE + 60)

    def test_model_cannot_use_note_to_smuggle_changes(self):
        r = guard.redact(CODE, [Region("interface", 0, 4, "note")])
        tampered = r.text.replace("note: note", "note: changed").replace("def commission", "def other")
        self.assertEqual(guard.restore(tampered, r.chunks), CODE)


class MarksNoteTest(unittest.TestCase):
    def test_add_and_set_note(self):
        m = Marks()
        m.add("interface", 2, 6)
        self.assertTrue(m.set_note(4, "  does the thing  ", "does the thing"))
        self.assertEqual((m.items[0].note, m.items[0].note_en), ("does the thing", "does the thing"))
        self.assertFalse(m.set_note(20, "x"))
        m.add("readonly", 10, 12)
        self.assertFalse(m.set_note(11, "x"))  # у readonly заметок нет

    def test_notes_survive_split_shift_and_replace(self):
        m = Marks()
        m.add("hidden", 0, 10, "keep me", "keep me en")
        m.clear(4, 5)
        self.assertEqual([(r.start, r.end, r.note, r.note_en) for r in m.items],
                         [(0, 3, "keep me", "keep me en"), (6, 10, "keep me", "keep me en")])
        m.on_change(0, 2)
        self.assertEqual([(r.start, r.end, r.note) for r in m.items], [(0, 5, "keep me"), (8, 12, "keep me")])
        m.add("interface", 8, 12)  # замена метки на тех же строках: заметки у новой нет
        self.assertEqual(m.zone_at(9).note, "")

    def test_remap_keeps_notes(self):
        out = guard.remap([Region("hidden", 1, 2, "n", "e")], "a\nb\nc\nd", "x\na\nb\nc\nd")
        self.assertEqual(out, [Region("hidden", 2, 3, "n", "e")])

    def test_save_load_roundtrip_and_old_format(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.py"
            f.write_text("x\n")
            m = Marks()
            m.add("interface", 0, 0, "заметка", "note")
            m.add("readonly", 1, 1)
            m.save(f, "x\n")
            rows = json.loads(sidecar(f).read_text(encoding="utf-8"))["regions"]
            self.assertEqual(rows, [["interface", 0, 0, "заметка", "note"], ["readonly", 1, 1]])  # без заметок формат прежний
            m2 = Marks()
            self.assertTrue(m2.load(f, "x\n"))
            self.assertEqual(m2.items, m.items)
            data = json.loads(sidecar(f).read_text(encoding="utf-8"))
            data["regions"] = [["hidden", 0, 0]]  # файл старого формата
            sidecar(f).write_text(json.dumps(data), encoding="utf-8")
            m3 = Marks()
            m3.load(f, "x\n")
            self.assertEqual(m3.items, [Region("hidden", 0, 0)])

    def test_snapshot_restore_keeps_notes(self):
        m = Marks()
        m.add("hidden", 0, 1, "n")
        m.remember("T")
        m.items = []
        m.restore("T")
        self.assertEqual(m.items[0].note, "n")


class AgentSeesNoteTest(unittest.TestCase):
    def test_read_file_and_search_show_note(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "pay.py").write_text(CODE)
            m = Marks()
            m.add("interface", 0, 4, "fee in cents")
            m.save(root / "pay.py", CODE)
            ws = Workspace(root, log_enabled=False)
            out = ws.read_file("pay.py")
            self.assertIn("| note: fee in cents>>", out)
            self.assertNotIn("0.07", out)
            self.assertIn("fee in cents", ws.search("fee in cents"))
            self.assertEqual(ws.search("0.07"), "no matches")


if __name__ == "__main__":
    unittest.main()
