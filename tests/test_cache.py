"""Кэширование промпта и точный расход токенов."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from aiedit import agent, outbound
from aiedit.providers import AnthropicProvider, OpenAIProvider
from aiedit.workspace import Workspace

BODIES = []
REPLIES = []


class Api(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        BODIES.append(body)
        text = REPLIES.pop(0) if REPLIES else "done"
        if "messages" in body and "max_tokens" in body and "model" in body and self.path.endswith("/messages"):
            out = {"content": [{"type": "text", "text": text}],
                   "usage": {"input_tokens": 50, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100,
                             "output_tokens": 20}}
        else:
            out = {"choices": [{"message": {"content": text}}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 768}}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class CacheCase(unittest.TestCase):
    def setUp(self):
        BODIES.clear()
        REPLIES.clear()
        self.httpd = HTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "a.py").write_text("def f():\n    return 1\n")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def claude(self, **kw):
        return AnthropicProvider("claude-sonnet-5-5", "k", url=self.base + "/v1/messages", **kw)


class AnthropicCacheTest(CacheCase):
    def test_body_marks_system_and_tail(self):
        body = self.claude().request_body("SYS", [{"role": "user", "content": "hi"}])
        self.assertEqual(body["system"], [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        off = self.claude(cache=False).request_body("SYS", [{"role": "user", "content": "hi"}])
        self.assertEqual(off["system"], "SYS")
        self.assertNotIn("cache_control", off)

    def test_usage_is_read(self):
        p = self.claude()
        p.complete("s", [{"role": "user", "content": "hi"}])
        self.assertEqual(p.last_usage, {"input": 50, "cache_read": 900, "cache_write": 100, "output": 20})

    def test_agent_steps_keep_a_byte_identical_prefix(self):
        """Иначе кэш молча не срабатывает: каждый запрос должен начинаться ровно с предыдущего."""
        REPLIES.extend(["@@tool read_file\npath: a.py\n@@end", "@@tool list_files\n@@end", "done"])
        res = agent.run("task", Workspace(self.root), self.claude())
        self.assertEqual(len(BODIES), 3)
        for prev, cur in zip(BODIES, BODIES[1:]):
            self.assertEqual(prev["system"], cur["system"])
            self.assertEqual(cur["messages"][:len(prev["messages"])], prev["messages"])
        self.assertEqual(res.usage, {"input": 150, "cache_read": 2700, "cache_write": 300, "output": 60})

    def test_filter_masks_the_same_way_every_step(self):
        (self.root / "a.py").write_text("def acme():\n    return 1\n")
        REPLIES.extend(["@@tool read_file\npath: a.py\n@@end", "@@tool list_files\n@@end", "done"])
        gate = outbound.Gate(["acme"], decide=lambda f, m: "mask")
        res = agent.run("task about acme", Workspace(self.root), gate.wrap(self.claude()))
        for prev, cur in zip(BODIES, BODIES[1:]):
            self.assertEqual(cur["messages"][:len(prev["messages"])], prev["messages"])
        self.assertNotIn("acme", json.dumps(BODIES))
        self.assertEqual(res.usage["cache_read"], 2700)  # расход виден и через фильтр


class OpenAIUsageTest(CacheCase):
    def test_cached_tokens_are_counted(self):
        p = OpenAIProvider("gpt-4o", "k", url=self.base + "/v1/chat/completions")
        p.complete("s", [{"role": "user", "content": "hi"}])
        self.assertEqual(p.last_usage, {"input": 232, "cache_read": 768, "cache_write": 0, "output": 30})


class UsageTextTest(unittest.TestCase):
    def test_exact_and_approximate(self):
        self.assertEqual(agent.usage_text({"input": 1500, "cache_read": 12000, "cache_write": 500, "output": 800}),
                         "токены: вход 14 тыс. (из кэша 12 тыс., 86%), ответ 0.8 тыс.")
        self.assertEqual(agent.usage_text({}, 40_000), "ушло ~10 тыс. токенов")
        self.assertEqual(agent.usage_text({}, 0), "")


if __name__ == "__main__":
    unittest.main()
