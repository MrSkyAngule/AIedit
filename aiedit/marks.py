"""Набор зон файла: сдвиг при правках текста и хранение в файле рядом с кодом."""
import hashlib
import json
from pathlib import Path

from dataclasses import replace

from .guard import CONCEALED, Region, remap

HUMAN = "human"  # отметка только для человека: модель её не получает, живёт отдельно от зон (Marks.human)
MIN_CHUNK_CHARS = 12  # закрытые куски короче этого по отпечатку не ищем: такие строки часто совпадают с обычным кодом


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".aimarks.json")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def line_hash(line: str) -> str:
    """Отпечаток строки без отступов: по нему закрытый код узнаётся в другом файле (copies.py)."""
    return hashlib.sha256(line.strip().encode()).hexdigest()[:16]


def fingerprints(text: str, regions: list[Region]) -> list[list]:
    """Отпечатки закрытых кусков: [уровень, [отпечатки строк]]. Сам код в файл зон не попадает."""
    lines = text.split("\n")
    out = []
    for r in regions:
        chunk = lines[max(r.start, 0):r.end + 1]
        if r.level in CONCEALED and sum(len(l.strip()) for l in chunk) >= MIN_CHUNK_CHARS:
            out.append([r.level, [line_hash(l) for l in chunk]])
    return out


MAX_SNAPSHOTS = 300


def _cut(regions: list[Region], start: int, end: int) -> tuple[list[Region], bool]:
    """Убирает строки start..end из диапазонов; шире диапазона обрезаются."""
    out, changed = [], False
    for r in regions:
        if r.end < start or r.start > end:
            out.append(r)
            continue
        changed = True
        if r.start < start:
            out.append(replace(r, end=start - 1))
        if r.end > end:
            out.append(replace(r, start=end + 1))
    return out, changed


def _shifted(regions: list[Region], line: int, delta: int) -> list[Region]:
    """В тексте с позиции строки `line` добавилось (delta>0) или убралось (delta<0) строк."""
    out = []
    for r in regions:
        s, e = r.start, r.end
        if delta > 0:
            if line < s:
                s, e = s + delta, e + delta
            elif line <= e:
                e += delta
        else:
            d = -delta
            if s > line and e <= line + d:
                continue  # зона целиком удалена

            def m(x):
                return x if x <= line else (line if x <= line + d else x - d)
            s, e = m(s), m(e)
        out.append(replace(r, start=s, end=e))
    return out


class Marks:
    def __init__(self):
        self.items: list[Region] = []
        # Отметки для себя. Нарочно НЕ в items: всё, что уходит модели, читает только items,
        # так что до неё отметки не доходят, даже если где-то забыли про них подумать.
        self.human: list[Region] = []
        # закрытый код из других файлов, который человек сам разрешил здесь оставить открытым (copies.chunk_id)
        self.allowed: list[str] = []
        self.outdated = False  # файл зон записан до отпечатков (marks.fingerprints): его стоит переписать
        self._snaps: dict[int, tuple[list[Region], list[Region]]] = {}  # состояние текста -> зоны и отметки при нём

    def state(self) -> tuple[tuple[Region, ...], tuple[Region, ...], tuple[str, ...]]:
        return tuple(self.items), tuple(self.human), tuple(self.allowed)

    def remember(self, text: str):
        """Запоминает зоны для текущего вида текста: так отмена возвращает их точно."""
        self._snaps.pop(hash(text), None)
        self._snaps[hash(text)] = (list(self.items), list(self.human))
        while len(self._snaps) > MAX_SNAPSHOTS:
            del self._snaps[next(iter(self._snaps))]

    def restore(self, text: str) -> bool:
        """Возвращает зоны, какими они были при таком тексте. False, если такого текста не помним."""
        snap = self._snaps.get(hash(text))
        if snap is None:
            return False
        self.items, self.human = list(snap[0]), list(snap[1])
        return True

    def clear(self, start: int, end: int) -> bool:
        """Снимает все метки со строк start..end. Зоны шире диапазона обрезаются."""
        self.items, changed = _cut(self.items, start, end)
        return changed

    def add_human(self, start: int, end: int, note: str = ""):
        """Отметка для себя на строках start..end, заменяет прежние там же."""
        self.clear_human(start, end)
        self.human.append(Region(HUMAN, start, end, note.strip()))

    def clear_human(self, start: int, end: int) -> bool:
        self.human, changed = _cut(self.human, start, end)
        return changed

    def human_at(self, line: int) -> Region | None:
        """Самая узкая отметка для себя на строке."""
        hits = [r for r in self.human if r.start <= line <= r.end]
        return min(hits, key=lambda r: r.end - r.start) if hits else None

    def remap_human(self, old: str, new: str):
        """Правка текста не человеком (ИИ, откат): отметки едут вместе со строками, к которым были привязаны."""
        self.human = remap(self.human, old, new)

    def add(self, level: str, start: int, end: int, note: str = "", note_en: str = ""):
        """Ставит метку на строки start..end, заменяя то, что там было."""
        self.clear(start, end)
        self.items.append(Region(level, start, end, note, note_en))

    def zone_at(self, line: int, levels: tuple[str, ...] | None = None) -> Region | None:
        """Самая узкая зона, накрывающая строку (можно ограничить уровнями)."""
        hits = [r for r in self.items if r.start <= line <= r.end and (levels is None or r.level in levels)]
        return min(hits, key=lambda r: r.end - r.start) if hits else None

    def set_note(self, line: int, note: str, note_en: str = "") -> bool:
        """Записывает заметку в зону под строкой. False, если там нет закрытой зоны."""
        zone = self.zone_at(line, ("interface", "hidden"))
        if zone is None:
            return False
        self.items[self.items.index(zone)] = replace(zone, note=note.strip(), note_en=note_en.strip())
        return True

    def on_change(self, line: int, delta: int):
        """В тексте с позиции строки `line` добавилось (delta>0) или убралось (delta<0) строк."""
        if delta == 0:
            return
        self.items = _shifted(self.items, line, delta)
        self.human = _shifted(self.human, line, delta)

    def save(self, path: Path, text: str):
        """Пишет файл зон. Если на диске уже то же самое, ничего не трогает (не будить git и наблюдателей)."""
        side = sidecar(path)
        if not self.items and not self.human and not self.allowed:
            side.unlink(missing_ok=True)
            return
        data = {"sha256": text_hash(text),
                "regions": [[r.level, r.start, r.end, *([r.note, r.note_en] if r.note or r.note_en else [])]
                            for r in self.items]}
        chunks = fingerprints(text, self.items)
        if chunks:
            data["chunks"] = chunks
        if self.allowed:
            data["allowed"] = sorted(set(self.allowed))
        if self.human:
            data["human"] = [[r.start, r.end, r.note] for r in self.human]
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        try:
            if side.read_text(encoding="utf-8") == payload:
                return
        except OSError:
            pass
        side.write_text(payload, encoding="utf-8")

    def load(self, path: Path, text: str) -> bool:
        """Читает зоны. Возвращает False, если файл менялся вне редактора."""
        self.items, self.human, self.allowed = [], [], []
        self.outdated = False
        side = sidecar(path)
        if not side.exists():
            return True
        data = json.loads(side.read_text(encoding="utf-8"))
        self.allowed = [str(c) for c in data.get("allowed", []) if isinstance(c, str)]
        self.items = [Region(row[0], row[1], row[2], *(str(x) for x in row[3:5])) for row in data["regions"]]
        self.human = [Region(HUMAN, int(row[0]), int(row[1]), str(row[2]) if len(row) > 2 else "")
                      for row in data.get("human", []) if isinstance(row, list) and len(row) >= 2]
        self.outdated = "chunks" not in data and bool(fingerprints(text, self.items))
        return data.get("sha256") == text_hash(text)
