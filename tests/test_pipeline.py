"""Конвейер: автор (Claude) -> проверяющий (GLM) -> автор -> ... пока замечаний нет -> статанализ -> автору."""
import tempfile
import unittest
from pathlib import Path

from aiedit import pipeline
from aiedit.providers import Provider
from aiedit.roster import Roster, Worker
from aiedit.workspace import Workspace
from tests.test_analysis import FAKE, FakeRunner

CLEAN = "@@findings\nsummary: no issues found\n@@end"
FINDING = ("@@findings\nsummary: one\n@@finding f1\nseverity: high\nfile: main.py\nline: 2\n"
           "issue: login is built from a string\nfix: use parameters\n@@end")


class Script(Provider):
    def __init__(self, name, replies, log):
        self.name, self.replies, self.log, self.seen = name, list(replies), log, []

    def complete(self, system, messages):
        self.seen.append(messages)
        self.log.append(self.name)
        return self.replies.pop(0) if self.replies else "done"


def edit(old, new, path="main.py"):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "main.py").write_text("def run():\n    return 1\n")
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD lint")  # «автор уже написал код» и в нём есть замечание анализатора
        self.order = []

    def tearDown(self):
        self.tmp.cleanup()

    def roster(self, glm=(), sonnet=()):
        self.glm = Script("glm", glm, self.order)
        self.sonnet = Script("sonnet", sonnet, self.order)
        return Roster([Worker("sonnet", self.sonnet, "mid", 2, 10, "mid"), Worker("glm", self.glm, "review", 0, 0, "mid", "security")])

    def sequence(self) -> list[str]:
        """Кто по очереди работал, без подряд идущих повторов (у автора правка и итоговый ответ - два обращения)."""
        return [who for i, who in enumerate(self.order) if i == 0 or who != self.order[i - 1]]

    def run_pipeline(self, roster, **kw):
        author = roster.get("sonnet")
        return pipeline.run("fix run", self.ws, roster, lambda path: author, analyzers=[FAKE], runner=FakeRunner(), **kw)


class OrderTest(Case):
    def test_review_goes_first_and_clean_code_then_goes_to_static_analysis(self):
        roster = self.roster(glm=[CLEAN, CLEAN], sonnet=[edit("return 2  # BAD lint", "return 2"), "fixed lint"])
        analysis, review, _ = self.run_pipeline(roster)
        self.assertEqual(self.sequence(), ["glm", "sonnet", "glm"])  # ревью, правка по замечанию статанализа, ревью ещё раз
        self.assertEqual((len(review.rounds), review.remaining, review.stalled), (2, [], ""))
        self.assertEqual((analysis.fixed_rounds, analysis.remaining), (1, []))

    def test_static_analysis_problems_go_to_the_author_not_to_the_reviewer(self):
        roster = self.roster(glm=[CLEAN, CLEAN], sonnet=[edit("return 2  # BAD lint", "return 2"), "ok"])
        self.run_pipeline(roster)
        fix_request = self.sonnet.seen[0][0]["content"]
        self.assertIn("Static analysis found NEW problems", fix_request)
        self.assertIn("main.py", fix_request)
        for sent in self.glm.seen:  # проверяющему замечания программ не пересылаются, он смотрит только дифф
            self.assertNotIn("Static analysis found", sent[0]["content"])

    def test_loop_claude_glm_claude_glm_until_clean_then_analysis(self):
        roster = self.roster(glm=[FINDING, CLEAN, CLEAN],
                             sonnet=[edit("return 2  # BAD lint", "return 2  # BAD lint  # reviewed"), "fixed review",
                                     edit("  # BAD lint  # reviewed", ""), "fixed lint"])
        analysis, review, _ = self.run_pipeline(roster)
        # Claude -> GLM -> Claude (исправил и проверил) -> GLM -> чисто -> статанализ -> Claude -> GLM
        self.assertEqual(self.sequence(), ["glm", "sonnet", "glm", "sonnet", "glm"])
        self.assertEqual([len(r.findings) for r in review.rounds], [1, 0, 0])
        self.assertEqual(analysis.remaining, [])

    def test_no_second_review_when_static_analysis_changed_nothing(self):
        self.ws.edit_file("main.py", "return 2  # BAD lint", "return 2")  # анализатору нечего сказать
        roster = self.roster(glm=[CLEAN, CLEAN])
        analysis, review, _ = self.run_pipeline(roster)
        self.assertEqual(self.sequence(), ["glm"])
        self.assertEqual((analysis.fixed_rounds, len(review.rounds)), (0, 1))

    def test_without_a_reviewer_only_static_analysis_runs(self):
        roster = Roster([Worker("sonnet", Script("sonnet", [edit("return 2  # BAD lint", "return 2"), "ok"], self.order),
                                "mid", 2, 10, "mid")])
        analysis, review, _ = self.run_pipeline(roster)
        self.assertIsNone(review)
        self.assertEqual(analysis.remaining, [])

    def test_switches_work_independently(self):
        roster = self.roster(glm=[CLEAN])
        analysis, review, _ = self.run_pipeline(roster, analyze_on=False)
        self.assertIsNone(analysis)
        self.assertEqual(len(review.rounds), 1)
        roster = self.roster(glm=[CLEAN])
        analysis, review, _ = self.run_pipeline(roster, review_on=False)
        self.assertIsNone(review)
        self.assertEqual(self.glm.seen, [])

    def test_nothing_to_check_when_there_are_no_changes(self):
        ws = Workspace(self.root, log_enabled=False)
        roster = self.roster(glm=[CLEAN])
        self.assertEqual(pipeline.run("t", ws, roster, lambda p: roster.get("sonnet")), (None, None, None))

    def test_stop_request_ends_the_pipeline(self):
        roster = self.roster(glm=[CLEAN])
        analysis, review, _ = self.run_pipeline(roster, should_stop=lambda: True)
        self.assertEqual((analysis, review), (None, None))
        self.assertEqual(self.order, [])


class StallTest(Case):
    def test_analysis_that_will_not_go_away_is_reported_as_stalled_not_looped_forever(self):
        roster = self.roster(glm=[CLEAN], sonnet=[edit("return 2  # BAD lint", "return 2  # BAD lint ", "main.py"), "ok"] * 6)
        analysis, review, _ = self.run_pipeline(roster)
        self.assertTrue(analysis.stalled)
        self.assertEqual(analysis.remaining[0].tool, "fake")
