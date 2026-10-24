"""Панель Git: ветка, изменённые файлы, дифф по двойному щелчку, коммит выбранного."""
from pathlib import Path

from . import gitops
from .qt import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, Qt,
                 QVBoxLayout, QWidget, Signal)


class GitPanel(QWidget):
    diffRequested = Signal(str, object, str)  # имя, текст в последнем коммите (или None), текст сейчас
    message = Signal(str)                     # что сказать человеку в чат

    def __init__(self):
        super().__init__()
        self.root: Path | None = None   # папка, открытая в редакторе
        self.repo: Path | None = None   # корень репозитория (может быть выше)
        self.entries: list[gitops.Entry] = []
        self.head = QLabel("")
        self.head.setWordWrap(True)
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(lambda _=False: self.refresh())
        top = QHBoxLayout()
        top.addWidget(self.head, 1)
        top.addWidget(self.refresh_btn)
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self.show_diff)
        self.files.setToolTip("Двойной щелчок - что изменилось. Галочка - войдёт в коммит")
        self.msg = QPlainTextEdit()
        self.msg.setPlaceholderText("Сообщение коммита: что и зачем изменено")
        self.msg.setFixedHeight(64)
        self.commit_btn = QPushButton("Закоммитить выбранное")
        self.commit_btn.clicked.connect(lambda _=False: self.commit())
        self.init_btn = QPushButton("Создать репозиторий (git init)")
        self.init_btn.clicked.connect(lambda _=False: self.init_repo())
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.files, 1)
        lay.addWidget(self.msg)
        lay.addWidget(self.commit_btn)
        lay.addWidget(self.init_btn)
        self.refresh()

    def set_root(self, root: Path | None):
        self.root = root
        self.refresh()

    def refresh(self):
        self.files.clear()
        self.entries = []
        self.repo = gitops.toplevel(self.root) if self.root else None
        in_repo = self.repo is not None
        for w in (self.files, self.msg, self.commit_btn, self.refresh_btn):
            w.setVisible(in_repo)
        self.init_btn.setVisible(self.root is not None and not in_repo)
        if self.root is None:
            self.head.setText("Открой папку проекта")
            return
        if not in_repo:
            self.head.setText("Эта папка не под git")
            return
        self.entries = gitops.status(self.repo)
        self.head.setText(f"Ветка: {gitops.branch(self.repo)} · изменений: {len(self.entries)}")
        for e in self.entries:
            name = f"{e.old_path} → {e.path}" if e.old_path else e.path
            item = QListWidgetItem(f"{name}  ({e.label})" + ("  - служебный файл редактора" if e.private else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if e.private else Qt.CheckState.Checked)
            self.files.addItem(item)

    def checked(self) -> list[gitops.Entry]:
        return [e for i, e in enumerate(self.entries) if self.files.item(i).checkState() == Qt.CheckState.Checked]

    def show_diff(self, item):
        e = self.entries[self.files.row(item)]
        old = gitops.head_text(self.repo, e.old_path or e.path)
        try:
            new = (self.repo / e.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            new = ""
        self.diffRequested.emit(e.path, old, new)

    def commit(self):
        chosen = self.checked()
        paths = [p for e in chosen for p in (e.path, e.old_path) if p]
        ok, out = gitops.commit(self.repo, paths, self.msg.toPlainText())
        if ok:
            self.msg.clear()
            self.message.emit(f"закоммичено файлов: {len(chosen)}")
        else:
            self.message.emit("коммит не прошёл: " + out[-400:])
        self.refresh()

    def init_repo(self):
        ok, out = gitops.init(self.root)
        self.message.emit("создан git-репозиторий" if ok else "git init не прошёл: " + out[-300:])
        self.refresh()
