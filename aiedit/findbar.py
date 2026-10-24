"""Панель поиска и замены под вкладками. Работает с редактором текущей вкладки."""
from .qt import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextCursor, QTextDocument,
                 Qt, QVBoxLayout, QWidget)


class FindBar(QWidget):
    def __init__(self, get_editor):
        super().__init__()
        self.get_editor = get_editor
        self.find = QLineEdit()
        self.find.setPlaceholderText("Найти")
        self.replace = QLineEdit()
        self.replace.setPlaceholderText("Заменить на")
        self.case = QCheckBox("Регистр")
        self.info = QLabel("")
        self.find.returnPressed.connect(self.next)
        self.find.textChanged.connect(self.update_info)
        self.case.toggled.connect(lambda _: self.update_info())
        self.replace.returnPressed.connect(self.replace_one)

        def button(title, slot):
            b = QPushButton(title)
            b.clicked.connect(lambda _=False: slot())
            return b
        top, bottom = QHBoxLayout(), QHBoxLayout()
        for w in (self.find, button("Назад", self.prev), button("Далее", self.next), self.case, self.info):
            top.addWidget(w)
        top.setStretch(0, 1)
        for w in (self.replace, button("Заменить", self.replace_one), button("Заменить всё", self.replace_all),
                  button("Закрыть", self.close_bar)):
            bottom.addWidget(w)
        bottom.setStretch(0, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.addLayout(top)
        lay.addLayout(bottom)
        self.hide()

    # --- показ ---
    def open(self, replace: bool = False):
        self.show()
        ed = self.get_editor()
        sel = ed.textCursor().selectedText()
        if sel and " " not in sel:
            self.find.setText(sel)
        (self.replace if replace else self.find).setFocus()
        self.find.selectAll()
        self.update_info()

    def close_bar(self):
        self.hide()
        self.get_editor().setFocus()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close_bar()
        else:
            super().keyPressEvent(e)

    # --- поиск ---
    def flags(self, backward: bool = False):
        f = QTextDocument.FindFlag(0)
        if self.case.isChecked():
            f |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            f |= QTextDocument.FindFlag.FindBackward
        return f

    def count(self) -> int:
        text, ed = self.find.text(), self.get_editor()
        if not text:
            return 0
        body = ed.toPlainText()
        return body.count(text) if self.case.isChecked() else body.lower().count(text.lower())

    def update_info(self):
        n = self.count()
        self.info.setText("" if not self.find.text() else (f"найдено: {n}" if n else "нет совпадений"))

    def _go(self, backward: bool) -> bool:
        text, ed = self.find.text(), self.get_editor()
        if not text:
            return False
        if ed.find(text, self.flags(backward)):
            return True
        cur = ed.textCursor()  # дошли до края - ищем с другого конца
        cur.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
        ed.setTextCursor(cur)
        return ed.find(text, self.flags(backward))

    def next(self) -> bool:
        return self._go(False)

    def prev(self) -> bool:
        return self._go(True)

    # --- замена ---
    def _selection_matches(self) -> bool:
        sel, text = self.get_editor().textCursor().selectedText(), self.find.text()
        return bool(text) and (sel == text if self.case.isChecked() else sel.lower() == text.lower())

    def replace_one(self) -> bool:
        ed = self.get_editor()
        if not self._selection_matches() and not self.next():
            return False
        ed.textCursor().insertText(self.replace.text())
        self.update_info()
        self.next()
        return True

    def replace_all(self) -> int:
        text, ed = self.find.text(), self.get_editor()
        if not text:
            return 0
        done = 0
        cur = ed.textCursor()
        cur.beginEditBlock()
        probe = QTextCursor(ed.document())
        while True:
            probe = ed.document().find(text, probe, self.flags())
            if probe.isNull():
                break
            probe.insertText(self.replace.text())
            done += 1
        cur.endEditBlock()
        self.update_info()
        self.info.setText(f"заменено: {done}")
        return done
