"""Журнал правок ИИ: список мест, где он что-то написал, от самых больших к самым маленьким.

Правка (hunk) - подряд идущий блок изменённых строк. Близкие блоки (через пару строк) склеиваются в один,
чтобы одна логическая правка не рассыпалась на кусочки. Размер - сколько строк добавлено и удалено."""
import difflib
from dataclasses import dataclass, field

MERGE_GAP = 2


@dataclass
class Hunk:
    file: str
    start: int           # первая изменённая строка нового текста (с 1); у чистого удаления - место удаления
    end: int
    added: int
    removed: int
    kind: str            # new | added | removed | changed
    preview: str
    author: str = ""
    remarks: list[str] = field(default_factory=list)  # замечания статанализа и безопасника к этому месту
    needs_confirm: bool = False
    diff: str = ""       # сама правка с двумя строками контекста (строки с +, - и пробелом)

    @property
    def size(self) -> int:
        return self.added + self.removed


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines  # последний пустой элемент - просто конец файла


def hunks_for(file: str, old: str | None, new: str, author: str = "", merge_gap: int = MERGE_GAP) -> list[Hunk]:
    a, b = _lines(old or ""), _lines(new)
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    groups: list[list[tuple]] = []
    for op in ops:
        if groups and op[3] - groups[-1][-1][4] <= merge_gap:  # j1 этого блока минус j2 прошлого
            groups[-1].append(op)
        else:
            groups.append([op])
    out = []
    for g in groups:
        added = sum(j2 - j1 for _, _, _, j1, j2 in g)
        removed = sum(i2 - i1 for _, i1, i2, _, _ in g)
        start = g[0][3] + 1 if added else min(g[0][3] + 1, max(len(b), 1))  # удаление: строка на его месте
        end = g[-1][4] if added else start
        kind = "new" if old is None else "removed" if not added else "added" if not removed else "changed"
        shown = next((b[j].strip() for _, _, _, j1, j2 in g for j in range(j1, j2) if b[j].strip()), "")
        i0, i1, j0, j1 = max(g[0][1] - 2, 0), g[-1][2] + 2, max(g[0][3] - 2, 0), g[-1][4] + 2
        body = list(difflib.unified_diff(a[i0:i1], b[j0:j1], "", "", n=2, lineterm=""))[2:]
        out.append(Hunk(file, start, max(end, start), added, removed, kind,
                        shown[:90] if shown else f"удалено строк: {removed}", author,
                        diff="\n".join(body[:200] + (["..."] if len(body) > 200 else []))))
    return out


def sorted_hunks(hunks: list[Hunk]) -> list[Hunk]:
    """Сначала самые большие. При равенстве - по файлу и строке, чтобы порядок не прыгал."""
    return sorted(hunks, key=lambda h: (-h.size, h.file, h.start))


def attach_remarks(hunks: list[Hunk], remarks: list[tuple[str, int | None, str]]) -> None:
    """remarks: (файл, строка, текст). Замечание садится на правку, накрывающую строку (с запасом в 1);
    без строки или мимо правок - на самую большую правку этого файла."""
    for file, line, text in remarks:
        mine = [h for h in hunks if h.file == file]
        if not mine:
            continue
        hit = next((h for h in mine if line is not None and h.start - 1 <= line <= h.end + 1), None)
        (hit or max(mine, key=lambda h: h.size)).remarks.append(text)


def total_text(hunks: list[Hunk]) -> str:
    files = {h.file for h in hunks}
    return (f"{len(hunks)} мест в {len(files)} файлах, +{sum(h.added for h in hunks)} "
            f"-{sum(h.removed for h in hunks)} строк")
