import tempfile
import unittest
from pathlib import Path

from aiedit import orchestrator
from aiedit.orchestrator import MAX_CHARS, Turn, build_messages
from aiedit.policy import Rules
from aiedit.providers import Provider


class Spy(Provider):
    name = "spy"

    def __init__(self):
        self.messages = None

    def complete(self, system, messages):
        self.messages = messages
        return "ok\n```\nx\n```"


def roles(msgs):
    return [m["role"] for m in msgs]


class MemoryTest(unittest.TestCase):
    def test_no_history_is_single_user_message(self):
        self.assertEqual(build_messages(None, "q"), [{"role": "user", "content": "q"}])

    def test_history_goes_first_in_order(self):
        h = [Turn("user", "a"), Turn("assistant", "b")]
        msgs = build_messages(h, "c")
        self.assertEqual([m["content"] for m in msgs], ["a", "b", "c"])
        self.assertEqual(roles(msgs), ["user", "assistant", "user"])

    def test_starts_with_user_after_trimming(self):
        h = [Turn("assistant", "orphan"), Turn("user", "a"), Turn("assistant", "b")]
        self.assertEqual(roles(build_messages(h, "c"))[0], "user")
        self.assertNotIn("orphan", str(build_messages(h, "c")))

    def test_same_roles_are_merged(self):
        h = [Turn("user", "a"), Turn("user", "b")]
        msgs = build_messages(h, "c")
        self.assertEqual(roles(msgs), ["user"])
        self.assertEqual(msgs[0]["content"], "a\n\nb\n\nc")

    def test_old_turns_dropped_by_count_and_size(self):
        h = [Turn("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(60)]
        self.assertLessEqual(len(build_messages(h, "q")), orchestrator.MAX_TURNS + 1)
        big = [Turn("user", "x" * (MAX_CHARS + 1)), Turn("assistant", "short"),
               Turn("user", "recent"), Turn("assistant", "r")]
        msgs = build_messages(big, "q")
        self.assertNotIn("x" * 100, str(msgs))
        self.assertIn("recent", str(msgs))

    def test_turns_about_closed_file_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "secret.py").write_text("x"); (root / "open.py").write_text("x")
            h = [Turn("user", "про секрет", root / "secret.py"),
                 Turn("assistant", "ответ про секрет", root / "secret.py"),
                 Turn("user", "про открытый", root / "open.py"),
                 Turn("assistant", "ответ открытый", root / "open.py"),
                 Turn("user", "общее"), Turn("assistant", "общий ответ")]
            self.assertIn("про секрет", str(build_messages(h, "q")))
            Rules(root).set_denied(root / "secret.py", True)
            text = str(build_messages(h, "q"))
            self.assertNotIn("секрет", text)
            self.assertIn("открытый", text)
            self.assertIn("общий ответ", text)

    def test_ask_sends_history_to_provider(self):
        spy = Spy()
        h = [Turn("user", "сделай A"), Turn("assistant", "сделал A")]
        orchestrator.ask("x = 1", "теперь B", [], {"cheap": spy, "strong": spy}, history=h)
        self.assertEqual([m["content"] for m in spy.messages[:2]], ["сделай A", "сделал A"])
        self.assertTrue(spy.messages[-1]["content"].startswith("теперь B"))

    def test_explain_sends_history_too(self):
        from aiedit.guard import Region
        spy = Spy()
        orchestrator.explain("x = 1", [Region("explain", 0, 0)], {"cheap": spy, "strong": spy},
                             history=[Turn("user", "q"), Turn("assistant", "a")])
        self.assertEqual(len(spy.messages), 3)

    def test_history_never_contains_hidden_code(self):
        # в историю попадает только то, что вернула модель, а она скрытого кода не видела
        from aiedit.guard import Region
        spy = Spy()
        orchestrator.ask("KEY = 'sk-1'\nx = 1", "t", [Region("hidden", 0, 0)],
                         {"cheap": spy, "strong": spy})
        self.assertNotIn("sk-1", str(spy.messages))


if __name__ == "__main__":
    unittest.main()
