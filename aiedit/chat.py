"""Панель чата: история сверху (новые сообщения снизу), поле ввода внизу."""
import html

from .qt import (QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, Qt, QTextEdit,
                 QToolButton, QVBoxLayout, QWidget, Signal)

USER_BG, USER_FG = "#2b6cb0", "#ffffff"
AI_BG, AI_FG = "#e4e7eb", "#1a1a1a"
MUTED = "#8a8f98"


def _text(s: str) -> str:
    return html.escape(s).replace("\n", "<br>")


def bubble_html(kind: str, text: str, meta: str = "") -> str:
    """HTML одного сообщения. kind: user, ai, system."""
    if kind == "check":
        label = f'<span style="color:{MUTED}; font-size:small;">{_text(meta)}</span>' if meta else ""
        return (f'<p style="margin:6px 0 0 0;">{label}</p>'
                f'<table width="100%" cellpadding="6" cellspacing="0" bgcolor="#22272e"><tr><td>'
                f'<pre style="color:#d0d7de; margin:0;">{html.escape(text)}</pre></td></tr></table>'
                f'<p style="margin:0;"></p>')
    if kind == "system":
        return f'<p align="center" style="color:{MUTED}; margin:6px 12px;"><i>{_text(text)}</i></p>'
    user = kind == "user"
    bg, fg = (USER_BG, USER_FG) if user else (AI_BG, AI_FG)
    side = "right" if user else "left"
    label = f'<span style="color:{MUTED}; font-size:small;">{_text(meta)}</span><br>' if meta else ""
    return (f'<p align="{side}" style="margin:6px 0 0 0;">{label}</p>'
            f'<table align="{side}" width="88%" cellpadding="8" cellspacing="0" bgcolor="{bg}">'
            f'<tr><td><span style="color:{fg};">{_text(text)}</span></td></tr></table>'
            f'<p style="margin:0;"></p>')


class ChatInput(QPlainTextEdit):
    """Enter отправляет, Shift+Enter - перенос строки."""
    submitted = Signal()

    def keyPressEvent(self, e):
        if (e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class ChatPanel(QWidget):
    sent = Signal(str)
    explainRequested = Signal()
    clearRequested = Signal()
    contextCleared = Signal()
    changed = Signal()
    stopRequested = Signal()

    def __init__(self):
        super().__init__()
        self.messages: list[tuple] = []  # (kind, text, meta) - для сохранения и перерисовки
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.busy_label = QLabel("ИИ думает...")
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.clicked.connect(lambda _=False: self.stopRequested.emit())
        self.busy_row = QWidget()
        busy = QHBoxLayout(self.busy_row)
        busy.setContentsMargins(0, 0, 0, 0)
        busy.addWidget(self.busy_label, 1)
        busy.addWidget(self.stop_btn)
        self.busy_row.hide()
        self.input = ChatInput()
        self.input.setPlaceholderText("Напиши задачу для ИИ (Enter - отправить, Shift+Enter - новая строка)")
        self.input.setFixedHeight(72)
        self.input.submitted.connect(self.submit)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.clicked.connect(self.submit)
        self.explain_btn = QPushButton("Объяснить зелёные зоны")
        self.explain_btn.clicked.connect(self.explainRequested)
        # меню справа вверху панели
        self.menu = QMenu(self)
        self.new_chat_action = self.menu.addAction("Новый чат")
        self.new_chat_action.setToolTip("Очистить историю и память ИИ")
        self.new_chat_action.triggered.connect(lambda _=False: self.clearRequested.emit())
        self.menu_btn = QToolButton()
        self.menu_btn.setText("Меню")
        self.menu_btn.setMenu(self.menu)
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self.menu_btn)
        # файлы, которые уходят в запрос как контекст (только чтение)
        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_clear = QPushButton("Сбросить контекст")
        self.context_clear.clicked.connect(lambda _=False: self.contextCleared.emit())
        self.context_row = QWidget()
        ctx = QHBoxLayout(self.context_row)
        ctx.setContentsMargins(0, 0, 0, 0)
        ctx.addWidget(self.context_label, 1)
        ctx.addWidget(self.context_clear)
        self.context_row.hide()

        row = QHBoxLayout()
        row.addWidget(self.explain_btn)
        row.addStretch()
        row.addWidget(self.send_btn)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.history, 1)
        lay.addWidget(self.context_row)
        lay.addWidget(self.busy_row)
        lay.addWidget(self.input)
        lay.addLayout(row)

    def set_context(self, names: list[str]):
        self.context_row.setVisible(bool(names))
        self.context_label.setText("В контексте (только чтение): " + ", ".join(names))

    def add_menu_action(self, action):
        self.menu.addAction(action)

    def submit(self):
        text = self.input.toPlainText().strip()
        if text and self.send_btn.isEnabled():
            self.input.clear()
            self.sent.emit(text)

    def add(self, kind: str, text: str, meta: str = "", save: bool = True):
        self.messages.append((kind, text, meta))
        self.history.append(bubble_html(kind, text, meta))
        bar = self.history.verticalScrollBar()
        bar.setValue(bar.maximum())
        if save:
            self.changed.emit()

    def reset(self, messages: list[tuple] | None = None):
        """Очищает окно и показывает сохранённые сообщения."""
        self.messages = []
        self.history.clear()
        for kind, text, meta in messages or []:
            self.add(kind, text, meta, save=False)

    def add_user(self, text: str):
        self.add("user", text)

    def add_ai(self, text: str, meta: str = ""):
        self.add("ai", text, meta)

    def add_system(self, text: str):
        self.add("system", text)

    def set_busy(self, busy: bool, text: str = "ИИ думает...", can_stop: bool = False):
        self.busy_label.setText(text)
        self.stop_btn.setVisible(busy and can_stop)
        self.busy_row.setVisible(busy)
