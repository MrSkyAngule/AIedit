"""Текстовое поле с колонкой слева: номера строк, цветные полосы меток, меню по клику."""
from .qt import (QColor, QMenu, QPainter, QPlainTextEdit, QPoint, QRect, QSize, QTextCharFormat,
                 QTextCursor, QTextEdit, QTextFormat, Qt, QWidget, Signal)

BAR_COLORS = {"hidden": "#e5534b", "readonly": "#d4a72c", "confirm": "#4c8dff",
              "explain": "#3fb950", "interface": "#a371f7"}
BAR_WIDTH = 6
FILL_ALPHA = 55
HIGHLIGHT_MODES = ("fill", "underline", "off")
MENU = (("hidden", "Скрыть от ИИ (hidden)"),
        ("readonly", "Только чтение (readonly)"),
        ("confirm", "С подтверждением (confirm)"),
        ("interface", "Только интерфейс (interface)"),
        ("explain", "Объяснить ИИ (explain)"))


class Gutter(QWidget):
    def __init__(self, edit: "CodeEdit"):
        super().__init__(edit)
        self.edit = edit
        self.anchor: int | None = None
        self.setMouseTracking(True)  # подсказка с заметкой при наведении

    def sizeHint(self):
        return QSize(self.edit.gutter_width(), 0)

    def paintEvent(self, event):
        self.edit.paint_gutter(event)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.anchor = self.edit.line_at(int(e.position().y()))
            self.edit.select_lines(self.anchor, self.anchor)

    def mouseMoveEvent(self, e):
        line = self.edit.line_at(int(e.position().y()))
        if self.anchor is not None:
            self.edit.select_lines(self.anchor, line)
        else:
            region = self.edit.regions[line] if line < len(self.edit.regions) else None
            self.setToolTip(f"{region.level}: {region.note}" if region is not None and region.note else "")

    def mouseReleaseEvent(self, e):
        if self.anchor is None:
            return
        end = self.edit.line_at(int(e.position().y()))
        a, b = sorted((self.anchor, end))
        self.anchor = None
        self.edit.select_lines(a, b)
        self.edit.show_mark_menu(self.mapToGlobal(QPoint(self.width(), int(e.position().y()))), a, b)


class CodeEdit(QPlainTextEdit):
    # (уровень или "unmark", первая строка, последняя строка)
    markRequested = Signal(str, int, int)

    def __init__(self):
        super().__init__()
        self.regions: list = []
        self.highlight = "fill"
        self.gutter = Gutter(self)
        self.blockCountChanged.connect(self.update_margin)
        self.updateRequest.connect(self.update_gutter)
        self.update_margin()

    def set_regions(self, regions: list):
        self.regions = regions
        self.gutter.update()
        self.paint_text_marks()

    def set_highlight(self, mode: str):
        self.highlight = mode
        self.paint_text_marks()

    def paint_text_marks(self):
        """Закраска или подчёркивание размеченных строк. Текст документа не меняется."""
        selections = []
        if self.highlight != "off":
            doc = self.document()
            for n, region in enumerate(self.regions):
                if region is None:
                    continue
                color = QColor(BAR_COLORS[region.level])
                fmt = QTextCharFormat()
                cur = QTextCursor(doc.findBlockByNumber(n))
                if self.highlight == "fill":
                    color.setAlpha(FILL_ALPHA)
                    fmt.setBackground(color)
                    fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
                else:
                    fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
                    fmt.setUnderlineColor(color)
                    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                     QTextCursor.MoveMode.KeepAnchor)
                sel = QTextEdit.ExtraSelection()
                sel.format = fmt
                sel.cursor = cur
                selections.append(sel)
        self.setExtraSelections(selections)

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return BAR_WIDTH + 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_margin(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_margin()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), self.palette().window())
        p.setPen(QColor(128, 128, 128))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                region = self.regions[n] if n < len(self.regions) else None
                if region is not None:
                    p.fillRect(0, top, BAR_WIDTH, bottom - top, QColor(BAR_COLORS[region.level]))
                    if region.note and region.start == n:  # белая точка: у зоны есть заметка
                        p.fillRect(1, top + 2, 4, 4, QColor("white"))
                p.drawText(0, top, self.gutter.width() - 6, self.fontMetrics().height(),
                           Qt.AlignmentFlag.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1
        p.end()

    def line_at(self, y: int) -> int:
        return self.cursorForPosition(QPoint(1, y)).blockNumber()

    def select_lines(self, a: int, b: int):
        a, b = sorted((a, b))
        doc = self.document()
        last = doc.findBlockByNumber(b)
        cur = QTextCursor(doc)
        cur.setPosition(doc.findBlockByNumber(a).position())
        cur.setPosition(last.position() + last.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cur)

    def show_mark_menu(self, pos, a: int, b: int):
        menu = QMenu(self)
        for level, title in MENU:
            menu.addAction(title).triggered.connect(
                lambda _=False, lv=level: self.markRequested.emit(lv, a, b))
        menu.addSeparator()
        note = menu.addAction("Заметка к зоне...")
        note.setEnabled(any(r is not None and r.level in ("interface", "hidden")
                            for r in self.regions[a:b + 1]))
        note.triggered.connect(lambda _=False: self.markRequested.emit("note", a, b))
        remove = menu.addAction("Снять метку")
        remove.setEnabled(any(self.regions[i] is not None for i in range(a, min(b + 1, len(self.regions)))))
        remove.triggered.connect(lambda _=False: self.markRequested.emit("unmark", a, b))
        menu.exec(pos)
