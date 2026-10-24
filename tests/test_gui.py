"""Тесты окна без экрана (offscreen). Запускаются на том Qt-биндинге, который выбран AIEDIT_QT."""
import json
import os
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from aiedit.qt import QApplication, QInputDialog, QTextDocument
except ImportError:  # нет Qt - тесты окна пропускаем
    QApplication = None

if QApplication is not None:
    from aiedit import chatstore, policy, sendlog, settings
    from aiedit.marks import text_hash
    from aiedit.app import MainWindow
    from aiedit.providers import Provider
    from aiedit.syntax import Highlighter
    from aiedit.translator import Translator

    app = QApplication.instance() or QApplication([])


class Top(Provider if QApplication else object):
    """Подставляет строку в начало файла."""
    name = "top"

    def complete(self, system, messages):
        code = messages[-1]["content"].partition("\n=== FILE ===\n")[2]
        return "Добавил строку сверху.\n```\n# top\n" + code + "\n```"


@unittest.skipIf(QApplication is None, "нет Qt")
class GuiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        os.environ["AIEDIT_CONFIG_DIR"] = str(self.base / "cfg")
        self.saved_keys = {k: os.environ.pop(k, None) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}  # без сети
        self.root = self.base / "proj"
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "pkg").mkdir()
        files = {"a.py": "def f():\n    return 1\n\ndef g():\n    return 2\n", "b.py": "y = 2\n",
                 "pkg/util.py": "def u():\n    return 'TOKEN_VALUE'\n", "secrets/k.py": "K = 'abc'\n"}
        for n, t in files.items():
            (self.root / n).write_text(t)
        self.windows = []

    def tearDown(self):
        for w in self.windows:
            w.docs.clear()
            w.close()
        os.environ.pop("AIEDIT_CONFIG_DIR", None)
        for k, v in self.saved_keys.items():
            if v:
                os.environ[k] = v
        self.tmp.cleanup()

    def window(self, open_file="a.py") -> "MainWindow":
        w = MainWindow()
        self.windows.append(w)
        w.journal_on = False  # окна журнала в посторонних тестах не нужны
        w.analyze_on = False  # настоящие анализаторы в посторонних тестах не запускаем
        w.conductor_on = False  # эти тесты проверяют прямой режим агента; дирижёр - в ConductorGuiTest
        w.tree.set_root(self.root)
        w.acknowledged.add(self.root)
        w.review_diff = lambda *a: True
        if open_file:
            w.load(self.root / open_file)
        return w

    @staticmethod
    def wait(w):
        for _ in range(3):
            app.processEvents()
            for t in (w.worker, w.check_worker):
                while t and not t.wait(10):  # окно должно жить: поток может ждать ответа из окна
                    app.processEvents()
            app.processEvents()

    @staticmethod
    def zones(w):
        return sorted((r.level, r.start, r.end) for r in w.marks.items)


class TabsTest(GuiCase):
    def test_tabs_keep_own_zones_and_reuse_empty_tab(self):
        w = self.window()
        self.assertEqual([d.name for d in w.docs], ["a.py"])
        w.on_gutter_mark("readonly", 1, 2)
        w.load(self.root / "b.py")
        w.on_gutter_mark("hidden", 0, 0)
        w.load(self.root / "a.py")  # уже открыт: переключение, не дубль
        self.assertEqual(len(w.docs), 2)
        self.assertEqual(self.zones(w), [("readonly", 1, 2)])

    def test_modified_marker_and_close_last_tab_leaves_empty_one(self):
        w = self.window()
        w.editor.appendPlainText("z")
        self.assertTrue(w.tabs.tabText(0).endswith("*"))
        w.confirm_discard = lambda d: True
        w.close_tab(0)
        self.assertEqual([d.name for d in w.docs], ["без имени"])

    def test_answer_lands_in_origin_tab(self):
        w = self.window()
        w.load(self.root / "b.py")
        w.tabs.setCurrentIndex(0)
        origin = w.doc
        w.ask_file("задача")
        w.tabs.setCurrentIndex(1)
        self.wait(w)
        self.assertIn("[demo", origin.text())
        self.assertNotIn("[demo", next(d for d in w.docs if d is not origin).text())

    def test_denied_tab_blocks_ai_buttons(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w = self.window()
        w.load(self.root / "secrets" / "k.py")
        self.assertTrue(w.ai_blocked)
        self.assertFalse(w.explain_btn.isEnabled())
        self.assertTrue(w.ask_btn.isEnabled())  # агент работает с проектом и без этого файла
        w.tabs.setCurrentIndex(0)
        self.assertTrue(w.explain_btn.isEnabled())


class UndoTest(GuiCase):
    def test_undo_redo_restore_zones_exactly(self):
        w = self.window()
        w.providers = {"cheap": Top(), "strong": Top()}
        w.on_gutter_mark("readonly", 3, 4)
        before = self.zones(w)
        w.ask_file("сверху")
        self.wait(w)
        after = self.zones(w)
        self.assertEqual(after, [("readonly", 4, 5)])
        w.undo()
        self.assertEqual(self.zones(w), before)
        w.redo()
        self.assertEqual(self.zones(w), after)

    def test_rejected_diff_changes_nothing(self):
        w = self.window()
        w.review_diff = lambda *a: False
        text = w.editor.toPlainText()
        w.ask_file("задача")
        self.wait(w)
        self.assertEqual(w.editor.toPlainText(), text)
        self.assertIn("rejected", w.history[-1].text)


class ChatTest(GuiCase):
    def test_memory_persists_per_project(self):
        w = self.window()
        w.ask_file("первая")
        self.wait(w)
        self.assertEqual(len(w.history), 2)
        again = self.window()
        self.assertEqual(len(again.history), 2)
        self.assertIn("первая", again.chat.history.toPlainText())
        other = self.base / "other"
        other.mkdir()
        again.tree.set_root(other)
        self.assertEqual((again.history, again.chat.messages), ([], []))
        again.tree.set_root(self.root)
        self.assertEqual(len(again.history), 2)

    def test_new_chat_clears_memory_and_file(self):
        w = self.window()
        w.ask_file("первая")
        self.wait(w)
        w.clear_chat()
        self.assertEqual(w.history, [])
        self.assertEqual(chatstore.load(self.root)[1], [])


class SecretsTest(GuiCase):
    def test_secret_choices(self):
        (self.root / "s.py").write_text('KEY = "sk-ant-api03-AbCdEf0123456789xyz"\nx = 1\n')
        w = self.window("s.py")
        w.ask_secrets = lambda lines, manual: "cancel"
        w.ask_file("t")
        self.assertEqual(w.history, [])
        self.assertEqual(w.chat.input.toPlainText(), "t")
        w.ask_secrets = lambda lines, manual: "hide"
        w.ask_file("t")
        self.wait(w)
        self.assertEqual(self.zones(w), [("hidden", 0, 0)])
        self.assertNotIn("sk-ant", w.chat.history.toPlainText())

    def test_env_file_can_be_denied_from_prompt(self):
        (self.root / ".env").write_text("A_SECRET=zzzzzzzz\n")
        w = self.window(".env")
        w.ask_secret_file = lambda p: "deny"
        w.ask_file("t")
        self.assertTrue(policy.is_denied(self.root / ".env"))
        self.assertFalse(w.explain_btn.isEnabled())


class ContextTest(GuiCase):
    def test_context_preview_log_and_prune(self):
        w = self.window()
        w.toggle_context(self.root / "pkg" / "util.py")
        w.chat.input.setPlainText("что делает u?")
        w.preview_file(w.chat.input.toPlainText().strip())
        text = w.preview.view.toPlainText()
        self.assertIn("--- pkg/util.py ---", text)
        w.ask_file("что делает u?")
        self.wait(w)
        self.assertEqual([f["name"] for f in sendlog.read(self.root)[-1]["files"]], ["a.py", "pkg/util.py"])
        policy.Rules(self.root).set_denied(self.root / "pkg", True)
        w.tree.rulesChanged.emit()
        self.assertEqual(w.context, [])

    def test_unsaved_zones_of_open_context_file_are_respected(self):
        w = self.window()
        w.load(self.root / "pkg" / "util.py")
        w.editor.appendPlainText("# unsaved edit")  # в файле есть несохранённый текст, значит и зона на диск не уйдёт
        w.on_gutter_mark("hidden", 1, 1)
        self.assertFalse((self.root / "pkg" / "util.py.aimarks.json").exists())
        w.toggle_context(self.root / "pkg" / "util.py")
        w.tabs.setCurrentIndex(0)
        w.chat.input.setPlainText("что делает u?")
        w.preview_file(w.chat.input.toPlainText().strip())
        text = w.preview.view.toPlainText()
        self.assertIn("--- pkg/util.py ---", text)
        self.assertNotIn("TOKEN_VALUE", text)

    def test_unsaved_text_of_open_context_file_is_sent(self):
        w = self.window()
        w.load(self.root / "b.py")
        w.editor.appendPlainText("fresh_unsaved_line = 1")
        w.toggle_context(self.root / "b.py")
        w.tabs.setCurrentIndex(0)
        w.chat.input.setPlainText("q")
        w.preview_file(w.chat.input.toPlainText().strip())
        self.assertIn("fresh_unsaved_line", w.preview.view.toPlainText())

    def test_denied_file_cannot_enter_context(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w = self.window()
        w.toggle_context(self.root / "secrets" / "k.py")
        self.assertEqual(w.context, [])

    def test_search_skips_denied_and_opens_line(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w = self.window()
        w.search_project()
        d = w.search_dlg
        d.query.setText("return")
        self.assertEqual(d.run(), 3)
        d.results.setCurrentRow(0)
        d.open_selected()
        self.assertEqual(w.editor.textCursor().blockNumber() + 1, d.hits[0].line)


class TreeOpsTest(GuiCase):
    def test_rename_keeps_tab_zones_and_denial(self):
        w = self.window()
        w.on_gutter_mark("readonly", 0, 0)
        w.save_file()
        w.tree.set_denied(self.root / "a.py", True)
        w.tree.ask_name = lambda *a, **k: "z.py"
        w.tree.rename(self.root / "a.py")
        self.assertEqual(w.doc.name, "z.py")
        self.assertTrue(policy.is_denied(self.root / "z.py"))
        self.assertTrue((self.root / "z.py.aimarks.json").exists())
        self.assertFalse((self.root / "a.py.aimarks.json").exists())

    def test_delete_closes_its_tab(self):
        w = self.window()
        w.load(self.root / "b.py")
        w.tree.confirm_delete = lambda p: True
        w.tree.delete(self.root / "b.py")
        self.assertEqual([d.name for d in w.docs], ["a.py"])


class FindTest(GuiCase):
    def test_find_replace_and_single_undo(self):
        w = self.window()
        fb = w.findbar
        fb.open()
        fb.find.setText("return")
        self.assertEqual(fb.count(), 2)
        fb.replace.setText("yield")
        self.assertEqual(fb.replace_all(), 2)
        self.assertNotIn("return", w.editor.toPlainText())
        w.undo()
        self.assertEqual(w.editor.toPlainText().count("return"), 2)

    def test_case_sensitive_count(self):
        w = self.window()
        fb = w.findbar
        fb.find.setText("DEF")
        self.assertEqual(fb.count(), 2)
        fb.case.setChecked(True)
        self.assertEqual(fb.count(), 0)


class SyntaxTest(GuiCase):
    @staticmethod
    def spans(text, suffix=".py"):
        doc = QTextDocument()
        h = Highlighter(doc)
        doc.setPlainText(text)
        h.set_language(suffix)
        out, b = [], doc.firstBlock()
        while b.isValid():
            out.append([(b.text()[f.start:f.start + f.length], f.format.foreground().color().name())
                        for f in b.layout().formats()])
            b = b.next()
        return out

    def test_docstring_on_one_line_does_not_swallow_next_lines(self):
        rows = self.spans('def f():\n    """doc."""\n    return 1  # c\n')
        words = {w for w, _ in rows[2]}
        self.assertIn("return", words)
        self.assertIn("# c", words)

    def test_hash_inside_string_is_not_comment_and_multiline_string(self):
        rows = self.spans('s = "a # b"\n"""x\ny"""\nz = 1\n')
        self.assertEqual([w for w, _ in rows[0]], ['"a # b"'])
        self.assertEqual(rows[1][0][1], rows[2][0][1])
        self.assertEqual(rows[3], [("1", rows[3][0][1])])

    def test_unknown_suffix_is_plain(self):
        self.assertEqual(self.spans("def f(): pass", ".txt"), [[]])


class ChecksTest(GuiCase):
    def test_auto_check_after_edit_keeps_denied_data_out_of_memory(self):
        (self.root / "secrets" / "k.py").write_text("def get():\n    return 'API_PASSWORD_VALUE'\n")
        (self.root / "t.py").write_text("import sys\nsys.path.insert(0, 'secrets')\nimport k\nassert False, k.get()\n")
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w = self.window()
        QInputDialog.getText = staticmethod(lambda *a, **k: (shlex.join([sys.executable, "t.py"]), True))
        w.set_check_command()
        w.act_auto.trigger()
        w.act_output.trigger()
        w.ask_file("задача")
        self.wait(w)
        kinds = [m[0] for m in w.chat.messages]
        self.assertIn("check", kinds)
        shown = next(m for m in w.chat.messages if m[0] == "check")
        self.assertIn("API_PASSWORD_VALUE", shown[1])  # человек видит вывод целиком
        self.assertIn("failed", w.history[-1].text)
        self.assertNotIn("API_PASSWORD_VALUE", w.history[-1].text)  # ИИ - нет
        note = w.history[-1].text.partition("[post-edit check")[2]  # то, что дописал редактор, а не демо-модель
        self.assertTrue(note)
        self.assertIsNone(re.search("[А-Яа-яЁё]", note))  # и по-английски

    def test_settings_are_per_project_and_reloaded(self):
        w = self.window()
        settings.set_project(self.root, auto=True, test_command="x")
        w.load_check_settings(self.root)
        self.assertTrue(w.act_auto.isChecked())
        self.assertFalse(w.act_output.isChecked())


class Script(Provider if QApplication else object):
    """Отвечает по сценарию и запоминает всё, что получил."""
    name = "script"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.systems = []

    def complete(self, system, messages):
        self.systems.append(system)
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "конец"

    def everything(self):
        return "\n".join(m["content"] for ms in self.seen for m in ms)


def call(name, **args):
    return f"@@tool {name}\n" + "\n".join(f"{k}: {v}" for k, v in args.items()) + "\n@@end"


def edit(path, old, new):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class AgentGuiTest(GuiCase):
    def agent(self, replies, open_file="a.py"):
        w = self.window(open_file)
        prov = Script(replies)
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        return w, prov

    def test_agent_edits_open_tab_and_other_file_after_review(self):
        w, prov = self.agent([call("read_file", path="b.py"),
                              edit("b.py", "y = 2", "y = 3"), edit("a.py", "return 1", "return 10"),
                              "Поправил a.py и b.py."])
        w.ask_ai("поправь значения")
        self.wait(w)
        self.assertIn("return 10", w.editor.toPlainText())  # открытая вкладка: правка не сохранена
        self.assertTrue(w.doc.modified)
        self.assertEqual((self.root / "a.py").read_text().count("return 10"), 0)
        self.assertEqual((self.root / "b.py").read_text(), "y = 3\n")  # закрытый файл: записан на диск
        self.assertIn("edits applied: a.py, b.py", w.history[-1].text.replace("b.py, a.py", "a.py, b.py"))
        kinds = [m[0] for m in w.chat.messages]
        self.assertIn("ai", kinds)
        self.assertIn("читаю b.py", [m[1] for m in w.chat.messages if m[0] == "system"])

    def test_agent_can_create_file_and_zones_follow_open_tab(self):
        w, prov = self.agent(["@@tool create_file\npath: pkg/new.py\n@@content\nx = 1\n@@end", "создал"])
        w.on_gutter_mark("readonly", 3, 4)
        w.ask_ai("создай файл")
        self.wait(w)
        self.assertEqual((self.root / "pkg" / "new.py").read_text(), "x = 1")
        self.assertEqual(self.zones(w), [("readonly", 3, 4)])

    def test_rejected_changes_touch_nothing_and_are_remembered(self):
        w, prov = self.agent([edit("b.py", "y = 2", "y = 3"), "готово"])
        w.review_changes = lambda items: [False] * len(items)
        w.ask_ai("поправь")
        self.wait(w)
        self.assertEqual((self.root / "b.py").read_text(), "y = 2\n")
        self.assertIn("rejected", w.history[-1].text)

    def test_user_edit_during_agent_run_wins_over_stale_change(self):
        w, prov = self.agent([edit("a.py", "return 1", "return 10"), "готово"])

        def user_types_meanwhile(items):
            w.editor.appendPlainText("# моя правка")
            return [True] * len(items)
        w.review_changes = user_types_meanwhile
        w.ask_ai("поправь")
        self.wait(w)
        self.assertNotIn("return 10", w.editor.toPlainText())
        self.assertIn("# моя правка", w.editor.toPlainText())
        self.assertIn("не применено", " ".join(m[1] for m in w.chat.messages if m[0] == "system"))

    def test_nothing_russian_reaches_provider_in_a_session(self):
        w, prov = self.agent([edit("b.py", "y = 2", "y = 3"), "first answer"])
        w.review_changes = lambda items: [False] * len(items)
        w.ask_ai("change b")
        self.wait(w)
        prov.replies = [edit("b.py", "y = 2", "y = 4"), "second answer"]
        w.review_changes = lambda items: [True] * len(items)
        w.ask_ai("try again")
        self.wait(w)
        third = Script(["third answer"])
        w.providers = {"cheap": third, "strong": third}
        w.ask_ai("and now?")
        self.wait(w)
        for text in (prov.everything(), third.everything()):
            self.assertIsNone(re.search("[А-Яа-яЁё]", text), re.findall(".{20}[А-Яа-яЁё]+.{20}", text)[:2])
        self.assertIn("[outcome: user rejected the edits, files unchanged]", third.everything())
        self.assertIn("[outcome: edits applied: b.py]", third.everything())

    def test_closed_files_never_reach_provider(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w, prov = self.agent([call("read_file", path="secrets/k.py"), call("search", query="abc"),
                              call("list_files"), "нет доступа"])
        w.ask_ai("что в папке с ключами?")
        self.wait(w)
        self.assertNotIn("'abc'", prov.everything())
        self.assertNotIn("secrets", prov.seen[0][0]["content"])

    def test_closed_open_tab_does_not_block_agent(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w, prov = self.agent(["ответ"], open_file="secrets/k.py")
        self.assertTrue(w.ai_blocked)
        self.assertTrue(w.ask_btn.isEnabled())
        w.ask_ai("привет")
        self.wait(w)
        self.assertIn("ответ", w.chat.history.toPlainText())
        self.assertNotIn("k.py", prov.seen[0][0]["content"])  # закрытый файл не назван даже как открытый

    def test_unsaved_tab_text_is_what_the_agent_reads(self):
        w, prov = self.agent([call("read_file", path="a.py"), "ок"])
        w.editor.appendPlainText("unsaved_marker = 1")
        w.ask_ai("прочитай")
        self.wait(w)
        self.assertIn("unsaved_marker", prov.seen[1][-1]["content"])

    def test_unsaved_hidden_zone_hides_code_from_agent(self):
        w, prov = self.agent([call("read_file", path="a.py"), "ок"])
        w.on_gutter_mark("hidden", 1, 1)  # зона ещё не сохранена на диск
        w.ask_ai("прочитай")
        self.wait(w)
        self.assertNotIn("return 1", prov.seen[1][-1]["content"])

    def test_stop_button_stops_after_current_step(self):
        w, prov = self.agent([call("list_files")] * 20)
        orig = prov.complete

        def stopping(system, messages):
            out = orig(system, messages)
            if len(prov.seen) == 2:
                w.stop_agent()
            return out
        prov.complete = stopping
        w.ask_ai("долго")
        self.wait(w)
        self.assertLess(len(prov.seen), 6)
        self.assertIn("остановлено", w.chat.history.toPlainText())

    def test_memory_and_persistence_work_in_agent_mode(self):
        w, prov = self.agent(["первый ответ"])
        w.ask_ai("первый вопрос")
        self.wait(w)
        self.assertEqual(len(w.history), 2)
        w.providers["strong"].replies = ["второй ответ"]
        w.ask_ai("второй вопрос")
        self.wait(w)
        self.assertEqual([m["content"] for m in prov.seen[1][:2]], ["первый вопрос", "первый ответ\n[outcome: no edits proposed]"])
        again = self.window()
        self.assertEqual(len(again.history), 4)

    def test_preview_shows_tree_without_closed_files_and_does_not_log(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w, _ = self.agent([])
        w.chat.input.setPlainText("что-то сделай")
        w.preview_request()
        text = w.preview.view.toPlainText()
        self.assertIn("Режим: агент", text)
        self.assertIn("что-то сделай", text)
        self.assertIn("pkg/", text)
        self.assertNotIn("secrets", text.split("=== ДАЛЬШЕ ===")[0].split("=== USER ===")[1])
        self.assertEqual(sendlog.read(self.root), [])

    def test_without_folder_falls_back_to_single_file(self):
        w = MainWindow()
        self.windows.append(w)
        w.load(self.root / "a.py")
        w.confirm_unrestricted = lambda folder: True
        w.review_diff = lambda *a: True
        w.ask_ai("задача")
        self.wait(w)
        self.assertIn("[demo", w.editor.toPlainText())

    def test_demo_agent_end_to_end(self):
        w = self.window()
        w.review_changes = lambda items: [True] * len(items)
        w.ask_ai("добавь комментарий в начало")
        self.wait(w)
        self.assertIn("# [demo", w.editor.toPlainText().splitlines()[0])


class TranslateGuiTest(GuiCase):
    def setUp(self):
        super().setUp()
        from tests.test_translator import Fake
        self.fake = Fake()

    def agent(self, replies, translate=True):
        w = self.window()
        prov = Script(replies)
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        w.translator = Translator(self.fake, log=w.log_translation)
        w.translate_on = translate
        return w, prov

    def test_agent_task_in_english_answer_in_russian_and_history(self):
        w, prov = self.agent(["Added a check to the function"])
        w.ask_ai("добавь проверку в функцию")
        self.wait(w)
        first = prov.seen[0][0]["content"]
        self.assertTrue(first.startswith("add a check in function"))
        users = [m for m in w.chat.messages if m[0] == "user"]
        self.assertEqual(users[0][1], "добавь проверку в функцию")  # человек видит своё
        self.assertIn("Добавил проверку в функцию", w.chat.history.toPlainText())
        self.assertIn("задача переведена: add a check", w.chat.history.toPlainText())
        self.assertEqual((w.history[0].text, w.history[0].en), ("добавь проверку в функцию", "add a check in function"))
        self.assertEqual(w.history[1].en.split("\n")[0], "Added a check to the function")
        self.assertIn("Добавил проверку", w.history[1].text)

    def test_next_request_sends_english_history(self):
        w, prov = self.agent(["Added a check to the function", "Fixed the bug in file"])
        w.ask_ai("добавь проверку в функцию")
        self.wait(w)
        w.ask_ai("исправь ошибку в файле")
        self.wait(w)
        sent = [m["content"] for m in prov.seen[-1]]
        self.assertTrue(sent[0].startswith("add a check in function"))
        self.assertTrue(sent[1].startswith("Added a check to the function"))
        self.assertIsNone(re.search("[А-Яа-яЁё]", "\n".join(sent)))

    def test_translator_calls_are_logged_without_text(self):
        w, prov = self.agent(["Added a check to the function"])
        w.ask_ai("добавь проверку в функцию")
        self.wait(w)
        modes = [e["mode"] for e in sendlog.read(self.root)]
        self.assertIn("translate:ru-en", modes)
        self.assertIn("translate:en-ru", modes)
        raw = (self.root / sendlog.LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("проверку", raw)

    def test_switched_off_sends_original_and_asks_user_language(self):
        w, prov = self.agent(["готово"], translate=False)
        w.ask_ai("добавь проверку в функцию")
        self.wait(w)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(prov.seen[0][0]["content"].startswith("добавь проверку"))
        self.assertIsNone(w.history[0].en)

    def test_translation_failure_is_reported_and_work_continues(self):
        from tests.test_translator import Fake
        w, prov = self.agent(["Added a check"])
        w.translator = Translator(Fake(fail=OSError("offline")))
        w.ask_ai("добавь проверку")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("Задача ушла как есть", text)
        self.assertIn("Added a check", text)
        self.assertTrue(prov.seen[0][0]["content"].startswith("добавь проверку"))

    def test_single_file_mode_and_explain_translate_too(self):
        w = MainWindow()
        self.windows.append(w)
        w.load(self.root / "a.py")
        prov = Script(["Added a check\n```\nx = 2\n```", "Added a check"])
        w.providers = {"cheap": prov, "strong": prov}
        w.confirm_unrestricted = lambda f: True
        w.review_diff = lambda *a: True
        w.translator = Translator(self.fake)
        w.ask_file("добавь проверку")
        self.wait(w)
        self.assertTrue(prov.seen[0][-1]["content"].startswith("add a check"))
        self.assertIn("Добавил проверку", w.chat.history.toPlainText())
        w.marks.add("explain", 0, 1)
        w.explain_ai()
        self.wait(w)
        self.assertIn("Добавил проверку", w.chat.history.toPlainText().split("Добавил проверку", 1)[1])

    def test_toggle_menu_item_persists_and_is_disabled_without_key(self):
        w = self.window()
        self.assertFalse(w.act_translate.isEnabled())  # ключа нет
        w.translator = Translator(self.fake)
        w.act_translate.setEnabled(True)
        w.act_translate.setChecked(True)
        w.toggle_translate()
        self.assertTrue(settings.get_user()["translate"])
        w.act_translate.setChecked(False)
        w.toggle_translate()
        self.assertFalse(w.translate_on)
        self.assertFalse(settings.get_user()["translate"])
        self.assertIsNone(w.active_translator())

    def test_preview_mentions_translation(self):
        w, _ = self.agent([])
        w.chat.input.setPlainText("добавь проверку")
        w.preview_request()
        text = w.preview.view.toPlainText()
        self.assertIn("Перевод: задача уйдёт по-английски", text)
        self.assertIn("Reply in English.", text)
        self.assertEqual(self.fake.calls, [])  # предпросмотр в сеть не ходит


class ConductorGuiTest(GuiCase):
    PLAN = ("@@plan\nsummary: Change b and a\n@@task t1\nworker: haiku\ngoal: Change b.\nfiles: b.py\nrisk: low\n"
            "@@task t2\nworker: opus\ngoal: Change a.\nfiles: a.py\nrisk: high\n@@end")

    def conduct(self, translate=False):
        from aiedit.roster import Roster, Worker
        w = self.window()
        w.conductor_on = True
        w.conductor_provider = Script([self.PLAN])
        self.workers = {"haiku": Script([edit("b.py", "y = 2", "y = 3"), "Changed b."]),
                        "opus": Script([edit("a.py", "return 1", "return 10"), "Changed a."]),
                        "sonnet": Script(["unused"])}
        w.roster = Roster([Worker(k, p, "x", price, price, tier) for (k, p), price, tier in
                           zip(self.workers.items(), (1, 4, 2), ("cheap", "strong", "mid"))])
        w.review_changes = lambda items: [True] * len(items)
        if translate:
            from tests.test_translator import Fake
            self.fake = Fake()
            w.translator, w.translate_on = Translator(self.fake), True
        return w

    def test_plan_is_shown_steps_go_to_workers_and_edits_are_merged(self):
        w = self.conduct()
        w.ask_ai("change b and a")
        self.wait(w)
        system = [m[1] for m in w.chat.messages if m[0] == "system"]
        self.assertIn("дирижёр составляет план...", system)
        self.assertIn("t1 → haiku: Change b.", system)
        self.assertIn("t2 → opus: Change a.", system)
        self.assertEqual((self.root / "b.py").read_text(), "y = 3\n")
        self.assertIn("return 10", w.editor.toPlainText())
        self.assertEqual(len(self.workers["sonnet"].seen), 0)
        meta = next(m[2] for m in w.chat.messages if m[0] == "ai")
        self.assertIn("дирижёр", meta)
        self.assertIn("haiku, opus", meta)
        self.assertIn("edits applied", w.history[-1].text)

    def test_translation_applies_to_task_and_summary(self):
        w = self.conduct(translate=True)
        w.conductor_provider = Script([self.PLAN])
        w.ask_ai("добавь проверку в функцию")
        self.wait(w)
        request = w.conductor_provider.seen[0][-1]["content"]
        self.assertTrue(request.startswith("REQUEST:\nadd a check in function"))
        self.assertIsNone(re.search("[А-Яа-яЁё]", request))
        self.assertTrue(w.conductor_provider.systems[0].endswith("Reply in English."))
        self.assertIsNotNone(w.history[0].en)

    def test_toggle_persists_and_direct_mode_returns(self):
        w = self.window()
        w.act_conductor.setChecked(True)
        w.toggle_conductor()
        self.assertTrue(settings.get_user()["conductor"])
        w.act_conductor.setChecked(False)
        w.toggle_conductor()
        self.assertFalse(w.conductor_on)
        self.assertFalse(settings.get_user()["conductor"])
        w.conductor_provider = Script([])
        prov = Script(["direct answer"])
        w.providers = {"cheap": prov, "strong": prov}
        w.ask_ai("hello")
        self.wait(w)
        self.assertEqual(w.conductor_provider.seen, [])
        self.assertIn("direct answer", w.chat.history.toPlainText())

    def test_default_is_on_and_demo_pipeline_works_without_keys(self):
        w = MainWindow()
        self.windows.append(w)
        w.analyze_on = False
        self.assertTrue(w.conductor_on)
        w.tree.set_root(self.root)
        w.acknowledged.add(self.root)
        w.review_changes = lambda items: [True] * len(items)
        w.ask_ai("write a test; fix the bug; add a docstring")
        self.wait(w)
        lines = [m[1] for m in w.chat.messages if m[0] == "system"]
        self.assertIn("t1 → sonnet: write a test", lines)
        self.assertIn("t2 → opus: fix the bug", lines)  # по умолчанию Opus - Backend-специалист и самый сильный
        self.assertIn("t3 → sonnet: add a docstring", lines)
        self.assertFalse([l for l in lines if "unknown worker" in l or "заменён" in l])

    def test_backend_step_goes_to_the_backend_model_with_default_roster(self):
        w = MainWindow()
        self.windows.append(w)
        w.analyze_on = False
        w.tree.set_root(self.root)
        w.acknowledged.add(self.root)
        w.review_changes = lambda items: [True] * len(items)
        w.ask_ai("add an api endpoint; write a test; update the readme")
        self.wait(w)
        lines = [m[1] for m in w.chat.messages if m[0] == "system"]
        self.assertIn("t1 → opus: add an api endpoint", lines)
        self.assertIn("t2 → sonnet: write a test", lines)
        self.assertIn("t3 → sonnet: update the readme", lines)

    def test_preview_describes_conductor_and_does_not_log_or_call(self):
        policy.Rules(self.root).set_denied(self.root / "secrets", True)
        w = self.conduct()
        w.chat.input.setPlainText("change b")
        w.preview_request()
        text = w.preview.view.toPlainText()
        self.assertIn("Режим: дирижёр", text)
        self.assertIn("Исполнители: haiku", text)
        self.assertIn("REQUEST:\nchange b", text)
        self.assertNotIn("secrets", text.split("=== USER ===")[1])
        self.assertEqual(w.conductor_provider.seen, [])
        self.assertEqual(sendlog.read(self.root), [])

    def test_stop_button_stops_the_plan(self):
        w = self.conduct()
        orig = self.workers["haiku"].complete

        def stopping(system, messages):
            w.stop_agent()
            return orig(system, messages)
        self.workers["haiku"].complete = stopping
        w.ask_ai("change b and a")
        self.wait(w)
        self.assertEqual(len(self.workers["opus"].seen), 0)
        self.assertIn("остановлено", w.chat.history.toPlainText())


class ConnectionsGuiTest(GuiCase):
    def dialog(self, rows=(), roles=None, ping_fn=None):
        from aiedit.connections import Roles
        from aiedit.connections_dialog import ConnectionsDialog
        kw = {"ping_fn": ping_fn} if ping_fn else {}
        dlg = ConnectionsDialog(None, list(rows), roles or Roles(), **kw)
        self.addCleanup(dlg.close)
        return dlg

    @staticmethod
    def pick(dlg, row, model, key="", extra=""):
        dlg.combos[row].setCurrentIndex(dlg.combos[row].findData(model))
        dlg.keys[row].setText(key)
        if extra:
            dlg.extras[row].setText(extra)

    def test_four_rows_and_only_filled_ones_count(self):
        dlg = self.dialog()
        self.assertEqual((len(dlg.combos), len(dlg.keys), len(dlg.status)), (4, 4, 4))
        self.assertEqual(dlg.rows(), [])
        self.pick(dlg, 0, "gpt-4o-mini", "sk-a")
        self.pick(dlg, 3, "sonnet", "sk-b")
        self.assertEqual([(c.model, c.key) for c in dlg.rows()], [("gpt-4o-mini", "sk-a"), ("sonnet", "sk-b")])

    def test_keys_are_hidden_until_asked(self):
        from aiedit.qt import QLineEdit
        dlg = self.dialog()
        self.assertEqual(dlg.keys[0].echoMode(), QLineEdit.EchoMode.Password)
        dlg.show_keys.setChecked(True)
        self.assertEqual(dlg.keys[0].echoMode(), QLineEdit.EchoMode.Normal)
        dlg.show_keys.setChecked(False)
        self.assertEqual(dlg.keys[3].echoMode(), QLineEdit.EchoMode.Password)

    def test_extra_field_only_for_custom_model(self):
        dlg = self.dialog()
        dlg.show()
        self.pick(dlg, 0, "custom")
        self.assertTrue(dlg.extras[0].isVisible())
        self.pick(dlg, 0, "sonnet")
        self.assertFalse(dlg.extras[0].isVisible())

    def test_role_lists_follow_rows_and_keep_choice(self):
        dlg = self.dialog()
        self.pick(dlg, 0, "gpt-4o-mini", "a")
        self.pick(dlg, 1, "sonnet", "b")
        self.assertEqual([dlg.conductor.itemData(i) for i in range(dlg.conductor.count())], ["", "gpt-4o-mini", "sonnet"])
        self.assertEqual([dlg.translator.itemData(i) for i in range(dlg.translator.count())], ["", "off", "gpt-4o-mini", "sonnet"])
        dlg.conductor.setCurrentIndex(dlg.conductor.findData("sonnet"))
        self.pick(dlg, 2, "opus", "c")  # добавили строку: выбор не слетает
        self.assertEqual(dlg.roles().conductor, "sonnet")
        self.pick(dlg, 1, "")  # убрали выбранную: откат на авто
        self.assertEqual(dlg.roles().conductor, "")

    def test_saved_rows_are_prefilled(self):
        from aiedit.connections import Connection, Roles
        dlg = self.dialog([Connection("gpt-4o", "sk-1"), Connection("custom", "", "llama3 | http://x/v1")],
                          Roles("llama3", "off"))
        self.assertEqual([(c.model, c.key, c.extra) for c in dlg.rows()],
                         [("gpt-4o", "sk-1", ""), ("custom", "", "llama3 | http://x/v1")])
        self.assertEqual((dlg.roles().conductor, dlg.roles().translator), ("llama3", "off"))

    def test_problems_ask_before_saving(self):
        dlg = self.dialog()
        self.pick(dlg, 0, "sonnet", "")  # ключа нет
        asked = []
        dlg.confirm_problems = lambda problems: (asked.append(problems), False)[1]
        dlg.try_accept()
        self.assertEqual(len(asked), 1)
        self.assertFalse(dlg.result_rows)
        dlg.confirm_problems = lambda problems: True
        dlg.try_accept()
        self.assertEqual([c.model for c in dlg.result_rows], ["sonnet"])

    def test_check_shows_status_per_row_without_key(self):
        dlg = self.dialog(ping_fn=lambda c: (c.key == "good", "работает" if c.key == "good" else "ключ не принят (HTTP 401)"))
        self.pick(dlg, 0, "gpt-4o", "good")
        self.pick(dlg, 2, "sonnet", "bad-key-text")
        dlg.check_all()
        dlg.worker.wait()
        app.processEvents()
        self.assertEqual(dlg.status[0].text(), "OK: работает")
        self.assertEqual(dlg.status[2].text(), "Ошибка: ключ не принят (HTTP 401)")
        self.assertEqual(dlg.status[1].text(), "")
        self.assertNotIn("bad-key-text", dlg.status[2].text())

    def test_saved_glm_without_address_shows_the_default_address(self):
        from aiedit.connections import Connection
        dlg = self.dialog([Connection("glm", "z-key")])
        self.assertEqual(dlg.extras[0].text(), "glm-5.3 | https://api.z.ai/api/paas/v4")
        dlg = self.dialog([Connection("glm", "z-key", "glm-5.3 | https://x.example/v4")])
        self.assertEqual(dlg.extras[0].text(), "glm-5.3 | https://x.example/v4")

    def test_dialog_has_no_purpose_picker_the_model_decides(self):
        dlg = self.dialog()
        self.assertFalse(hasattr(dlg, "specialties"))
        self.pick(dlg, 0, "opus", "k")
        self.assertEqual([c.model for c in dlg.rows()], ["opus"])
        self.assertFalse(hasattr(dlg.rows()[0], "specialty"))

    def test_glm_row_prefills_address_and_security_role(self):
        dlg = self.dialog()
        dlg.show()
        self.pick(dlg, 0, "glm", "z-key")
        self.assertTrue(dlg.extras[0].isVisible())
        self.assertEqual(dlg.extras[0].text(), "glm-5.3 | https://api.z.ai/api/paas/v4")
        dlg.extras[0].setText("glm-5.3 | https://open.bigmodel.cn/api/paas/v4")  # человек поправил адрес
        row = dlg.rows()[0]
        self.assertEqual((row.model, row.extra), ("glm", "glm-5.3 | https://open.bigmodel.cn/api/paas/v4"))
        self.pick(dlg, 0, "sonnet", "k")
        self.assertEqual((dlg.extras[0].text(), dlg.extras[0].isVisible()), ("", False))

    def test_specialty_reaches_roster_and_summary(self):
        from aiedit.connections import Connection, Roles
        w = self.window()
        w.save_connections([Connection("sonnet", "a"), Connection("opus", "b")], Roles())
        self.assertEqual(w.roster.get("opus").specialty, "backend")
        self.assertIn("подключено: sonnet, opus (Backend)", w.chat.history.toPlainText())

    def test_only_openai_is_enough_and_applies_without_restart(self):
        from aiedit import connections
        from aiedit.connections import Connection, Roles
        w = self.window()
        self.assertFalse(w.act_translate.isEnabled())  # ключей нет
        self.assertTrue(w.save_connections([Connection("gpt-4o-mini", "sk-oa")], Roles()))
        self.assertEqual(w.roster.keys(), ["gpt-4o-mini"])
        self.assertEqual(w.conductor_provider.model, "gpt-4o-mini")
        self.assertIsNotNone(w.translator)
        self.assertEqual(w.translator.model, "gpt-4o-mini")
        self.assertTrue(w.act_translate.isEnabled())
        self.assertIn("gpt-4o-mini", w.statusBar().currentMessage())
        self.assertEqual(oct(connections.file_path().stat().st_mode & 0o777), "0o600")
        text = w.chat.history.toPlainText() + w.statusBar().currentMessage()
        self.assertIn("подключено: gpt-4o-mini", text)
        self.assertNotIn("sk-oa", text)

    def test_new_window_loads_saved_connections(self):
        from aiedit.connections import Connection, Roles
        self.window().save_connections([Connection("gpt-4o", "k1"), Connection("haiku", "k2")], Roles("haiku", "off"))
        again = self.window()
        self.assertEqual(again.roster.keys(), ["gpt-4o", "haiku"])
        self.assertEqual(again.conductor_provider.model, "claude-haiku-5-5")
        self.assertIsNone(again.translator)
        self.assertFalse(again.act_translate.isEnabled())

    def test_clearing_all_rows_returns_to_env_or_demo(self):
        from aiedit.connections import Connection, Roles
        w = self.window()
        w.save_connections([Connection("gpt-4o", "k")], Roles())
        w.save_connections([], Roles())
        self.assertEqual(w.ai_setup.source, "demo")
        self.assertEqual(w.roster.keys(), ["sonnet", "opus"])
        self.assertIn("подключений нет", w.chat.history.toPlainText())

    def test_connected_model_really_answers_end_to_end(self):
        from aiedit.connections import Connection, Roles
        from tests.test_catalog import server
        base, seen = server(self, reply={"choices": [{"message": {"content": "Done by the local model."}}]})
        w = self.window()
        w.save_connections([Connection("custom", "", f"my-local | {base}")], Roles())
        w.conductor_on = False
        w.ask_ai("hello there")
        self.wait(w)
        self.assertIn("Done by the local model.", w.chat.history.toPlainText())
        body = seen["body"]
        self.assertEqual(body["model"], "my-local")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertTrue(body["messages"][1]["content"].startswith("hello there"))

    def test_skipped_rows_are_reported_without_keys(self):
        from aiedit.connections import Connection, Roles
        w = self.window()
        w.save_connections([Connection("sonnet", ""), Connection("gpt-4o", "sk-SHOW-NOT")], Roles())
        text = w.chat.history.toPlainText()
        self.assertIn("подключение пропущено", text)
        self.assertNotIn("sk-SHOW-NOT", text)

    def test_menu_has_connections_item(self):
        w = self.window()
        titles = [a.text() for m in w.menuBar().findChildren(type(w.menuBar().addMenu("x"))) for a in m.actions()]
        self.assertIn("Подключения ИИ...", titles)


class NoteGuiTest(GuiCase):
    CODE = "def commission(amount: int) -> float:\n    return amount * 0.07\n\n\ndef free():\n    return 1\n"

    def setUp(self):
        super().setUp()
        (self.root / "fee.py").write_text(self.CODE)

    def window_with_zone(self, level="interface"):
        w = self.window("fee.py")
        w.on_gutter_mark(level, 0, 1)
        return w

    def test_note_is_set_saved_and_shown_to_the_agent(self):
        w = self.window_with_zone()
        w.ask_note = lambda zone: "fee for a payment, amount in cents"
        w.on_gutter_mark("note", 0, 1)
        self.assertEqual(w.marks.zone_at(0).note, "fee for a payment, amount in cents")
        w.save_file()
        self.assertIn("fee for a payment", (self.root / "fee.py.aimarks.json").read_text(encoding="utf-8"))
        prov = Script(["@@tool read_file\npath: fee.py\n@@end", "ok"])
        w.providers = {"cheap": prov, "strong": prov}
        w.ask_ai("explain fee")
        self.wait(w)
        seen = prov.seen[1][-1]["content"]
        self.assertIn("| note: fee for a payment, amount in cents>>", seen)
        self.assertIn("def commission(amount: int) -> float", seen)
        self.assertNotIn("0.07", seen)

    def test_unsaved_note_is_used_too(self):
        w = self.window("fee.py")
        w.editor.appendPlainText("# unsaved edit")  # несохранённый текст: зоны и заметка ждут сохранения
        w.on_gutter_mark("hidden", 0, 1)
        w.ask_note = lambda zone: "calculates the fee"
        w.on_gutter_mark("note", 0, 1)
        self.assertFalse((self.root / "fee.py.aimarks.json").exists())
        prov = Script(["@@tool read_file\npath: fee.py\n@@end", "ok"])
        w.providers = {"cheap": prov, "strong": prov}
        w.ask_ai("go")
        self.wait(w)
        self.assertIn("lines | note: calculates the fee>>", prov.seen[1][-1]["content"])

    def test_note_only_on_interface_or_hidden(self):
        w = self.window_with_zone("readonly")
        asked = []
        w.ask_note = lambda zone: asked.append(zone) or "x"
        w.on_gutter_mark("note", 0, 1)
        self.assertEqual(asked, [])
        self.assertIn("только на зону interface или hidden", w.chat.history.toPlainText())

    def test_cancel_keeps_old_note_and_empty_text_removes_it(self):
        w = self.window_with_zone()
        w.ask_note = lambda zone: "first"
        w.on_gutter_mark("note", 0, 1)
        w.ask_note = lambda zone: None
        w.on_gutter_mark("note", 0, 1)
        self.assertEqual(w.marks.zone_at(0).note, "first")
        seen = []
        w.ask_note = lambda zone: seen.append(zone.note) or ""
        w.on_gutter_mark("note", 0, 1)
        self.assertEqual((seen, w.marks.zone_at(0).note), (["first"], ""))

    def test_russian_note_is_translated_for_the_model(self):
        from tests.test_translator import Fake
        w = self.window_with_zone()
        w.translator = Translator(Fake(ru_en={"комиссия": "fee", "за": "for", "платёж": "a payment"}))
        w.translate_on = True
        w.ask_note = lambda zone: "комиссия за платёж"
        w.on_gutter_mark("note", 0, 1)
        for t in list(w.note_workers):
            t.wait()
        app.processEvents()
        zone = w.marks.zone_at(0)
        self.assertEqual((zone.note, zone.note_en), ("комиссия за платёж", "fee for a payment"))
        redacted = __import__("aiedit.guard", fromlist=["x"]).redact(w.editor.toPlainText(), w.marks.items).text
        self.assertIn("note: fee for a payment", redacted)
        self.assertNotIn("комиссия", redacted)

    def test_translation_result_is_dropped_if_note_changed_meanwhile(self):
        from tests.test_translator import Fake
        w = self.window_with_zone()
        w.translator = Translator(Fake(ru_en={"комиссия": "fee"}))
        w.translate_on = True
        w.ask_note = lambda zone: "комиссия"
        w.on_gutter_mark("note", 0, 1)
        w.marks.set_note(0, "другое")  # человек поменял, пока шёл перевод
        for t in list(w.note_workers):
            t.wait()
        app.processEvents()
        self.assertEqual(w.marks.zone_at(0).note_en, "")

    def test_russian_note_without_translator_warns(self):
        w = self.window_with_zone()
        w.ask_note = lambda zone: "комиссия"
        w.on_gutter_mark("note", 0, 1)
        self.assertIn("уйдёт модели как написана", w.chat.history.toPlainText())
        self.assertEqual(w.note_workers, [])

    def test_gutter_shows_tooltip_and_menu_entry_state(self):
        w = self.window_with_zone()
        w.ask_note = lambda zone: "fee in cents"
        w.on_gutter_mark("note", 0, 1)
        region = w.editor.regions[0]
        self.assertEqual(region.note, "fee in cents")
        w.on_gutter_mark("readonly", 4, 5)
        self.assertEqual(w.editor.regions[4].level, "readonly")

    def test_note_survives_ai_edit_and_undo(self):
        w = self.window_with_zone()
        w.ask_note = lambda zone: "kept"
        w.on_gutter_mark("note", 0, 1)
        w.editor.setPlainText("# new first line\n" + w.editor.toPlainText())
        self.assertEqual(w.marks.zone_at(1).note, "kept")  # зона сдвинулась вместе с заметкой
        w.undo()
        self.assertEqual(w.marks.zone_at(0).note, "kept")


class ReviewGuiTest(GuiCase):
    HIGH = ("@@findings\nsummary: injection\n@@finding f1\nseverity: high\nfile: server.py\nline: 2\n"
            "issue: SQL built by concatenation\nfix: use parameters\n@@end")
    CLEAN = "@@findings\nsummary: no issues found\n@@end"
    PLAN = ("@@plan\nsummary: add login\n@@task t1\nworker: opus\ngoal: Write the login.\nfiles: server.py\nrisk: high\n@@end")

    def setUp(self):
        super().setUp()
        (self.root / "server.py").write_text("def login(user):\n    q = 'SELECT ' + user\n    return run(q)\n")
        (self.root / "hidden.py").write_text("SECRET_LOGIC = 1\n")
        from aiedit.marks import Marks
        m = Marks(); m.add("hidden", 0, 0); m.save(self.root / "hidden.py", "SECRET_LOGIC = 1\n")

    def window_with_reviewer(self, reviewer, opus, conductor=True):
        from aiedit.roster import Roster, Worker
        w = self.window("a.py")
        w.conductor_on = conductor
        self.p = {"glm": Script(reviewer), "sonnet": Script(["ok"]), "opus": Script(opus)}
        w.conductor_provider = Script([self.PLAN])
        w.roster = Roster([Worker("sonnet", self.p["sonnet"], "mid", 2, 10, "mid"),
                           Worker("opus", self.p["opus"], "strong", 4, 20, "strong", "backend"),
                           Worker("glm", self.p["glm"], "review", 0, 0, "mid", "security")])
        w.providers = w.roster.providers()
        w.review_on = True
        w.refresh_review_action()
        self.items = []
        w.review_changes = lambda items: (self.items.extend(items), [True] * len(items))[1]
        return w

    def test_review_fix_loop_report_and_memory(self):
        w = self.window_with_reviewer([self.HIGH, self.CLEAN], [
            "@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "wrote it",
            "@@tool edit_file\npath: server.py\n@@old\n'SELECT ' + user\n@@new\n'SELECT ?'\n@@end", "fixed"])
        w.ask_ai("add a login")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("безопасность: проверяет glm", text)
        self.assertIn("безопасность (glm): замечаний нет, исправлено за 1 кр.", text)
        self.assertIn("SELECT ?", (self.root / "server.py").read_text())
        self.assertIn("[outcome: edits applied: server.py; security review: clean after 1 fix round(s)]", w.history[-1].text)
        self.assertIsNone(re.search("[А-Яа-яЁё]", w.history[-1].en or ""))

    def test_remaining_findings_and_hidden_gaps_reach_chat_and_the_review_dialog(self):
        w = self.window_with_reviewer(
            [self.HIGH, self.HIGH],
            ["@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "wrote it",
             "This is a false positive."])
        w.ask_ai("add a login")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("осталось замечаний 1", text)
        self.assertIn("[high] server.py:2 - SQL built by concatenation", text)
        self.assertIn("ответ автора: opus: This is a false positive.", text)
        notes = self.items[0]["notes"]
        self.assertTrue(any("Безопасность [high] строка 2" in n for n in notes), notes)
        self.assertIn("security review: 1 open finding(s) (high: 1) after 0 fix round(s)", w.history[-1].text)

    def test_unreadable_reviewer_reply_is_reported(self):
        w = self.window_with_reviewer(["Looks fine."], ["@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "ok"])
        w.ask_ai("add a login")
        self.wait(w)
        self.assertIn("ответ проверяющего не разобран", w.chat.history.toPlainText())
        self.assertIn("could not be parsed", w.history[-1].text)

    def test_hidden_code_is_called_out_as_unreviewed(self):
        w = self.window_with_reviewer(["@@tool read_file\npath: hidden.py\n@@end", self.CLEAN],
                                      ["@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "ok"])
        w.ask_ai("add a login")
        self.wait(w)
        self.assertIn("не проверено, закрытый код: hidden.py - 1 из 2 строк", w.chat.history.toPlainText())
        self.assertNotIn("SECRET_LOGIC", self.p["glm"].everything())

    def test_direct_mode_reviews_too(self):
        w = self.window_with_reviewer([self.CLEAN], [], conductor=False)
        self.p["sonnet"].replies = ["@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "wrote it"]
        w.ask_ai("add a login")
        self.wait(w)
        self.assertIn("безопасность (glm): замечаний нет", w.chat.history.toPlainText())
        self.assertEqual(len(self.p["opus"].seen), 0)

    def test_toggle_persists_and_off_means_no_reviewer_calls(self):
        w = self.window_with_reviewer([self.CLEAN], ["@@tool edit_file\npath: server.py\n@@old\nreturn run(q)\n@@new\nreturn run(q)  # v1\n@@end", "ok"])
        w.act_review.setChecked(False)
        w.toggle_review()
        self.assertFalse(settings.get_user()["review"])
        w.ask_ai("add a login")
        self.wait(w)
        self.assertEqual(self.p["glm"].seen, [])
        self.assertNotIn("безопасность (", w.chat.history.toPlainText())

    def test_action_is_disabled_without_a_reviewer_and_enabled_after_connecting_glm(self):
        from aiedit.connections import Connection, Roles
        w = self.window()
        self.assertFalse(w.act_review.isEnabled())
        self.assertFalse(w.review_active())
        w.save_connections([Connection("sonnet", "a"), Connection("glm", "z-key")], Roles())
        self.assertTrue(w.act_review.isEnabled())
        self.assertTrue(w.act_review.isChecked())
        self.assertEqual(w.roster.reviewer().key, "glm")
        self.assertIn("проверка: glm-5.3", w.statusBar().currentMessage())
        self.assertNotIn("glm-5.3", [x.provider.name for x in w.roster.by_price()])  # в исполнители не попал
        self.assertIn("glm (Безопасность", w.chat.history.toPlainText())


class AnalysisGuiTest(GuiCase):
    SCRIPT = ("import sys\nfor f in sys.argv[1:]:\n"
              "    for n, l in enumerate(open(f, encoding='utf-8'), 1):\n"
              "        if 'BAD' in l: print(f'{f}:{n}: unused thing')\n")

    def setUp(self):
        super().setUp()
        from aiedit import analysis
        self.analysis = analysis
        self.saved_analyzers = analysis.ANALYZERS
        analysis.ANALYZERS = [analysis.Analyzer("lint", (".py",), ("-c", self.SCRIPT), self.parse, binary=sys.executable)]
        (self.root / "main.py").write_text("def run():\n    return 1\n")

    def tearDown(self):
        self.analysis.ANALYZERS = self.saved_analyzers
        super().tearDown()

    @staticmethod
    def parse(out):
        from aiedit.analysis import Issue
        return [Issue("lint", m[0].removeprefix("./"), int(m[1]), "W1", m[2])
                for m in re.findall(r"^(\S+):(\d+): (.+)$", out, re.M)]

    def window_with(self, replies, conductor=False):
        w = self.window("main.py")
        w.analyze_on = True
        w.conductor_on = conductor
        self.prov = Script(replies)
        w.providers = {"cheap": self.prov, "strong": self.prov}
        w.review_changes = lambda items: (self.items.extend(items), [True] * len(items))[1]
        self.items = []
        return w

    BAD_EDIT = "@@tool edit_file\npath: main.py\n@@old\nreturn 1\n@@new\nreturn 2  # BAD\n@@end"
    FIX_EDIT = "@@tool edit_file\npath: main.py\n@@old\n# BAD\n@@new\n# fine\n@@end"

    def test_problem_found_returned_to_the_author_and_fixed_before_you_see_the_diff(self):
        w = self.window_with([self.BAD_EDIT, "wrote it", self.FIX_EDIT, "fixed the warning"])
        w.ask_ai("change run")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("статанализ: новых замечаний 1, возвращаю авторам", text)
        self.assertIn("статанализ (lint): новых замечаний нет, исправлено за 1 кр.", text)
        self.assertEqual(self.items[0]["new"], "def run():\n    return 2  # fine\n")  # ты видишь уже исправленное
        self.assertIn("static analysis (lint): clean after 1 fix round(s)", w.history[-1].text)
        self.assertIsNone(re.search("[А-Яа-яЁё]", (w.history[-1].en or "") + self.prov.everything()))

    def test_remaining_issue_is_shown_in_chat_and_in_the_dialog(self):
        w = self.window_with([self.BAD_EDIT, "wrote it", "I will not touch it."])
        w.ask_ai("change run")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("осталось новых замечаний 1", text)
        self.assertIn("[lint W1] main.py:2 - unused thing", text)
        self.assertTrue(any("Статанализ [lint W1] строка 2: unused thing" in n for n in self.items[0]["notes"]))
        self.assertIn("static analysis (lint): 1 open issue(s) after 0 fix round(s)", w.history[-1].text)

    def test_runs_before_the_security_review_in_conductor_mode(self):
        from aiedit.roster import Roster, Worker
        w = self.window_with([], conductor=True)
        glm, sonnet = Script(["@@findings\nsummary: no issues found\n@@end"]), Script([self.BAD_EDIT, "ok", self.FIX_EDIT, "ok"])
        w.conductor_provider = Script(["@@plan\nsummary: s\n@@task t1\nworker: sonnet\ngoal: change run\nfiles: main.py\nrisk: low\n@@end"])
        w.roster = Roster([Worker("sonnet", sonnet, "mid", 2, 10, "mid"), Worker("glm", glm, "review", 0, 0, "mid", "security")])
        w.review_on = True
        w.ask_ai("change run")
        self.wait(w)
        lines = [m[1] for m in w.chat.messages if m[0] == "system"]
        first_analysis = next(i for i, l in enumerate(lines) if l.startswith("статанализ"))
        first_review = next(i for i, l in enumerate(lines) if l.startswith("безопасность"))
        self.assertLess(first_analysis, first_review)
        self.assertIn("# fine", glm.seen[0][-1]["content"])  # безопасник видит уже исправленный код

    def test_toggle_persists_and_off_means_no_analyzer_run(self):
        w = self.window_with([self.BAD_EDIT, "wrote it"])
        w.act_analyze.setChecked(False)
        w.toggle_analyze()
        self.assertFalse(settings.get_user()["analyze"])
        w.ask_ai("change run")
        self.wait(w)
        self.assertNotIn("статанализ (", w.chat.history.toPlainText())
        self.assertNotIn("static analysis", w.history[-1].text)

    def test_toggle_message_lists_found_and_missing_programs(self):
        self.analysis.ANALYZERS = [self.analysis.Analyzer("lint", (".py",), (), self.parse, binary=sys.executable),
                                   self.analysis.Analyzer("ghost", (".py",), (), self.parse, binary="no-such-tool-xyz")]
        w = self.window("main.py")
        w.act_analyze.setChecked(True)
        w.toggle_analyze()
        text = w.chat.history.toPlainText()
        self.assertIn("статанализ включён", text)
        self.assertIn("Не найдены: no-such-tool-xyz", text)

    def test_no_analyzers_installed_is_reported_once_and_does_not_fail(self):
        self.analysis.ANALYZERS = [self.analysis.Analyzer("ghost", (".py",), (), self.parse, binary="no-such-tool-xyz")]
        w = self.window_with([self.BAD_EDIT, "ok", self.BAD_EDIT.replace("return 2", "return 3").replace("return 1", "return 2  # BAD"), "ok"])
        w.ask_ai("change run")
        self.wait(w)
        text = w.chat.history.toPlainText()
        self.assertIn("статанализ: ghost: программа не найдена", text)
        self.assertIn("ни одна программа не отработала", text)
        self.assertIn("static analysis: no analyzers available, nothing was checked", w.history[-1].text)
        w.ask_ai("again")
        self.wait(w)
        self.assertEqual(w.chat.history.toPlainText().count("ghost: программа не найдена"), 1)


class JournalGuiTest(GuiCase):
    def setUp(self):
        super().setUp()
        (self.root / "big.py").write_text("x = 1\n" + "".join(f"def old{i}():\n    return {i}\n" for i in range(12)))
        (self.root / "small.py").write_text("a = 1\nb = 2\nc = 3\n")

    def hunks(self):
        from aiedit.journal import Hunk
        return [Hunk("b.py", 5, 6, 2, 0, "added", "two lines", "sonnet", ["Статанализ [ruff F841]: unused"]),
                Hunk("a.py", 1, 40, 40, 3, "changed", "big change", "opus"),
                Hunk("a.py", 90, 90, 1, 1, "changed", "tiny", "opus"),
                Hunk("c.py", 9, 9, 0, 1, "removed", "удалено строк: 1")]

    def view(self, hunks=None, goto=None):
        from aiedit.journal_dialog import JournalView
        v = JournalView(None, self.hunks() if hunks is None else hunks, goto or (lambda f, l: None))
        self.addCleanup(v.close)
        return v

    def test_files_are_sorted_by_total_size_and_places_inside_by_size(self):
        v = self.view()
        top = [v.tree.topLevelItem(i) for i in range(v.tree.topLevelItemCount())]
        self.assertEqual([t.text(0) for t in top], ["a.py", "b.py", "c.py"])
        self.assertEqual([t.text(1) for t in top], ["+41 -4", "+2 -0", "+0 -1"])
        self.assertEqual(top[0].text(2), "2 мест")
        self.assertEqual([top[0].child(i).text(0) for i in range(2)], ["строки 1-40", "строка 90"])
        self.assertEqual([top[0].child(i).text(1) for i in range(2)], ["+40 -3", "+1 -1"])
        self.assertEqual(top[0].text(4), "opus")
        self.assertEqual(top[1].text(5), "1")
        self.assertIn("unused", top[1].child(0).toolTip(5))
        self.assertIn("4 мест в 3 файлах, +43 -5 строк", v.summary.text())

    def test_file_click_opens_the_biggest_place_and_place_click_its_own_line(self):
        seen = []
        v = self.view(goto=lambda f, l: seen.append((f, l)))
        a = v.file_items["a.py"]
        v.open_item(a)
        v.open_item(a.child(1))
        v.open_item(v.file_items["b.py"])
        self.assertEqual(seen, [("a.py", 1), ("a.py", 90), ("b.py", 5)])

    def test_opened_places_become_read_and_progress_is_counted_once(self):
        v = self.view()
        a = v.file_items["a.py"]
        v.open_item(a.child(0))
        v.open_item(a.child(0))
        self.assertEqual(len(v.read), 1)
        self.assertIn("Просмотрено мест: 1 из 4", v.summary.text())
        self.assertNotEqual(v.file_items["a.py"].foreground(0).color().name(), "#828282")  # одно из двух мест
        v.open_item(a.child(1))
        self.assertEqual(v.file_items["a.py"].foreground(0).color().name(), "#828282")  # файл прочитан целиком
        self.assertIn("Просмотрено мест: 2 из 4", v.summary.text())

    def test_selection_shows_file_summary_or_place_diff(self):
        from aiedit.journal import hunks_for
        h = hunks_for("a.py", "a\nb\nc\n", "a\nB\nc\n", "opus")
        h[0].remarks.append("Безопасность [high]: injection")
        v = self.view(h)
        self.assertIn("мест 1", v.detail.toPlainText())  # выбран первый файл
        v.tree.setCurrentItem(v.hunk_items[0])
        text = v.detail.toPlainText()
        self.assertIn("-b", text)
        self.assertIn("+B", text)
        self.assertIn("Безопасность [high]: injection", text)

    def test_empty_journal_does_not_crash(self):
        v = self.view([])
        self.assertIn("правок нет", v.summary.text())
        v.open_item(None)
        v.show_detail(None)

    def agent_window(self, journal=True):
        w = self.window("big.py")
        w.journal_on = journal
        big = "".join(f"x{i} = {i}\n" for i in range(12))
        prov = Script([f"@@tool edit_file\npath: big.py\n@@old\nx = 1\n@@new\n{big}\n@@end",
                       "@@tool edit_file\npath: small.py\n@@old\nb = 2\n@@new\nb = 22\n@@end", "done"])
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        return w

    def run_agent(self, w):
        w.ask_ai("change both")
        self.wait(w)

    def test_journal_opens_as_a_tab_next_to_the_files(self):
        w = self.agent_window()
        self.run_agent(w)
        self.assertIsNotNone(w.journal_view)
        self.assertIs(w.tabs.currentWidget(), w.journal_view)
        titles = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        self.assertIn("Журнал правок (2)", titles)
        self.assertEqual(w.journal_view.files, ["big.py", "small.py"])  # самый большой файл первым

    def test_clicking_files_opens_each_in_its_own_tab_and_the_journal_stays(self):
        w = self.agent_window()
        self.run_agent(w)
        before = w.tabs.count()
        w.journal_view.open_item(w.journal_view.file_items["small.py"])
        self.assertEqual(w.doc.name, "small.py")
        self.assertEqual(w.editor.textCursor().blockNumber() + 1, 2)  # строка правки
        w.journal_view.open_item(w.journal_view.file_items["big.py"])
        self.assertEqual(w.doc.name, "big.py")
        names = [d.name for d in w.docs]
        self.assertEqual(names.count("small.py"), 1)
        self.assertEqual(names.count("big.py"), 1)  # уже открытый файл не дублируется
        self.assertEqual(w.tabs.count(), before + 1)  # big.py был открыт, small.py добавил одну вкладку
        self.assertIsNotNone(w.journal_view)
        self.assertGreaterEqual(w.tabs.indexOf(w.journal_view), 0)

    def test_app_keeps_working_while_the_journal_tab_is_current(self):
        w = self.agent_window()
        self.run_agent(w)
        self.assertIs(w.tabs.currentWidget(), w.journal_view)
        self.assertEqual(w.path.name, "big.py")  # последний файл, а не падение
        self.assertTrue(w.editor.toPlainText())
        w.save_file()
        w.refresh_access()
        self.assertTrue(w.ask_btn.isEnabled())

    def test_mark_hotkeys_switch_back_to_the_file_instead_of_marking_blindly(self):
        w = self.agent_window()
        self.run_agent(w)
        self.assertIs(w.tabs.currentWidget(), w.journal_view)
        w.mark("readonly")
        self.assertIs(w.tabs.currentWidget(), w.doc.editor)  # показали файл, на котором поставили метку
        self.assertEqual([r.level for r in w.marks.items], ["readonly"])

    def test_closing_the_journal_tab_and_a_second_run_replace_it(self):
        w = self.agent_window()
        self.run_agent(w)
        w.close_tab(w.tabs.indexOf(w.journal_view))
        self.assertIsNone(w.journal_view)
        self.assertEqual(w.doc.name, "big.py")
        w.open_journal()
        w.open_journal()
        journals = [i for i in range(w.tabs.count()) if w.tabs.tabText(i).startswith("Журнал правок")]
        self.assertEqual(len(journals), 1)  # повторное открытие заменяет вкладку, а не плодит

    def test_only_applied_files_are_listed_and_nothing_applied_means_no_tab(self):
        w = self.agent_window()
        w.review_changes = lambda items: [i["name"] == "small.py" for i in items]
        self.run_agent(w)
        self.assertEqual([h.file for h in w.last_journal], ["small.py"])
        w2 = self.agent_window()
        w2.review_changes = lambda items: [False] * len(items)
        self.run_agent(w2)
        self.assertIsNone(w2.journal_view)

    def test_switched_off_keeps_data_and_menu_opens_it(self):
        w = self.agent_window(journal=False)
        self.run_agent(w)
        self.assertIsNone(w.journal_view)
        self.assertEqual(len(w.last_journal), 2)
        w.open_journal()
        self.assertIsNotNone(w.journal_view)

    def test_empty_menu_message_and_toggle_persistence(self):
        w = self.window()
        w.open_journal()
        self.assertIn("журнал пуст", w.chat.history.toPlainText())
        w.act_journal.setChecked(False)
        w.toggle_journal()
        self.assertFalse(settings.get_user()["journal"])

    def test_single_file_mode_also_opens_the_journal_tab(self):
        w = MainWindow()
        self.windows.append(w)
        w.load(self.root / "a.py")
        w.journal_on = True
        w.confirm_unrestricted = lambda f: True
        w.review_diff = lambda *a: True
        w.ask_file("do it")
        self.wait(w)
        self.assertEqual([(h.file, h.kind) for h in w.last_journal], [("a.py", "added")])
        self.assertIsNotNone(w.journal_view)

    def test_remarks_land_on_the_right_place_and_confirm_is_flagged(self):
        from types import SimpleNamespace as NS
        w = self.window()
        sec = NS(skipped="", remaining=[NS(severity="high", file="a.py", line=2, issue="injection")])
        stat = NS(skipped="", remaining=[NS(tool="ruff", code="F841", file="a.py", line=2, message="unused")])
        hunks = w.build_journal([("a.py", "x\ny\nz\n", "x\nY2\nz\n", "opus", True)], reports=(stat, sec))
        self.assertEqual(sorted(hunks[0].remarks), sorted([
            "Правка затрагивает зону confirm: проверь внимательно", "Безопасность [high]: injection",
            "Статанализ [ruff F841]: unused"]))


class ZonePersistenceTest(GuiCase):
    def sidecar(self, name="a.py"):
        return self.root / f"{name}.aimarks.json"

    def test_zone_survives_closing_the_tab_without_any_save(self):
        w = self.window("a.py")
        w.on_gutter_mark("hidden", 0, 1)
        self.assertTrue(self.sidecar().exists())  # записана сразу
        w.close_tab(0)
        again = self.window("a.py")
        self.assertEqual([(r.level, r.start, r.end) for r in again.marks.items], [("hidden", 0, 1)])

    def test_zone_survives_a_restart_and_still_protects(self):
        w = self.window("a.py")
        w.on_gutter_mark("hidden", 1, 1)
        w2 = self.window("a.py")
        prov = Script(["@@tool read_file\npath: a.py\n@@end", "ok"])
        w2.providers = {"cheap": prov, "strong": prov}
        w2.ask_ai("read it")
        self.wait(w2)
        self.assertNotIn("return 1", prov.seen[1][-1]["content"])

    def test_every_kind_of_change_is_saved_at_once(self):
        w = self.window("a.py")
        w.on_gutter_mark("readonly", 0, 0)
        w.on_gutter_mark("interface", 3, 4)
        w.ask_note = lambda zone: "fee in cents"
        w.on_gutter_mark("note", 3, 4)
        data = json.loads(self.sidecar().read_text(encoding="utf-8"))["regions"]
        self.assertEqual(data, [["readonly", 0, 0], ["interface", 3, 4, "fee in cents", ""]])
        w.on_gutter_mark("unmark", 0, 0)
        self.assertEqual(len(json.loads(self.sidecar().read_text(encoding="utf-8"))["regions"]), 1)
        w.on_gutter_mark("unmark", 3, 4)
        self.assertFalse(self.sidecar().exists())  # убрали последнюю зону - файла зон нет

    def test_hidden_secret_lines_chosen_in_the_dialog_are_saved(self):
        (self.root / "s.py").write_text('KEY = "sk-ant-api03-AbCdEf0123456789xyz"\nx = 1\n')
        w = self.window("s.py")
        w.ask_secrets = lambda lines, manual: "hide"
        w.find_secrets()
        self.assertIn('"hidden"', self.sidecar("s.py").read_text(encoding="utf-8"))

    def test_with_unsaved_text_zones_wait_for_the_save_so_line_numbers_match_the_disk(self):
        w = self.window("a.py")
        w.editor.moveCursor(w.editor.textCursor().MoveOperation.Start)
        w.editor.insertPlainText("# new first line\n")
        self.assertTrue(w.doc.modified)
        w.on_gutter_mark("readonly", 1, 1)
        self.assertFalse(self.sidecar().exists())  # текст ещё не сохранён
        w.save_file()
        data = json.loads(self.sidecar().read_text(encoding="utf-8"))
        self.assertEqual(data["regions"], [["readonly", 1, 1]])
        self.assertEqual(data["sha256"], text_hash((self.root / "a.py").read_text(encoding="utf-8")))

    def test_opening_a_file_does_not_rewrite_its_zones_file(self):
        w = self.window("a.py")
        w.on_gutter_mark("hidden", 0, 0)
        stamp = self.sidecar().stat().st_mtime_ns
        os.utime(self.sidecar(), ns=(stamp - 10**9, stamp - 10**9))
        before = self.sidecar().stat().st_mtime_ns
        self.window("a.py")  # открыли заново
        self.assertEqual(self.sidecar().stat().st_mtime_ns, before)

    def test_stale_zones_file_is_not_overwritten_just_by_opening(self):
        w = self.window("a.py")
        w.on_gutter_mark("hidden", 0, 0)
        (self.root / "a.py").write_text((self.root / "a.py").read_text() + "# changed outside\n")
        old = self.sidecar().read_text(encoding="utf-8")
        again = self.window("a.py")
        self.assertEqual(self.sidecar().read_text(encoding="utf-8"), old)  # хэш остался старым - файл помечен устаревшим
        self.assertEqual(len(again.marks.items), 1)

    def test_untitled_text_and_save_as(self):
        w = self.window(None)
        w.editor.setPlainText("x = 1\n")
        w.on_gutter_mark("hidden", 0, 0)  # не падает: сохранять некуда
        target = self.root / "new.py"
        import aiedit.app as appmod
        old = appmod.QFileDialog.getSaveFileName
        appmod.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))
        try:
            w.save_as()
        finally:
            appmod.QFileDialog.getSaveFileName = old
        self.assertTrue((self.root / "new.py.aimarks.json").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root игнорирует права на папку")
    def test_unwritable_folder_reports_once_and_does_not_crash(self):
        w = self.window("a.py")
        os.chmod(self.root, 0o500)
        try:
            w.on_gutter_mark("hidden", 0, 0)
            w.on_gutter_mark("readonly", 3, 3)
            w.on_gutter_mark("readonly", 4, 4)
            text = w.chat.history.toPlainText()
        finally:
            os.chmod(self.root, 0o700)
        self.assertEqual(text.count("не удалось сохранить зоны для a.py"), 1)
        self.assertEqual(len(w.marks.items), 3)  # в памяти всё осталось
        w.on_gutter_mark("readonly", 5, 5)  # доступ вернулся: сохранилось всё накопленное
        self.assertEqual(len(json.loads(self.sidecar().read_text(encoding="utf-8"))["regions"]), 4)

    def test_saving_identical_zones_does_not_touch_the_file(self):
        from aiedit.marks import Marks
        f = self.root / "b.py"
        m = Marks()
        m.add("hidden", 0, 0)
        m.save(f, "y = 2\n")
        side = self.root / "b.py.aimarks.json"
        os.utime(side, ns=(1, 1))
        m.save(f, "y = 2\n")
        self.assertEqual(side.stat().st_mtime_ns, 1)
        m.add("readonly", 1, 1)
        m.save(f, "y = 2\n")
        self.assertNotEqual(side.stat().st_mtime_ns, 1)


class TerminalGuiTest(GuiCase):
    def setUp(self):
        super().setUp()
        from aiedit import checks, sandbox
        self.sandbox = sandbox
        self.ran = []
        saved = (sandbox.run_bwrap, sandbox._available)

        def fake(command, work, venv, extra, timeout):
            self.ran.append((command, (work / "a.py").read_text()))
            return checks.CheckResult(command, 0, "1 passed", 0.2)
        sandbox.run_bwrap, sandbox._available = fake, ""  # без настоящей песочницы: проверяем окно
        self.addCleanup(lambda: setattr(sandbox, "run_bwrap", saved[0]) or setattr(sandbox, "_available", saved[1]))

    def agent(self, replies, answer="run"):
        w = self.window()
        prov = Script(replies)
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        self.asked = []
        w.ask_command = lambda command, name: (self.asked.append((command, name)), answer)[1]
        return w, prov

    def test_command_is_approved_runs_on_copy_with_edits_and_shows_output(self):
        w, prov = self.agent([edit("a.py", "return 1", "return 11"), call("run_command", command="pytest -q"),
                              "Тесты прошли."])
        w.ask_ai("поправь и проверь")
        self.wait(w)
        self.assertEqual(self.asked, [("pytest -q", "pytest")])
        self.assertEqual(len(self.ran), 1)
        self.assertIn("return 11", self.ran[0][1])  # правка агента уже в копии
        self.assertIn("@@result run_command\n$ pytest -q\nexit code 0\n1 passed", prov.everything())
        chat = w.chat.history.toPlainText()
        self.assertIn("песочница · прошла", chat)
        self.assertIn("1 passed", chat)
        self.assertIn("run_command", prov.systems[0])

    def test_always_allow_then_no_question(self):
        w, prov = self.agent([call("run_command", command="pytest"), call("run_command", command="pytest -x"),
                              call("run_command", command="pytest; ls"), "ок"], answer="always")
        w.ask_ai("проверь")
        self.wait(w)
        self.assertEqual([c for c, _ in self.asked], ["pytest", "pytest; ls"])  # составную всё равно спросили
        self.assertEqual(w.allowed_commands(), ["pytest"])
        self.assertEqual(len(self.ran), 3)

    def test_denied_command_is_reported_to_model(self):
        w, prov = self.agent([call("run_command", command="rm -rf /"), "ок"], answer="deny")
        w.ask_ai("сделай")
        self.wait(w)
        self.assertEqual(self.ran, [])
        self.assertIn(self.sandbox.OUT_DECLINED, prov.everything())

    def test_terminal_off_means_no_tool(self):
        w, prov = self.agent(["ок"])
        w.act_terminal.setChecked(False)
        w.toggle_terminal()
        w.ask_ai("сделай")
        self.wait(w)
        self.assertNotIn("run_command", prov.systems[0])
        self.assertFalse(settings.get_user()["terminal"])

    def test_unavailable_sandbox_disables_menu(self):
        self.sandbox._available = "песочница пока есть только на Linux"
        w, prov = self.agent(["ок"])
        self.assertFalse(w.act_terminal.isEnabled())
        self.assertFalse(w.terminal_active())
        w.ask_ai("сделай")
        self.wait(w)
        self.assertNotIn("run_command", prov.systems[0])


class AskGuiTest(GuiCase):
    def agent(self, replies):
        w = self.window()
        prov = Script(replies)
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        return w, prov

    @staticmethod
    def until_question(w):
        for _ in range(500):
            app.processEvents()
            if w.waiting is not None or w.worker is None:
                return
            w.worker.wait(10)

    def test_question_in_chat_answer_from_input_and_work_continues(self):
        w, prov = self.agent([call("ask_user", question="Какой файл: a.py или b.py?"),
                              edit("b.py", "y = 2", "y = 3"), "Поправил b.py."])
        w.ask_ai("поправь значение")
        self.until_question(w)
        self.assertIsNotNone(w.waiting)
        self.assertIn("Какой файл: a.py или b.py?", w.chat.history.toPlainText())
        self.assertTrue(w.ask_btn.isEnabled())  # можно отвечать
        self.assertFalse(w.explain_btn.isEnabled())
        self.assertIn("Ответь на вопрос", w.chat.input.placeholderText())
        self.assertNotIn("задаю вопрос", w.chat.history.toPlainText())
        w.chat.input.setPlainText("b.py")
        w.chat.submit()
        self.assertIsNone(w.waiting)
        self.assertEqual(w.chat.input.placeholderText(), w.chat.PLACEHOLDER)
        self.wait(w)
        self.assertIn("@@result ask_user\nb.py\n@@end", prov.everything())
        self.assertEqual((self.root / "b.py").read_text(), "y = 3\n")
        self.assertEqual(len(w.history), 2)  # ответ на вопрос - не новая задача
        self.assertEqual(w.history[0].text, "поправь значение")
        self.assertIn("Поправил b.py.", w.chat.history.toPlainText())

    def test_stop_while_waiting(self):
        w, prov = self.agent([call("ask_user", question="Какой?"), "не должно быть"])
        w.ask_ai("поправь")
        self.until_question(w)
        w.stop_agent()
        self.wait(w)
        self.assertIsNone(w.worker)
        self.assertIsNone(w.waiting)
        self.assertEqual(len(prov.seen), 1)
        self.assertIn("остановлено", w.chat.history.toPlainText())

    def test_answer_goes_through_outbound_filter(self):
        key = "sk-ant-api03-AbCdEf0123456789xyz"
        w, prov = self.agent([call("ask_user", question="Какой ключ?"), "ок"])
        w.ask_outbound = lambda findings, model: "mask"
        w.ask_ai("подключи сервис")
        self.until_question(w)
        w.ask_ai(f"вот он {key}")
        self.wait(w)
        self.assertNotIn(key, prov.everything())
        self.assertIn("вот он <<masked-1>>", prov.everything())


class OutboundGuiTest(GuiCase):
    KEY = "sk-ant-api03-AbCdEf0123456789xyz"

    def agent(self, replies, answer="mask"):
        w = self.window()
        prov = Script(replies)
        w.providers = {"cheap": prov, "strong": prov}
        w.review_changes = lambda items: [True] * len(items)
        self.asked = []
        w.ask_outbound = lambda findings, model: (self.asked.append([f.kind for f in findings]), answer)[1]
        return w, prov

    def test_key_in_chat_is_masked_in_agent_mode_and_asked_once(self):
        w, prov = self.agent(["первый", "второй"])
        w.ask_ai(f"почему ключ {self.KEY} не работает?")
        self.wait(w)
        self.assertNotIn(self.KEY, prov.everything())
        self.assertIn("почему ключ <<masked-1>> не работает?", prov.everything())
        self.assertEqual(w.history[0].text, f"почему ключ {self.KEY} не работает?")  # у себя храним как есть
        w.ask_ai("а теперь?")  # история с ключом уходит снова - решение уже есть, не спрашиваем
        self.wait(w)
        self.assertEqual(self.asked, [["ключ API (sk-...)"]])
        self.assertNotIn(self.KEY, prov.everything())
        self.assertTrue(any(e.get("mode") == "filter" for e in sendlog.read(self.root)))
        self.assertNotIn(self.KEY, (self.root / sendlog.LOG_NAME).read_text())

    def test_cancel_sends_nothing_and_keeps_the_task(self):
        w, prov = self.agent(["не должен ответить"], answer="cancel")
        w.ask_ai(f"ключ {self.KEY}")
        self.wait(w)
        self.assertEqual(prov.seen, [])
        self.assertIn("остановлен в фильтре исходящего", w.chat.history.toPlainText())
        self.assertEqual(w.chat.input.toPlainText(), f"ключ {self.KEY}")
        self.assertIsNone(w.worker)

    def test_single_file_mode_is_filtered_too(self):
        w = self.window()
        w.tree.root = None  # без папки: запрос по одному файлу
        prov = Script(["Готово.\n```\ndef f():\n    return 1\n\ndef g():\n    return 2\n```"])
        w.providers = {"cheap": prov, "strong": prov}
        w.ask_outbound = lambda findings, model: "mask"
        w.ask_ai(f"вот ключ {self.KEY}")
        self.wait(w)
        self.assertTrue(prov.seen)
        self.assertNotIn(self.KEY, prov.everything())

    def test_words_from_settings_and_menu(self):
        w, prov = self.agent(["ок"])
        w.edit_outbound_words = w.edit_outbound_words  # пункт меню существует
        w.gate.set_words(["Газпром"])
        settings.set_user(outbound_words=w.gate.words)
        w.ask_ai("сделай отчёт для газпром")
        self.wait(w)
        self.assertNotIn("газпром", prov.everything().lower())
        self.assertEqual(self.asked, [["слово из списка"]])
        self.assertEqual(MainWindow().gate.words, ["Газпром"])  # список пережил перезапуск

    def test_pasted_hidden_code_is_caught(self):
        w, prov = self.agent(["ок"])
        w.marks.add("hidden", 0, 1)  # def f(): return 1
        w.ask_ai("почему здесь\n    return 1\nа не 2?")  # короткая строка - не ловим
        self.wait(w)
        self.assertEqual(self.asked, [])
        (self.root / "c.py").write_text("def calc():\n    return amount * 0.07  # secret fee\n")
        d = w.load(self.root / "c.py")
        d.marks.add("hidden", 1, 1)
        w.ask_ai("поправь:\n    return amount * 0.07  # secret fee")
        self.wait(w)
        self.assertEqual(self.asked, [["строка закрытого кода"]])
        self.assertNotIn("0.07", "\n".join(m["content"] for m in prov.seen[-1]))

    def test_interface_signature_is_not_closed_code(self):
        w, _ = self.agent([])
        (self.root / "c.py").write_text("def calculate_fee(amount, rate):\n    return amount * rate * 0.07\n")
        d = w.load(self.root / "c.py")
        d.marks.add("interface", 0, 1)
        lines = w.closed_code_lines()
        self.assertIn("    return amount * rate * 0.07", lines)
        self.assertNotIn("def calculate_fee(amount, rate):", lines)

    def test_translator_goes_through_the_filter(self):
        from tests.test_translator import Fake
        w, prov = self.agent(["Done"])
        fake = Fake()
        seen = []
        w.translator = Translator(lambda p: (seen.append(p["messages"][-1]["content"]), fake(p))[1])
        w.translate_on = True
        w.gate.set_words(["Газпром"])
        w.ask_ai("сделай отчёт для Газпром")
        self.wait(w)
        self.assertTrue(seen)
        self.assertNotIn("Газпром", "\n".join(seen))
        self.assertEqual(len(self.asked), 1)  # переводчик и исполнитель - одно решение

    def test_toggle_off_sends_as_is(self):
        w, prov = self.agent(["ок"])
        w.act_outbound.setChecked(False)
        w.toggle_outbound()
        w.ask_ai(f"ключ {self.KEY}")
        self.wait(w)
        self.assertEqual(self.asked, [])
        self.assertIn(self.KEY, prov.everything())
        self.assertFalse(settings.get_user()["outbound"])

    def test_new_chat_asks_again(self):
        w, _ = self.agent(["1", "2"])
        w.ask_ai(f"ключ {self.KEY}")
        self.wait(w)
        w.clear_chat()
        w.ask_ai(f"ключ {self.KEY}")
        self.wait(w)
        self.assertEqual(len(self.asked), 2)


if __name__ == "__main__":
    unittest.main()
