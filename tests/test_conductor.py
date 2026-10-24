import re
import tempfile
import unittest
from pathlib import Path

from aiedit import conductor
from aiedit.conductor import make_plan, ordered, parse_plan
from aiedit.guard import Region
from aiedit.marks import Marks
from aiedit.orchestrator import Turn
from aiedit.policy import Rules
from aiedit.providers import DemoProvider, Provider
from aiedit.roster import Roster, Worker
from aiedit.workspace import Workspace

GOOD = """@@plan
summary: Add validation and tests
@@task t1
worker: sonnet
goal: Add an amount > 0 check to charge() in pkg/pay.py.
files: pkg/pay.py
needs:
risk: medium
@@task t2
worker: haiku
goal: Write unit tests for the new check.
files: tests/test_pay.py
needs: t1
risk: low
@@end"""


def three_workers() -> Roster:
    """Разбор плана проверяем на реестре из трёх исполнителей: логика не зависит от того, что в реестре по умолчанию."""
    return Roster([Worker("haiku", DemoProvider("d-haiku"), "cheap tasks", 0.1, 0.5, "cheap"),
                   Worker("sonnet", DemoProvider("d-sonnet"), "ordinary tasks", 2.0, 10.0, "mid"),
                   Worker("opus", DemoProvider("d-opus"), "hard tasks", 4.0, 20.0, "strong")])


class Script(Provider):
    name = "conductor-script"

    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        return self.reply


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("pkg", "secrets", "tests"):
            (self.root / d).mkdir(parents=True)
        for n, t in {"main.py": "x = 1\n", "pkg/pay.py": "def charge():\n    return 1\n\nKEY = 'k'\n",
                     "secrets/k.py": "K = 1\n"}.items():
            (self.root / n).write_text(t)
        m = Marks(); m.add("readonly", 0, 1); m.add("hidden", 3, 3); m.save(self.root / "pkg" / "pay.py", "def charge():\n    return 1\n\nKEY = 'k'\n")
        Rules(self.root).set_denied(self.root / "secrets", True)
        self.ws = Workspace(self.root)
        self.roster = three_workers()

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, text, protected=False):
        return parse_plan(text, self.ws, self.roster, "do it", protected)


class ParseTest(Case):
    def test_good_plan(self):
        plan = self.parse(GOOD)
        self.assertFalse(plan.fallback)
        self.assertEqual(plan.summary, "Add validation and tests")
        self.assertEqual([(t.id, t.worker, t.risk, t.files, t.needs) for t in plan.tasks],
                         [("t1", "sonnet", "medium", ["pkg/pay.py"], []), ("t2", "haiku", "low", ["tests/test_pay.py"], ["t1"])])
        self.assertEqual(plan.notes, [])

    def test_tolerates_prose_markdown_wrapping_and_wrapped_goal(self):
        text = "Sure! Here is the plan:\n```\n" + GOOD.replace("goal: Add an amount", "goal: Add an\n  amount") + "\n```\nDone."
        plan = self.parse(text)
        self.assertEqual(len(plan.tasks), 2)
        self.assertEqual(plan.tasks[0].goal, "Add an amount > 0 check to charge() in pkg/pay.py.")

    def test_missing_end_marker_still_parses(self):
        plan = self.parse(GOOD.replace("@@end", ""))
        self.assertEqual(len(plan.tasks), 2)

    def test_unknown_worker_replaced_by_risk(self):
        text = "@@plan\nsummary: s\n@@task t1\nworker: gpt-9\ngoal: do it\nrisk: high\n@@task t2\nworker:\ngoal: other\nrisk: low\n@@end"
        plan = self.parse(text)
        self.assertEqual([t.worker for t in plan.tasks], ["opus", "haiku"])
        self.assertEqual(len(plan.notes), 2)

    def test_worker_names_are_case_insensitive(self):
        plan = self.parse("@@plan\n@@task t1\nworker: HAIKU\ngoal: x\n@@end")
        self.assertEqual(plan.tasks[0].worker, "haiku")

    def test_paths_outside_project_or_closed_are_dropped(self):
        text = ("@@plan\n@@task t1\nworker: haiku\ngoal: x\nfiles: pkg/pay.py, ../etc/passwd, secrets/k.py, "
                "/etc/hosts, .env, new/file.py\n@@end")
        self.assertEqual(self.parse(text).tasks[0].files, ["pkg/pay.py", "new/file.py"])

    def test_bad_dependencies_removed_and_cycles_broken(self):
        text = ("@@plan\n@@task t1\nworker: haiku\ngoal: a\nneeds: t9, t1\n@@task t2\nworker: haiku\ngoal: b\nneeds: t3\n"
                "@@task t3\nworker: haiku\ngoal: c\nneeds: t2\n@@end")
        plan = self.parse(text)
        self.assertEqual([t.needs for t in plan.tasks], [[], [], []])
        self.assertIn("circular dependencies dropped", plan.notes)

    def test_task_limit_and_ids_are_renumbered(self):
        text = "@@plan\n" + "".join(f"@@task x{i}\nworker: haiku\ngoal: step {i}\n" for i in range(10)) + "@@end"
        plan = self.parse(text)
        self.assertEqual([t.id for t in plan.tasks], [f"t{i}" for i in range(1, conductor.MAX_TASKS + 1)])

    def test_task_without_goal_is_skipped_and_bad_risk_defaults(self):
        plan = self.parse("@@plan\n@@task t1\nworker: haiku\n@@task t2\nworker: haiku\ngoal: g\nrisk: scary\n@@end")
        self.assertEqual([(t.id, t.risk) for t in plan.tasks], [("t1", "medium")])

    def test_garbage_gives_one_step_fallback(self):
        for junk in ("", "I cannot do that", "@@plan\n@@end", "{'tasks': []}"):
            plan = self.parse(junk)
            self.assertTrue(plan.fallback, junk)
            self.assertEqual((len(plan.tasks), plan.tasks[0].goal, plan.tasks[0].worker), (1, "do it", "sonnet"))
        self.assertEqual(self.parse("junk", protected=True).tasks[0].worker, "opus")

    def test_long_goal_is_capped(self):
        plan = self.parse("@@plan\n@@task t1\nworker: haiku\ngoal: " + "x" * 5000 + "\n@@end")
        self.assertEqual(len(plan.tasks[0].goal), conductor.MAX_GOAL)


class OrderTest(Case):
    def test_dependencies_first_then_plan_order(self):
        text = ("@@plan\n@@task t1\nworker: haiku\ngoal: a\nneeds: t3\n@@task t2\nworker: haiku\ngoal: b\n"
                "@@task t3\nworker: haiku\ngoal: c\n@@end")
        self.assertEqual([t.id for t in ordered(self.parse(text))], ["t2", "t3", "t1"])


class MakePlanTest(Case):
    def test_request_contains_everything_the_conductor_needs_and_nothing_closed(self):
        prov = Script(GOOD)
        plan = make_plan("add a check", self.ws, self.roster, prov, open_file=self.root / "pkg" / "pay.py")
        system, messages = prov.seen[0]
        user = messages[-1]["content"]
        self.assertEqual(system, conductor.CONDUCTOR_SYSTEM)
        for part in ("REQUEST:\nadd a check", "WORKERS:", "- haiku", "- opus", "PROTECTED (zones):",
                     "pkg/pay.py: hidden x1, readonly x1", "PROJECT FILES (2 levels):", "OPEN FILE: pkg/pay.py"):
            self.assertIn(part, user)
        self.assertNotIn("secrets", user)
        self.assertEqual(len(plan.tasks), 2)

    def test_history_goes_in_english_and_system_is_english(self):
        prov = Script(GOOD)
        hist = [Turn("user", "привет", None, "hello"), Turn("assistant", "Привет", None, "Hi")]
        make_plan("t", self.ws, self.roster, prov, history=hist)
        self.assertEqual([m["content"] for m in prov.seen[0][1][:2]], ["hello", "Hi"])
        self.assertIsNone(re.search("[А-Яа-я]", conductor.CONDUCTOR_SYSTEM + prov.seen[0][1][-1]["content"]))

    def test_protected_files_make_fallback_use_strongest(self):
        plan = make_plan("t", self.ws, self.roster, Script("nonsense"))
        self.assertTrue(plan.fallback)
        self.assertEqual(plan.tasks[0].worker, "opus")

    def test_live_tab_zones_count_as_protected(self):
        live = {str(self.root / "main.py"): ("x = 1\n", [Region("confirm", 0, 0)])}
        self.assertIn("main.py: confirm x1", Workspace(self.root, live=live).protected_overview())

    def test_call_is_logged_as_conductor_without_content(self):
        from aiedit import sendlog
        make_plan("secret task text", self.ws, self.roster, Script(GOOD))
        entries = sendlog.read(self.root)
        self.assertIn("conductor", [e["mode"] for e in entries])
        self.assertNotIn("secret task text", (self.root / sendlog.LOG_NAME).read_text(encoding="utf-8"))

    def test_system_prompt_is_short(self):
        self.assertLess(len(conductor.CONDUCTOR_SYSTEM), 1800)


if __name__ == "__main__":
    unittest.main()
