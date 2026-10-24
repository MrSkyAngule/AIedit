"""Своё окно выбора папки: путь сверху, список подпапок, быстрые переходы."""
from pathlib import Path

from .qt import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                 QListWidgetItem, QPushButton, QVBoxLayout)

UP = ".."


class FolderDialog(QDialog):
    def __init__(self, parent=None, start: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Открыть папку")
        self.resize(520, 480)
        self.current = Path.home()
        self.chosen: Path | None = None

        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.go(Path(self.path_edit.text()).expanduser()))
        self.listing = QListWidget()
        self.listing.itemActivated.connect(self.enter)
        self.listing.itemDoubleClicked.connect(self.enter)
        self.hidden = QCheckBox("скрытые папки")
        self.hidden.toggled.connect(lambda _: self.refresh())
        self.note = QLabel()

        quick = QHBoxLayout()
        for title, target in (("Домой", Path.home()), ("/", Path("/"))):
            b = QPushButton(title)
            b.clicked.connect(lambda _=False, t=target: self.go(t))
            quick.addWidget(b)
        quick.addStretch()
        quick.addWidget(self.hidden)

        ok, cancel = QPushButton("Выбрать эту папку"), QPushButton("Отмена")
        ok.setDefault(True)
        ok.clicked.connect(self.accept_current)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(ok)

        lay = QVBoxLayout(self)
        lay.addWidget(self.path_edit)
        lay.addLayout(quick)
        lay.addWidget(self.listing)
        lay.addWidget(self.note)
        lay.addLayout(buttons)
        self.go(start if start and start.is_dir() else Path.home())

    def go(self, target: Path):
        try:
            target = target.resolve()
            self.dirs = sorted(
                (p.name for p in target.iterdir() if p.is_dir()),
                key=str.lower)
        except OSError as e:
            self.note.setText(f"нельзя открыть: {e.strerror or e}")
            return
        self.current = target
        self.refresh()

    def refresh(self):
        self.note.setText("")
        self.path_edit.setText(str(self.current))
        self.listing.clear()
        if self.current.parent != self.current:
            self.listing.addItem(QListWidgetItem(UP))
        for name in self.dirs:
            if self.hidden.isChecked() or not name.startswith("."):
                self.listing.addItem(QListWidgetItem(name))

    def enter(self, item):
        name = item.text()
        self.go(self.current.parent if name == UP else self.current / name)

    def accept_current(self):
        self.chosen = self.current
        self.accept()

    @staticmethod
    def pick(parent=None, start: Path | None = None) -> Path | None:
        dlg = FolderDialog(parent, start)
        return dlg.chosen if dlg.exec() else None
