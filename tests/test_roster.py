import http.server
import json
import os
import threading
import unittest

from aiedit import conductor, roster
from aiedit.providers import AnthropicProvider, DemoProvider, default_providers
from aiedit.roster import Roster, Worker, default_conductor, default_roster


def fake_server(testcase, reply=None):
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps(reply or {"content": [{"type": "thinking", "thinking": ""},
                                                   {"type": "text", "text": "plan "}, {"type": "text", "text": "text"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}/v1/messages", seen


class AnthropicRequestTest(unittest.TestCase):
    def test_conductor_style_request_is_short_and_low_effort(self):
        url, seen = fake_server(self)
        p = AnthropicProvider("claude-sonnet-5-5", "k", effort="low", max_tokens=1500, url=url)
        out = p.complete("sys", [{"role": "user", "content": "task"}])
        body = seen["body"]
        self.assertEqual(out, "plan text")  # thinking-блоки не попадают в ответ
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-sonnet-5-5", 1500))
        self.assertEqual(body["output_config"], {"effort": "low"})
        for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(seen["headers"]["x-api-key"], "k")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")

    def test_default_has_no_effort_field(self):
        url, seen = fake_server(self)
        AnthropicProvider("claude-haiku-5-5", "k", url=url).complete("s", [{"role": "user", "content": "t"}])
        self.assertNotIn("output_config", seen["body"])
        self.assertEqual(seen["body"]["max_tokens"], 8192)


class RosterTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self.saved:
            os.environ["ANTHROPIC_API_KEY"] = self.saved

    def three(self):
        return Roster([Worker("haiku", DemoProvider("h"), "cheap", 0.1, 0.5, "cheap"),
                       Worker("sonnet", DemoProvider("s"), "mid", 2.0, 10.0, "mid"),
                       Worker("opus", DemoProvider("o"), "strong", 4.0, 20.0, "strong")])

    def test_default_roster_is_sonnet_plus_opus_for_backend(self):
        r = default_roster()
        self.assertEqual(r.keys(), ["sonnet", "opus"])
        self.assertIsInstance(r.get("sonnet").provider, DemoProvider)  # без ключа - демо
        self.assertEqual((r.get("sonnet").specialty, r.get("opus").specialty), ("", "backend"))
        self.assertIsNone(r.get("haiku"))
        self.assertEqual(r.labels(), "sonnet, opus (Backend)")

    def test_with_key_uses_real_sonnet_and_conductor(self):
        os.environ["ANTHROPIC_API_KEY"] = "k"
        r = default_roster()
        self.assertEqual([w.provider.name for w in r.by_price()], ["claude-sonnet-5-5", "claude-opus-5-5"])
        self.assertEqual(r.get("SONNET").price_in, 2.0)
        c = default_conductor()
        self.assertEqual((c.name, c.effort, c.max_tokens), ("claude-sonnet-5-5", "low", 1500))

    def test_roles_in_the_default_roster(self):
        r = default_roster()
        self.assertEqual((r.cheapest().key, r.mid().key, r.strongest().key), ("sonnet", "sonnet", "opus"))
        self.assertEqual([r.for_risk(x).key for x in ("low", "medium", "high", "???")], ["sonnet", "sonnet", "opus", "sonnet"])

    def test_roles_and_fallbacks_with_several_workers(self):
        r = self.three()
        self.assertEqual((r.cheapest().key, r.mid().key, r.strongest().key), ("haiku", "sonnet", "opus"))
        self.assertEqual([r.for_risk(x).key for x in ("low", "medium", "high", "LOW", "???", "")],
                         ["haiku", "sonnet", "opus", "haiku", "sonnet", "sonnet"])
        self.assertIsNone(r.get("gpt"))
        self.assertIs(r.get(" Haiku "), r.workers["haiku"])

    def test_describe_is_compact_and_cheap_first(self):
        text = self.three().describe().split("\n")
        self.assertEqual([l.split(" ")[1] for l in text], ["haiku", "sonnet", "opus"])
        self.assertIn("$0.1/$0.5", text[0])
        self.assertLess(len("\n".join(text)), 700)
        self.assertEqual(len(default_roster().describe().split("\n")), 2)

    def test_specialty_is_told_to_the_conductor(self):
        text = default_roster().describe()
        self.assertIn("- opus ($4/$20 per 1M tokens) [specialty: backend - server-side code:", text)
        self.assertNotIn("specialty", text.split("\n")[0])  # у универсала пометки нет
        self.assertIn("specialty", conductor.CONDUCTOR_SYSTEM)
        self.assertIn("best choice for steps in that area", conductor.CONDUCTOR_SYSTEM)

    def test_single_worker_roster_works(self):
        only = Roster([Worker("solo", DemoProvider("d"), "everything", 1, 1, "mid")])
        self.assertEqual((only.cheapest().key, only.strongest().key, only.for_risk("high").key), ("solo",) * 3)
        with self.assertRaises(ValueError):
            Roster([])

    def test_default_providers_compat(self):
        p = default_providers()
        self.assertEqual(sorted(p), ["cheap", "strong"])
        self.assertEqual((p["cheap"].name, p["strong"].name), ("demo-sonnet", "demo-sonnet"))

    def test_demo_conductor_uses_only_workers_from_the_roster(self):
        def plan(roster):
            request = f"REQUEST:\nwrite a test; fix the bug; add a feature\n\nWORKERS:\n{roster.describe()}\n\nPROTECTED"
            return DemoProvider("c").complete("Mode: conductor", [{"role": "user", "content": request}])
        default = plan(default_roster())
        self.assertEqual([l for l in default.split("\n") if l.startswith("worker:")],
                         ["worker: sonnet", "worker: opus", "worker: sonnet"])
        backend = plan(default_roster()).replace("write a test", "x")
        request = "REQUEST:\nadd an api endpoint\n\nWORKERS:\n" + default_roster().describe() + "\n\nPROTECTED"
        routed = DemoProvider("c").complete("Mode: conductor", [{"role": "user", "content": request}])
        self.assertIn("worker: opus", routed)  # слова api/endpoint ведут к Backend-специалисту
        self.assertTrue(backend)
        many = plan(self.three())
        self.assertEqual([l for l in many.split("\n") if l.startswith("worker:")],
                         ["worker: haiku", "worker: opus", "worker: sonnet"])

    def reviewer_roster(self):
        return Roster([Worker("sonnet", DemoProvider("s"), "mid", 2.0, 10.0, "mid"),
                       Worker("opus", DemoProvider("o"), "strong", 4.0, 20.0, "strong", "backend"),
                       Worker("glm", DemoProvider("g"), "reviewer", 0.0, 0.0, "mid", "security")])

    def test_reviewer_is_not_a_builder(self):
        r = self.reviewer_roster()
        self.assertEqual(r.reviewer().key, "glm")
        self.assertEqual([w.key for w in r.builders()], ["sonnet", "opus"])
        self.assertEqual((r.cheapest().key, r.strongest().key), ("sonnet", "opus"))  # цена 0 не делает его «дешёвым»
        self.assertEqual({r.for_risk(x).key for x in ("low", "medium", "high")}, {"sonnet", "opus"})
        self.assertIsNone(r.get_builder("glm"))
        self.assertIsNotNone(r.get("glm"))
        self.assertEqual(sorted(r.providers()), ["cheap", "strong"])
        self.assertNotEqual(r.providers()["cheap"].name, "g")
        self.assertNotEqual(r.providers()["strong"].name, "g")

    def test_conductor_table_does_not_list_the_reviewer(self):
        text = self.reviewer_roster().describe()
        self.assertNotIn("glm", text)
        self.assertIn("price unknown", Roster([Worker("x", DemoProvider("x"), "s", 0, 0, "mid")]).describe())
        self.assertIn("glm (Безопасность (только проверка))", self.reviewer_roster().labels())

    def test_only_reviewers_connected_means_they_have_to_build(self):
        solo = Roster([Worker("glm", DemoProvider("g"), "reviewer", 0, 0, "mid", "security")])
        self.assertIsNone(solo.reviewer())  # проверять некого проверять
        self.assertEqual(solo.builders()[0].key, "glm")
        self.assertIs(solo.get_builder("glm"), solo.workers["glm"])

    def test_plan_cannot_hand_a_step_to_the_reviewer(self):
        import tempfile
        from pathlib import Path
        from aiedit.conductor import parse_plan
        from aiedit.workspace import Workspace
        with tempfile.TemporaryDirectory() as d:
            plan = parse_plan("@@plan\n@@task t1\nworker: glm\ngoal: fix it\nrisk: low\n@@end", Workspace(Path(d)),
                              self.reviewer_roster(), "fix it")
        self.assertEqual(plan.tasks[0].worker, "sonnet")
        self.assertIn("unknown worker", plan.notes[0])

    def test_no_clash_with_module_name(self):
        self.assertTrue(hasattr(roster, "default_roster"))


if __name__ == "__main__":
    unittest.main()
