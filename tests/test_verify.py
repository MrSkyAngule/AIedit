"""Ворота зелёных тестов: редактор сам запускает проверку и возвращает красное автору, пока не позеленеет."""
import tempfile
import unittest
from pathlib import Path

from aiedit import pipeline, sandbox, verify
from aiedit.providers import Provider
from aiedit.roster import Roster, Worker
from aiedit.workspace import Workspace

CLEAN = "@@findings\nsummary: no issues found\n@@end"


class Script(Provider):
    def __init__(self, name, replies):
        self.name, self.replies, self.seen = name, list(replies), []

    def complete(self, system, messages):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else "done"


def edit(old, new, path="main.py"):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "main.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
        (self.root / "test_main.py").write_text("import pytest\nfrom main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("main.py", "def add(a, b):", "def add(a, b):  # v1")  # «автор уже написал код»
        self.commands = []

    def tearDown(self):
        self.tmp.cleanup()

    def terminal(self, command):
        """Поддельная песочница: красная, пока в main.py есть BUG."""
        self.commands.append(command)
        st = self.ws.states[self.root / "main.py"]
        if "BUG" in st.text:
            return f"$ {command}\nexit code 1\nFAILED test_main.py::test_add - assert -1 == 3"
        return f"$ {command}\nexit code 0\n1 passed"

    def author(self, *replies):
        self.prov = Script("sonnet", replies)
        return Worker("sonnet", self.prov, "mid", 2, 10, "mid")


class DetectTest(Case):
    def test_configured_command_wins(self):
        self.assertEqual(verify.detect_command(self.ws, " make test "), ("make test", "настройки проекта"))

    def test_pytest_is_found_by_the_test_files(self):
        self.assertEqual(verify.detect_command(self.ws)[0], verify.PYTEST)

    def test_unittest_is_found_when_nothing_mentions_pytest(self):
        (self.root / "test_main.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        self.assertEqual(verify.detect_command(Workspace(self.root, log_enabled=False))[0], verify.UNITTEST)

    def test_without_tests_it_at_least_checks_that_the_code_compiles(self):
        (self.root / "test_main.py").unlink()
        cmd, why = verify.detect_command(Workspace(self.root, log_enabled=False))
        self.assertEqual(cmd, verify.COMPILE)
        self.assertIn("компилируется", why)

    def test_nothing_to_check_in_a_project_without_code(self):
        empty = Workspace(Path(tempfile.mkdtemp()).resolve(), log_enabled=False)
        self.assertEqual(verify.detect_command(empty)[0], "")

    def test_new_files_the_agent_has_not_applied_yet_count_too(self):
        ws = Workspace(Path(tempfile.mkdtemp()).resolve(), log_enabled=False)
        ws.create_file("test_x.py", "def test_a():\n    assert True\n")
        self.assertEqual(verify.detect_command(ws)[0], verify.PYTEST)


class LoopTest(Case):
    def run_loop(self, author, terminal=None, **kw):
        return verify.loop("add numbers", self.ws, author, terminal or self.terminal, verify.PYTEST, **kw)

    def test_red_goes_back_to_the_author_and_the_loop_ends_green(self):
        rep = self.run_loop(self.author(edit("return a - b  # BUG", "return a + b"), "fixed the sign"))
        self.assertEqual((rep.green, rep.runs, rep.fixes, rep.stalled), (True, 2, 1, ""))
        self.assertIn("a + b", self.ws.states[self.root / "main.py"].text)
        asked = self.prov.seen[0][0]["content"]
        self.assertIn("it FAILED", asked)
        self.assertIn("assert -1 == 3", asked)  # автор получил настоящий вывод запуска
        self.assertIn("Do NOT delete a test or weaken an assertion", asked)

    def test_green_from_the_start_costs_one_run_and_no_author_call(self):
        self.ws.edit_file("main.py", "return a - b  # BUG", "return a + b")
        rep = self.run_loop(self.author())
        self.assertEqual((rep.green, rep.runs, rep.fixes), (True, 1, 0))
        self.assertEqual(self.prov.seen, [])

    def test_same_failure_again_stops_the_loop_with_a_reason(self):
        rep = self.run_loop(self.author(edit("# v1", "# v2"), "tried", edit("# v2", "# v3"), "tried again", "never"))
        self.assertEqual(rep.green, False)
        self.assertIn("та же ошибка", rep.stalled)
        self.assertEqual(rep.runs, 2)

    def test_author_who_changes_nothing_ends_the_loop(self):
        rep = self.run_loop(self.author("I cannot fix this."))
        self.assertEqual((rep.green, rep.runs), (False, 1))
        self.assertIn("ничего не изменил", rep.stalled)

    def test_cap_on_rounds_when_every_failure_is_a_new_one(self):
        words = iter("alpha beta gamma delta epsilon zeta eta theta".split())
        replies = []
        for i in range(8):
            replies += [edit(f"# v{i + 1}", f"# v{i + 2}"), "ok"]

        def terminal(command):
            self.commands.append(command)
            return f"$ {command}\nexit code 1\nFAILED because of {next(words)}"
        rep = self.run_loop(self.author(*replies), terminal, rounds=3)
        self.assertEqual((rep.green, rep.runs, rep.fixes), (False, 4, 3))
        self.assertIn("предел кругов", rep.stalled)

    def test_declined_or_unavailable_terminal_means_not_run_not_red(self):
        rep = self.run_loop(self.author(), lambda c: sandbox.OUT_DECLINED)
        self.assertEqual((rep.green, rep.runs), (None, 1))
        self.assertIn("declined", rep.skipped)

    def test_deleted_tests_are_noticed(self):
        (self.root / "test_main.py").write_text("def test_a():\n    assert 1\n    assert 2\n    assert 3\n    assert 4\n    assert 5\n")
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("main.py", "def add(a, b):", "def add(a, b):  # v1")
        rep = self.run_loop(self.author(
            "@@tool edit_file\npath: test_main.py\n@@old\n    assert 2\n    assert 3\n    assert 4\n    assert 5\n@@new\n@@end", "removed checks"),
            terminal=lambda c: f"$ {c}\nexit code {1 if 'assert 2' in self.ws.states[self.root / 'test_main.py'].text else 0}\nout")
        self.assertTrue(any("уменьшилось" in n for n in rep.notes))

    def test_stop_request_is_respected(self):
        rep = self.run_loop(self.author(), should_stop=lambda: True)
        self.assertEqual(rep.runs, 0)


class PipelineTest(Case):
    def roster(self, glm, sonnet):
        self.glm = Script("glm", glm)
        self.sonnet = Script("sonnet", sonnet)
        self.order = []
        return Roster([Worker("sonnet", self.sonnet, "mid", 2, 10, "mid"), Worker("glm", self.glm, "review", 0, 0, "mid", "security")])

    def go(self, roster, **kw):
        return pipeline.run("add numbers", self.ws, roster, lambda p: roster.get("sonnet"), terminal=self.terminal,
                            analyze_on=False, **kw)

    def test_tests_go_green_before_glm_looks_at_the_code(self):
        roster = self.roster([CLEAN], [edit("return a - b  # BUG", "return a + b"), "fixed"])
        _, review, ver = self.go(roster)
        self.assertEqual((ver.green, ver.fixes), (True, 1))
        first_review_diff = self.glm.seen[0][0]["content"]
        self.assertIn("a + b", first_review_diff)       # GLM получил уже исправленный, зелёный код
        self.assertNotIn("+    return a - b", first_review_diff)  # багованной строки среди добавленных уже нет
        self.assertNotIn("PROJECT ENVIRONMENT", first_review_diff)  # проверяющему окружение не нужно
        self.assertEqual(len(review.rounds), 1)

    def test_tests_run_again_when_glm_made_the_author_change_the_code(self):
        self.ws.edit_file("main.py", "return a - b  # BUG", "return a + b")  # зелёный с самого начала
        finding = ("@@findings\nsummary: x\n@@finding f1\nseverity: high\nfile: main.py\nline: 2\nissue: no input validation\n@@end")
        roster = self.roster([finding, CLEAN], [edit("# v1", "# v2"), "added a note"])
        _, review, ver = self.go(roster)
        self.assertEqual(review.fixed_rounds, 1)
        self.assertEqual(ver.runs, 2)  # до GLM и после его исправления
        self.assertTrue(ver.green)

    def test_no_second_run_when_nothing_changed_since_the_last_green(self):
        self.ws.edit_file("main.py", "return a - b  # BUG", "return a + b")
        roster = self.roster([CLEAN], [])
        _, _, ver = self.go(roster)
        self.assertEqual(ver.runs, 1)

    def test_switch_off_and_no_terminal_mean_no_verification(self):
        roster = self.roster([CLEAN], [])
        self.assertIsNone(self.go(roster, verify_on=False)[2])
        self.assertIsNone(pipeline.run("t", self.ws, roster, lambda p: roster.get("sonnet"), analyze_on=False)[2])
        self.assertEqual(self.commands, [])

    def test_red_result_is_reported_not_hidden(self):
        roster = self.roster([CLEAN], ["I cannot fix this."])
        _, _, ver = self.go(roster)
        self.assertFalse(ver.green)
        self.assertIn("FAILED", ver.output)
        self.assertTrue(ver.stalled)
