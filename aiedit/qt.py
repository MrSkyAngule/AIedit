"""Единая точка импорта Qt: PyQt6 или PySide6.

Выбор: переменная AIEDIT_QT=pyqt6|pyside6, иначе берётся первый установленный.
"""
import importlib
import os

_NAMES = {"pyqt6": "PyQt6", "pyside6": "PySide6"}


def _pick() -> str:
    want = os.environ.get("AIEDIT_QT", "").lower()
    if want:
        if want not in _NAMES:
            raise ImportError(f"AIEDIT_QT должен быть pyqt6 или pyside6, а не {want!r}")
        order = [_NAMES[want]]
    else:
        order = list(_NAMES.values())
    for pkg in order:
        try:
            importlib.import_module(f"{pkg}.QtWidgets")
            return pkg
        except ImportError:
            continue
    raise ImportError(f"не найден Qt-биндинг: {', '.join(order)}")


BINDING = _pick()
_core = importlib.import_module(f"{BINDING}.QtCore")
_gui = importlib.import_module(f"{BINDING}.QtGui")
_widgets = importlib.import_module(f"{BINDING}.QtWidgets")

Qt, QThread, QTimer = _core.Qt, _core.QThread, _core.QTimer
QDir, QPoint, QRect, QSize = _core.QDir, _core.QPoint, _core.QRect, _core.QSize
QSortFilterProxyModel = _core.QSortFilterProxyModel
Signal = getattr(_core, "pyqtSignal", None) or _core.Signal
QAction, QColor, QFont, QPainter = _gui.QAction, _gui.QColor, _gui.QFont, _gui.QPainter
QFileSystemModel = getattr(_gui, "QFileSystemModel", None) or _widgets.QFileSystemModel
QKeySequence, QTextCursor, QTextFormat = _gui.QKeySequence, _gui.QTextCursor, _gui.QTextFormat
QTextCharFormat, QTextDocument = _gui.QTextCharFormat, _gui.QTextDocument
QSyntaxHighlighter = _gui.QSyntaxHighlighter
QApplication, QDockWidget, QFileDialog = _widgets.QApplication, _widgets.QDockWidget, _widgets.QFileDialog
QMainWindow, QMenu, QMessageBox = _widgets.QMainWindow, _widgets.QMenu, _widgets.QMessageBox
QPlainTextEdit, QPushButton, QTextEdit = _widgets.QPlainTextEdit, _widgets.QPushButton, _widgets.QTextEdit
QVBoxLayout, QWidget = _widgets.QVBoxLayout, _widgets.QWidget
QCheckBox, QDialog, QHBoxLayout, QLabel = (_widgets.QCheckBox, _widgets.QDialog,
                                           _widgets.QHBoxLayout, _widgets.QLabel)
QLineEdit, QListWidget, QListWidgetItem = _widgets.QLineEdit, _widgets.QListWidget, _widgets.QListWidgetItem
QTreeView, QToolButton = _widgets.QTreeView, _widgets.QToolButton
QTabWidget, QInputDialog = _widgets.QTabWidget, _widgets.QInputDialog
QComboBox, QGridLayout = _widgets.QComboBox, _widgets.QGridLayout
QAbstractItemView, QTreeWidget, QTreeWidgetItem = (_widgets.QAbstractItemView, _widgets.QTreeWidget,
                                                   _widgets.QTreeWidgetItem)
