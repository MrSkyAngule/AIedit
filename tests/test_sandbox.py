"""Терминал для ИИ: копия без закрытого кода, песочница, подтверждение команд."""
import os
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, checks, prompts, sandbox
from aiedit.marks import Marks
from aiedit.policy import Rules
from aiedit.workspace import Workspace
from tests.test_agent import Script, tool

PAY = '''import os

API = "sk-ant-api03-AbCdEf0123456789xyz"


@route("/fee")
def fee(amount, rate=0.07):
    """SECRET_DOC"""
    return amount * rate * 42


class Vault:
    def open(self, code):
        return code == 1337


def visible(x):
    return x + 1
'''


class StubTest(unittest.TestCase):
    def test_interface_keeps_signature_and_decorator_drops_body_defaults_docstring(self):
        out = "\n".join(sandbox.stub_python('@route("/fee")\ndef fee(amount, rate=0.07):\n    """SECRET_DOC"""\n    return amount * 42'))
        self.assertIn("@route('/fee')", out)
        self.assertIn("def fee(amount, rate=...):", out)
        self.assertIn(sandbox.STUB.replace('"', "'"), out)
        for leak in ("0.07", "42", "SECRET_DOC"):
            self.assertNotIn(leak, out)
        compile(out, "x", "exec")

    def test_class_methods_assignments_and_imports(self):
        out = "\n".join(sandbox.stub_python("import hmac\nKEY = 'x'\nclass V(Base):\n    LIMIT = 5\n    def open(self, c):\n        return c == 1337\n"))
        self.assertIn("import hmac", out)
        self.assertIn("KEY = None", out)
        self.assertIn("class V(Base):", out)
        self.assertIn("    LIMIT = None", out)
        self.assertIn("    def open(self, c):", out)
        self.assertNotIn("1337", out)
        compile(out, "x", "exec")

    def test_indented_and_broken_pieces(self):
        self.assertEqual(sandbox.stub_python("    return x * 7"), ["    " + sandbox.STUB])
        self.assertEqual(sandbox.stub_python("    x = (1 +"), ["    " + sandbox.STUB])

    def test_hidden_leaves_no_names(self):
        self.assertEqual(sandbox.blank_python("def secret_name(a):\n    return a"), ["pass"])
        self.assertEqual(sandbox.blank_python("    def secret_name(self):\n        return 1"), ["    pass"])
        self.assertEqual(sandbox.blank_python("    return amount * 0.07"), ["    " + sandbox.STUB])
        self.assertEqual(sandbox.blank_python("    total = (a +"), ["    " + sandbox.STUB])


class CopyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("secret", "pkg", ".venv", "node_modules"):
            (self.root / d).mkdir(parents=True)
        (self.root / "pay.py").write_text(PAY)
        m = Marks()
        m.add("interface", 5, 8)  # fee
        m.add("hidden", 11, 13)   # Vault
        m.save(self.root / "pay.py", PAY)
        (self.root / "secret" / "data.txt").write_text("TOP_SECRET_DATA\n")
        (self.root / "pkg" / "util.py").write_text("def u():\n    return 1\n")
        (self.root / "notes.md").write_text("plain\n")
        (self.root / "logo.bin").write_bytes(b"\x00\xff\x00BIN")
        (self.root / ".venv" / "x.py").write_text("VENV\n")
        (self.root / ".env").write_text("A_PASSWORD=zzzzzzzz\n")
        Rules(self.root).set_denied(self.root / "secret", True)
        self.ws = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class CopyTest(CopyCase):
    def copy(self) -> Path:
        dest = Path(self.tmp.name) / "copy"
        dest.mkdir()
        self.skipped = sandbox.build_copy(self.ws, dest)
        return dest

    def test_copy_has_only_what_the_model_may_see(self):
        dest = self.copy()
        names = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        self.assertEqual(names, ["logo.bin", "notes.md", "pay.py", "pkg/util.py"])
        text = (dest / "pay.py").read_text()
        for leak in ("sk-ant", "0.07", "42", "SECRET_DOC", "Vault", "1337"):
            self.assertNotIn(leak, text)
        self.assertIn("def fee(amount, rate=...):", text)
        self.assertIn("def visible(x):\n    return x + 1", text)
        self.assertEqual(text.count("\n"), PAY.count("\n"))  # номера строк те же
        compile(text, "pay.py", "exec")
        self.assertEqual((dest / "logo.bin").read_bytes(), b"\x00\xff\x00BIN")
        self.assertEqual(self.ws.states, {})  # рабочая область агента не тронута

    def test_open_tab_text_and_zones_win(self):
        live = "def visible(x):\n    return x + 2  # unsaved\n"
        self.ws = Workspace(self.root, {self.root / "pkg" / "util.py": (live, [])})
        self.assertEqual((self.copy() / "pkg" / "util.py").read_text(), live)

    def test_file_with_stale_zones_is_left_out(self):
        (self.root / "pay.py").write_text(PAY + "# changed outside\n")
        dest = self.copy()
        self.assertFalse((dest / "pay.py").exists())
        self.assertEqual(self.skipped, ["pay.py"])

    def test_non_python_closed_lines_become_empty(self):
        (self.root / "cfg.txt").write_text("a\nsecret line\nb\n")
        m = Marks()
        m.add("hidden", 1, 1)
        m.save(self.root / "cfg.txt", "a\nsecret line\nb\n")
        self.assertEqual((self.copy() / "cfg.txt").read_text(), "a\n\nb\n")


class FakeRun:
    def __init__(self, code=0, output="ok", **kw):
        self.result = dict(code=code, output=output, **kw)
        self.calls = []

    def __call__(self, command, work, venv, extra, timeout):
        self.calls.append((command, work, {p.relative_to(work).as_posix(): p.read_text()
                                           for p in work.rglob("*.py")}))
        return checks.CheckResult(command, self.result["code"], self.result["output"], 0.1,
                                  self.result.get("timed_out", False), self.result.get("error", ""))


class TerminalTest(CopyCase):
    def terminal(self, answer="run", **kw):
        self.run = FakeRun(**kw)
        self.asked, self.shown = [], []
        t = sandbox.Terminal(self.ws, lambda c: (self.asked.append(c), answer)[1], self.shown.append, self.run)
        self.addCleanup(t.close)
        return t

    def test_declined_command_does_not_run(self):
        t = self.terminal("deny")
        self.assertEqual(t("pytest"), sandbox.OUT_DECLINED)
        self.assertEqual((self.asked, self.run.calls), (["pytest"], []))
        self.assertIsNone(t.tmp)

    def test_pending_edits_are_in_the_copy_and_closed_code_is_not(self):
        t = self.terminal()
        self.ws.edit_file("pkg/util.py", "return 1", "return 5")
        out = t("python -c 'import pkg.util'")
        files = self.run.calls[0][2]
        self.assertIn("return 5", files["pkg/util.py"])
        self.assertNotIn("0.07", files["pay.py"])
        self.assertTrue(out.startswith("$ python -c 'import pkg.util'\nexit code 0\nok"))
        self.assertEqual(len(self.shown), 1)
        self.ws.edit_file("pkg/util.py", "return 5", "return 1")  # откатил правку - копия тоже
        t("true")
        self.assertIn("return 1", self.run.calls[1][2]["pkg/util.py"])
        work = t.work
        t.close()
        self.assertFalse(work.exists())

    def test_edits_into_interface_zone_stay_closed(self):
        t = self.terminal()
        self.ws.create_file("new.py", "x = 1\n")
        t("true")
        self.assertEqual(self.run.calls[0][2]["new.py"], "x = 1\n")
        self.assertNotIn("0.07", self.run.calls[0][2]["pay.py"])

    def test_output_for_model(self):
        t = self.terminal(code=1, output="line\n" * 5000 + 'KEY = "sk-ant-api03-AbCdEf0123456789xyz"')
        out = t("pytest")
        self.assertIn("exit code 1", out)
        self.assertIn("[... earlier output cut ...]", out)
        self.assertNotIn("sk-ant-api03", out)
        self.assertLess(len(out), sandbox.MAX_TO_MODEL + 200)
        self.assertIn("timed out", self.terminal(timed_out=True, code=None)("sleep 99"))
        self.assertIn("could not start: boom", self.terminal(error="boom", code=None)("x"))

    def test_bad_commands(self):
        t = self.terminal()
        self.assertTrue(t("  ").startswith("ERROR"))
        self.assertTrue(t("x" * (sandbox.MAX_COMMAND + 1)).startswith("ERROR"))
        self.assertEqual(self.asked, [])

    def test_preapproved_only_simple_commands(self):
        allowed = ["pytest", "python"]
        self.assertTrue(sandbox.preapproved("pytest -q tests/test_a.py", allowed))
        self.assertTrue(sandbox.preapproved("python -m pytest", allowed))
        for cmd in ("pytest; rm -rf .", "pytest | tee x", "python -c \"$(cat x)\"", "pytest > out", "ls", "pytest 'x",
                    "pytest && curl x", "pytest *"):
            self.assertFalse(sandbox.preapproved(cmd, allowed), cmd)


class AgentTerminalTest(CopyCase):
    def test_tool_offered_only_with_terminal_and_not_to_read_only(self):
        prov = Script(["ok"])
        agent.run("t", self.ws, prov, terminal=lambda c: "")
        self.assertIn("run_command", prov.seen[0][0])
        self.assertTrue(prov.seen[0][0].endswith(prompts.REPLY_LANGUAGE))
        prov = Script(["ok"])
        agent.run("t", Workspace(self.root), prov)
        self.assertNotIn("run_command", prov.seen[0][0])
        ws = Workspace(self.root)
        ws.read_only = True
        prov = Script([tool("run_command", command="ls"), "ok"])
        agent.run("t", ws, prov, terminal=lambda c: "RAN")
        self.assertNotIn("run_command", prov.seen[0][0])
        self.assertNotIn("RAN", prov.everything())

    def test_command_result_goes_back_to_model(self):
        ran = []
        prov = Script([tool("run_command", command="pytest -q"), "Tests pass."])
        res = agent.run("t", self.ws, prov, terminal=lambda c: (ran.append(c), "$ pytest -q\nexit code 0\n1 passed")[1])
        self.assertEqual(ran, ["pytest -q"])
        self.assertIn("@@result run_command\n$ pytest -q\nexit code 0\n1 passed\n@@end", prov.everything())
        self.assertIn("запускаю в песочнице: pytest -q", res.calls)

    def test_without_terminal_model_is_told(self):
        prov = Script([tool("run_command", command="ls"), "ok"])
        agent.run("t", self.ws, prov)
        self.assertIn("ERROR: no terminal here", prov.everything())


@unittest.skipIf(sandbox.available(), f"песочница недоступна: {sandbox.available()}")
class RealSandboxTest(CopyCase):
    def run_in(self, command, timeout=20):
        shown = []
        t = sandbox.Terminal(self.ws, lambda c: "run", shown.append, timeout=timeout)
        try:
            out = t(command)
        finally:
            t.close()
        return out, shown[0]

    def test_cannot_see_denied_files_home_or_closed_code(self):
        out, res = self.run_in("ls -a; cat pay.py; ls secret; ls /home; cat " + str(self.root / "secret" / "data.txt"))
        for leak in ("TOP_SECRET_DATA", "0.07", "1337", "sk-ant", ".env"):
            self.assertNotIn(leak, res.output, leak)
        self.assertIn("def fee(amount, rate=...):", res.output)
        self.assertIn("'secret': No such file", res.output)

    def test_no_network_and_clean_environment(self):
        os.environ["AIEDIT_TEST_SECRET_ENV"] = "leak-me"
        try:
            out, res = self.run_in("env; python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 3)\"")
        finally:
            del os.environ["AIEDIT_TEST_SECRET_ENV"]
        self.assertNotIn("leak-me", res.output)
        self.assertNotIn("API_KEY", res.output)
        self.assertRegex(res.output, "Network is unreachable|Errno 101|unreachable")

    def test_writes_stay_in_the_copy(self):
        self.run_in("echo hacked > pay.py; echo new > made.txt")
        self.assertEqual((self.root / "pay.py").read_text(), PAY)
        self.assertFalse((self.root / "made.txt").exists())

    def test_closed_function_raises_and_visible_code_runs(self):
        _, res = self.run_in("python3 -c 'import builtins; builtins.route = lambda p: (lambda f: f)\n"
                             "import pay; print(pay.visible(1))\ntry:\n pay.fee(1)\nexcept NotImplementedError as e: print(\"closed:\", e)'")
        self.assertIn("2\nclosed: hidden from AI", res.output)

    def test_timeout_kills(self):
        out, res = self.run_in("sleep 30", timeout=1)
        self.assertTrue(res.timed_out)
        self.assertIn("timed out", out)


if __name__ == "__main__":
    unittest.main()
