"""Всё, что редактор сам пишет модели, должно быть по-английски (меньше токенов).
Задачи и файлы здесь на латинице, поэтому любая кириллица в запросе - протёкший русский текст."""
import re
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, orchestrator, prompts
from aiedit.guard import Region
from aiedit.marks import Marks
from aiedit.orchestrator import FileInput, Turn
from aiedit.policy import Rules
from aiedit.providers import Provider
from aiedit.workspace import Workspace

CYRILLIC = re.compile("[А-Яа-яЁё]")
CODE = ('API = "sk-ant-api03-AbCdEf0123456789xyz"\n\n\ndef charge(u, amount):\n    return amount * 100\n\n\n'
        "def refund(u, amount):\n    return -amount\n\n\ndef free():\n    return 1\n\n\ndef tail():\n    return 0\n")


class Script(Provider):
    name = "script"

    def __init__(self, replies=()):
        self.replies, self.sent = list(replies), []

    def complete(self, system, messages):
        self.sent.append(system)
        self.sent += [m["content"] for m in messages]
        return self.replies.pop(0) if self.replies else "ok\n```\nx = 1\n```"

    def assert_english(self, case):
        for i, text in enumerate(self.sent):
            found = CYRILLIC.search(text)
            case.assertIsNone(found, f"cyrillic at item {i}: ...{text[max(found.start() - 40, 0):found.end() + 40]!r}" if found else "")


class EnglishCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "secrets").mkdir()
        for name, text in {"main.py": "from pkg import pay\n", "pkg/pay.py": CODE, "secrets/k.py": "K = 1\n",
                           ".env": "A_SECRET=zzzzzzzz\n", "big.txt": "line\n" * 5000}.items():
            (self.root / name).write_text(text)
        m = Marks()
        m.add("interface", 3, 4); m.add("readonly", 7, 8); m.add("confirm", 11, 12)
        m.save(self.root / "pkg" / "pay.py", CODE)
        Rules(self.root).set_denied(self.root / "secrets", True)

    def tearDown(self):
        self.tmp.cleanup()


class AgentEnglishTest(EnglishCase):
    def test_every_kind_of_tool_result_and_error_is_english(self):
        bad_edits = [
            "@@tool bogus\n@@end",
            "@@tool read_file\nnonsense line\n@@end",
            "@@tool read_file\nstart_line: abc\n@@end",
            "@@tool read_file\n@@old\nx\n@@end",
            "@@tool read_file\n@@end",
            "@@tool read_file\npath: secrets/k.py\n@@end",
            "@@tool read_file\npath: ../etc/passwd\n@@end",
            "@@tool read_file\npath: .env\n@@end",
            "@@tool list_files\npath: secrets\n@@end",
            "@@tool search\nquery:\n@@end",
            "@@tool search\nquery: refund\n@@end",
            "@@tool search\nquery: zzzz_none\n@@end",
            "@@tool read_file\npath: big.txt\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nreturn\n@@new\nx\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nnot there\n@@new\nx\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nreturn -amount\n@@new\nreturn 0\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nreturn 1\n@@new\nreturn 2\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nreturn 0\n@@new\nreturn 0\n@@end",
            "@@tool edit_file\npath: nope.py\n@@old\na\n@@new\nb\n@@end",
            "@@tool create_file\npath: .env\n@@content\nx\n@@end",
            "@@tool create_file\npath: main.py\n@@content\nx\n@@end",
            "@@tool create_file\npath: ok/new.py\n@@content\nx\n@@end",
            "@@tool list_files\n@@end", "ok",
        ]
        prov = Script(bad_edits)
        agent.run("do the thing", Workspace(self.root), prov, history=[Turn("user", "q"), Turn("assistant", "a")])
        results = "\n".join(prov.sent)
        for expected in ("unknown tool", "ERROR: file not found", "ERROR: old_text not found in the file", "edit accepted",
                         "needs user confirmation", "edit rejected by protection", "file will be created",
                         "lines 1-400 of 5001", "Use start_line=401", "ERROR: cannot create a file here"):
            self.assertIn(expected, results, expected)
        prov.assert_english(self)

    def test_system_prompts_are_english_and_ask_for_user_language(self):
        for text in (prompts.ASK_SYSTEM, prompts.EXPLAIN_SYSTEM, prompts.AGENT_SYSTEM):
            self.assertIsNone(CYRILLIC.search(text))
            self.assertIn("language of the user's task", text)

    def test_user_task_goes_through_verbatim(self):
        prov = Script(["done"])
        agent.run("добавь проверку", Workspace(self.root), prov)
        self.assertTrue(prov.sent[1].startswith("добавь проверку"))
        self.assertIsNone(CYRILLIC.search(prov.sent[0]))  # system
        self.assertIsNone(CYRILLIC.search(prov.sent[1].partition("\n\n")[2]))  # всё после задачи


class SingleFileEnglishTest(EnglishCase):
    def regions(self):
        return [Region("interface", 3, 4), Region("readonly", 7, 8), Region("confirm", 11, 12)]

    def test_ask_with_zones_context_and_truncation(self):
        prov = Script()
        ctx = FileInput(self.root / "big.txt", "x = 1\n" * 20000, [])
        orchestrator.ask(CODE, "refactor", self.regions(), {"cheap": prov, "strong": prov},
                         source=self.root / "pkg" / "pay.py", context=[ctx],
                         history=[Turn("user", "q"), Turn("assistant", "a")])
        text = "\n".join(prov.sent)
        for expected in ("readonly: lines", "confirm: lines", "=== CONTEXT (read-only) ===", "[... truncated ...]", "=== ZONES ==="):
            self.assertIn(expected, text)
        prov.assert_english(self)

    def test_ask_without_zones_says_none(self):
        prov = Script()
        orchestrator.ask("x = 1\n", "t", [], {"cheap": prov, "strong": prov})
        self.assertIn(f"=== ZONES ===\n{prompts.NONE}\n", "\n".join(prov.sent))
        prov.assert_english(self)

    def test_explain_is_english(self):
        prov = Script(["explained"])
        orchestrator.explain(CODE, [Region("explain", 7, 8)], {"cheap": prov, "strong": prov},
                             history=[Turn("user", "q"), Turn("assistant", "a")])
        self.assertIn("Zone 1: lines", "\n".join(prov.sent))
        prov.assert_english(self)

    def test_verdict_has_english_twins(self):
        v = orchestrator.guard.check_edit(CODE, CODE.replace("return -amount", "return 0").replace("return 1", "return 3"),
                                          self.regions())
        self.assertEqual(len(v.violations), len(v.violations_en))
        self.assertEqual(len(v.needs_confirm), len(v.needs_confirm_en))
        self.assertIsNone(CYRILLIC.search(" ".join(v.violations_en + v.needs_confirm_en)))
        self.assertTrue(CYRILLIC.search(" ".join(v.violations)))  # человеку - по-русски


class OutcomeTextTest(unittest.TestCase):
    def test_outcomes_are_english(self):
        for name in dir(prompts):
            if name.startswith("OUT_"):
                self.assertIsNone(CYRILLIC.search(getattr(prompts, name)), name)
        self.assertEqual(prompts.outcome_applied(["a.py"], ["b.py (changed)"]),
                         "edits applied: a.py; not applied: b.py (changed)")


if __name__ == "__main__":
    unittest.main()
