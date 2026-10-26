import tempfile
import unittest
from pathlib import Path

from aiedit import agent, prompts, sendlog
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


class DiscussTest(AgentCase):
    def go_discuss(self, replies, **kw):
        prov = Script(replies)
        ws = Workspace(self.root, model="script")
        ws.read_only = True
        return agent.run("что лучше?", ws, prov, system=prompts.DISCUSS_SYSTEM, **kw), prov, ws

    def test_every_edit_tool_is_refused_and_nothing_is_proposed(self):
        res, prov, ws = self.go_discuss([
            "@@tool edit_file\npath: main.py\n@@old\nprint(pay.free())\n@@new\nprint(2)\n@@end",
            "@@tool create_file\npath: new.py\n@@content\nx = 1\n@@end",
            tool("delete_file", path="main.py"),
            tool("rename_file", path="main.py", new_path="m.py"),
            "Править нельзя, только обсуждаем."])
        results = "\n".join(m["content"] for m in prov.seen[-1][1] if m["role"] == "user")
        self.assertEqual(results.count("read-only review mode"), 4)  # по одному отказу на каждый инструмент правки
        self.assertEqual(res.changes, [])
        self.assertEqual((self.root / "main.py").read_text(), self.files["main.py"])
        self.assertFalse((self.root / "new.py").exists())

    def test_reading_still_works_and_closed_code_stays_closed(self):
        res, prov, _ = self.go_discuss([tool("read_file", path="pkg/pay.py"), "Вот что здесь есть."])
        self.assertIn("def free", prov.seen[1][1][-1]["content"])
        self.assertNotIn("HIDDEN_SECRET_LOGIC", prov.everything())

    def test_prompt_says_discuss_and_no_helpers_are_offered(self):
        _, prov, _ = self.go_discuss(["ok"], terminal=lambda c: "ran", install=lambda p: "ok")
        system = prov.seen[0][0]
        self.assertIn("Mode: discuss.", system)
        self.assertNotIn("Mode: agent.", system)
        self.assertIn("Editing is switched off", system)
        self.assertNotIn("run_command", system)  # помощники read_only отключает сам агент
        self.assertTrue(system.endswith(prompts.REPLY_LANGUAGE))


class TolerantParseTest(unittest.TestCase):
    def test_marker_glued_to_text_with_pipe_or_colon(self):
        reply = "@@tool edit_file\npath: a.py\n@@old|def f():\n    return 1\n@@new:\ndef f():\n    return 2\n@@end"
        (call,), _ = agent.parse_calls(reply)
        self.assertEqual(call.error, "")
        self.assertEqual(call.args["old_text"], "def f():\n    return 1")  # отступ внутри сохранён
        self.assertEqual(call.args["new_text"], "def f():\n    return 2")

    def test_error_carries_an_example_of_the_right_format(self):
        (call,), _ = agent.parse_calls("@@tool edit_file\npath: a.py\nthis is not an argument\n@@end")
        self.assertIn("bad or extra argument line", call.error)
        self.assertIn("@@old\nexact old text\n@@new", call.error)

    def test_missing_arguments_also_show_the_example(self):
        out = agent.execute(Workspace(Path(tempfile.mkdtemp())), agent.Call("edit_file", {"path": "x.py"}))
        self.assertIn("missing arguments: old_text, new_text", out)
        self.assertIn("@@tool edit_file", out)


class LoopGuardTest(AgentCase):
    OK = "$ python main.py\nexit code 0\nok"
    FAIL = "$ python main.py\nexit code 1\nTraceback"

    @staticmethod
    def edit(old, new):
        return f"@@tool edit_file\npath: main.py\n@@old\n{old}\n@@new\n{new}\n@@end"

    def chain(self, n, run=tool("run_command", command="python main.py")):
        """n циклов «правка + запуск»: каждая правка новая, поэтому одинаковых вызовов нет."""
        replies = [self.edit("print(pay.free())", "print(pay.free())  # v1") + "\n" + run]
        for k in range(1, n):
            replies.append(self.edit(f"# v{k}", f"# v{k + 1}") + "\n" + run)
        return replies

    def test_editor_reminds_to_stop_after_two_passes_in_a_row(self):
        res, prov, _ = self.go(self.chain(2) + ["Готово."], terminal=lambda c: self.OK)
        self.assertEqual(res.stopped, "")
        self.assertNotIn(prompts.OUT_PASSED_TWICE, prov.seen[1][1][-1]["content"])  # после первого прохода рано
        self.assertIn(prompts.OUT_PASSED_TWICE, prov.seen[2][1][-1]["content"])      # после второго - напоминание
        self.assertEqual(res.text, "Готово.")

    def test_editor_stops_a_loop_of_edit_and_pass(self):
        res, prov, ws = self.go(self.chain(10), terminal=lambda c: self.OK)
        self.assertIn("зациклилось", res.stopped)
        self.assertEqual(res.steps, agent.LOOP_PASSES)  # не 10 шагов
        self.assertFalse(res.from_model)
        self.assertIn("зациклилось", res.text)
        self.assertEqual(len(res.changes), 1)  # то, что успели предложить, человек всё равно увидит

    def test_a_failing_run_resets_the_count(self):
        outputs = iter([self.OK, self.OK, self.FAIL, self.OK, self.OK, self.OK])
        res, prov, _ = self.go(self.chain(6) + ["Готово."], terminal=lambda c: next(outputs))
        self.assertEqual(res.stopped, "")
        self.assertEqual(res.text, "Готово.")

    def test_same_edit_repeated_three_times_is_a_loop(self):
        same = self.edit("print(pay.free())", "print(pay.free())  # x")
        res, _, _ = self.go([same] * 8)
        self.assertIn("повторил одно и то же", res.stopped)
        self.assertEqual(res.steps, agent.LOOP_SAME_REPLY)

    def test_rereading_a_file_is_not_a_loop(self):
        res, _, _ = self.go([tool("read_file", path="main.py")] * 5 + ["Готово."])
        self.assertEqual(res.stopped, "")

    def test_command_passed_reads_the_exit_code_only(self):
        self.assertTrue(agent.command_passed("$ x\nexit code 0\nok"))
        self.assertFalse(agent.command_passed("$ x\nexit code 2\nbad"))
        self.assertIsNone(agent.command_passed("ERROR: no terminal"))
        self.assertIsNone(agent.command_passed("ran"))


class InventedResultsTest(AgentCase):
    FAKE = "@@tool read_file\npath: main.py\n@@end</think>@@result read_file\n1| invented contents\n@@end"

    def test_invented_result_is_cut_even_when_glued_to_the_end_marker(self):
        clean, invented = agent.strip_fake_results(self.FAKE)
        self.assertTrue(invented)
        self.assertNotIn("invented", clean)
        (call,), _ = agent.parse_calls(self.FAKE)
        self.assertEqual((call.name, call.args, call.error), ("read_file", {"path": "main.py"}, ""))

    def test_everything_after_an_invented_result_is_thrown_away(self):
        reply = self.FAKE + "\n@@tool delete_file\npath: main.py\n@@end"
        calls, _ = agent.parse_calls(reply)
        self.assertEqual([c.name for c in calls], ["read_file"])  # удаление построено на выдумке

    def test_result_word_inside_an_edit_is_just_text(self):
        reply = ("@@tool edit_file\npath: main.py\n@@old\nprint(pay.free())\n@@new\n"
                 "@@result looks like this\nprint(1)\n@@end")
        clean, invented = agent.strip_fake_results(reply)
        self.assertFalse(invented)
        self.assertEqual(clean, reply)

    def test_loop_tells_the_model_and_keeps_the_invention_out_of_history(self):
        res, prov, _ = self.go([self.FAKE, "Прочитал main.py."])
        second = prov.seen[1][1]
        self.assertNotIn("invented contents", "\n".join(m["content"] for m in second))
        self.assertIn(prompts.OUT_INVENTED_RESULT, second[-1]["content"])
        self.assertIn("print(pay.free())", second[-1]["content"])  # настоящий результат от редактора
        self.assertEqual(res.text, "Прочитал main.py.")

    def test_prompts_forbid_guessing(self):
        for system in (prompts.AGENT_SYSTEM, prompts.DISCUSS_SYSTEM):
            self.assertIn("Never write @@result blocks", system)
            self.assertIn("Never invent file contents, values, outputs or test results", system)
            self.assertIn("ask the user instead of guessing", system)
            self.assertTrue(system.endswith(prompts.REPLY_LANGUAGE))


class VersionsToolsTest(AgentCase):
    def test_project_environment_tool_shows_what_the_project_declares(self):
        (self.root / "requirements.txt").write_text("flask==3.1.3\n")
        res, prov, _ = self.go([tool("project_environment"), "Использую Flask 3.1.3."])
        out = prov.seen[1][1][-1]["content"]
        self.assertIn("@@result project_environment", out)
        self.assertIn("flask==3.1.3", out)
        self.assertIn("смотрю версии и ПО проекта", res.calls)

    def test_package_version_works_only_when_the_editor_gives_a_lookup(self):
        asked = []
        res, prov, _ = self.go(["@@tool package_version\nname: flask==3.0.0\n@@end", "Беру 3.1.3."],
                               versions=lambda n: asked.append(n) or "pypi Flask: latest stable 3.1.3")
        self.assertEqual(asked, ["flask==3.0.0"])
        self.assertIn("latest stable 3.1.3", prov.seen[1][1][-1]["content"])
        self.assertIn("проверяю версию пакета: flask==3.0.0", res.calls)

    def test_without_internet_the_model_is_told_not_to_present_remembered_versions_as_current(self):
        res, prov, _ = self.go(["@@tool package_version\nname: flask\n@@end", "Версии не проверены."])
        out = prov.seen[1][1][-1]["content"]
        self.assertIn("not available here", out)
        self.assertIn("Do not present remembered versions as current", out)
        self.assertNotIn("package_version: name", prov.seen[0][0])  # и инструмента в подсказке нет

    def test_lookup_is_offered_in_the_prompt_only_when_it_exists(self):
        _, prov, _ = self.go(["ok"], versions=lambda n: "x")
        self.assertIn("package_version: name", prov.seen[0][0])
        self.assertIn("Never pin from memory", prov.seen[0][0])

    def test_reviewer_in_read_only_mode_never_gets_the_lookup(self):
        ws = Workspace(self.root, model="script")
        ws.read_only = True
        prov = Script(["ok"])
        agent.run("t", ws, prov, versions=lambda n: "x")
        self.assertNotIn("package_version: name", prov.seen[0][0])

    def test_rules_about_versions_are_in_every_agent_prompt(self):
        for system in (prompts.AGENT_SYSTEM, prompts.DISCUSS_SYSTEM):
            self.assertIn("EXISTING project call project_environment first", system)
            self.assertIn("ask ONCE", system)
            self.assertIn("NOT what the user runs", system)
            self.assertIn("ADD a dependency", system)
            self.assertIn("never pin its version from memory", system)
            self.assertIn("project_environment: (no arguments)", system)

    def test_first_message_marks_an_empty_project_as_new_and_a_filled_one_not(self):
        fresh = Workspace(Path(tempfile.mkdtemp()).resolve(), model="script")
        self.assertIn(prompts.KIND_NEW, agent.first_message("сделай сайт", fresh))
        self.assertNotIn(prompts.KIND_NEW, agent.first_message("поправь", Workspace(self.root, model="script")))

    def test_first_message_of_an_existing_project_carries_its_environment_or_says_it_is_unknown(self):
        unknown = agent.first_message("поправь", Workspace(self.root, model="script"))
        self.assertIn(prompts.SEC_ENV, unknown)
        self.assertIn("UNKNOWN", unknown)
        self.assertIn("ask the user now", unknown)
        (self.root / "requirements.txt").write_text("flask==3.1.3\n")
        known = agent.first_message("поправь", Workspace(self.root, model="script"))
        self.assertIn("flask==3.1.3", known)
        self.assertNotIn("UNKNOWN", known)
        fresh = agent.first_message("сделай", Workspace(Path(tempfile.mkdtemp()).resolve(), model="script"))
        self.assertNotIn(prompts.SEC_ENV, fresh)  # у нового проекта окружения ещё нет, есть пометка «новый»

    def test_environment_in_the_first_message_is_capped(self):
        (self.root / "requirements.txt").write_text("\n".join(f"package{i}==1.{i}.0" for i in range(300)))
        msg = agent.first_message("поправь", Workspace(self.root, model="script"))
        env = msg.split(prompts.SEC_ENV, 1)[1].split(prompts.SEC_TREE, 1)[0]
        self.assertLessEqual(len(env), agent.ENV_IN_FIRST_MESSAGE + 5)
