"""Агент может переспросить человека, когда задача неоднозначна (инструмент ask_user)."""
import unittest

from aiedit import agent, flows, prompts
from aiedit.translator import Translator
from tests.test_agent import AgentCase, tool
from tests.test_conductor_run import RunCase, Script as NamedScript
from tests.test_translator import Fake


class Human:
    def __init__(self, *answers):
        self.answers, self.questions = list(answers), []

    def __call__(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else None


class AskLoopTest(AgentCase):
    def test_question_pauses_and_answer_comes_back_as_result(self):
        human = Human("в pay.py")
        res, prov, _ = self.go([tool("ask_user", question="Which file: main.py or pay.py?"), "Done."], ask=human)
        self.assertEqual(human.questions, ["Which file: main.py or pay.py?"])
        self.assertIn("@@result ask_user\nв pay.py\n@@end", prov.seen[1][1][-1]["content"])
        self.assertEqual(res.text, "Done.")
        self.assertIn("задаю вопрос", res.calls)

    def test_tool_is_offered_only_when_someone_can_answer(self):
        _, prov, _ = self.go(["ok"], ask=Human())
        self.assertIn("ask_user", prov.seen[0][0])
        self.assertTrue(prov.seen[0][0].endswith(prompts.REPLY_LANGUAGE))
        _, prov, _ = self.go(["ok"])
        self.assertNotIn("ask_user", prov.seen[0][0])

    def test_without_human_model_is_told_to_decide(self):
        _, prov, _ = self.go([tool("ask_user", question="Which?"), "ok"])
        self.assertIn(prompts.OUT_ASK_UNAVAILABLE, prov.seen[1][1][-1]["content"])

    def test_no_answer_means_assume_and_say_so(self):
        _, prov, _ = self.go([tool("ask_user", question="Which?"), "ok"], ask=Human(None))
        self.assertIn(prompts.OUT_NO_ANSWER, prov.seen[1][1][-1]["content"])

    def test_question_limit(self):
        human = Human("a", "b", "c", "d")
        replies = [tool("ask_user", question=f"q{i}") for i in range(4)] + ["ok"]
        _, prov, _ = self.go(replies, ask=human)
        self.assertEqual(len(human.questions), agent.MAX_QUESTIONS)
        self.assertIn(prompts.OUT_ASK_LIMIT, prov.seen[4][1][-1]["content"])

    def test_multiline_question_section(self):
        (call,), _ = agent.parse_calls("@@tool ask_user\n@@question\nWhich one?\n1) a\n2) b\n@@end")
        self.assertEqual(call.args, {"question": "Which one?\n1) a\n2) b"})

    def test_empty_question_is_an_error_not_a_question(self):
        human = Human("x")
        _, prov, _ = self.go(["@@tool ask_user\n@@end", "ok"], ask=human)
        self.assertEqual(human.questions, [])
        self.assertIn("ERROR: missing arguments: question", prov.seen[1][1][-1]["content"])

    def test_stop_while_waiting_ends_the_run(self):
        stopped = []

        def human(q):
            stopped.append(True)
            return None
        res, prov, _ = self.go([tool("ask_user", question="?"), "should not be asked"], ask=human,
                               should_stop=lambda: bool(stopped))
        self.assertEqual(len(prov.seen), 1)
        self.assertIn("остановлено", res.stopped)


class DemoAskTest(AgentCase):
    def test_demo_agent_asks_when_told_to(self):
        from aiedit.providers import DemoProvider
        from aiedit.workspace import Workspace
        human = Human("main.py")
        res = agent.run("спроси меня", Workspace(self.root), DemoProvider(), ask=human)
        self.assertTrue(human.questions[0].startswith("Демо-вопрос"))
        self.assertIn("ты ответил «main.py»", res.text)
        res = agent.run("спроси меня", Workspace(self.root), DemoProvider())  # некому ответить - не спрашивает
        self.assertNotIn("Демо-вопрос", res.text)


class AskTranslationTest(AgentCase):
    def test_question_reaches_human_in_russian_and_answer_goes_back_in_english(self):
        tr = Translator(Fake(ru_en={"исправь": "fix", "файле": "file", "ошибку": "the bug", "в": "in"},
                             en_ru={"Fixed": "Исправил", "in": "в", "file": "файле", "the": "", "bug": "ошибку"}))
        human = Human("исправь в файле")
        prov = NamedScript("m", [tool("ask_user", question="Fixed the bug in file"), "Fixed the bug in file"])
        from aiedit.workspace import Workspace
        res = flows.run_agent("исправь ошибку", Workspace(self.root), prov, translator=tr, ask=human)
        self.assertEqual(human.questions, ["Исправил ошибку в файле"])
        self.assertIn("@@result ask_user\nfix in file\n@@end", prov.sent())
        self.assertEqual(res.text, "Исправил ошибку в файле")


class ConductorAskTest(RunCase):
    PLAN = "@@plan\nsummary: s\n@@task t1\nworker: sonnet\ngoal: change charge\nrisk: low\n@@end"

    def test_worker_can_ask_during_its_step(self):
        human = Human("only positive amounts")
        roster = self.roster(sonnet=[tool("ask_user", question="Allow zero?"), "done"])
        res, _ = self.conduct(self.PLAN, roster, ask=human)
        self.assertEqual(human.questions, ["Allow zero?"])
        self.assertIn("only positive amounts", self.p["sonnet"].sent())
        self.assertIn("ask_user", self.p["sonnet"].seen[0][0])

    def test_reviewer_never_gets_the_tool(self):
        from aiedit import review
        self.assertNotIn("ask_user", review.REVIEW_SYSTEM)


if __name__ == "__main__":
    unittest.main()
