import tempfile
import unittest
from pathlib import Path

from aiedit import flows
from aiedit.guard import Region
from aiedit.orchestrator import Turn
from aiedit.providers import Provider
from aiedit.translator import Translator
from aiedit.workspace import Workspace
from tests.test_translator import Fake


class Script(Provider):
    name = "script"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        return self.replies.pop(0)


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "a.py").write_text("x = 1\n")
        self.fake = Fake()
        self.tr = Translator(self.fake)

    def tearDown(self):
        self.tmp.cleanup()


class AgentFlowTest(FlowCase):
    def run_it(self, task, replies, tr="default", **kw):
        prov = Script(replies)
        ws = Workspace(self.root)
        res = flows.run_agent(task, ws, prov, translator=self.tr if tr == "default" else tr, **kw)
        return res, prov

    def test_task_goes_english_and_answer_comes_back_russian(self):
        res, prov = self.run_it("добавь проверку в функцию", ["Added a check to the function"])
        system, messages = prov.seen[0]
        self.assertTrue(messages[0]["content"].startswith("add a check in function"))
        self.assertTrue(system.endswith("Reply in English."))
        self.assertEqual(res.task_en.startswith("add a check"), True)
        self.assertEqual(res.text_en, "Added a check to the function")
        self.assertEqual(res.text, "Добавил проверку в функцию")

    def test_without_translator_nothing_changes(self):
        res, prov = self.run_it("добавь проверку", ["done"], tr=None)
        self.assertTrue(prov.seen[0][1][0]["content"].startswith("добавь проверку"))
        self.assertTrue(prov.seen[0][0].endswith("language of the user's task."))
        self.assertEqual((res.text, res.text_en, res.task_en, res.notes), ("done", "done", "", []))

    def test_english_task_is_not_translated_but_reply_rule_is_english(self):
        res, prov = self.run_it("add a check", ["Added a check"])
        self.assertTrue(prov.seen[0][1][0]["content"].startswith("add a check"))
        self.assertEqual(res.task_en, "")
        self.assertEqual(len(self.fake.calls), 1)  # только ответ en->ru
        self.assertEqual(res.text, "Добавил проверку")

    def test_translation_failure_sends_original_and_reports(self):
        tr = Translator(Fake(fail=OSError("offline")))
        res, prov = self.run_it("добавь проверку", ["Added a check"], tr=tr)
        self.assertTrue(prov.seen[0][1][0]["content"].startswith("добавь проверку"))
        self.assertEqual(res.text, "Added a check")  # ответ как есть
        self.assertEqual(len(res.notes), 2)
        self.assertIn("Задача ушла как есть", res.notes[0])
        self.assertIn("Ответ показан как есть", res.notes[1])

    def test_progress_shows_what_was_sent(self):
        seen = []
        self.run_it("добавь проверку", ["Added a check"], on_progress=seen.append)
        self.assertEqual(seen[0], "задача переведена: add a check")
        self.assertEqual(seen[-1], "перевожу ответ...")

    def test_code_in_task_and_answer_stays_local(self):
        res, prov = self.run_it("добавь проверку в `secret_fn()`", ["Added a check.\n```py\nSECRET_VALUE = 1\n```"])
        self.assertIn("`secret_fn()`", prov.seen[0][1][0]["content"])
        for _, user, _ in self.fake.calls:
            self.assertNotIn("secret_fn", user)
            self.assertNotIn("SECRET_VALUE", user)
        self.assertIn("SECRET_VALUE = 1", res.text)

    def test_stop_message_is_not_translated(self):
        res, prov = self.run_it("добавь проверку", ["@@tool list_files\n@@end"] * 30, should_stop=lambda: True)
        self.assertFalse(res.from_model)
        self.assertEqual(res.text_en, "")
        self.assertTrue(res.text.startswith("Работа остановлена"))
        self.assertEqual([c[0] for c in self.fake.calls], ["ru-en"])

    def test_history_uses_english_versions_for_the_model(self):
        hist = [Turn("user", "что делает", None, "what does"), Turn("assistant", "Добавил проверку", None, "Added a check")]
        res, prov = self.run_it("исправь ошибку", ["Fixed the bug"], history=hist)
        msgs = prov.seen[0][1]
        self.assertEqual([m["content"] for m in msgs[:2]], ["what does", "Added a check"])
        self.assertTrue(msgs[2]["content"].startswith("fix the bug"))


class AskExplainFlowTest(FlowCase):
    def test_ask_translates_task_and_note(self):
        prov = Script(["Added a check\n```\nx = 2\n```"])
        res = flows.run_ask(self.tr, "x = 1\n", "добавь проверку", [], {"cheap": prov, "strong": prov},
                            source=self.root / "a.py", root=self.root)
        self.assertTrue(prov.seen[0][1][-1]["content"].startswith("add a check"))
        self.assertTrue(prov.seen[0][0].endswith("Reply in English."))
        self.assertEqual((res.note_en, res.note), ("Added a check", "Добавил проверку"))
        self.assertEqual(res.new_text.strip(), "x = 2")
        self.assertTrue(res.task_en)

    def test_ask_without_translator(self):
        prov = Script(["Готово\n```\nx = 2\n```"])
        res = flows.run_ask(None, "x = 1\n", "добавь", [], {"cheap": prov, "strong": prov})
        self.assertEqual(res.note, "Готово")
        self.assertEqual(res.task_en, "")

    def test_explain_translates_answer_back(self):
        prov = Script(["Added a check"])
        res = flows.run_explain(self.tr, "x = 1\n", [Region("explain", 0, 0)], {"cheap": prov, "strong": prov})
        self.assertTrue(prov.seen[0][0].endswith("Reply in English."))
        self.assertEqual((res.text_en, res.text), ("Added a check", "Добавил проверку"))

    def test_explain_with_nothing_to_explain_makes_no_calls(self):
        prov = Script([])
        res = flows.run_explain(self.tr, "x = 1\n", [], {"cheap": prov, "strong": prov})
        self.assertEqual(res.zones, 0)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
