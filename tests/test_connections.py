import os
import stat
import tempfile
import unittest
from pathlib import Path

from aiedit import connections
from aiedit.connections import Connection, Roles, build, ping
from aiedit.providers import AnthropicProvider, DemoProvider, OpenAIProvider
from tests.test_catalog import server


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AIEDIT_CONFIG_DIR")}
        os.environ["AIEDIT_CONFIG_DIR"] = self.tmp.name

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()


class StorageTest(Env):
    def test_roundtrip_and_file_is_private(self):
        rows = [Connection("gpt-4o-mini", "sk-secret-1"), Connection("custom", "", "llama3 | http://x/v1")]
        roles = Roles("gpt-4o-mini", "off")
        self.assertTrue(connections.save(rows, roles))
        self.assertEqual(connections.load(), (rows, roles))
        mode = stat.S_IMODE(connections.file_path().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_overwrite_keeps_private_mode_and_leaves_no_temp_files(self):
        connections.save([Connection("sonnet", "k1")], Roles())
        connections.file_path().chmod(0o644)
        connections.save([Connection("sonnet", "k2")], Roles())
        self.assertEqual(stat.S_IMODE(connections.file_path().stat().st_mode), 0o600)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["connections.json"])

    def test_key_never_in_repr(self):
        self.assertNotIn("sk-secret-1", repr(Connection("gpt-4o", "sk-secret-1")))
        self.assertNotIn("sk-secret-1", repr([Connection("gpt-4o", "sk-secret-1")]))

    def test_missing_or_broken_file_gives_empty(self):
        self.assertEqual(connections.load(), ([], Roles()))
        connections.file_path().write_text("{ broken")
        self.assertEqual(connections.load(), ([], Roles()))
        connections.file_path().write_text('{"connections": [1, {"key": "x"}, {"model": "opus", "key": "k"}], "roles": 5}')
        self.assertEqual(connections.load()[0], [Connection("opus", "k")])

    def test_at_most_four_rows(self):
        connections.save([Connection("sonnet", f"k{i}") for i in range(10)], Roles())
        self.assertEqual((len(connections.load()[0]), connections.MAX_ROWS), (4, 4))


class BuildTest(Env):
    def test_only_openai_is_enough(self):
        setup = build([Connection("gpt-4o-mini", "sk-oa")])
        self.assertEqual(setup.roster.keys(), ["gpt-4o-mini"])
        self.assertIsInstance(setup.conductor, OpenAIProvider)
        self.assertEqual((setup.conductor.model, setup.conductor.max_tokens), ("gpt-4o-mini", 1500))
        self.assertEqual((setup.translator.model, setup.translator.temperature), ("gpt-4o-mini", 0))
        self.assertEqual((setup.problems, setup.source), ([], "connections"))

    def test_several_services_and_default_roles(self):
        setup = build([Connection("haiku", "a"), Connection("opus", "b"), Connection("sonnet", "c"),
                       Connection("gpt-4o-mini", "d")])
        self.assertEqual(setup.roster.keys(), ["haiku", "opus", "sonnet", "gpt-4o-mini"])
        self.assertEqual(setup.conductor.model, "claude-sonnet-5-5")  # средний по цене
        self.assertEqual((setup.conductor.effort, setup.conductor.max_tokens), ("low", 1500))
        self.assertEqual(setup.translator.model, "gpt-4o-mini")
        self.assertIsInstance(setup.roster.get("opus").provider, AnthropicProvider)

    def test_explicit_roles(self):
        rows = [Connection("sonnet", "a"), Connection("gpt-4o-mini", "b")]
        setup = build(rows, Roles("gpt-4o-mini", "sonnet"))
        self.assertEqual(setup.conductor.model, "gpt-4o-mini")
        self.assertIsInstance(setup.translator, AnthropicProvider)  # переводить может любая модель
        self.assertIsNone(build(rows, Roles("sonnet", "off")).translator)
        self.assertEqual(build(rows, Roles("ghost", "ghost")).conductor.model, "claude-sonnet-5-5")

    def test_no_translator_when_only_anthropic_and_auto(self):
        self.assertIsNone(build([Connection("sonnet", "a")]).translator)

    def test_auto_translator_prefers_mini_then_4o(self):
        self.assertEqual(build([Connection("gpt-4o", "a"), Connection("gpt-4o-mini", "b")]).translator.model, "gpt-4o-mini")
        self.assertEqual(build([Connection("sonnet", "a"), Connection("gpt-4o", "b")]).translator.model, "gpt-4o")

    def test_custom_local_model_needs_no_key(self):
        setup = build([Connection("custom", "", "llama3 | http://localhost:11434/v1")])
        self.assertEqual(setup.roster.keys(), ["llama3"])
        self.assertEqual(setup.roster.get("llama3").price_in, 0.0)
        self.assertIsNone(setup.translator)  # своя модель переводчиком сама не становится
        manual = build([Connection("custom", "", "llama3 | http://localhost:11434/v1")], Roles("", "llama3"))
        self.assertEqual(manual.translator.model, "llama3")

    def test_bad_rows_are_skipped_with_reasons_and_never_leak_keys(self):
        setup = build([Connection("sonnet", "  "), Connection("gpt-4o", "sk-LEAK-ME"), Connection("gpt-99", "sk-LEAK-ME"),
                       Connection("custom", "sk-LEAK-ME", "nonsense"), Connection("gpt-4o", "sk-LEAK-ME")])
        self.assertEqual(setup.roster.keys(), ["gpt-4o"])
        self.assertEqual(len(setup.problems), 4)
        self.assertIn("не указан ключ", setup.problems[0])
        self.assertIn("уже подключена", setup.problems[3])
        self.assertNotIn("sk-LEAK-ME", " ".join(setup.problems))

    def test_nothing_valid_falls_back_to_env_then_demo(self):
        setup = build([Connection("sonnet", "")])
        self.assertEqual((setup.source, len(setup.problems)), ("demo", 1))
        self.assertIsInstance(setup.roster.get("sonnet").provider, DemoProvider)
        os.environ["ANTHROPIC_API_KEY"] = "env-key"
        os.environ["OPENAI_API_KEY"] = "env-oa"
        env = build([])
        self.assertEqual(env.source, "env")
        self.assertIsInstance(env.roster.get("sonnet").provider, AnthropicProvider)
        self.assertEqual((env.translator.model, env.translator.temperature), ("gpt-4o-mini", 0))

    def test_demo_when_nothing_at_all(self):
        setup = build([])
        self.assertEqual(setup.source, "demo")
        self.assertIsNone(setup.translator)
        self.assertEqual(setup.conductor.name, "demo-conductor")


class SpecialtyTest(Env):
    def test_role_is_decided_by_the_model_not_by_the_row(self):
        setup = build([Connection("sonnet", "a"), Connection("opus", "b"), Connection("gpt-4o", "c"),
                       Connection("custom", "", "llama3 | http://localhost:11434/v1"), Connection("glm", "d")])
        self.assertEqual({w.key: w.specialty for w in setup.roster.workers.values()},
                         {"sonnet": "", "opus": "backend", "gpt-4o": "", "llama3": "", "glm": "security"})
        self.assertIn("[specialty: backend", setup.roster.describe())
        self.assertEqual(setup.roster.labels(), "sonnet, opus (Backend), gpt-4o, llama3, glm (Безопасность (только проверка))")

    def test_old_files_with_a_specialty_field_still_load_and_the_field_is_ignored(self):
        import json
        connections.save([Connection("opus", "k")], Roles())
        data = json.loads(connections.file_path().read_text(encoding="utf-8"))
        data["connections"][0]["specialty"] = "frontend"
        connections.file_path().write_text(json.dumps(data), encoding="utf-8")
        rows = connections.load()[0]
        self.assertEqual([c.model for c in rows], ["opus"])
        self.assertEqual(build(rows).roster.get("opus").specialty, "backend")
        self.assertNotIn("specialty", connections.file_path().read_text(encoding="utf-8") if connections.save(rows, Roles()) else "")

    def test_default_conductor_is_not_the_specialist(self):
        setup = build([Connection("opus", "b"), Connection("sonnet", "a")])
        self.assertEqual(setup.conductor.model, "claude-sonnet-5-5")


class GlmTest(Env):
    def test_glm_connects_as_the_reviewer_and_needs_a_key(self):
        setup = build([Connection("sonnet", "a"), Connection("glm", "z-key")])
        self.assertEqual(setup.roster.reviewer().key, "glm")
        self.assertEqual(setup.roster.get("glm").provider.url, "https://api.z.ai/api/paas/v4/chat/completions")
        self.assertEqual([w.key for w in setup.roster.builders()], ["sonnet"])
        bad = build([Connection("sonnet", "a"), Connection("glm", "")])
        self.assertEqual(bad.roster.keys(), ["sonnet"])
        self.assertIn("не указан ключ", bad.problems[0])

    def test_bad_address_is_reported_without_the_key(self):
        setup = build([Connection("sonnet", "a"), Connection("glm", "z-secret", "glm-5.3 | nonsense")])
        self.assertEqual(setup.roster.keys(), ["sonnet"])
        self.assertIn("http", setup.problems[0])
        self.assertNotIn("z-secret", setup.problems[0])


class PingTest(Env):
    def test_ok_and_failures_without_key_in_message(self):
        base, _ = server(self)
        ok = ping(OpenAIProvider("m", "sk-PING-KEY", url=base + "/chat/completions"))
        self.assertEqual(ok, (True, "работает"))
        base, _ = server(self, status=401, reply={"error": {"message": "bad"}})
        bad, text = ping(OpenAIProvider("m", "sk-PING-KEY", url=base + "/chat/completions"))
        self.assertFalse(bad)
        self.assertIn("ключ не принят", text)
        self.assertNotIn("sk-PING-KEY", text)
        down, text = ping(OpenAIProvider("m", "sk-PING-KEY", url="http://127.0.0.1:9/x", timeout=2))
        self.assertFalse(down)
        self.assertNotIn("sk-PING-KEY", text)

    def test_status_hints(self):
        for code, hint in ((403, "доступ запрещён"), (404, "не найдены"), (429, "лимит"), (500, "ошибка сервиса")):
            base, _ = server(self, status=code, reply={"error": 1})
            self.assertIn(hint, ping(OpenAIProvider("m", "k", url=base + "/c"))[1])


if __name__ == "__main__":
    unittest.main()
