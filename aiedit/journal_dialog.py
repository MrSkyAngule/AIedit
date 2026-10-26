"""Вкладка «Журнал правок ИИ»: файлы, где ИИ что-то написал, от самых больших правок к самым маленьким.
Внутри файла видны его места, тоже по убыванию размера. Клик открывает файл в своей вкладке на нужной строке."""
import html

from . import journal
from .journal import Hunk
from .qt import (QAbstractItemView, QColor, QFont, QLabel, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                 QWidget)

COLUMNS = ["Файл / место", "Размер", "Строки", "Что написано", "Кто", "Замечания"]
KINDS = {"new": "новый файл", "added": "добавлено", "removed": "удалено", "changed": "изменено"}
STYLE = {"+": "background:#d4f5d4;", "-": "background:#f9d6d6;", "@": "background:#e3e8f0;"}
READ_COLOR = QColor(130, 130, 130)


def diff_to_html(diff: str) -> str:
    return "".join(f'<div style="{STYLE.get(l[:1], "")} color:#111; white-space:pre;">{html.escape(l) or "&nbsp;"}</div>'
                   for l in diff.split("\n"))


def _span(h: Hunk) -> str:
    return f"{h.start}" if h.start == h.end else f"{h.start}-{h.end}"


def ai_author(author: str) -> str:
    """В журнале видно, что место написал ИИ, а не человек."""
    return f"ИИ · {author}" if author else ""


class JournalView(QWidget):
    def __init__(self, parent, hunks: list[Hunk], on_goto):
        super().__init__(parent)
        self.hunks = journal.sorted_hunks(hunks)
        self.on_goto = on_goto  # (файл, строка)
        self.read: set[int] = set()
        by_file: dict[str, list[int]] = {}
        for i, h in enumerate(self.hunks):
            by_file.setdefault(h.file, []).append(i)  # внутри файла порядок уже «большие первыми»
        self.files = sorted(by_file, key=lambda f: (-sum(self.hunks[i].size for i in by_file[f]), f))
        self.file_items: dict[str, QTreeWidgetItem] = {}
        self.hunk_items: list[QTreeWidgetItem | None] = [None] * len(self.hunks)

        self.summary = QLabel()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setRootIsDecorated(True)
        biggest = max((h.size for h in self.hunks), default=1)
        for name in self.files:
            idx = by_file[name]
            added, removed = sum(self.hunks[i].added for i in idx), sum(self.hunks[i].removed for i in idx)
            remarks = sum(len(self.hunks[i].remarks) for i in idx)
            authors = ", ".join(dict.fromkeys(ai_author(self.hunks[i].author) for i in idx if self.hunks[i].author))
            top = QTreeWidgetItem([name, f"+{added} -{removed}", f"{len(idx)} мест", "", authors, str(remarks) if remarks else ""])
            top.setFont(0, self._bold())
            top.setToolTip(0, "Клик откроет файл в отдельной вкладке на самой большой правке")
            self.tree.addTopLevelItem(top)
            self.file_items[name] = top
            for i in idx:
                h = self.hunks[i]
                child = QTreeWidgetItem([f"строки {_span(h)}" if h.start != h.end else f"строка {h.start}",
                                         f"+{h.added} -{h.removed}", KINDS[h.kind], h.preview, ai_author(h.author),
                                         str(len(h.remarks)) if h.remarks else ""])
                child.setBackground(1, QColor(255, 170, 0, 30 + int(130 * h.size / biggest)))  # чем больше, тем ярче
                child.setToolTip(1, f"{KINDS[h.kind]}, строк затронуто: {h.size}")
                if h.remarks:
                    child.setForeground(5, QColor("#b42318"))
                    child.setToolTip(5, "\n".join(h.remarks))
                top.addChild(child)
                self.hunk_items[i] = child
        self.tree.expandAll()
        for col in range(len(COLUMNS)):
            self.tree.resizeColumnToContents(col)
        self.tree.itemClicked.connect(lambda item, _col: self.open_item(item))
        self.tree.itemActivated.connect(lambda item, _col: self.open_item(item))
        self.tree.currentItemChanged.connect(lambda cur, _prev: self.show_detail(cur))

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("monospace", 10))
        self.detail.setMaximumHeight(210)
        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.detail)
        self.update_summary()
        if self.files:
            self.tree.setCurrentItem(self.file_items[self.files[0]])

    @staticmethod
    def _bold() -> QFont:
        f = QFont()
        f.setBold(True)
        return f

    # --- состояние ---
    def hunk_index(self, item: QTreeWidgetItem) -> int | None:
        return next((i for i, it in enumerate(self.hunk_items) if it is item), None)

    def file_of(self, item: QTreeWidgetItem) -> str | None:
        return next((f for f, it in self.file_items.items() if it is item), None)

    def update_summary(self):
        self.summary.setText(
            (journal.total_text(self.hunks) if self.hunks else "правок нет")
            + f". Читать сверху вниз: сначала самые большие. Клик по файлу открывает его в своей вкладке. "
            f"Просмотрено мест: {len(self.read)} из {len(self.hunks)}.")

    def mark_read(self, i: int):
        if i in self.read:
            return
        self.read.add(i)
        for col in range(len(COLUMNS)):
            self.hunk_items[i].setForeground(col, READ_COLOR)
        name = self.hunks[i].file
        if all(j in self.read for j, h in enumerate(self.hunks) if h.file == name):
            for col in range(len(COLUMNS)):
                self.file_items[name].setForeground(col, READ_COLOR)
        self.update_summary()

    # --- действия ---
    def open_item(self, item: QTreeWidgetItem | None):
        """Файл открывается в своей вкладке: на самом большом месте, а место - на своей строке."""
        if item is None:
            return
        i = self.hunk_index(item)
        if i is None:
            name = self.file_of(item)
            if name is None:
                return
            i = next(j for j, h in enumerate(self.hunks) if h.file == name)  # первое = самое большое
        h = self.hunks[i]
        self.mark_read(i)
        self.on_goto(h.file, h.start)

    def show_detail(self, item: QTreeWidgetItem | None):
        if item is None:
            return
        i = self.hunk_index(item)
        if i is None:
            name = self.file_of(item)
            places = [h for h in self.hunks if h.file == name]
            lines = "".join(f"<li>строки {_span(h)}: +{h.added} -{h.removed}, {html.escape(h.preview)}</li>" for h in places)
            self.detail.setHtml(f"<p><b>{html.escape(name or '')}</b>: мест {len(places)}</p><ul>{lines}</ul>")
            return
        h = self.hunks[i]
        head = (f"<p><b>{html.escape(h.file)}</b>, строки {h.start}-{h.end}, {KINDS[h.kind]}"
                + (f", {html.escape(ai_author(h.author))}" if h.author else "") + "</p>")
        notes = "".join(f'<p style="color:#b42318;">{html.escape(r)}</p>' for r in h.remarks)
        self.detail.setHtml(head + notes + diff_to_html(h.diff))
