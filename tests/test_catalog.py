import http.server
import json
import threading
import unittest

from aiedit import catalog
from aiedit.catalog import make_provider, parse_custom, spec_for, worker_key
from aiedit.providers import AnthropicProvider, OpenAIProvider
from aiedit.translator import Translator, provider_transport


def server(testcase, reply=None, status=200):
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(reply or {"choices": [{"message": {"content": "hello"}}]}).encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    testcase.addCleanup(srv.server_close)
    testcase.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_port}/v1", seen


class OpenAIProviderTest(unittest.TestCase):
    def test_request_shape_and_parsing(self):
        base, seen = server(self)
        p = OpenAIProvider("gpt-4o-mini", "sk-x", url=base + "/chat/completions")
        self.assertEqual(p.complete("SYS", [{"role": "user", "content": "hi"}]), "hello")
        body = seen["body"]
        self.assertEqual(body["model"], "gpt-4o-mini")
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}])
        for absent in ("temperature", "max_tokens", "max_completion_tokens", "tools", "response_format"):
            self.assertNotIn(absent, body)
        self.assertEqual(seen["auth"], "Bearer sk-x")

    def test_limits_and_temperature_only_when_asked(self):
        base, seen = server(self)
        OpenAIProvider("m", "k", url=base + "/c", temperature=0, max_tokens=1500).complete("s", [{"role": "user", "content": "x"}])
        self.assertEqual((seen["body"]["temperature"], seen["body"]["max_completion_tokens"]), (0, 1500))
        OpenAIProvider("m", "k", url=base + "/c", max_tokens=9, token_param="max_tokens").complete("s", [{"role": "user", "content": "x"}])
        self.assertEqual(seen["body"]["max_tokens"], 9)
        self.assertNotIn("max_completion_tokens", seen["body"])

    def test_no_key_means_no_auth_header(self):
        base, seen = server(self)
        OpenAIProvider("llama3", "", url=base + "/chat/completions").complete("s", [{"role": "user", "content": "x"}])
        self.assertIsNone(seen["auth"])

    def test_null_content_becomes_empty_string(self):
        base, _ = server(self, reply={"choices": [{"message": {"content": None}}]})
        self.assertEqual(OpenAIProvider("m", "k", url=base + "/c").complete("s", [{"role": "user", "content": "x"}]), "")


class FactoryTest(unittest.TestCase):
    def test_catalog_keys_are_unique_and_complete(self):
        keys = [m.key for m in catalog.CATALOG]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue({"sonnet", "opus", "haiku", "gpt-4o", "gpt-4o-mini", "glm"} <= set(keys))
        for m in catalog.CATALOG:
            self.assertIn(m.service, ("anthropic", "openai", "compatible"))
            self.assertIn(m.tier, ("cheap", "mid", "strong"))

    def test_providers_by_service_and_role(self):
        a = make_provider("sonnet", "k")
        self.assertIsInstance(a, AnthropicProvider)
        self.assertEqual((a.model, a.effort, a.max_tokens), ("claude-sonnet-5-5", None, 8192))
        c = make_provider("sonnet", "k", role="conductor")
        self.assertEqual((c.effort, c.max_tokens), ("low", 1500))
        o = make_provider("gpt-4o-mini", "k")
        self.assertIsInstance(o, OpenAIProvider)
        self.assertEqual((o.model, o.temperature, o.max_tokens), ("gpt-4o-mini", None, None))
        t = make_provider("gpt-4o-mini", "k", role="translator")
        self.assertEqual(t.temperature, 0)
        self.assertEqual(make_provider("gpt-4o", "k", role="conductor").max_tokens, 1500)

    def test_custom_model(self):
        p = make_provider("custom", "", "llama3 | http://localhost:11434/v1/", role="conductor")
        self.assertEqual((p.model, p.url, p.token_param, p.max_tokens),
                         ("llama3", "http://localhost:11434/v1/chat/completions", "max_tokens", 1500))
        self.assertEqual(worker_key("custom", "My Model:7b | http://x/v1"), "My-Model-7b")
        self.assertEqual(worker_key("sonnet"), "sonnet")
        self.assertEqual(spec_for("custom", "llama3 | http://x/v1").price_in, 0.0)

    def test_custom_errors_are_readable(self):
        for bad in ("", "llama3", "| http://x/v1", "llama3 | localhost:1234", "llama3 | ftp://x"):
            with self.assertRaises(ValueError, msg=bad):
                parse_custom(bad)
        with self.assertRaises(ValueError):
            spec_for("gpt-99")

    def test_glm_is_an_editable_openai_compatible_reviewer(self):
        spec = catalog.by_key("glm")
        self.assertEqual((spec.service, spec.model_id, spec.specialty), ("compatible", "glm-5.3", "security"))
        p = make_provider("glm", "k")  # адрес по умолчанию
        self.assertEqual((p.model, p.url, p.token_param), ("glm-5.3", "https://api.z.ai/api/paas/v4/chat/completions", "max_tokens"))
        fixed = make_provider("glm", "k", "glm-5.3 | https://open.bigmodel.cn/api/paas/v4/")  # поправленный человеком
        self.assertEqual(fixed.url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(make_provider("glm", "k", role="translator").temperature, 0)
        self.assertEqual(worker_key("glm"), "glm")
        with self.assertRaises(ValueError):
            make_provider("glm", "k", "glm-5.3")  # без адреса

    def test_any_provider_can_translate(self):
        base, seen = server(self, reply={"choices": [{"message": {"content": "add a check"}}]})
        prov = make_provider("custom", "", f"llama3 | {base}", role="translator")
        res = Translator(provider_transport(prov)).ru_to_en("добавь проверку")
        self.assertTrue(res.translated)
        self.assertEqual(res.text, "add a check")
        roles = [m["role"] for m in seen["body"]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(seen["body"]["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
