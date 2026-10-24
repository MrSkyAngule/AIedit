import tempfile
import unittest
from pathlib import Path

from aiedit import guard, orchestrator, policy, sendlog
from aiedit.guard import Region
from aiedit.orchestrator import FileInput
from aiedit.policy import AccessDenied, Rules
from aiedit.providers import Provider

SRC = '''def pay(user: str, amount: int = 5, *, key="sk-123") -> int:
    """секрет"""
    return hash(key)

API_KEY = "sk-456"

def free():
    return 2
'''


class Spy(Provider):
    name = "spy"

    def __init__(self):
        self.calls = []

    def complete(self, system, messages):
        self.calls.append((system, messages))
        return "сделал\n```\nx = 1\n```"


class InterfaceTest(unittest.TestCase):
    def test_signatures_without_bodies_or_defaults(self):
        red = guard.redact(SRC, [Region("interface", 0, 4)])
        self.assertIn("def pay(user: str, amount: int=..., *, key=...) -> int", red.text)
        self.assertIn("API_KEY = ...", red.text)
        for leak in ("sk-123", "sk-456", "секрет", "hash"):
            self.assertNotIn(leak, red.text)
        self.assertEqual(guard.restore(red.text, red.chunks), SRC)

    def test_hidden_has_no_signatures(self):
        red = guard.redact(SRC, [Region("hidden", 0, 4)])
        self.assertNotIn("pay", red.text)

    def test_interface_zone_cannot_be_edited(self):
        regions = [Region("interface", 0, 4)]
        new = SRC.replace("return hash(key)", "return 0")
        self.assertFalse(guard.check_edit(SRC, new, regions).ok)

    def test_non_python_falls_back_to_plain_placeholder(self):
        js = "function f(a) {\n  return a;\n}\n"
        red = guard.redact(js, [Region("interface", 0, 2)])
        self.assertNotIn("function", red.text)
        self.assertNotIn("|", red.text)

    def test_model_edit_of_signature_text_does_not_corrupt(self):
        red = guard.redact(SRC, [Region("interface", 0, 4)])
        tampered = red.text.replace("amount: int=...", "amount: str=...")
        self.assertEqual(guard.restore(tampered, red.chunks), SRC)


class SendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "secrets").mkdir(parents=True)
        for n, t in (("a.py", "x = 1\n"), ("b.py", "KEY = 'sk-zzz'\ny = 2\n"), ("secrets/k.py", "K = 1\n")):
            (self.root / n).write_text(t)
        self.spy = Spy()
        self.provs = {"cheap": self.spy, "strong": self.spy}

    def tearDown(self):
        self.tmp.cleanup()

    def test_send_is_logged_without_content(self):
        orchestrator.ask("x = 1\nKEY = 'sk-1'", "t", [Region("hidden", 1, 1)], self.provs,
                         source=self.root / "a.py")
        entries = sendlog.read(self.root)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual((e["mode"], e["model"]), ("ask", "spy"))
        self.assertEqual(e["files"], [{"name": "a.py", "hidden_chunks": 1, "hidden_lines": 1}])
        raw = (self.root / sendlog.LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("sk-1", raw)
        self.assertIn("a.py", sendlog.format_entry(e))

    def test_denied_send_is_not_logged_and_not_sent(self):
        Rules(self.root).set_denied(self.root / "secrets", True)
        with self.assertRaises(AccessDenied):
            orchestrator.ask("K = 1", "t", [], self.provs, source=self.root / "secrets" / "k.py")
        self.assertEqual(self.spy.calls, [])
        self.assertEqual(sendlog.read(self.root), [])

    def test_service_files_are_private(self):
        Rules(self.root).set_denied(self.root / "secrets", True)
        orchestrator.ask("x", "t", [], self.provs, source=self.root / "a.py")
        for name in (sendlog.LOG_NAME, ".aiedit-chat.json", "a.py.aimarks.json"):
            self.assertTrue(policy.is_denied(self.root / name), name)

    def test_context_file_is_sent_redacted_and_after_policy(self):
        ctx = FileInput(self.root / "b.py", (self.root / "b.py").read_text(), [Region("hidden", 0, 0)])
        orchestrator.ask("x = 1\n", "t", [], self.provs, source=self.root / "a.py", context=[ctx])
        user = self.spy.calls[0][1][-1]["content"]
        self.assertIn("=== CONTEXT (read-only) ===", user)
        self.assertIn("--- b.py ---", user)
        self.assertIn("y = 2", user)
        self.assertNotIn("sk-zzz", user)
        self.assertLess(user.index("CONTEXT"), user.index("=== FILE ==="))
        self.assertEqual([f["name"] for f in sendlog.read(self.root)[0]["files"]], ["a.py", "b.py"])

    def test_denied_context_file_blocks_whole_request(self):
        Rules(self.root).set_denied(self.root / "secrets", True)
        ctx = FileInput(self.root / "secrets" / "k.py", "K = 1\n", [])
        with self.assertRaises(AccessDenied):
            orchestrator.ask("x = 1\n", "t", [], self.provs, source=self.root / "a.py", context=[ctx])
        self.assertEqual(self.spy.calls, [])

    def test_rules_changed_after_prepare_still_block_send(self):
        req = orchestrator.prepare_ask(FileInput(self.root / "a.py", "x = 1\n", []), "t", self.provs)
        Rules(self.root).set_denied(self.root / "a.py", True)
        with self.assertRaises(AccessDenied):
            orchestrator.send(req)
        self.assertEqual(self.spy.calls, [])

    def test_preview_matches_what_is_sent(self):
        regions = [Region("hidden", 1, 1)]
        req = orchestrator.prepare_ask(FileInput(self.root / "a.py", "x = 1\nKEY = 'sk-9'\n", regions),
                                       "сделай", self.provs)
        preview = orchestrator.describe(req)
        orchestrator.send(req)
        system, messages = self.spy.calls[0]
        self.assertIn(system, preview)
        self.assertIn(messages[-1]["content"], preview)
        self.assertNotIn("sk-9", preview)

    def test_context_is_truncated(self):
        big = FileInput(self.root / "b.py", "z = 1\n" * 20000, [])
        req = orchestrator.prepare_ask(FileInput(self.root / "a.py", "x\n", []), "t", self.provs, context=[big])
        self.assertIn("[... truncated ...]", req.messages[-1]["content"])
        self.assertLess(len(req.messages[-1]["content"]), orchestrator.MAX_CONTEXT_CHARS + 1000)


if __name__ == "__main__":
    unittest.main()
