"""Поиск по проекту: результаты можно открыть или добавить файл в контекст ИИ."""
from pathlib import Path

from . import related
from .qt import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                 QPushButton, QVBoxLayout)


class SearchDialog(QDialog):
    def __init__(self, parent, root: Path, on_open, on_context):
        super().__init__(parent)
        self.root, self.on_open, self.on_context = root, on_open, on_context
        self.hits: list[related.Hit] = []
        self.setWindowTitle(f"Поиск по проекту - {root.name}")
        self.resize(720, 480)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Что искать (закрытые от ИИ файлы тоже не показываются)")
        self.query.returnPressed.connect(self.run)
        self.case = QCheckBox("Регистр")
        go = QPushButton("Искать")
        go.clicked.connect(lambda _=False: self.run())
        self.results = QListWidget()
        self.results.itemActivated.connect(lambda _item: self.open_selected())
        self.status = QLabel("")
        open_btn, ctx_btn = QPushButton("Открыть"), QPushButton("Добавить файл в контекст ИИ")
        open_btn.clicked.connect(lambda _=False: self.open_selected())
        ctx_btn.clicked.connect(lambda _=False: self.context_selected())
        top, bottom = QHBoxLayout(), QHBoxLayout()
        for w in (self.query, self.case, go):
            top.addWidget(w)
        top.setStretch(0, 1)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(open_btn)
        bottom.addWidget(ctx_btn)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.results)
        lay.addLayout(bottom)

    def run(self) -> int:
        self.hits = related.search(self.root, self.query.text(), self.case.isChecked())
        self.results.clear()
        for h in self.hits:
            self.results.addItem(QListWidgetItem(f"{h.path.relative_to(self.root)}:{h.line}   {h.text}"))
        n = len(self.hits)
        self.status.setText("ничего не найдено" if not n else
                            f"найдено: {n}" + (" (показаны первые)" if n >= related.MAX_HITS else ""))
        return n

    def selected(self) -> related.Hit | None:
        row = self.results.currentRow()
        return self.hits[row] if 0 <= row < len(self.hits) else None

    def open_selected(self):
        if (hit := self.selected()) is not None:
            self.on_open(hit.path, hit.line)

    def context_selected(self):
        if (hit := self.selected()) is not None:
            self.on_context(hit.path)
