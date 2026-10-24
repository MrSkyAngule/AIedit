import re
import tempfile
import unittest
from pathlib import Path

from aiedit import conductor, sendlog
from aiedit.policy import AccessDenied, Rules
from aiedit.providers import Provider
from aiedit.roster import Roster, Worker
from tests.test_review import CLEAN, HIGH
from aiedit.workspace import Workspace

PLAN = """@@plan
summary: Validate and test
@@task t1
worker: sonnet
goal: Add an amount check to charge().
files: pkg/pay.py
risk: medium
@@task t2
worker: haiku
goal: Report what charge() looks like now.
needs: t1
risk: low
@@task t3
worker: haiku
goal: Say hello.
risk: low
@@end"""


class Script(Provider):
    def __init__(self, name, replies):
        self.name, self.replies, self.seen = name, list(replies), []

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        r = self.replies.pop(0) if self.replies else "ok"
        if isinstance(r, Exception):
            raise r
        return r

    def sent(self):
        return "\n".join(s + "\n" + "\n".join(m["content"] for m in ms) for s, ms in self.seen)


def edit(path, old, new):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "pkg" / "pay.py").write_text("def charge(amount):\n    return amount * 100\n")
        Rules(self.root).set_denied(self.root / "pkg" / "none", True)
        self.ws = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def roster(self, sonnet=(), haiku=(), opus=()):
        self.p = {k: Script(f"model-{k}", r) for k, r in (("haiku", haiku), ("sonnet", sonnet), ("opus", opus))}
        return Roster([Worker(k, self.p[k], "x", price, price, tier)
                       for k, price, tier in (("haiku", 1, "cheap"), ("sonnet", 2, "mid"), ("opus", 4, "strong"))])

    def conduct(self, plan, roster, task="add a check", **kw):
        kw.setdefault("analyze_on", False)  # настоящие анализаторы в этих тестах не нужны
        cond = Script("conductor", [plan])
        res = conductor.run(task, self.ws, roster, cond, **kw)
        return res, cond


class RunTest(RunCase):
    def test_steps_go_to_assigned_workers_in_dependency_order(self):
        roster = self.roster(sonnet=[edit("pkg/pay.py", "return amount * 100", "return amount * 100 if amount > 0 else 0"),
                                     "Added the check."],
                             haiku=["Now charge() has a guard.", "Hello!"])
        res, cond = self.conduct(PLAN, roster)
        self.assertEqual([(r.task.id, r.status) for r in res.results], [("t1", "done"), ("t2", "done"), ("t3", "done")])
        self.assertEqual(len(self.p["sonnet"].seen), 2)  # edit + итог
        self.assertEqual(len(self.p["haiku"].seen), 2)
        self.assertEqual(len(self.p["opus"].seen), 0)
        first_sonnet = self.p["sonnet"].seen[0][1][0]["content"]
        for part in ("Add an amount check", "Files: pkg/pay.py", "Overall request: add a check", "step 1 of 3"):
            self.assertIn(part, first_sonnet)
        t2 = self.p["haiku"].seen[0][1][0]["content"]
        self.assertIn("Done so far:\n- t1 (sonnet): Added the check.", t2)

    def test_later_step_sees_earlier_edit_on_shared_workspace(self):
        roster = self.roster(sonnet=[edit("pkg/pay.py", "return amount * 100", "return amount * 200"), "done"],
                             haiku=["@@tool read_file\npath: pkg/pay.py\n@@end", "saw it", "hello"])
        res, _ = self.conduct(PLAN, roster)
        read_result = self.p["haiku"].seen[1][1][-1]["content"]
        self.assertIn("amount * 200", read_result)
        self.assertEqual(len(res.changes), 1)
        self.assertIn("amount * 200", res.changes[0].new)
        self.assertIn("amount * 100", (self.root / "pkg" / "pay.py").read_text())  # диск не тронут

    def test_failed_step_skips_dependents_but_independent_steps_run(self):
        roster = self.roster(sonnet=[RuntimeError("provider down")], haiku=["hello"])
        res, _ = self.conduct(PLAN, roster)
        self.assertEqual({r.task.id: r.status for r in res.results}, {"t1": "failed", "t2": "skipped", "t3": "done"})
        self.assertIn("FAILED: RuntimeError: provider down", res.text)
        self.assertIn("SKIPPED", res.text)
        self.assertEqual(len(self.p["haiku"].seen), 1)

    def test_access_denied_in_a_worker_aborts_everything(self):
        roster = self.roster(sonnet=[AccessDenied("closed")], haiku=["hello"])
        with self.assertRaises(AccessDenied):
            self.conduct(PLAN, roster)

    def test_stop_flag_leaves_remaining_steps_stopped(self):
        roster = self.roster(sonnet=["ok"], haiku=["ok"])
        flags = iter([False, False, True, True, True, True, True])
        res, _ = self.conduct(PLAN, roster, should_stop=lambda: next(flags, True))
        self.assertTrue(res.stopped)
        self.assertIn("stopped", {r.status for r in res.results})
        self.assertIn("STOPPED", res.text)

    def test_total_step_budget(self):
        roster = self.roster(sonnet=["@@tool list_files\n@@end"] * 50, haiku=["@@tool list_files\n@@end"] * 50)
        old = conductor.MAX_TOTAL_STEPS
        conductor.MAX_TOTAL_STEPS = 6
        try:
            res, _ = self.conduct(PLAN, roster)
        finally:
            conductor.MAX_TOTAL_STEPS = old
        self.assertLessEqual(res.steps, 6)
        self.assertIn("общий лимит", res.stopped)

    def test_single_step_plan_returns_only_that_text(self):
        plan = "@@plan\nsummary: s\n@@task t1\nworker: haiku\ngoal: say hi\n@@end"
        res, _ = self.conduct(plan, self.roster(haiku=["Hi there"]))
        self.assertEqual(res.text, "Hi there")

    def test_unreadable_plan_runs_request_as_one_step(self):
        roster = self.roster(sonnet=["handled"])
        res, _ = self.conduct("sorry, no plan", roster, task="do the thing")
        self.assertTrue(res.plan.fallback)
        self.assertEqual(res.text, "handled")
        self.assertIn("do the thing", self.p["sonnet"].seen[0][1][0]["content"])

    def test_progress_and_calls_are_prefixed_by_step(self):
        seen = []
        roster = self.roster(sonnet=["@@tool list_files\n@@end", "ok"], haiku=["ok", "ok"])
        res, _ = self.conduct(PLAN, roster, on_progress=seen.append)
        self.assertEqual(seen[0], "дирижёр составляет план...")
        self.assertIn("t1 → sonnet: Add an amount check to charge().", seen)
        self.assertIn("t1: смотрю список файлов: .", seen)
        self.assertIn("t1: смотрю список файлов: .", res.calls)

    def test_log_shows_which_model_got_each_call(self):
        roster = self.roster(sonnet=["ok"], haiku=["ok", "ok"])
        self.conduct(PLAN, roster)
        models = [(e["mode"], e["model"]) for e in sendlog.read(self.root) if e["mode"] in ("agent", "conductor")]
        self.assertEqual(models[0], ("conductor", "conductor"))
        self.assertEqual({m for k, m in models if k == "agent"}, {"model-sonnet", "model-haiku"})

    def test_everything_is_english_and_reply_rule_follows_translation(self):
        roster = self.roster(sonnet=["ok"], haiku=["ok", "ok"])
        res, cond = self.conduct(PLAN, roster, english_reply=True)
        for prov in (cond, *self.p.values()):
            self.assertIsNone(re.search("[А-Яа-яЁё]", prov.sent()), prov.name)
        self.assertTrue(self.p["sonnet"].seen[0][0].endswith("Reply in English."))
        roster = self.roster(sonnet=["ok"], haiku=["ok", "ok"])
        self.conduct(PLAN, roster, english_reply=False)
        self.assertTrue(self.p["sonnet"].seen[0][0].endswith("language of the user's task."))

    def test_workers_never_get_history_only_the_conductor_does(self):
        from aiedit.orchestrator import Turn
        roster = self.roster(sonnet=["ok"], haiku=["ok", "ok"])
        res, cond = self.conduct(PLAN, roster, history=[Turn("user", "old question"), Turn("assistant", "old answer")])
        self.assertIn("old question", cond.sent())
        for prov in self.p.values():
            self.assertNotIn("old question", prov.sent())

    def test_conductor_and_workers_only_see_open_files(self):
        Rules(self.root).set_denied(self.root / "pkg", True)
        roster = self.roster(sonnet=["@@tool read_file\npath: pkg/pay.py\n@@end", "none"], haiku=["ok", "ok"])
        res, cond = self.conduct(PLAN, roster)
        self.assertNotIn("amount * 100", cond.sent() + self.p["sonnet"].sent())


class ReviewIntegrationTest(RunCase):
    PLAN2 = ("@@plan\nsummary: two files\n@@task t1\nworker: sonnet\ngoal: Edit pay.\nrisk: medium\n"
             "@@task t2\nworker: opus\ngoal: Edit server.\nrisk: high\n@@end")

    def setUp(self):
        super().setUp()
        (self.root / "server.py").write_text("def login(user):\n    q = 'SELECT ' + user\n    return run(q)\n")

    def reviewed_roster(self, reviewer=(), sonnet=(), opus=()):
        self.p = {"glm": Script("glm", reviewer), "sonnet": Script("model-sonnet", sonnet), "opus": Script("model-opus", opus)}
        return Roster([Worker("sonnet", self.p["sonnet"], "mid", 2, 2, "mid"),
                       Worker("opus", self.p["opus"], "strong", 4, 4, "strong", "backend"),
                       Worker("glm", self.p["glm"], "review", 0, 0, "mid", "security")])

    def test_review_runs_after_the_steps_and_goes_to_the_author_of_the_file(self):
        roster = self.reviewed_roster(
            reviewer=[HIGH, CLEAN],
            sonnet=[edit("pkg/pay.py", "return amount * 100", "return amount * 101"), "pay done"],
            opus=[edit("server.py", "return run(q)", "return run(q)  # checked"), "server done",
                  edit("server.py", "'SELECT ' + user", "'SELECT ?'"), "fixed"])
        res, _ = self.conduct(self.PLAN2, roster)
        self.assertIsNotNone(res.review)
        self.assertEqual([len(r.findings) for r in res.review.rounds], [1, 0])
        self.assertEqual(len(self.p["sonnet"].seen), 2)  # свой шаг; замечание про server.py ему не ушло
        self.assertEqual(len(self.p["opus"].seen), 4)  # шаг + исправление
        self.assertIn("SELECT ?", [c for c in res.changes if c.path.name == "server.py"][0].new)
        self.assertIn("Fix these review findings", self.p["opus"].seen[2][1][0]["content"])
        diff = self.p["glm"].seen[0][1][0]["content"]
        self.assertIn("pay.py", diff)
        self.assertIn("server.py", diff)

    def test_review_off_stopped_or_no_changes_means_no_review(self):
        roster = self.reviewed_roster(reviewer=[CLEAN], sonnet=[edit("pkg/pay.py", "100", "101"), "ok"], opus=["ok"])
        res, _ = self.conduct(self.PLAN2, roster, review_on=False)
        self.assertIsNone(res.review)
        self.assertEqual(self.p["glm"].seen, [])
        self.ws = Workspace(self.root)  # новый сценарий - чистая рабочая область
        roster = self.reviewed_roster(reviewer=[CLEAN], sonnet=["just talk"], opus=["just talk"])
        res, _ = self.conduct(self.PLAN2, roster)
        self.assertIsNone(res.review)  # правок нет - проверять нечего
        self.ws = Workspace(self.root)
        roster = self.reviewed_roster(reviewer=[CLEAN], sonnet=[edit("pkg/pay.py", "100", "101"), "ok"], opus=["ok"])
        res, _ = self.conduct(self.PLAN2, roster, should_stop=lambda: True)
        self.assertIsNone(res.review)

    def test_reviewer_is_never_given_a_plan_step(self):
        roster = self.reviewed_roster(reviewer=[CLEAN], sonnet=["ok", "ok"], opus=["ok"])
        plan = self.PLAN2.replace("worker: opus", "worker: glm")
        self.conduct(plan, roster)
        self.assertEqual(len(self.p["glm"].seen), 0)  # шаг ушёл не проверяющему
        self.assertEqual((len(self.p["sonnet"].seen), len(self.p["opus"].seen)), (1, 1))  # риск high - сильному

    def test_direct_agent_flow_reviews_with_the_mid_worker_as_author(self):
        from aiedit import flows
        roster = self.reviewed_roster(reviewer=[HIGH, CLEAN],
                                      sonnet=[edit("server.py", "return run(q)", "return run(q)  # v1"), "done",
                                              edit("server.py", "'SELECT ' + user", "'SELECT ?'"), "fixed"])
        res = flows.run_agent("add login", self.ws, self.p["sonnet"], roster=roster, review_on=True)
        self.assertEqual([len(r.findings) for r in res.review.rounds], [1, 0])
        self.assertIn("SELECT ?", res.changes[0].new)
        direct_off = flows.run_agent("t", self.ws, Script("m", ["nothing"]), roster=roster, review_on=False)
        self.assertIsNone(direct_off.review)


if __name__ == "__main__":
    unittest.main()
