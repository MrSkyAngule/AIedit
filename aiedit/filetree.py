"""Дерево файлов выбранной папки. Служебные файлы зон (.aimarks.json) в нём скрыты."""
from pathlib import Path

from . import fileops
from .policy import Rules, is_private_name
from .qt import (QColor, QDir, QFileSystemModel, QInputDialog, QLabel, QMenu, QMessageBox,
                 QSortFilterProxyModel, Qt, QTreeView, QVBoxLayout, QWidget, Signal)

DENIED_COLOR = "#e5534b"


class TreeProxy(QSortFilterProxyModel):
    """Прячет служебные файлы, красит запрещённое для ИИ."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules: Rules | None = None

    def filterAcceptsRow(self, row, parent):
        name = self.sourceModel().fileName(self.sourceModel().index(row, 0, parent))
        return not is_private_name(name)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        value = super().data(index, role)
        if self.rules is None or index.column() != 0:
            return value
        path = self.sourceModel().filePath(self.mapToSource(index))
        if not self.rules.covers(path):
            return value
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(DENIED_COLOR)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "закрыто от ИИ"
        if role == Qt.ItemDataRole.DisplayRole and self.rules.is_explicit(path):
            return f"{value}  [закрыто от ИИ]"
        return value


class FileTree(QWidget):
    fileActivated = Signal(str)
    rulesChanged = Signal()
    pathRenamed = Signal(str, str)
    pathDeleted = Signal(str)
    contextToggled = Signal(str)
    rootChanged = Signal(str)
    in_context = staticmethod(lambda path: False)

    def __init__(self):
        super().__init__()
        self.root: Path | None = None
        self.rules: Rules | None = None
        self.title = QLabel("папка не выбрана\n(Файл - Открыть папку)")

        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.proxy = TreeProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTreeView()
        self.view.setModel(self.proxy)
        self.view.setHeaderHidden(True)
        self.view.activated.connect(self.on_activated)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.show_menu)
        self.view.hide()

        lay = QVBoxLayout(self)
        for w in (self.title, self.view):
            lay.addWidget(w)

    def set_root(self, folder: Path):
        self.root = folder
        self.rules = self.proxy.rules = Rules(folder)
        self.title.setText(folder.name or str(folder))
        self.title.setToolTip(str(folder))
        self.model.setRootPath(str(folder))
        self.view.setRootIndex(self.proxy.mapFromSource(self.model.index(str(folder))))
        for col in (1, 2, 3):
            self.view.setColumnHidden(col, True)
        self.view.show()
        self.rootChanged.emit(str(folder))

    def path_at(self, index) -> Path:
        return Path(self.model.filePath(self.proxy.mapToSource(index)))

    def show_menu(self, pos):
        if self.rules is None or self.root is None:
            return
        index = self.view.indexAt(pos)
        path = self.path_at(index) if index.isValid() else None
        folder = self.root if path is None else (path if path.is_dir() else path.parent)
        menu = QMenu(self)
        menu.addAction("Новый файл...").triggered.connect(lambda: self.new_file(folder))
        menu.addAction("Новая папка...").triggered.connect(lambda: self.new_folder(folder))
        if path is not None:
            menu.addSeparator()
            menu.addAction("Переименовать...").triggered.connect(lambda: self.rename(path))
            menu.addAction("Удалить...").triggered.connect(lambda: self.delete(path))
            menu.addSeparator()
            if path.is_file() and not self.rules.covers(path):
                title = "Убрать из контекста ИИ" if self.in_context(path) else "Добавить в контекст ИИ"
                menu.addAction(title).triggered.connect(lambda: self.contextToggled.emit(str(path)))
            if self.rules.is_explicit(path):
                menu.addAction("Разрешить ИИ").triggered.connect(lambda: self.set_denied(path, False))
            elif self.rules.covers(path):
                menu.addAction("Закрыто через родительскую папку").setEnabled(False)
            else:
                menu.addAction("Запретить ИИ").triggered.connect(lambda: self.set_denied(path, True))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # --- операции с файлами ---
    def error(self, text: str):
        QMessageBox.warning(self, "Не получилось", text)

    def ask_name(self, title: str, label: str, text: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, label, text=text)
        return name if ok and name.strip() else None

    def new_file(self, folder: Path):
        if (name := self.ask_name("Новый файл", "Имя файла:")) is None:
            return
        try:
            fileops.create_file(folder, name)
        except (fileops.FileOpError, OSError) as e:
            self.error(str(e))

    def new_folder(self, folder: Path):
        if (name := self.ask_name("Новая папка", "Имя папки:")) is None:
            return
        try:
            fileops.create_folder(folder, name)
        except (fileops.FileOpError, OSError) as e:
            self.error(str(e))

    def rename(self, path: Path):
        if (name := self.ask_name("Переименовать", "Новое имя:", path.name)) is None:
            return
        try:
            target = fileops.rename(path, name, self.rules)
        except (fileops.FileOpError, OSError) as e:
            self.error(str(e))
            return
        self.rules.load()
        self.view.viewport().update()
        self.pathRenamed.emit(str(path), str(target))
        self.rulesChanged.emit()

    def confirm_delete(self, path: Path) -> bool:
        what = f"папку «{path.name}» и всё внутри ({fileops.count_inside(path)} файлов)" if path.is_dir() \
            else f"файл «{path.name}»"
        answer = QMessageBox.question(self, "Удалить", f"Удалить {what}? Это нельзя отменить.",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def delete(self, path: Path):
        if not self.confirm_delete(path):
            return
        try:
            fileops.delete(path)
        except OSError as e:
            self.error(str(e))
            return
        self.pathDeleted.emit(str(path))

    def set_denied(self, path: Path, flag: bool):
        if self.rules.set_denied(path, flag):
            self.view.viewport().update()
            self.rulesChanged.emit()

    def on_activated(self, index):
        src = self.proxy.mapToSource(index)
        if not self.model.isDir(src):
            self.fileActivated.emit(self.model.filePath(src))
