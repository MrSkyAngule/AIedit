"""Один открытый файл: поле редактора, зоны, путь и признаки состояния."""
from pathlib import Path

from . import guard
from .editor import CodeEdit
from .marks import Marks
from .qt import QFont, QPlainTextEdit, QTimer
from .syntax import Highlighter


class Doc:
    def __init__(self, highlight: str = "fill"):
        self.path: Path | None = None
        self.marks = Marks()
        self.silent = False  # true, пока текст заменяется целиком (загрузка, правка ИИ)
        self.block_count = 1
        self.saved_marks: list = []  # зоны, как они лежат на диске
        self.on_save_error = None    # куда сообщить, если зоны сохранить не удалось
        self._error_reported = False
        self.editor = CodeEdit()
        self.editor.setFont(QFont("monospace", 11))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.set_highlight(highlight)
        dark = self.editor.palette().base().color().lightness() < 128
        self.highlighter = Highlighter(self.editor.document(), dark)
        self.editor.document().contentsChange.connect(self.on_contents_change)
        self.timer = QTimer(self.editor)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.paint)
        self.editor.textChanged.connect(self.timer.start)

    def set_path(self, path: Path | None):
        self.path = path
        self.highlighter.set_language(path.suffix if path else "")

    @property
    def name(self) -> str:
        return self.path.name if self.path else "без имени"

    @property
    def modified(self) -> bool:
        return self.editor.document().isModified()

    def text(self) -> str:
        return self.editor.toPlainText()

    def on_contents_change(self, pos: int, removed: int, added: int):
        doc = self.editor.document()
        if not self.silent:
            self.marks.on_change(doc.findBlock(pos).blockNumber(), doc.blockCount() - self.block_count)
        self.block_count = doc.blockCount()

    def paint(self):
        self.marks.remember(self.text())
        self.editor.set_regions(guard.line_regions(self.editor.blockCount(), self.marks.items))
        self.autosave_marks()

    def autosave_marks(self) -> bool:
        """Зоны переживают закрытие вкладки и программы: пишутся сразу, как только изменились.
        Пока в файле есть несохранённый текст, ждём его сохранения: номера строк относятся к тексту в окне,
        и на диск они должны попасть вместе с ним."""
        if self.path is None or self.modified or self.marks.items == self.saved_marks:
            return True
        try:
            self.marks.save(self.path, self.text())
        except OSError as e:
            if self.on_save_error and not self._error_reported:  # один раз, а не после каждой буквы
                self.on_save_error(f"не удалось сохранить зоны для {self.path.name}: {e.strerror or e}")
            self._error_reported = True
            return False
        self._error_reported = False
        self.saved_marks = list(self.marks.items)
        return True

    def replace_text(self, text: str):
        self.silent = True
        self.editor.setPlainText(text)
        self.silent = False
        self.block_count = self.editor.blockCount()
        self.editor.document().setModified(False)

    def load(self, path: Path, text: str) -> bool:
        """Загружает текст и зоны. False, если файл менялся вне редактора."""
        self.replace_text(text)
        self.set_path(path)
        fresh = self.marks.load(path, text)
        self.saved_marks = list(self.marks.items)
        self.paint()
        return fresh

    def save(self):
        text = self.text()
        self.path.write_text(text, encoding="utf-8")
        self.marks.save(self.path, text)
        self.saved_marks = list(self.marks.items)
        self.editor.document().setModified(False)
