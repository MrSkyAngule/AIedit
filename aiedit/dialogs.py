"""Простые окна: показ текста (предпросмотр запроса, журнал отправок)."""
import difflib
import html

from .qt import (QDialog, QFont, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
                 QPushButton, Qt, QTextEdit, QVBoxLayout)

_DIFF_STYLE = {"+": "background:#d4f5d4;", "-": "background:#f9d6d6;", "@": "background:#e3e8f0;"}


def diff_html(old: str, new: str, context: int = 3) -> str:
    """Построчный дифф «было и стало» в HTML. Пусто, если различий нет."""
    lines = difflib.unified_diff(old.split("\n"), new.split("\n"), "было", "стало",
                                 n=context, lineterm="")
    out = []
    for line in lines:
        style = _DIFF_STYLE.get(line[:1], "")
        if line.startswith(("---", "+++")):
            style = "background:#e3e8f0;"
        out.append(f'<div style="{style} color:#111; white-space:pre;">{html.escape(line) or "&nbsp;"}</div>')
    return "".join(out)


def confirm_diff(parent, old: str, new: str, notes: list[str] | None = None,
                 title: str = "Правка ИИ") -> bool:
    """Показывает дифф. True - применить, False - отклонить."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(820, 560)
    lay = QVBoxLayout(dlg)
    for note in notes or []:
        label = QLabel(note)
        label.setWordWrap(True)
        label.setStyleSheet("color:#b45309; font-weight:bold;")
        lay.addWidget(label)
    view = QTextEdit()
    view.setReadOnly(True)
    view.setFont(QFont("monospace", 10))
    view.setHtml(diff_html(old, new))
    lay.addWidget(view)
    apply_btn, reject_btn = QPushButton("Применить"), QPushButton("Отклонить")
    apply_btn.setDefault(True)
    apply_btn.clicked.connect(dlg.accept)
    reject_btn.clicked.connect(dlg.reject)
    row = QHBoxLayout()
    row.addStretch()
    row.addWidget(reject_btn)
    row.addWidget(apply_btn)
    lay.addLayout(row)
    dlg.view = view
    return bool(dlg.exec())


def show_diff(parent, title: str, old: str, new: str) -> QDialog:
    """Дифф только для чтения (без «применить»). Не блокирует окно."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(820, 560)
    view = QTextEdit()
    view.setReadOnly(True)
    view.setFont(QFont("monospace", 10))
    view.setHtml(diff_html(old, new) or "<i>различий нет</i>")
    close = QPushButton("Закрыть")
    close.clicked.connect(dlg.accept)
    lay = QVBoxLayout(dlg)
    lay.addWidget(view)
    lay.addWidget(close)
    dlg.view = view
    dlg.open()
    return dlg


def show_text(parent, title: str, text: str, width: int = 760, height: int = 560) -> QDialog:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(width, height)
    view = QPlainTextEdit(text)
    view.setReadOnly(True)
    view.setFont(QFont("monospace", 10))
    close = QPushButton("Закрыть")
    close.clicked.connect(dlg.accept)
    lay = QVBoxLayout(dlg)
    lay.addWidget(view)
    lay.addWidget(close)
    dlg.view = view
    dlg.open()  # не блокирует, так что окно можно оставить открытым
    return dlg


class ReviewDialog(QDialog):
    """Набор правок по нескольким файлам: галочки у файлов и дифф выбранного."""

    def __init__(self, parent, items: list[dict], title: str = "Правки ИИ"):
        super().__init__(parent)
        self.items = items
        self.setWindowTitle(title)
        self.resize(900, 580)
        self.files = QListWidget()
        for it in items:
            row = QListWidgetItem(it["name"] + ("  (удаление)" if it.get("deleted") else
                                               "  (новый)" if it.get("created") else ""))
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked)
            self.files.addItem(row)
        self.files.setMaximumWidth(280)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#b45309; font-weight:bold;")
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("monospace", 10))
        self.files.currentRowChanged.connect(self.show_item)
        apply_btn, reject_btn = QPushButton("Применить выбранные"), QPushButton("Отклонить всё")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)
        reject_btn.clicked.connect(self.reject)
        right = QVBoxLayout()
        right.addWidget(self.note)
        right.addWidget(self.view)
        body = QHBoxLayout()
        body.addWidget(self.files)
        body.addLayout(right, 1)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(reject_btn)
        row.addWidget(apply_btn)
        lay = QVBoxLayout(self)
        lay.addLayout(body)
        lay.addLayout(row)
        self.files.setCurrentRow(0)

    def show_item(self, i: int):
        if 0 <= i < len(self.items):
            it = self.items[i]
            self.note.setText("\n".join(it.get("notes") or []))
            self.view.setHtml(diff_html(it["old"] or "", it["new"]))

    def selected(self) -> list[bool]:
        return [self.files.item(i).checkState() == Qt.CheckState.Checked for i in range(len(self.items))]


def review_changes(parent, items: list[dict], title: str = "Правки ИИ") -> list[bool]:
    """Показывает правки. Возвращает по флагу на файл; всё False, если человек отклонил."""
    dlg = ReviewDialog(parent, items, title)
    return dlg.selected() if dlg.exec() else [False] * len(items)
