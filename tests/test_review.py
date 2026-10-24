import re
import tempfile
import unittest
from pathlib import Path

from aiedit.marks import Marks
from aiedit.policy import AccessDenied, Rules
from aiedit.providers import Provider
from aiedit.review import parse_findings, review_once, run_cycle
from aiedit.roster import Roster, Worker
from aiedit.workspace import Workspace

SERVER = "def login(user, password):\n    query = 'SELECT * FROM users WHERE name=' + user\n    return run(query)\n"
PAY = 'KEY = "sk-ant-api03-AbCdEf0123456789xyz"\n\n\ndef fee(amount):\n    return amount * 0.07  # HIDDEN_FORMULA\n'
HIGH = ("@@findings\nsummary: one injection\n@@finding f1\nseverity: high\nfile: server.py\nline: 2\n"
        "issue: SQL built by string concatenation\nfix: use parameters\n@@end")
CLEAN = "@@findings\nsummary: no issues found\n@@end"


def edit(path, old, new):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class Script(Provider):
    def __init__(self, name, replies):
        self.name, self.replies, self.seen = name, list(replies), []

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        r = self.replies.pop(0) if self.replies else CLEAN
        if isinstance(r, Exception):
            raise r
        return r

    def sent(self):
        return "\n".join(s + "\n" + "\n".join(m["content"] for m in ms) for s, ms in self.seen)

    def results(self):
        return "\n".join(m["content"] for _, ms in self.seen for m in ms if m["role"] == "user")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "server.py").write_text(SERVER)
        (self.root / "pay.py").write_text(PAY)
        (self.root / "secrets" / "k.py").write_text("TOP_SECRET = 1\n")
        m = Marks(); m.add("hidden", 3, 4); m.save(self.root / "pay.py", PAY)
        Rules(self.root).set_denied(self.root / "secrets", True)
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("server.py", "return run(query)", "return run(query)  # v1")  # «автор уже написал код»

    def tearDown(self):
        self.tmp.cleanup()

    def roster(self, reviewer=(), sonnet=(), opus=()):
        self.p = {"glm": Script("glm", reviewer), "sonnet": Script("sonnet", sonnet), "opus": Script("opus", opus)}
        return Roster([Worker("sonnet", self.p["sonnet"], "mid", 2, 10, "mid"),
                       Worker("opus", self.p["opus"], "strong", 4, 20, "strong", "backend"),
                       Worker("glm", self.p["glm"], "review", 0, 0, "mid", "security")])

    def cycle(self, roster, author="sonnet", **kw):
        return run_cycle("add login", self.ws, roster, lambda path: roster.get(author), **kw)


class ParseTest(Case):
    def test_good_and_messy_findings(self):
        text = ("Here you go:\n```\n" + HIGH.replace("@@end", "@@finding f2\nseverity: LOW\nfile: pay.py\nissue: tiny\n"
                "@@finding f3\nseverity: weird\nfile: server.py\nline: line 3\nissue: wrapped\n  second line\n@@end") + "\n```")
        summary, found, ok = parse_findings(text, self.ws)
        self.assertTrue(ok)
        self.assertEqual(summary, "one injection")
        self.assertEqual([(f.severity, f.file, f.line) for f in found],
                         [("high", "server.py", 2), ("low", "pay.py", None), ("medium", "server.py", 3)])
        self.assertEqual(found[2].issue, "wrapped second line")
        self.assertEqual([f.id for f in found], ["f1", "f2", "f3"])

    def test_findings_about_closed_or_foreign_files_and_empty_issues_are_dropped(self):
        text = ("@@findings\nsummary: x\n@@finding f1\nseverity: high\nfile: secrets/k.py\nissue: leaks\n"
                "@@finding f2\nseverity: high\nfile: ../etc/passwd\nissue: bad\n@@finding f3\nseverity: high\nfile: server.py\n"
                "@@finding f4\nseverity: high\nfile: .env\nissue: bad\n@@end")
        self.assertEqual(parse_findings(text, self.ws)[1], [])

    def test_no_block_is_unparsed_and_empty_block_is_clean(self):
        self.assertEqual(parse_findings("I think it is fine", self.ws), ("", [], False))
        self.assertEqual(parse_findings(CLEAN, self.ws), ("no issues found", [], True))


class ReviewOnceTest(Case):
    def test_reviewer_gets_diff_not_closed_code_and_cannot_edit(self):
        roster = self.roster(reviewer=[edit("server.py", "v1", "v2"), "@@tool read_file\npath: pay.py\n@@end", HIGH])
        rnd = review_once("add login", self.ws, roster.reviewer())
        self.assertEqual([f.file for f in rnd.findings], ["server.py"])
        first = self.p["glm"].seen[0][1][0]["content"]
        self.assertIn("Task the author was given: add login", first)
        self.assertIn("+    return run(query)  # v1", first)
        self.assertIn("read-only", self.p["glm"].seen[1][1][-1]["content"])
        for leak in ("HIDDEN_FORMULA", "sk-ant", "TOP_SECRET", "0.07"):
            self.assertNotIn(leak, self.p["glm"].sent())
        self.assertFalse(self.ws.read_only)  # режим снят после проверки
        self.assertIn("v1", self.ws.changes()[0].new)  # правка проверяющего не прошла

    def test_workspace_state_is_restored_even_if_provider_fails(self):
        roster = self.roster(reviewer=[RuntimeError("down")])
        self.ws.model = "before"
        with self.assertRaises(RuntimeError):
            review_once("t", self.ws, roster.reviewer())
        self.assertEqual((self.ws.read_only, self.ws.model), (False, "before"))


class CycleTest(Case):
    def test_clean_review_needs_no_fixes(self):
        roster = self.roster(reviewer=[CLEAN])
        rep = self.cycle(roster)
        self.assertEqual((len(rep.rounds), rep.remaining, rep.fixed_rounds), (1, [], 0))
        self.assertEqual(self.p["sonnet"].seen, [])
        self.assertEqual(rep.gaps, [])  # скрытый pay.py не менялся и не читался

    def test_finding_goes_back_to_the_author_then_rechecked_clean(self):
        roster = self.roster(
            reviewer=[HIGH, CLEAN],
            sonnet=[edit("server.py", "'SELECT * FROM users WHERE name=' + user", "'SELECT * FROM users WHERE name=?'"),
                    "Switched to a parameter."])
        rep = self.cycle(roster)
        self.assertEqual([len(r.findings) for r in rep.rounds], [1, 0])
        self.assertEqual(rep.fixed_rounds, 1)
        self.assertIn("name=?", self.ws.changes()[0].new)
        fix_prompt = self.p["sonnet"].seen[0][1][0]["content"]
        self.assertIn("Fix these review findings", fix_prompt)
        self.assertIn("- f1 [high] server.py:2: SQL built by string concatenation Suggested: use parameters", fix_prompt)
        recheck = self.p["glm"].seen[-1][1][0]["content"]
        self.assertIn("Check whether each one is fixed", recheck)
        self.assertIn("name=?", recheck)  # во второй проход попал уже исправленный дифф
        self.assertIn("sonnet: Switched to a parameter.", rep.rounds[0].fix_notes)

    def test_author_may_edit_only_files_from_the_findings(self):
        roster = self.roster(reviewer=[HIGH, CLEAN],
                             sonnet=[edit("pay.py", "amount", "money"), "gave up"])
        self.cycle(roster)
        self.assertIn("this step may only edit: server.py", self.p["sonnet"].seen[1][1][-1]["content"])
        self.assertNotIn("money", self.ws.changes()[-1].new)
        self.assertIsNone(self.ws.edit_scope)

    def test_low_findings_are_reported_but_not_sent_back(self):
        low = HIGH.replace("severity: high", "severity: low")
        roster = self.roster(reviewer=[low])
        rep = self.cycle(roster)
        self.assertEqual(len(rep.rounds), 1)
        self.assertEqual(rep.remaining[0].severity, "low")
        self.assertEqual(self.p["sonnet"].seen, [])

    def test_two_fix_rounds_at_most(self):
        roster = self.roster(reviewer=[HIGH] * 5,
                             sonnet=[edit("server.py", "# v1", "# v2"), "ok", edit("server.py", "# v2", "# v3"), "ok", "never"])
        rep = self.cycle(roster)
        self.assertEqual(len(rep.rounds), 3)  # проверка, исправление, проверка, исправление, проверка
        self.assertEqual(len(self.p["glm"].seen), 3)
        self.assertEqual(len(rep.remaining), 1)

    def test_author_who_changes_nothing_ends_the_dispute(self):
        roster = self.roster(reviewer=[HIGH, HIGH], sonnet=["This is a false positive: the query is built from a constant."])
        rep = self.cycle(roster)
        self.assertEqual(len(rep.rounds), 1)
        self.assertEqual(len(self.p["glm"].seen), 1)
        self.assertIn("false positive", rep.rounds[0].fix_notes[0])
        self.assertEqual(len(rep.remaining), 1)  # человеку решать

    def test_findings_are_split_between_authors_of_different_files(self):
        self.ws.edit_file("pay.py", "def fee(amount):", "def fee(amount, cur):")
        both = ("@@findings\nsummary: two\n@@finding f1\nseverity: high\nfile: server.py\nissue: a\n"
                "@@finding f2\nseverity: medium\nfile: pay.py\nissue: b\n@@end")
        roster = self.roster(reviewer=[both, CLEAN], sonnet=["done a"], opus=["done b"])
        authors = {self.root / "server.py": "sonnet", self.root / "pay.py": "opus"}
        rep = run_cycle("t", self.ws, roster, lambda p: roster.get(authors[p]))
        self.assertIn("server.py", self.p["sonnet"].seen[0][1][0]["content"])
        only = lambda prov: prov.seen[0][1][0]["content"].split("Findings:")[1].split("=== PROJECT FILES")[0]
        self.assertNotIn("pay.py", only(self.p["sonnet"]))
        self.assertIn("pay.py", only(self.p["opus"]))
        self.assertEqual(len(rep.rounds[0].fix_notes), 2)

    def test_nothing_to_do_cases(self):
        roster = self.roster(reviewer=[CLEAN])
        empty = Workspace(self.root, log_enabled=False)
        self.assertEqual(run_cycle("t", empty, roster, lambda p: roster.get("sonnet")).skipped, "no changes to review")
        no_reviewer = Roster([Worker("sonnet", Script("s", []), "mid", 2, 10, "mid")])
        self.assertEqual(run_cycle("t", self.ws, no_reviewer, lambda p: no_reviewer.get("sonnet")).skipped,
                         "no reviewer connected")

    def test_garbage_from_the_reviewer_is_flagged_not_fatal(self):
        roster = self.roster(reviewer=["Looks good to me!"])
        rep = self.cycle(roster)
        self.assertTrue(rep.unparsed)
        self.assertEqual(rep.remaining, [])
        self.assertEqual(self.p["sonnet"].seen, [])

    def test_stop_flag(self):
        roster = self.roster(reviewer=[HIGH])
        rep = self.cycle(roster, should_stop=lambda: True)
        self.assertEqual((rep.rounds, rep.stopped), ([], "остановлено пользователем"))

    def test_access_denied_aborts(self):
        roster = self.roster(reviewer=[AccessDenied("closed")])
        with self.assertRaises(AccessDenied):
            self.cycle(roster)

    def test_gaps_report_hidden_code_that_was_read_or_changed(self):
        roster = self.roster(reviewer=["@@tool read_file\npath: pay.py\n@@end", CLEAN])
        rep = self.cycle(roster)
        self.assertEqual([(n, h) for n, h, t in rep.gaps], [("pay.py", 3)])  # строка с ключом и две строки зоны
        self.assertNotIn("HIDDEN_FORMULA", self.p["glm"].sent())

    def test_everything_sent_to_models_is_english(self):
        roster = self.roster(reviewer=[HIGH, CLEAN], sonnet=["ok"])
        self.cycle(roster, english_reply=True)
        for prov in self.p.values():
            self.assertIsNone(re.search("[А-Яа-яЁё]", prov.sent()), prov.name)
        self.assertTrue(self.p["glm"].seen[0][0].endswith("Reply in English."))
        self.assertTrue(self.p["sonnet"].seen[0][0].endswith("Reply in English."))

    def test_progress_messages(self):
        seen = []
        roster = self.roster(reviewer=[HIGH, CLEAN], sonnet=[edit("server.py", "# v1", "# v2"), "ok"])
        self.cycle(roster, on_progress=seen.append)
        self.assertIn("безопасность: проверяет glm", seen)
        self.assertIn("безопасность: замечаний к исправлению: 1, возвращаю авторам", seen)
        self.assertIn("безопасность: проверяет glm (круг 1)", seen)


if __name__ == "__main__":
    unittest.main()
