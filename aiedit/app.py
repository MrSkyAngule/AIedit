"""Редактор (PyQt6 или PySide6): код без служебных меток, зоны в колонке слева, панель ИИ."""
import os
import sys
from pathlib import Path

from .searchdialog import SearchDialog
from .qt import (BINDING, QAction, QApplication, QDockWidget, QFileDialog, QKeySequence,
                 QInputDialog, QMainWindow, QMessageBox, Qt, QTabWidget, QTextCursor, QThread, QTimer, QVBoxLayout, QWidget,
                 Signal)
from . import (agent, analysis, checks, journal, conductor, flows, prompts, dialogs, guard, orchestrator, outbound, policy, related,
               secrets_scan, sendlog, settings)
from . import chatstore
from .chat import ChatPanel
from .document import Doc
from .filetree import FileTree
from .findbar import FindBar
from .folder_dialog import FolderDialog
from .marks import Marks
from .policy import Rules
from .translator import Translator
from .workspace import Workspace, write_change
from . import connections
from .connections_dialog import ConnectionsDialog
from .journal_dialog import JournalView
from .translator import has_cyrillic, provider_transport


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args))
        except Exception as e:  # сеть, ключ, формат ответа
            self.failed.emit(f"{type(e).__name__}: {e}")


class AgentWorker(QThread):
    """Крутит агента в отдельном потоке. Отмена проверяется между шагами."""
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, task, ws, provider, history, open_file, translator=None, roster=None,
                 conduct=False, review_on=False, analyze_on=False):
        super().__init__()
        self.args = (task, ws, provider, history, open_file)
        self.translator = translator
        self.roster = roster      # исполнители; нужны дирижёру и проверке безопасностью
        self.conduct = conduct    # True: provider - это дирижёр
        self.review_on = review_on
        self.analyze_on = analyze_on
        self.cancel = False

    def run(self):
        task, ws, provider, history, open_file = self.args
        try:
            if self.conduct:
                result = flows.run_conductor(task, ws, self.roster, provider, history, open_file, self.translator,
                                             on_progress=self.progress.emit, should_stop=lambda: self.cancel,
                                             review_on=self.review_on, analyze_on=self.analyze_on)
            else:
                result = flows.run_agent(task, ws, provider, history, open_file, self.translator,
                                         on_progress=self.progress.emit, should_stop=lambda: self.cancel,
                                         roster=self.roster, review_on=self.review_on, analyze_on=self.analyze_on)
            self.done.emit(result)
        except Exception as e:  # доступ закрыли по ходу работы, сеть, формат ответа
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    outboundAsk = Signal(object)  # вопрос фильтра исходящего из рабочего потока в окно

    def __init__(self):
        super().__init__()
        user = settings.get_user()
        self.gate = outbound.Gate(user.get("outbound_words", []), self.decide_outbound, self.log_filter,
                                  bool(user.get("outbound", True)))
        self.outboundAsk.connect(self.answer_outbound, Qt.ConnectionType.BlockingQueuedConnection)
        self.translator: Translator | None = None
        self.setup_ai(self.current_setup())
        self.conductor_on = bool(settings.get_user().get("conductor", True))
        self.review_on = bool(settings.get_user().get("review", True))
        self.analyze_on = bool(settings.get_user().get("analyze", True))
        self.analysis_notes_shown: set[str] = set()
        self.journal_on = bool(settings.get_user().get("journal", True))
        self.last_journal: list = []   # места последних правок ИИ (журнал правок)
        self.journal_view: JournalView | None = None  # вкладка журнала, если открыта
        self.last_doc: Doc | None = None  # последний файл, с которым работали (когда открыта вкладка журнала)
        self.applied: list = []        # (изменение, имя файла), реально применённые в последнем запросе
        self.worker: Worker | None = None
        self.ai_blocked = False  # текущий файл закрыт от ИИ правилами папки
        self.highlight = "fill"
        self.acknowledged: set[Path | None] = set()  # папки, где предупреждение уже принято
        self.history: list[orchestrator.Turn] = []  # память разговора
        self.context: list[Path] = []  # файлы, которые идут в запрос только для чтения
        self.accepted_secrets: set[str] = set()  # строки, которые человек разрешил отправить
        self.accepted_files: set[Path] = set()  # "секретные" по имени файлы, принятые в сессии
        self.sent_task, self.sent_source = "", None
        self.req_doc: Doc | None = None  # вкладка, из которой ушёл запрос
        self.check_worker: Worker | None = None
        self.note_workers: list[Worker] = []
        self.agent_provider = None
        self.translate_on = bool(settings.get_user().get("translate", True))
        self.check_cfg: dict = {}  # настройки проверки для открытого проекта
        self.undo_actions: list[QAction] = []
        self.docs: list[Doc] = []
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.findbar = FindBar(lambda: self.editor)
        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)
        lay.addWidget(self.findbar)
        self.setCentralWidget(center)

        self.chat = ChatPanel()
        self.chat.sent.connect(self.ask_ai)
        self.chat.explainRequested.connect(self.explain_ai)
        self.ask_btn, self.explain_btn = self.chat.send_btn, self.chat.explain_btn
        self.chat.clearRequested.connect(self.clear_chat)
        self.chat_dock = dock = QDockWidget("Чат с ИИ", self)
        dock.setWidget(self.chat)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.tree = FileTree()
        self.tree.fileActivated.connect(lambda name: self.open_path(Path(name)))
        self.tree.rulesChanged.connect(self.refresh_access)
        self.tree.rulesChanged.connect(self.prune_context)
        self.tree.pathRenamed.connect(self.on_path_renamed)
        self.tree.contextToggled.connect(lambda name: self.toggle_context(Path(name)))
        self.tree.in_context = lambda path: Path(os.path.realpath(path)) in self.context
        self.chat.contextCleared.connect(self.clear_context)
        self.chat.stopRequested.connect(self.stop_agent)
        self.chat.changed.connect(self.save_chat)
        self.tree.pathDeleted.connect(self.on_path_deleted)
        self.tree.rootChanged.connect(lambda name: self.load_chat(Path(name)))
        self.tree.rootChanged.connect(lambda name: self.load_check_settings(Path(name)))
        files_dock = QDockWidget("Файлы", self)
        files_dock.setWidget(self.tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, files_dock)

        self.build_menus()
        self.add_doc()
        self.refresh_access()
        self.show_models_in_status()

    # --- меню ---
    def action(self, menu, text, slot, shortcut=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda _=False: slot())
        menu.addAction(a)
        return a

    def build_menus(self):
        f = self.menuBar().addMenu("Файл")
        self.action(f, "Открыть папку...", self.open_folder, "Ctrl+Shift+O")
        self.action(f, "Открыть файл...", self.open_file, "Ctrl+O")
        self.action(f, "Сохранить", self.save_file, "Ctrl+S")
        self.action(f, "Сохранить как...", self.save_as, "Ctrl+Shift+S")
        e = self.menuBar().addMenu("Правка")
        undo = self.action(e, "Отменить", self.undo, "Ctrl+Z")
        redo = self.action(e, "Повторить", self.redo, "Ctrl+Y")
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        e.addSeparator()
        self.action(e, "Найти...", self.findbar.open, "Ctrl+F")
        self.action(e, "Найти далее", self.findbar.next, "F3")
        self.action(e, "Найти назад", self.findbar.prev, "Shift+F3")
        self.action(e, "Заменить...", lambda: self.findbar.open(replace=True), "Ctrl+H")
        self.undo_actions = [undo, redo]
        for act in self.undo_actions:  # горячие клавиши только когда фокус в редакторе кода
            act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        f.addSeparator()
        self.action(f, "Закрыть вкладку", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        v = self.menuBar().addMenu("Вид")
        for mode, title in (("fill", "Подсветка: закраска"), ("underline", "Подсветка: подчёркивание"),
                            ("off", "Подсветка: выключена")):
            self.action(v, title, lambda _=False, md=mode: self.set_highlight(md))
        m = self.menuBar().addMenu("Разметка")
        self.action(m, "Скрыть от ИИ (hidden)", lambda: self.mark("hidden"), "Ctrl+1")
        self.action(m, "Только чтение (readonly)", lambda: self.mark("readonly"), "Ctrl+2")
        self.action(m, "С подтверждением (confirm)", lambda: self.mark("confirm"), "Ctrl+3")
        self.action(m, "Только интерфейс (interface)", lambda: self.mark("interface"), "Ctrl+5")
        self.action(m, "Объяснить ИИ (explain)", lambda: self.mark("explain"), "Ctrl+4")
        self.action(m, "Заметка к зоне...", lambda: self.edit_note(*self.block_range()), "Ctrl+Shift+N")
        self.action(m, "Снять метку", self.unmark, "Ctrl+0")
        self.action(m, "Найти секреты...", self.find_secrets, "Ctrl+Shift+K")
        ai = self.menuBar().addMenu("ИИ")
        self.action(ai, "Поиск по проекту...", self.search_project, "Ctrl+Shift+F")
        self.action(ai, "Добавить импорты файла в контекст", self.add_imports_to_context)
        self.action(ai, "Сбросить контекст", self.clear_context)
        ai.addSeparator()
        self.action(ai, "Подключения ИИ...", self.open_connections, "Ctrl+Shift+A")
        ai.addSeparator()
        self.act_outbound = self.action(ai, "Фильтр исходящего: проверять всё перед отправкой", self.toggle_outbound)
        self.act_outbound.setCheckable(True)
        self.act_outbound.setChecked(self.gate.enabled)
        self.action(ai, "Слова для фильтра исходящего...", self.edit_outbound_words)
        ai.addSeparator()
        self.act_translate = self.action(ai, "Переводить задачи и ответы", self.toggle_translate)
        self.act_translate.setCheckable(True)
        self.refresh_translate_action()
        self.action(ai, "Журнал правок ИИ...", self.open_journal)
        self.act_journal = self.action(ai, "Открывать журнал правок после работы ИИ", self.toggle_journal)
        self.act_journal.setCheckable(True)
        self.act_journal.setChecked(self.journal_on)
        self.act_analyze = self.action(ai, "Статанализ после правок (ruff, mypy, bandit, eslint, tsc)", self.toggle_analyze)
        self.act_analyze.setCheckable(True)
        self.act_analyze.setChecked(self.analyze_on)
        self.act_review = self.action(ai, "Проверка безопасности после правок", self.toggle_review)
        self.act_review.setCheckable(True)
        self.refresh_review_action()
        self.act_conductor = self.action(ai, "Дирижёр: раздавать шаги разным моделям", self.toggle_conductor)
        self.act_conductor.setCheckable(True)
        self.act_conductor.setChecked(self.conductor_on)
        self.action(ai, "Запустить проверку", self.run_check, "Ctrl+Shift+T")
        self.action(ai, "Команда проверки...", self.set_check_command)
        self.act_auto = self.action(ai, "Запускать проверку после правки ИИ",
                                    lambda: self.set_check_flag("auto", self.act_auto.isChecked()))
        self.act_output = self.action(ai, "Отправлять ИИ вывод проверки",
                                      lambda: self.set_check_flag("send_output", self.act_output.isChecked()))
        for act in (self.act_auto, self.act_output):
            act.setCheckable(True)
        ai.addSeparator()
        for act in (self.action(ai, "Что уйдёт ИИ...", self.preview_request, "Ctrl+Shift+P"),
                    self.action(ai, "Журнал отправок...", self.show_send_log)):
            self.chat.add_menu_action(act)

    # --- вкладки и файлы ---
    @property
    def doc(self) -> Doc:
        """Текущий файл. Если открыта вкладка журнала, то последний файл, с которым работали."""
        w = self.tabs.currentWidget()
        found = next((d for d in self.docs if d.editor is w), None)
        if found is not None:
            self.last_doc = found
            return found
        return self.last_doc if self.last_doc in self.docs else self.docs[0]

    editor = property(lambda self: self.doc.editor)
    marks = property(lambda self: self.doc.marks)
    path = property(lambda self: self.doc.path, lambda self, v: self.doc.set_path(v))
    silent = property(lambda self: self.doc.silent, lambda self, v: setattr(self.doc, "silent", v))
    block_count = property(lambda self: self.doc.block_count,
                           lambda self, v: setattr(self.doc, "block_count", v))

    def add_doc(self) -> Doc:
        doc = Doc(self.highlight)
        doc.on_save_error = self.say
        doc.editor.markRequested.connect(self.on_gutter_mark)
        doc.editor.document().modificationChanged.connect(lambda _=False, d=doc: self.set_tab_label(d))
        for act in self.undo_actions:
            doc.editor.addAction(act)
        self.docs.append(doc)
        self.tabs.addTab(doc.editor, doc.name)
        self.tabs.setCurrentWidget(doc.editor)
        return doc

    def set_tab_label(self, doc: Doc):
        i = self.tabs.indexOf(doc.editor)
        if i >= 0:
            self.tabs.setTabText(i, doc.name + (" *" if doc.modified else ""))
            self.tabs.setTabToolTip(i, str(doc.path or ""))

    def on_tab_changed(self, _index: int):
        if self.docs and self.tabs.currentWidget() is not None:
            self.refresh_access()

    def update_title(self):
        note = " [закрыто от ИИ]" if self.ai_blocked else ""
        self.setWindowTitle(f"aiedit - {self.path or 'без имени'}{note}")

    def refresh_access(self):
        """Согласует кнопки ИИ с правилами. Настоящий запрет стоит в orchestrator."""
        if self.tree.rules is not None:  # правила могли поменять не через дерево
            self.tree.rules.load()
            self.tree.view.viewport().update()
        blocked = self.path is not None and policy.is_denied(self.path)
        if blocked and not self.ai_blocked:
            self.say("файл закрыт от ИИ: он не попадёт в запросы"
                     + ("" if self.tree.root else ", запросы по нему отключены"))
        self.ai_blocked = blocked
        idle = self.worker is None
        project = self.tree.root is not None  # агент работает с проектом, закрытая вкладка ему не мешает
        self.ask_btn.setEnabled(idle and (project or not blocked))
        self.explain_btn.setEnabled(idle and not blocked)
        self.explain_btn.setToolTip("файл закрыт от ИИ" if blocked else "")
        self.ask_btn.setToolTip("файл закрыт от ИИ" if blocked and not project else "")
        self.update_title()

    def replace_text(self, text: str):
        self.doc.replace_text(text)

    def load(self, path: Path) -> Doc:
        """Открывает файл во вкладке (уже открытый только показывает)."""
        for d in self.docs:
            if d.path and d.path.resolve() == path.resolve():
                self.tabs.setCurrentWidget(d.editor)
                return d
        text = path.read_text(encoding="utf-8")
        cur = self.doc
        doc = cur if cur.path is None and not cur.modified and cur.text() == "" else self.add_doc()
        fresh = doc.load(path, text)
        self.set_tab_label(doc)
        self.tabs.setCurrentWidget(doc.editor)
        self.refresh_access()
        if not fresh:
            self.statusBar().showMessage("файл менялся вне редактора - зоны могли сместиться", 8000)
        return doc

    def paint_regions(self):
        self.doc.paint()

    def confirm_discard(self, doc: Doc) -> bool:
        """Спрашивает про несохранённые изменения вкладки. False - отмена."""
        if not doc.modified:
            return True
        self.tabs.setCurrentWidget(doc.editor)
        answer = QMessageBox.question(
            self, "Несохранённые изменения", f"Сохранить изменения в «{doc.name}»?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            self.save_file()
            return not doc.modified
        return answer == QMessageBox.StandardButton.Discard

    def close_tab(self, index: int):
        widget = self.tabs.widget(index)
        if widget is self.journal_view and widget is not None:  # вкладка журнала: просто закрыть
            self.tabs.removeTab(index)
            widget.deleteLater()
            self.journal_view = None
            self.refresh_access()
            return
        doc = next((d for d in self.docs if d.editor is widget), None)
        if doc is None or not self.confirm_discard(doc):
            return
        if self.req_doc is doc:
            self.req_doc = None  # ответ на запрос из этой вкладки применять уже некуда
        self.tabs.removeTab(index)
        self.docs.remove(doc)
        doc.editor.deleteLater()
        if not self.docs:
            self.add_doc()
        self.refresh_access()

    def closeEvent(self, event):
        for doc in list(self.docs):
            if not self.confirm_discard(doc):
                event.ignore()
                return
        event.accept()

    @staticmethod
    def _inside(path: Path | None, folder: Path) -> Path | None:
        """Путь относительно folder или None, если path не внутри (без разрешения симлинков)."""
        if path is None:
            return None
        try:
            return Path(os.path.abspath(path)).relative_to(os.path.abspath(folder))
        except ValueError:
            return None

    def on_path_renamed(self, old: str, new: str):
        for d in self.docs:
            rel = self._inside(d.path, Path(old))
            if rel is not None:
                d.set_path(Path(new) / rel)
                self.set_tab_label(d)
        self.refresh_access()

    def on_path_deleted(self, gone: str):
        for d in list(self.docs):
            if self._inside(d.path, Path(gone)) is not None:
                d.editor.document().setModified(False)
                self.close_tab(self.tabs.indexOf(d.editor))
        self.refresh_access()

    def open_path(self, path: Path):
        try:
            self.load(path)
        except (OSError, UnicodeDecodeError) as e:
            self.statusBar().showMessage(f"не удалось открыть {path.name}: {e}", 6000)

    def open_file(self):
        name, _ = QFileDialog.getOpenFileName(self, "Открыть", str(self.tree.root or Path.home()))
        if name:
            self.open_path(Path(name))

    def project_folder(self) -> Path | None:
        """Папка, чьи правила действуют на текущий файл."""
        root = self.tree.root
        if root is not None and (self.path is None or self.path.resolve().is_relative_to(root)):
            return root
        return self.path.resolve().parent if self.path else None

    def confirm_unrestricted(self, folder: Path | None) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Ничего не закрыто от ИИ")
        where = f"В папке «{folder.name or folder}»" if folder else "В проекте"
        box.setText(f"{where} вы не закрыли от ИИ ни одной папки и ни одного файла.")
        what = f"файл «{self.path.name}»" if self.path else "текст из редактора"
        box.setInformativeText(
            f"Сейчас ИИ получит {what}, и он уйдёт стороннему провайдеру. То же будет со "
            "всем остальным в папке.\n\nЗакрыть можно правым кликом по папке или файлу "
            "в дереве - «Запретить ИИ».")
        go = box.addButton("Продолжить без запретов", QMessageBox.ButtonRole.AcceptRole)
        back = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(back)
        box.exec()
        return box.clickedButton() is go

    def may_send(self, project: bool = False) -> bool:
        """Перед запросом к ИИ: файл-секрет, пустые правила проекта, секреты в тексте.
        В режиме проекта файлы выбирает агент, а секреты скрывает рабочая область, так что
        остаётся только предупреждение о пустых правилах."""
        if (not project and self.path and secrets_scan.suspicious_file(self.path)
                and self.path not in self.accepted_files):
            choice = self.ask_secret_file(self.path)
            if choice == "deny":
                self.deny_current_file()
                return False
            if choice != "send":
                self.say("запрос отменён: файл похож на секретный")
                return False
            self.accepted_files.add(self.path)
        folder = self.project_folder()
        if folder not in self.acknowledged and not (folder and Rules(folder).denied):
            if not self.confirm_unrestricted(folder):
                self.say("запрос отменён: в проекте ничего не закрыто от ИИ")
                return False
            self.acknowledged.add(folder)
        return True if project else self.review_secrets(manual=False)

    # --- секреты ---
    def ask_secret_file(self, path: Path) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Файл похож на секретный")
        box.setText(f"«{path.name}» обычно хранит ключи или пароли.")
        box.setInformativeText("Лучше закрыть его от ИИ целиком. Если отправить, его содержимое уйдёт провайдеру.")
        deny = box.addButton("Закрыть файл от ИИ", QMessageBox.ButtonRole.AcceptRole)
        send = box.addButton("Всё равно отправить", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(deny)
        box.exec()
        return {deny: "deny", send: "send"}.get(box.clickedButton(), "cancel")

    def deny_current_file(self):
        root = self.tree.root
        if self.tree.rules is not None and root and self.path.resolve().is_relative_to(root.resolve()):
            self.tree.set_denied(self.path, True)
        else:
            Rules(self.path.parent).set_denied(self.path, True)
            self.refresh_access()
        self.say(f"«{self.path.name}» закрыт от ИИ")

    def ask_secrets(self, lines: list[str], manual: bool) -> str:
        """hide, send или cancel. lines - человекочитаемый список находок."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Похоже на секреты")
        box.setText(f"Найдено возможных секретов: {len(lines)}. Модель ещё видит их.")
        shown = "\n".join(lines[:8]) + (f"\n... и ещё {len(lines) - 8}" if len(lines) > 8 else "")
        box.setInformativeText(shown)
        hide = box.addButton("Скрыть от ИИ (hidden)", QMessageBox.ButtonRole.AcceptRole)
        send = box.addButton("Оставить" if manual else "Отправить как есть",
                             QMessageBox.ButtonRole.DestructiveRole)
        if not manual:
            box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(hide)
        box.exec()
        return {hide: "hide", send: "send"}.get(box.clickedButton(), "cancel")

    def review_secrets(self, manual: bool = True) -> bool:
        """Ищет открытые секреты в файле. True - можно продолжать."""
        text = self.editor.toPlainText()
        lines = text.split("\n")
        found = [f for f in secrets_scan.uncovered(secrets_scan.scan(text), self.marks.items, len(lines))
                 if lines[f.line] not in self.accepted_secrets]
        if not found:
            if manual:
                self.say("похожего на секреты не найдено (или всё уже скрыто)")
            return True
        choice = self.ask_secrets([f"строка {f.line + 1}: {f.kind}" for f in found], manual)
        if choice == "hide":
            for a, b in secrets_scan.to_ranges(found):
                self.marks.add("hidden", a, b)
            self.paint_regions()
            self.say(f"скрыто от ИИ строк с секретами: {len(found)}")
            return True
        if choice == "send":
            self.accepted_secrets.update(lines[f.line] for f in found)
            return True
        self.say("запрос отменён: в файле есть возможные секреты")
        return False

    def find_secrets(self):
        self.review_secrets(manual=True)

    # --- контекст из нескольких файлов ---
    def show_context(self):
        self.chat.set_context([p.name for p in self.context])

    def toggle_context(self, path: Path):
        path = Path(os.path.realpath(path))
        if path in self.context:
            self.context.remove(path)
        elif policy.is_denied(path):
            self.say(f"«{path.name}» закрыт от ИИ - в контекст не добавлен")
        elif secrets_scan.suspicious_file(path):
            self.say(f"«{path.name}» похож на секретный - в контекст не добавлен")
        else:
            self.context.append(path)
        self.show_context()

    def clear_context(self):
        self.context.clear()
        self.show_context()

    def add_imports_to_context(self):
        root, file = self.tree.root, self.path
        if root is None or file is None:
            self.say("нужна открытая папка и сохранённый файл")
            return
        added = [p for p in related.python_imports(self.editor.toPlainText(), file, root)
                 if p not in self.context and not secrets_scan.suspicious_file(p)]
        self.context += added
        self.show_context()
        self.say(f"в контекст добавлено файлов по импортам: {len(added)}")

    def prune_context(self):
        """Закрытые от ИИ файлы выпадают из контекста сами."""
        closed = [p for p in self.context if policy.is_denied(p)]
        for p in closed:
            self.context.remove(p)
            self.say(f"«{p.name}» закрыт от ИИ - убран из контекста")
        if closed:
            self.show_context()

    def open_at(self, path: Path, line: int):
        self.open_path(path)
        doc = self.doc
        if doc.path and Path(os.path.realpath(doc.path)) == Path(os.path.realpath(path)):
            cur = QTextCursor(doc.editor.document().findBlockByNumber(max(line - 1, 0)))
            doc.editor.setTextCursor(cur)
            doc.editor.centerCursor()

    def search_project(self):
        if self.tree.root is None:
            self.say("сначала открой папку проекта")
            return
        self.search_dlg = SearchDialog(self, self.tree.root, self.open_at, self.toggle_context)
        self.search_dlg.show()

    # --- проверка после правки ---
    def load_check_settings(self, root: Path):
        self.check_cfg = settings.get_project(root)
        self.act_auto.setChecked(bool(self.check_cfg.get("auto")))
        self.act_output.setChecked(bool(self.check_cfg.get("send_output")))

    def set_check_flag(self, key: str, value: bool):
        if self.tree.root is None:
            self.say("сначала открой папку проекта")
            self.load_check_settings(Path.cwd())  # вернуть переключатели в прежнее положение
            return
        self.check_cfg[key] = value
        settings.set_project(self.tree.root, **{key: value})
        if key == "send_output" and value:
            self.say("вывод проверки будет уходить ИИ после очистки: из него вырезаются строки с текстом "
                     "из закрытых файлов, скрытого кода и секретов. Это лучшее усилие, а не гарантия: "
                     "выключи пункт, если тесты могут печатать чувствительные данные")

    def set_check_command(self):
        if self.tree.root is None:
            self.say("сначала открой папку проекта")
            return
        cmd, ok = QInputDialog.getText(self, "Команда проверки",
                                       "Команда запускается в папке проекта (без оболочки):",
                                       text=self.check_cfg.get("test_command", "python -m unittest"))
        if ok and cmd.strip():
            self.check_cfg["test_command"] = cmd.strip()
            settings.set_project(self.tree.root, test_command=cmd.strip())
            self.say(f"команда проверки: {cmd.strip()}")

    def concealed_lines(self) -> set[str]:
        """Строки скрытого кода во всех открытых файлах: в вывод проверки для ИИ они не попадут."""
        out = set()
        for d in self.docs:
            lines = d.text().split("\n")
            for r in d.marks.items:
                if r.level in guard.CONCEALED:
                    out.update(l.strip() for l in lines[r.start:r.end + 1])
        return out

    def run_check(self):
        cmd, root = self.check_cfg.get("test_command"), self.tree.root
        if root is None or not cmd:
            self.say("проверка не настроена: ИИ - Команда проверки...")
            return
        if self.check_worker:
            self.say("проверка ещё идёт")
            return
        self.chat.set_busy(True, "Идёт проверка...") if not self.worker else None
        self.say(f"запускаю проверку: {cmd}")
        self.check_worker = Worker(checks.run_command, cmd, root)
        self.check_worker.done.connect(self.on_check_done)
        self.check_worker.failed.connect(lambda err: (self.say(f"проверка: {err}"), self.end_check()))
        self.check_worker.start()

    def run_check_after_ai(self):
        # remember() вызывается уже после apply_result, поэтому запуск отложен до записи реплики
        self.run_check()

    def end_check(self):
        self.check_worker = None
        if not self.worker:
            self.chat.set_busy(False)

    def on_check_done(self, res: checks.CheckResult):
        self.end_check()
        shown = checks.tail(res.output.strip()) or "(вывода нет)"
        self.chat.add("check", shown, f"проверка {res.status()} · {res.seconds:.1f} с · {res.command}")
        if self.history and self.history[-1].role == "assistant":
            note = f"[post-edit check: {res.status_en()}]"
            if self.check_cfg.get("send_output") and res.output.strip():
                concealed = self.concealed_lines()
                tokens = checks.protected_tokens(self.tree.root, concealed)
                note += "\n" + checks.tail(checks.scrub(res.output.strip(), concealed, tokens), 800)
            last = self.history[-1]
            self.history[-1] = orchestrator.Turn(last.role, f"{last.text}\n{note}", last.source,
                                                 f"{last.en}\n{note}" if last.en else None)
            self.save_chat()

    # --- предпросмотр и журнал ---
    def current_input(self) -> orchestrator.FileInput:
        return orchestrator.FileInput(self.path, self.editor.toPlainText(), list(self.marks.items),
                                      self.tree.root)

    def context_inputs(self) -> list[orchestrator.FileInput]:
        """Контекстные файлы со своими зонами. Секреты в них скрываются всегда."""
        self.prune_context()
        out = []
        own = Path(os.path.realpath(self.path)) if self.path else None
        for path in self.context:
            if path == own:
                continue  # открытый файл и так идёт как основной
            live = next((d for d in self.docs if d.path and Path(os.path.realpath(d.path)) == path), None)
            if live is not None:  # файл открыт во вкладке: берём его текст и зоны, даже несохранённые
                text, regions = live.text(), list(live.marks.items)
            else:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                marks = Marks()
                marks.load(path, text)
                regions = list(marks.items)
            n = len(text.split("\n"))
            for a, b in secrets_scan.to_ranges(secrets_scan.uncovered(secrets_scan.scan(text), regions, n)):
                regions.append(guard.Region("hidden", a, b))
            out.append(orchestrator.FileInput(path, text, regions, self.tree.root))
        return out

    def preview_request(self):
        """Показывает ровно тот текст, который получит провайдер."""
        task = self.chat.input.toPlainText().strip()
        if self.tree.root is not None:
            return self.preview_agent(task or "<здесь будет твоя задача>")
        self.preview_file(task)

    def preview_file(self, task: str):
        """Предпросмотр однофайлового режима (папка не открыта)."""
        try:
            req = None
            if not task:
                req = orchestrator.prepare_explain(self.current_input(), self.providers,
                                                   self.history, self.context_inputs())
            if req is None:
                req = orchestrator.prepare_ask(self.current_input(), task or "<здесь будет твоя задача>",
                                               self.providers, self.history, self.context_inputs())
        except policy.AccessDenied as e:
            self.say(f"предпросмотр невозможен: {e}")
            return
        self.preview = dialogs.show_text(self, "Что уйдёт ИИ", orchestrator.describe(req))

    def preview_conductor(self, task: str):
        ws = self.make_workspace(log=False)
        msgs = orchestrator.build_messages(self.history, conductor.build_request(task, ws, self.roster, self.path))
        tr = self.active_translator()
        text = "\n".join([
            f"Режим: дирижёр по проекту «{ws.root.name}»", f"Модель дирижёра: {self.conductor_provider.name}",
            "Исполнители: " + ", ".join(f"{w.key} ({w.provider.name})" for w in self.roster.by_price())
            + (f". Специализации: {self.roster.labels()}" if any(w.specialty for w in self.roster.workers.values()) else ""),
            ("Перевод: задача уйдёт по-английски (gpt-4o-mini переводит только прозу, без кода, путей и "
             "секретов)" if tr else "Перевод: выключен"), "", "=== SYSTEM (дирижёр) ===", conductor.CONDUCTOR_SYSTEM, ""]
            + [f"=== {m['role'].upper()} ===\n{m['content']}\n" for m in msgs]
            + ["=== ДАЛЬШЕ ===",
               "Дирижёр вернёт план. Каждый шаг выполняет назначенный исполнитель агентом: ему уходит",
               "цель шага, общий запрос и итоги прошлых шагов, а файлы он читает инструментами с той же",
               "защитой. Правки со всех шагов собираются в один дифф, и применяются только после подтверждения."])
        self.preview = dialogs.show_text(self, "Что уйдёт ИИ", text)

    def preview_agent(self, task: str):
        if self.conductor_on:
            return self.preview_conductor(task)
        ws = self.make_workspace(log=False)
        msgs = orchestrator.build_messages(self.history, agent.first_message(task, ws, self.path))
        tr = self.active_translator()
        text = "\n".join([
            f"Режим: агент по проекту «{ws.root.name}»", f"Модель: {self.providers['strong'].name}",
            ("Перевод: задача уйдёт по-английски (gpt-4o-mini переводит только прозу, без кода, путей и "
             "секретов), ответ модели вернётся по-английски и будет переведён тебе" if tr else "Перевод: выключен"),
            "", "=== SYSTEM ===", prompts.with_reply(agent.SYSTEM, tr is not None), ""] + [f"=== {m['role'].upper()} ===\n{m['content']}\n" for m in msgs]
            + ["=== ДАЛЬШЕ ===",
               "Файлы модель запрашивает сама инструментами read_file, search, list_files. Каждый ответ",
               "инструмента проходит защиту (закрытые файлы не существуют, hidden и секреты скрыты,",
               "interface даёт только сигнатуры) и записывается в журнал отправок. Правки показываются",
               "тебе диффом и применяются только после подтверждения."])
        self.preview = dialogs.show_text(self, "Что уйдёт ИИ", text)

    def show_send_log(self):
        folder = self.tree.root or sendlog.project_dir(self.path)
        entries = sendlog.read(folder) if folder else []
        text = "\n".join(sendlog.format_entry(e) for e in reversed(entries)) or "Пока ничего не отправлялось."
        self.log_view = dialogs.show_text(self, "Журнал отправок", text)

    def open_folder(self):
        folder = FolderDialog.pick(self, self.tree.root)
        if folder:
            self.tree.set_root(folder)

    def save_file(self):
        doc = self.doc
        if doc.path is None:
            return self.save_as()
        doc.save()
        self.set_tab_label(doc)
        self.statusBar().showMessage(f"сохранено: {doc.path}", 3000)

    def save_as(self):
        name, _ = QFileDialog.getSaveFileName(self, "Сохранить как")
        if name:
            self.path = Path(name)
            self.refresh_access()
            self.save_file()

    # --- зоны ---
    def set_highlight(self, mode: str):
        self.highlight = mode
        for d in self.docs:
            d.editor.set_highlight(mode)
        self.statusBar().showMessage(f"подсветка в тексте: {mode}", 3000)

    def undo(self):
        self.editor.undo()
        self.after_undo()

    def redo(self):
        self.editor.redo()
        self.after_undo()

    def after_undo(self):
        """После отмены или повтора возвращает зоны, какими они были при таком тексте."""
        self.marks.restore(self.editor.toPlainText())
        self.paint_regions()

    def show_doc(self):
        """Если открыта вкладка журнала, команды разметки действуют на последний файл, и мы его показываем."""
        doc = self.doc
        if self.tabs.currentWidget() is not doc.editor:
            self.tabs.setCurrentWidget(doc.editor)

    def block_range(self) -> tuple[int, int]:
        self.show_doc()
        doc, cur = self.editor.document(), self.editor.textCursor()
        a = doc.findBlock(cur.selectionStart()).blockNumber()
        b = doc.findBlock(cur.selectionEnd()).blockNumber()
        if b > a and doc.findBlockByNumber(b).position() == cur.selectionEnd():
            b -= 1  # выделение заканчивается в начале строки - её не берём
        return a, b

    def mark(self, level: str):
        self.marks.add(level, *self.block_range())
        self.paint_regions()

    def unmark(self):
        self.marks.clear(*self.block_range())
        self.paint_regions()

    def ask_note(self, zone) -> str | None:
        text, ok = QInputDialog.getText(
            self, "Заметка к зоне",
            f"Что про этот код можно знать модели ({zone.level}, строки {zone.start + 1}-{zone.end + 1})?\n"
            "Например: «комиссия за платёж, сумма в копейках». Секретов не пиши: заметка уйдёт модели.",
            text=zone.note)
        return text if ok else None

    def edit_note(self, a: int, b: int):
        d = self.doc
        line = next((n for n in range(a, b + 1) if d.marks.zone_at(n, ("interface", "hidden"))), None)
        if line is None:
            self.say("заметку можно поставить только на зону interface или hidden")
            return
        text = self.ask_note(d.marks.zone_at(line, ("interface", "hidden")))
        if text is None:
            return
        d.marks.set_note(line, text, "")
        d.paint()
        if not text.strip():
            return
        tr = self.active_translator()
        if tr is not None and has_cyrillic(text):
            self.translate_note(d, line, text.strip(), tr)
        elif has_cyrillic(text):
            self.say("заметка уйдёт модели как написана, по-русски. Включи перевод или пиши по-английски: так дешевле")

    def translate_note(self, doc, line: int, text: str, tr):
        worker = Worker(tr.ru_to_en, text)
        self.note_workers.append(worker)

        def done(res):
            self.note_workers.remove(worker)
            zone = doc.marks.zone_at(line, ("interface", "hidden")) if doc in self.docs else None
            if zone is None or zone.note != text:
                return  # пока переводилось, заметку поменяли или зону сняли
            if res.translated:
                doc.marks.set_note(line, text, res.text)
                doc.paint()
                self.say("заметка переведена для модели: " + res.text[:120])
            elif res.note not in ("перевод не нужен", "переводить нечего"):
                self.say(f"заметка не переведена: {res.note}. Уйдёт как написана")
        worker.done.connect(done)
        worker.failed.connect(lambda err: (self.note_workers.remove(worker), self.say(f"заметка не переведена: {err}")))
        worker.start()

    def on_gutter_mark(self, action: str, a: int, b: int):
        if action == "note":
            self.edit_note(a, b)
            return
        if action == "unmark":
            self.marks.clear(a, b)
        else:
            self.marks.add(action, a, b)
        self.paint_regions()

    # --- ИИ ---
    def say(self, msg: str):
        self.chat.add_system(msg)

    def run_worker(self, fn, *args, on_done):
        self.ask_btn.setEnabled(False)
        self.explain_btn.setEnabled(False)
        self.chat.set_busy(True)
        self.worker = Worker(fn, *args)
        self.worker.done.connect(on_done)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def remember(self, task: str, reply: str, outcome: str | None = None,
                 task_en: str | None = None, reply_en: str | None = None):
        """Запоминает обмен репликами. Хранится только текст разговора, не содержимое файлов.
        task_en и reply_en - английские версии для модели, если шёл перевод."""
        suffix = f"\n[outcome: {outcome}]" if outcome else ""
        self.history += [orchestrator.Turn("user", task, self.sent_source, task_en or None),
                         orchestrator.Turn("assistant", reply + suffix, self.sent_source,
                                           reply_en + suffix if reply_en else None)]
        self.update_chat_title()
        self.save_chat()

    def save_chat(self):
        if self.tree.root is not None:
            chatstore.save(self.tree.root, self.chat.messages, self.history)

    def update_chat_title(self):
        n = len(self.history)
        self.chat_dock.setWindowTitle(f"Чат с ИИ (в памяти реплик: {n})" if n else "Чат с ИИ")

    def load_chat(self, root: Path):
        """Подгружает чат этого проекта. Если запрос ещё идёт, чат не трогаем."""
        if self.worker:
            return
        messages, self.history = chatstore.load(root)
        self.chat.reset(messages)
        self.update_chat_title()

    def clear_chat(self):
        if self.worker:
            return
        self.history.clear()
        self.gate.reset()  # новый чат - решения фильтра спрашиваются заново
        self.chat.reset()
        self.update_chat_title()
        self.say("новый чат: память очищена")

    def ask_ai(self, task: str):
        """Отправка из чата: при открытой папке работает агент по всему проекту, иначе один файл."""
        if self.tree.root is not None:
            self.ask_agent(task)
        else:
            self.ask_file(task)

    def ask_file(self, task: str):
        task = task.strip()
        if not task or self.worker or self.ai_blocked:
            return
        self.chat.add_user(task)
        if not self.may_send():
            self.chat.input.setPlainText(task)  # не теряем набранное
            return
        self.req_doc = self.doc
        self.base_text = self.req_doc.text()
        self.base_regions = list(self.req_doc.marks.items)
        self.sent_task, self.sent_source = task, self.path
        self.watch_task(task)
        self.run_worker(flows.run_ask, self.active_translator(), self.base_text, task, self.base_regions,
                        self.guarded_providers(), self.path, list(self.history), self.context_inputs(),
                        self.tree.root, on_done=self.on_result)

    # --- подключения ИИ ---
    @staticmethod
    def current_setup() -> "connections.Setup":
        conns, roles = connections.load()
        return connections.build(conns, roles) if conns else connections.env_setup()

    def setup_ai(self, setup: "connections.Setup"):
        """Принимает собранные подключения: реестр исполнителей, дирижёра, переводчика."""
        self.ai_setup = setup
        self.roster = setup.roster
        self.providers = setup.roster.providers()  # cheap/strong для режимов без дирижёра
        self.conductor_provider = setup.conductor
        tp = setup.translator
        self.translator = Translator(provider_transport(tp), log=self.log_translation, model=tp.name) if tp else None

    def refresh_translate_action(self):
        have = self.translator is not None
        self.act_translate.setEnabled(have)
        self.act_translate.setChecked(have and self.translate_on)
        self.act_translate.setToolTip("" if have else "подключи GPT-4o mini или GPT-4o в «ИИ - Подключения ИИ...» "
                                      "или выбери переводчика вручную")

    def show_models_in_status(self):
        s = self.ai_setup
        src = {"connections": "подключения", "env": "ключи из окружения", "demo": "демо, ключей нет"}[s.source]
        names = ", ".join(w.provider.name for w in s.roster.by_price())
        tr = s.translator.name if s.translator else "нет"
        rev = s.roster.reviewer()
        self.statusBar().showMessage(f"{BINDING}; {src}; исполнители: {names}; дирижёр: {s.conductor.name}; "
                                     f"проверка: {rev.provider.name if rev else 'нет'}; перевод: {tr}")

    def open_connections(self):
        conns, roles = connections.load()
        dlg = ConnectionsDialog(self, conns, roles)
        if dlg.exec():
            self.save_connections(dlg.result_rows, dlg.result_roles)

    def save_connections(self, rows, roles) -> bool:
        """Сохраняет подключения и сразу применяет их без перезапуска."""
        if not connections.save(rows, roles):
            self.say("не удалось сохранить подключения (нет доступа к папке настроек)")
            return False
        setup = self.current_setup()
        self.setup_ai(setup)
        self.refresh_translate_action()
        self.refresh_review_action()
        self.show_models_in_status()
        for p in setup.problems:
            self.say(f"подключение пропущено: {p}")
        if setup.source == "connections":
            self.say("подключено: " + setup.roster.labels() + f"; дирижёр: {setup.conductor.name}; "
                     + (f"перевод: {setup.translator.name}" if setup.translator else "перевод выключен"))
        else:
            self.say("подключений нет: работают ключи из окружения или демо-режим")
        return True

    # --- фильтр исходящего ---
    def decide_outbound(self, findings: list[outbound.Finding], model: str) -> str:
        """Вызывается из потока запроса: окно можно показать только из главного, туда и передаём."""
        if QThread.currentThread() == self.thread():
            return self.ask_outbound(findings, model)
        box = {"findings": findings, "model": model, "answer": "cancel"}
        self.outboundAsk.emit(box)  # ждёт, пока человек ответит
        return box["answer"]

    def answer_outbound(self, box: dict):
        box["answer"] = self.ask_outbound(box["findings"], box["model"])

    def ask_outbound(self, findings: list[outbound.Finding], model: str) -> str:
        """mask, send или cancel."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Фильтр исходящего")
        box.setText(f"В запросе к {model} найдено то, что лучше не отправлять: {len(findings)}.")
        lines = [f"• {f.kind}: {f.preview()}" for f in findings[:10]]
        if len(findings) > 10:
            lines.append(f"... и ещё {len(findings) - 10}")
        box.setInformativeText("\n".join(lines) + "\n\n«Скрыть» заменит это метками; в ответе модели "
                               "метки вернутся обратно. Решение запомнится до нового чата.")
        hide = box.addButton("Скрыть и отправить", QMessageBox.ButtonRole.AcceptRole)
        send = box.addButton("Отправить как есть", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(hide)
        box.exec()
        return {hide: "mask", send: "send"}.get(box.clickedButton(), "cancel")

    def log_filter(self, entry: dict):
        """В журнал отправок: что нашёл фильтр и что решил человек. Самих значений там нет."""
        sendlog.record(None, entry, getattr(self.tree, "root", None))

    def toggle_outbound(self):
        self.gate.enabled = self.act_outbound.isChecked()
        settings.set_user(outbound=self.gate.enabled)
        self.say("фильтр исходящего включён: каждый запрос проверяется на секреты, слова из списка и закрытый код"
                 if self.gate.enabled else
                 "фильтр исходящего выключен: текст задач и чата уходит модели без проверки")

    def edit_outbound_words(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "Слова для фильтра исходящего",
            "По одному на строке: имена клиентов, название компании, внутренние адреса серверов.\n"
            "Найденное в запросе фильтр предложит скрыть. Регистр не важен.\n"
            "Список хранится в настройках пользователя, не в проекте.",
            "\n".join(self.gate.words))
        if not ok:
            return
        words = [w.strip() for w in text.split("\n") if w.strip()]
        self.gate.set_words(words)
        if not settings.set_user(outbound_words=self.gate.words):
            self.say("не удалось сохранить список слов (нет доступа к папке настроек)")
        short = len(words) - len(self.gate.words)
        self.say(f"слов в фильтре: {len(self.gate.words)}"
                 + (f"; пропущено слишком коротких: {short}" if short > 0 else ""))

    def closed_code_lines(self) -> set[str]:
        """Строки закрытого кода в открытых файлах, кроме сигнатур interface: их модель и так видит."""
        out = set()
        for d in self.docs:
            lines = d.text().split("\n")
            for r in d.marks.items:
                if r.level == "hidden":
                    out.update(lines[r.start:r.end + 1])
                elif r.level == "interface":
                    out.update(l for l in lines[r.start:r.end + 1]
                               if not l.lstrip().startswith(("def ", "async def ", "class ", "@")))
        return out

    def watch_task(self, task: str):
        """Если человек вставил в задачу строки закрытого кода, фильтр спросит про них перед отправкой."""
        self.gate.watch_closed(task, self.closed_code_lines())

    def guarded_providers(self) -> dict:
        return {k: self.gate.wrap(p) for k, p in self.providers.items()}

    # --- переводчик ---
    def active_translator(self) -> Translator | None:
        """Переводчик за фильтром исходящего: переводу уходит проза задач и ответов, она тоже проверяется."""
        t = self.translator if self.translate_on else None
        if t is None:
            return None
        guarded = Translator(self.gate.wrap_transport(t.transport, t.model), t.log, t.model)
        guarded.cache = t.cache
        return guarded

    def log_translation(self, direction: str, chars: int):
        """Каждое обращение к переводчику - тоже отправка стороннему сервису, она идёт в журнал."""
        sendlog.record(None, {"mode": f"translate:{direction}", "model": self.translator.model, "files": [],
                              "chars": chars, "messages": 0}, getattr(self.tree, "root", None))

    def toggle_translate(self):
        self.translate_on = self.act_translate.isChecked()
        settings.set_user(translate=self.translate_on)
        if self.translate_on:
            self.say("перевод включён: в OpenAI уходит только проза задач и ответов, без кода, путей, "
                     "ссылок и строк с секретами. Модели-исполнителю задача уходит по-английски")
        else:
            self.say("перевод выключен: задачи уходят как написаны, ответы приходят на твоём языке")

    # --- журнал правок ---
    def toggle_journal(self):
        self.journal_on = self.act_journal.isChecked()
        settings.set_user(journal=self.journal_on)
        self.say("журнал правок откроется после работы ИИ" if self.journal_on else "журнал правок после работы ИИ не откроется")

    def goto_place(self, file: str, line: int):
        root = self.tree.root
        self.open_at(root / file if root else Path(file), line)

    def build_journal(self, entries, authors: dict | None = None, reports=()) -> list:
        """entries: (имя файла, старый текст, новый текст, кто писал, нужна ли проверка confirm-зоны)."""
        hunks: list = []
        for name, old, new, author, confirm in entries:
            mine = journal.hunks_for(name, old, new, (authors or {}).get(name, author))
            if confirm and mine:
                journal.attach_remarks(mine, [(name, None, "Правка затрагивает зону confirm: проверь внимательно")])
            hunks += mine
        remarks = []
        for report in reports:
            if report is None or report.skipped:
                continue
            for f in getattr(report, "remaining", []):
                if hasattr(f, "severity"):
                    remarks.append((f.file, f.line, f"Безопасность [{f.severity}]: {f.issue}"))
                else:
                    remarks.append((f.file, f.line, f"Статанализ [{f.tool} {f.code}]: {f.message}"))
        journal.attach_remarks(hunks, remarks)
        return hunks

    def show_journal(self, hunks: list):
        """Открывает журнал: места правок от больших к малым. Окно не блокирует работу в редакторе."""
        self.last_journal = hunks
        if not hunks or not self.journal_on:
            return
        self.put_journal_tab(hunks)

    def open_journal(self):
        if not self.last_journal:
            self.say("журнал пуст: ИИ ещё ничего не писал в этом сеансе")
            return
        self.put_journal_tab(self.last_journal)

    def put_journal_tab(self, hunks: list):
        """Журнал открывается вкладкой рядом с файлами. Повторный журнал заменяет прежний."""
        if self.journal_view is not None:
            i = self.tabs.indexOf(self.journal_view)
            if i >= 0:
                self.tabs.removeTab(i)
            self.journal_view.deleteLater()
        self.journal_view = JournalView(self, hunks, self.goto_place)
        i = self.tabs.addTab(self.journal_view, f"Журнал правок ({len(hunks)})")
        self.tabs.setTabToolTip(i, "Места, где ИИ что-то написал, от самых больших к самым маленьким")
        self.tabs.setCurrentIndex(i)

    def toggle_analyze(self):
        self.analyze_on = self.act_analyze.isChecked()
        settings.set_user(analyze=self.analyze_on)
        if not self.analyze_on:
            self.say("статанализ выключен")
            return
        root = self.tree.root or Path.cwd()
        found = [a.exe for a in analysis.ANALYZERS if analysis.find_binary(a.exe, root)]
        missing = [a.exe for a in analysis.ANALYZERS if a.exe not in found]
        self.say("статанализ включён: правки проверяются на копии проекта, автору возвращаются только новые "
                 "замечания. Найдены: " + (", ".join(found) or "ничего") + (f". Не найдены: {', '.join(missing)}" if missing else ""))

    def show_analysis(self, report):
        """Отчёт статанализа в чат. О ненайденных программах говорим один раз за сессию."""
        if report is None or report.skipped:
            return
        for note in report.notes:
            if note not in self.analysis_notes_shown:
                self.analysis_notes_shown.add(note)
                self.say(f"статанализ: {note}")
        if not report.rounds or report.stopped and not report.rounds:
            return
        if not report.tools:
            self.say("статанализ: ни одна программа не отработала, ничего не проверено "
                     "(например: pip install ruff mypy bandit)")
            return
        names = ", ".join(report.tools)
        if not report.remaining:
            self.say(f"статанализ ({names}): новых замечаний нет"
                     + (f", исправлено за {report.fixed_rounds} кр." if report.fixed_rounds else ""))
        else:
            self.say(f"статанализ ({names}): осталось новых замечаний {len(report.remaining)}")
            for i in report.remaining:
                self.say(f"  [{i.tool} {i.code}] {i.file}:{i.line} - {i.message}")
        ignored = sum(r.ignored for r in report.rounds)
        if ignored:
            self.say(f"статанализ: замечаний в закрытых зонах пропущено: {ignored}")

    def review_active(self) -> bool:
        return self.review_on and self.roster.reviewer() is not None

    def refresh_review_action(self):
        have = self.roster.reviewer() is not None
        self.act_review.setEnabled(have)
        self.act_review.setChecked(have and self.review_on)
        self.act_review.setToolTip("" if have else "подключи проверяющую модель (специализация «Безопасность») "
                                                   "в «ИИ - Подключения ИИ...»")

    def toggle_review(self):
        self.review_on = self.act_review.isChecked()
        settings.set_user(review=self.review_on)
        r = self.roster.reviewer()
        self.say((f"проверка безопасности включена: {r.key} читает все правки до применения, серьёзные замечания "
                  "возвращаются автору (не больше двух кругов)") if self.review_on and r else "проверка безопасности выключена")

    def show_review(self, report):
        """Короткий отчёт проверяющего в чат. Закрытый от него код называется явно."""
        if report is None or report.skipped:
            return
        if report.stopped and not report.rounds:
            return
        if report.unparsed:
            self.say(f"безопасность ({report.reviewer}): ответ проверяющего не разобран, замечания неизвестны")
        elif not report.remaining:
            self.say(f"безопасность ({report.reviewer}): замечаний нет"
                     + (f", исправлено за {report.fixed_rounds} кр." if report.fixed_rounds else ""))
        else:
            self.say(f"безопасность ({report.reviewer}): осталось замечаний {len(report.remaining)}, "
                     f"кругов исправления: {report.fixed_rounds}")
            for f in report.remaining:
                self.say(f"  [{f.severity}] {f.file}" + (f":{f.line}" if f.line else "") + f" - {f.issue}")
        for rnd in report.rounds:
            for note in rnd.fix_notes:
                self.say(f"ответ автора: {note}")
        if report.gaps:
            self.say("не проверено, закрытый код: " + "; ".join(f"{n} - {h} из {t} строк" for n, h, t in report.gaps))

    def toggle_conductor(self):
        self.conductor_on = self.act_conductor.isChecked()
        settings.set_user(conductor=self.conductor_on)
        if self.conductor_on:
            self.say(f"дирижёр включён ({self.conductor_provider.name}): задачу разобьёт на шаги и отдаст "
                     f"исполнителям: {self.roster.labels()}")
        else:
            self.say(f"дирижёр выключен: задачу целиком выполняет {self.providers['strong'].name}")

    def show_notes(self, notes: list[str], task_en: str = ""):
        for n in notes:
            self.say(n)

    # --- агент по проекту ---
    def live_files(self) -> dict:
        """Открытые вкладки: их текст и зоны важнее диска, даже несохранённые."""
        return {d.path: (d.text(), list(d.marks.items)) for d in self.docs if d.path}

    def make_workspace(self, log: bool = True) -> Workspace:
        return Workspace(self.tree.root, self.live_files(), self.providers["strong"].name, log_enabled=log)

    def ask_agent(self, task: str):
        task = task.strip()
        if not task or self.worker:
            return
        self.chat.add_user(task)
        if not self.may_send(project=True):
            self.chat.input.setPlainText(task)
            return
        self.sent_task, self.sent_source = task, self.path
        provider = self.conductor_provider if self.conductor_on else self.providers["strong"]
        self.ask_btn.setEnabled(False)
        self.explain_btn.setEnabled(False)
        self.chat.set_busy(True, "Дирижёр планирует..." if self.conductor_on else "Агент работает...", can_stop=True)
        self.agent_provider = provider
        self.watch_task(task)
        self.worker = AgentWorker(task, self.make_workspace(), self.gate.wrap(provider), list(self.history), self.path,
                                  self.active_translator(), self.gate.wrap_roster(self.roster), self.conductor_on,
                                  self.review_active(), self.analyze_on)
        self.worker.progress.connect(self.say)
        self.worker.done.connect(self.on_agent_done)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def stop_agent(self):
        if isinstance(self.worker, AgentWorker):
            self.worker.cancel = True
            self.say("останавливаю агента после текущего шага...")

    def review_changes(self, items: list[dict]) -> list[bool]:
        return dialogs.review_changes(self, items, "Правки ИИ по проекту")

    def on_agent_done(self, res: agent.AgentResult):
        self.finish()
        meta = f"{self.agent_provider.name} · агент · шагов: {res.steps}"
        if isinstance(res, conductor.ConductResult) and res.plan is not None:
            workers = ", ".join(dict.fromkeys(t.worker for t in res.plan.tasks))
            meta = f"{self.agent_provider.name} · дирижёр · шагов плана: {len(res.plan.tasks)} ({workers}) · запросов: {res.steps}"
        if res.stopped:
            meta += f" · {res.stopped}"
        self.chat.add_ai(res.text or "Готово.", meta)
        self.show_notes(res.notes)
        self.show_analysis(res.analysis)
        self.show_review(res.review)
        outcome = prompts.OUT_NO_EDITS
        if res.changes:
            outcome = self.apply_changes(res.changes, res.review, res.analysis)
            for extra in (prompts.analysis_outcome(res.analysis), prompts.review_outcome(res.review)):
                if extra:
                    outcome += "; " + extra
        self.remember(self.sent_task, res.text or "Готово.", outcome, res.task_en, res.text_en)
        if res.changes and self.applied:
            self.show_journal(self.build_journal(
                [(name, ch.old, ch.new, "", bool(ch.needs_confirm)) for ch, name in self.applied], res.authors,
                (res.analysis, res.review)))
        if res.changes and outcome.startswith("edits applied: ") and not outcome.startswith("edits applied: none") and self.check_cfg.get("auto") and self.check_cfg.get("test_command"):
            QTimer.singleShot(0, self.run_check_after_ai)

    @staticmethod
    def review_notes(report, name: str) -> list[str]:
        """Что проверяющий сказал именно про этот файл: остаток замечаний и непроверенный закрытый код."""
        if report is None or report.skipped:
            return []
        notes = [f"Безопасность [{f.severity}]" + (f" строка {f.line}" if f.line else "") + f": {f.issue}"
                 for f in report.remaining if f.file == name]
        notes += [f"Не проверено безопасником: закрыто {h} из {t} строк этого файла."
                  for n, h, t in report.gaps if n == name]
        return notes

    @staticmethod
    def analysis_notes(report, name: str) -> list[str]:
        if report is None or report.skipped:
            return []
        return [f"Статанализ [{i.tool} {i.code}] строка {i.line}: {i.message}" for i in report.remaining if i.file == name]

    def apply_changes(self, changes, report=None, analysis_report=None) -> str:
        """Показывает набор правок и применяет выбранные. Возвращает итог для памяти."""
        items = [{"name": self.make_ws_label(c.path), "old": c.old, "new": c.new, "created": c.created,
                  "notes": [f"Затрагивает {z}: проверь внимательно." for z in c.needs_confirm]
                  + self.analysis_notes(analysis_report, self.make_ws_label(c.path))
                  + self.review_notes(report, self.make_ws_label(c.path))} for c in changes]
        flags = self.review_changes(items)
        done, skipped, skipped_en = [], [], []
        self.applied = []
        for ch, ok, item in zip(changes, flags, items):
            if not ok:
                continue
            reason = self.write_one(ch)
            if reason:
                skipped.append(f"{item['name']} ({reason[0]})")
                skipped_en.append(f"{item['name']} ({reason[1]})")
            else:
                done.append(item["name"])
                self.applied.append((ch, item["name"]))
        for line in skipped:
            self.say(f"не применено: {line}")
        if done:
            self.say("применено: " + ", ".join(done))
        if not done and not skipped:
            self.say("правки отклонены")
            return prompts.OUT_USER_REJECT_ALL
        return prompts.outcome_applied(done, skipped_en)

    def make_ws_label(self, path: Path) -> str:
        try:
            return path.relative_to(os.path.realpath(self.tree.root)).as_posix()
        except ValueError:
            return str(path)

    def write_one(self, ch) -> tuple[str, str] | None:
        """Применяет одно изменение. При неудаче возвращает причину (по-русски, по-английски), иначе None."""
        doc = next((d for d in self.docs if d.path and Path(os.path.realpath(d.path)) == ch.path), None)
        if doc is not None:
            if doc.text() != ch.old:
                return "изменился в редакторе, пока работал агент", "changed in the editor while the agent worked"
            doc.marks.remember(ch.old)
            doc.silent = True
            cur = doc.editor.textCursor()
            cur.beginEditBlock()
            cur.select(QTextCursor.SelectionType.Document)
            cur.insertText(ch.new)
            cur.endEditBlock()
            doc.silent = False
            doc.block_count = doc.editor.blockCount()
            doc.marks.items = list(ch.regions)
            doc.paint()
            return None
        try:
            now = ch.path.read_text(encoding="utf-8") if ch.path.exists() else None
            if now != ch.old:
                return (("изменился на диске, пока работал агент", "changed on disk while the agent worked")
                        if ch.old is not None else ("такой файл уже появился", "such a file appeared meanwhile"))
            write_change(ch)
        except (OSError, UnicodeDecodeError) as e:
            return str(e), str(e)
        return None

    def explain_ai(self):
        if self.worker or self.ai_blocked:
            return
        if not any(r.level == "explain" for r in self.marks.items):
            self.say("нет зелёных зон: выдели строки в колонке слева и выбери explain (Ctrl+4)")
            return
        if not self.may_send():
            return
        self.chat.add_user("Объясни зелёные зоны")
        self.sent_task, self.sent_source = "Объясни зелёные зоны", self.path
        self.run_worker(flows.run_explain, self.active_translator(), self.editor.toPlainText(),
                        list(self.marks.items), self.guarded_providers(), self.path, list(self.history),
                        self.context_inputs(), self.tree.root, on_done=self.on_explain)

    def finish(self):
        self.worker = None
        self.chat.set_busy(False)
        self.refresh_access()

    def on_fail(self, err: str):
        if err.startswith(outbound.SendCancelled.__name__):
            self.say("запрос остановлен в фильтре исходящего: найденное модели не ушло")
            if not self.chat.input.toPlainText().strip():
                self.chat.input.setPlainText(self.sent_task)  # не теряем набранное
        else:
            self.say(f"ошибка: {err}")
        self.finish()

    def on_explain(self, res: orchestrator.Explanation):
        self.finish()
        self.chat.add_ai(res.text, f"{res.provider.name} · зон: {res.zones}")
        self.show_notes(res.notes)
        if res.zones:
            self.remember(self.sent_task, res.text, None,
                          "Explain the green zones" if res.text_en and self.active_translator() else None, res.text_en)

    def on_result(self, res: orchestrator.Result):
        self.finish()
        meta = f"{res.provider.name} · {res.reason}"
        if res.hidden_count:
            meta += f" · скрыто кусков: {res.hidden_count}"
        reply = res.note or "Готово."
        self.chat.add_ai(reply, meta)
        if res.task_en:
            self.say("задача переведена: " + res.task_en.replace("\n", " ")[:160])
        self.show_notes(res.notes)
        self.remember(self.sent_task, reply, self.apply_result(res), res.task_en, res.note_en)

    def review_diff(self, old: str, new: str, notes: list[str]) -> bool:
        return dialogs.confirm_diff(self, old, new, notes, f"Правка ИИ - {self.req_doc.name}")

    def apply_result(self, res: orchestrator.Result) -> str:
        """Применяет правку, если проверка зон её пропускает. Возвращает итог для памяти."""
        d = self.req_doc
        v = res.verdict
        if not v.ok:
            self.say("правка отклонена: " + "; ".join(v.violations))
            return prompts.OUT_ZONE_REJECT
        if d is None or d not in self.docs:
            self.say("вкладку закрыли, пока ИИ думал - правка не применена")
            return prompts.OUT_TAB_CLOSED
        if d.text() != self.base_text:
            self.say("файл изменился, пока ИИ думал - правка не применена")
            return prompts.OUT_CHANGED
        if res.new_text == self.base_text:
            self.say("ИИ ничего не изменил")
            return prompts.OUT_NO_CHANGE
        notes = [f"Правка затрагивает {z}: проверь её внимательно." for z in v.needs_confirm]
        if not self.review_diff(self.base_text, res.new_text, notes):
            self.say("правка отклонена пользователем")
            return prompts.OUT_USER_REJECT
        if d not in self.docs:
            return prompts.OUT_TAB_CLOSED_REVIEW
        d.marks.remember(self.base_text)
        d.silent = True
        cur = d.editor.textCursor()
        cur.beginEditBlock()
        cur.select(QTextCursor.SelectionType.Document)
        cur.insertText(res.new_text)
        cur.endEditBlock()
        d.silent = False
        d.block_count = d.editor.blockCount()
        d.marks.items = guard.remap(self.base_regions, self.base_text, res.new_text)
        d.paint()
        self.say("правка применена")
        self.show_journal(self.build_journal([(d.name, self.base_text, res.new_text, res.provider.name,
                                               bool(v.needs_confirm))]))
        if self.check_cfg.get("auto") and self.check_cfg.get("test_command"):
            QTimer.singleShot(0, self.run_check_after_ai)
        return prompts.OUT_APPLIED


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    win.resize(1100, 700)
    win.show()
    if target:
        win.tree.set_root(target if target.is_dir() else target.parent)
        if not target.is_dir():
            win.load(target)
    sys.exit(app.exec())
