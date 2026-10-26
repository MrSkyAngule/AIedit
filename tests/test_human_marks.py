"""Отметки для себя: сохраняются рядом с файлом, едут за строками, а модель о них не узнаёт."""
import json
import tempfile
import unittest
from pathlib import Path

from aiedit.guard import Region
from aiedit.marks import Marks, sidecar
from aiedit.workspace import Change, Workspace, write_change
from tests.test_agent import AgentCase, tool

NOTE = "ТОЛЬКО_ДЛЯ_ЧЕЛОВЕКА_проверить_формулу"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "a.py"
        self.text = "a\nb\nc\nd\n"
        self.path.write_text(self.text)

    def tearDown(self):
        self.tmp.cleanup()

    def test_human_marks_are_not_zones(self):
        m = Marks()
        m.add_human(1, 2, "  проверить  ")
        self.assertEqual(m.items, [])  # всё, что уходит модели, читает только items
        self.assertEqual([(r.start, r.end, r.note) for r in m.human], [(1, 2, "проверить")])
        self.assertEqual(m.human_at(2).note, "проверить")
        self.assertIsNone(m.human_at(3))

    def test_saved_next_to_the_file_and_loaded_back(self):
        m = Marks()
        m.add("hidden", 0, 0)
        m.add_human(2, 3, "дописать")
        m.save(self.path, self.text)
        data = json.loads(sidecar(self.path).read_text())
        self.assertEqual(data["human"], [[2, 3, "дописать"]])
        back = Marks()
        self.assertTrue(back.load(self.path, self.text))
        self.assertEqual([(r.level, r.start) for r in back.items], [("hidden", 0)])
        self.assertEqual([(r.start, r.end, r.note) for r in back.human], [(2, 3, "дописать")])

    def test_only_human_marks_still_make_a_file_and_removing_them_deletes_it(self):
        m = Marks()
        m.add_human(0, 0, "x")
        m.save(self.path, self.text)
        self.assertTrue(sidecar(self.path).exists())
        m.clear_human(0, 0)
        m.save(self.path, self.text)
        self.assertFalse(sidecar(self.path).exists())

    def test_old_file_without_human_key_loads(self):
        sidecar(self.path).write_text(json.dumps({"sha256": "x", "regions": [["readonly", 0, 1]]}))
        m = Marks()
        m.load(self.path, self.text)
        self.assertEqual(len(m.items), 1)
        self.assertEqual(m.human, [])

    def test_marks_follow_inserted_and_removed_lines(self):
        m = Marks()
        m.add_human(2, 2, "n")
        m.on_change(0, 2)  # две строки вставили выше
        self.assertEqual((m.human[0].start, m.human[0].end), (4, 4))
        m.on_change(0, -2)
        self.assertEqual((m.human[0].start, m.human[0].end), (2, 2))
        m.on_change(1, -1)  # удалили строку 2 (убираются строки после 1-й), на ней стояла отметка
        self.assertEqual(m.human, [])

    def test_undo_brings_marks_back_with_the_text(self):
        m = Marks()
        m.add_human(0, 0, "n")
        m.remember("one")
        m.add_human(1, 1, "other")
        m.remember("two")
        self.assertTrue(m.restore("one"))
        self.assertEqual([r.note for r in m.human], ["n"])

    def test_write_change_keeps_and_shifts_human_marks(self):
        m = Marks()
        m.add_human(2, 2, "здесь")
        m.save(self.path, self.text)
        new = "NEW1\nNEW2\n" + self.text  # ИИ вставил две строки выше
        write_change(Change(self.path, self.text, new, [], [], False))
        back = Marks()
        back.load(self.path, new)
        self.assertEqual([(r.start, r.note) for r in back.human], [(4, "здесь")])

    def test_write_change_without_old_marks_is_fine(self):
        other = Path(self.tmp.name) / "fresh.py"
        write_change(Change(other, None, "x\n", [Region("hidden", 0, 0)], [], False))
        self.assertTrue(sidecar(other).exists())
        self.assertFalse(json.loads(sidecar(other).read_text()).get("human"))


class NeverReachesTheModelTest(AgentCase):
    def setUp(self):
        super().setUp()
        m = Marks()
        m.load(self.root / "main.py", self.files["main.py"])
        m.add_human(0, 1, NOTE)
        m.save(self.root / "main.py", self.files["main.py"])

    def test_agent_never_sees_the_note_or_the_file_that_holds_it(self):
        res, prov, ws = self.go([
            tool("list_files", path=".", depth=3), tool("read_file", path="main.py"),
            tool("search", query="ЧЕЛОВЕКА"), tool("read_file", path="main.py.aimarks.json"),
            "Готово."])
        self.assertTrue((self.root / "main.py.aimarks.json").exists())  # отметка на месте
        everything = prov.everything()
        self.assertNotIn(NOTE, everything)
        self.assertNotIn("ТОЛЬКО_ДЛЯ", everything)
        self.assertNotIn("aimarks", everything.replace("main.py.aimarks.json", "").replace("ERROR", ""))  # в списке файла нет
        self.assertIn("print(pay.free())", everything)  # сам код модель видит как обычно

    def test_workspace_regions_do_not_include_human_marks(self):
        ws = Workspace(self.root, model="script")
        st = ws.state(ws.resolve("main.py"))
        self.assertFalse(any(NOTE in r.note for r in st.regions))
        self.assertFalse(any(r.level == "human" for r in st.regions))
