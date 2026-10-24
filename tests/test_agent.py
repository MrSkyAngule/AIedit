import tempfile
import unittest
from pathlib import Path

from aiedit import agent, sendlog
from aiedit.marks import Marks
from aiedit.orchestrator import Turn
from aiedit.policy import AccessDenied, Rules
from aiedit.providers import Provider
from aiedit.workspace import Workspace


class Script(Provider):
    """Отвечает по сценарию и запоминает всё, что получил."""
    name = "script"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []  # (system, messages)

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        return self.replies.pop(0) if self.replies else "конец"

    def everything(self) -> str:
        return "\n".join(m["content"] for _, ms in self.seen for m in ms)


def tool(name, **args):
    body = "\n".join(f"{k}: {v}" for k, v in args.items() if "\n" not in str(v))
    return f"@@tool {name}\n{body}\n@@end"


class ParseTest(unittest.TestCase):
    def test_single_and_text_outside(self):
        calls, text = agent.parse_calls("Сначала посмотрю.\n@@tool read_file\npath: a.py\nstart_line: 3\n@@end\nЖду.")
        self.assertEqual([(c.name, c.args, c.error) for c in calls], [("read_file", {"path": "a.py", "start_line": 3}, "")])
        self.assertEqual(text, "Сначала посмотрю.\n\nЖду.")

    def test_edit_sections_keep_indentation_and_multiline(self):
        reply = "@@tool edit_file\npath: a.py\n@@old\ndef f():\n    return 1\n@@new\ndef f():\n    return 2  # \"x\"\n@@end"
        (call,), _ = agent.parse_calls(reply)
        self.assertEqual(call.args["old_text"], "def f():\n    return 1")
        self.assertEqual(call.args["new_text"], 'def f():\n    return 2  # "x"')

    def test_empty_new_section_means_deletion(self):
        (call,), _ = agent.parse_calls("@@tool edit_file\npath: a.py\n@@old\nx = 1\n@@new\n@@end")
        self.assertEqual(call.args["new_text"], "")

    def test_content_section_and_values_with_colons(self):
        (call,), _ = agent.parse_calls("@@tool create_file\npath: a/b.py\n@@content\nurl = 'http://x:80'\n@@end")
        self.assertEqual(call.args, {"path": "a/b.py", "content": "url = 'http://x:80'"})

    def test_several_blocks_and_cap(self):
        reply = "\n".join(tool("read_file", path=f"f{i}.py") for i in range(20))
        calls, _ = agent.parse_calls(reply)
        self.assertEqual(len(calls), agent.MAX_CALLS_PER_REPLY)

    def test_errors_are_reported_not_raised(self):
        calls, _ = agent.parse_calls("@@tool rm_rf\npath: /\n@@end\n@@tool read_file\nnonsense\n@@end\n"
                                     "@@tool read_file\nstart_line: abc\n@@end\n@@tool read_file\n@@old\nx\n@@end")
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c.error for c in calls))

    def test_no_blocks_is_final_answer(self):
        self.assertEqual(agent.parse_calls("Просто ответ")[0], [])


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("pkg", "secrets"):
            (self.root / d).mkdir(parents=True)
        self.files = {"main.py": "from pkg import pay\nprint(pay.free())\n",
                      "pkg/pay.py": "def free():\n    return 1\n\n\ndef vault():\n    return 'HIDDEN_SECRET_LOGIC'\n",
                      "secrets/k.py": "TOP_SECRET_VALUE = 1\n", ".env": "A_SECRET=zzzzzzzz\n"}
        for n, t in self.files.items():
            (self.root / n).write_text(t)
        m = Marks(); m.add("hidden", 4, 5); m.save(self.root / "pkg" / "pay.py", self.files["pkg/pay.py"])
        Rules(self.root).set_denied(self.root / "secrets", True)

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, replies, task="сделай", **kw):
        prov = Script(replies)
        ws = Workspace(self.root, model="script")
        return agent.run(task, ws, prov, **kw), prov, ws


class LoopTest(AgentCase):
    def test_reads_then_edits_then_answers(self):
        res, prov, ws = self.go([
            tool("read_file", path="pkg/pay.py"),
            "@@tool edit_file\npath: pkg/pay.py\n@@old\ndef free():\n    return 1\n@@new\ndef free():\n    return 2\n@@end",
            "Заменил возвращаемое значение free."])
        self.assertEqual(res.text, "Заменил возвращаемое значение free.")
        self.assertEqual(res.steps, 3)
        self.assertEqual([c.path.name for c in res.changes], ["pay.py"])
        self.assertIn("return 2", res.changes[0].new)
        self.assertIn("HIDDEN_SECRET_LOGIC", res.changes[0].new)  # скрытое сохранилось
        self.assertEqual((self.root / "pkg" / "pay.py").read_text(), self.files["pkg/pay.py"])  # диск цел
        self.assertEqual(res.calls, ["читаю pkg/pay.py", "предлагаю правку в pkg/pay.py"])

    def test_model_gets_tree_task_and_results(self):
        res, prov, _ = self.go([tool("read_file", path="main.py"), "готово"], task="что тут?")
        first = prov.seen[0][1][0]["content"]
        self.assertTrue(first.startswith("что тут?"))
        self.assertIn("pkg/", first)
        self.assertIn("main.py", first)
        second = prov.seen[1][1]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertIn("@@result read_file", second[2]["content"])
        self.assertIn("1| from pkg import pay", second[2]["content"])

    def test_errors_go_back_to_model_so_it_can_retry(self):
        res, prov, _ = self.go([
            "@@tool edit_file\npath: main.py\n@@old\nнет такого\n@@new\nx\n@@end",
            "@@tool bogus\n@@end",
            "не вышло, сдаюсь"])
        results = prov.seen[1][1][-1]["content"] + prov.seen[2][1][-1]["content"]
        self.assertIn("not found in the file", results)
        self.assertIn("unknown tool", results)
        self.assertEqual(res.changes, [])

    def test_history_is_used(self):
        hist = [Turn("user", "прошлый вопрос"), Turn("assistant", "прошлый ответ")]
        _, prov, _ = self.go(["ок"], history=hist)
        msgs = prov.seen[0][1]
        self.assertEqual([m["content"] for m in msgs[:2]], ["прошлый вопрос", "прошлый ответ"])

    def test_open_file_is_named(self):
        _, prov, _ = self.go(["ок"], open_file=self.root / "main.py")
        self.assertIn("=== OPEN FILE ===\nmain.py", prov.seen[0][1][0]["content"])

    def test_progress_callback(self):
        seen = []
        self.go([tool("list_files"), tool("search", query="free"), "ок"], on_progress=seen.append)
        self.assertEqual(seen, ["смотрю список файлов: .", "ищу «free»"])


class LimitsTest(AgentCase):
    def test_step_limit(self):
        res, _, _ = self.go([tool("list_files")] * (agent.MAX_STEPS + 5))
        self.assertEqual(res.steps, agent.MAX_STEPS)
        self.assertIn("лимит шагов", res.stopped)
        self.assertIn("остановлена", res.text)

    def test_stop_flag(self):
        flags = iter([False, True])
        res, prov, _ = self.go([tool("list_files"), "не дойдёт"], should_stop=lambda: next(flags))
        self.assertEqual(len(prov.seen), 1)
        self.assertEqual(res.stopped, "остановлено пользователем")

    def test_big_reads_are_compacted_not_stopped(self):
        (self.root / "big.txt").write_text("x" * 59000)
        reads = [tool("read_file", path="big.txt")] * 10 + ["done"]
        res, prov, _ = self.go(reads)
        self.assertEqual(res.stopped, "")
        self.assertEqual(res.text, "done")
        self.assertGreater(res.compactions, 0)


class SafetyTest(AgentCase):
    def test_provider_never_receives_closed_content(self):
        replies = [tool("read_file", path="pkg/pay.py"), tool("read_file", path="secrets/k.py"),
                   tool("read_file", path=".env"), tool("search", query="SECRET"),
                   tool("search", query="HIDDEN"), tool("list_files", path="secrets"), "готово"]
        _, prov, _ = self.go(replies)
        everything = prov.everything()
        for leak in ("TOP_SECRET_VALUE", "HIDDEN_SECRET_LOGIC", "zzzzzzzz", "A_SECRET"):
            self.assertNotIn(leak, everything)
        self.assertIn("not found", everything)

    def test_model_cannot_edit_hidden_or_denied(self):
        res, _, _ = self.go([
            "@@tool edit_file\npath: secrets/k.py\n@@old\nTOP_SECRET_VALUE = 1\n@@new\nx\n@@end",
            "@@tool edit_file\npath: pkg/pay.py\n@@old\nHIDDEN_SECRET_LOGIC\n@@new\nx\n@@end",
            "ок"])
        self.assertEqual(res.changes, [])

    def test_access_revoked_mid_loop_aborts_before_next_send(self):
        prov = Script([tool("read_file", path="main.py"), tool("list_files"), "никогда"])
        ws = Workspace(self.root)
        orig = prov.complete

        def revoke_after_second(system, messages):
            out = orig(system, messages)
            if len(prov.seen) == 2:  # содержимое main.py уже ушло модели во втором запросе
                Rules(self.root).set_denied(self.root / "main.py", True)
            return out
        prov.complete = revoke_after_second
        with self.assertRaises(AccessDenied):
            agent.run("t", ws, prov)
        self.assertEqual(len(prov.seen), 2)

    def test_file_denied_before_it_is_read_is_just_missing(self):
        prov = Script([tool("read_file", path="main.py"), "ок"])
        orig = prov.complete

        def revoke_first(system, messages):
            out = orig(system, messages)
            Rules(self.root).set_denied(self.root / "main.py", True)
            return out
        prov.complete = revoke_first
        res = agent.run("t", Workspace(self.root), prov)
        self.assertEqual(res.text, "ок")
        self.assertIn("not found", prov.seen[1][1][-1]["content"])

    def test_log_has_agent_calls_and_tool_results_but_no_content(self):
        self.go([tool("read_file", path="pkg/pay.py"), "готово"])
        modes = [e["mode"] for e in sendlog.read(self.root)]
        self.assertEqual(modes, ["tool:list_files", "agent", "tool:read_file", "agent"])
        raw = (self.root / sendlog.LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN_SECRET_LOGIC", raw)

    def test_tool_exception_does_not_break_loop(self):
        ws = Workspace(self.root)
        ws.read_file = lambda **k: 1 / 0
        res = agent.run("t", ws, Script([tool("read_file", path="main.py"), "итог"]))
        self.assertEqual(res.text, "итог")


if __name__ == "__main__":
    unittest.main()


class DemoAgentTest(AgentCase):
    def run_demo(self, task):
        from aiedit.providers import DemoProvider
        return agent.run(task, Workspace(self.root), DemoProvider("demo"))

    def test_demo_reads_and_answers(self):
        res = self.run_demo("что в проекте?")
        self.assertEqual(res.calls, ["читаю main.py"])
        self.assertIn("прочитал main.py", res.text)
        self.assertEqual(res.changes, [])

    def test_demo_adds_comment_when_asked(self):
        res = self.run_demo("добавь комментарий")
        self.assertEqual([c.path.name for c in res.changes], ["main.py"])
        self.assertTrue(res.changes[0].new.startswith("# [demo]"))
        self.assertIn("from pkg import pay", res.changes[0].new)

    def test_preview_workspace_does_not_log(self):
        ws = Workspace(self.root, log_enabled=False)
        ws.list_files()
        ws.read_file("main.py")
        self.assertEqual(sendlog.read(self.root), [])
