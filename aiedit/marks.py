"""Набор зон файла: сдвиг при правках текста и хранение в файле рядом с кодом."""
import hashlib
import json
from pathlib import Path

from dataclasses import replace

from .guard import Region


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".aimarks.json")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


MAX_SNAPSHOTS = 300


class Marks:
    def __init__(self):
        self.items: list[Region] = []
        self._snaps: dict[int, list[Region]] = {}  # состояние текста -> зоны при нём

    def remember(self, text: str):
        """Запоминает зоны для текущего вида текста: так отмена возвращает их точно."""
        self._snaps.pop(hash(text), None)
        self._snaps[hash(text)] = list(self.items)
        while len(self._snaps) > MAX_SNAPSHOTS:
            del self._snaps[next(iter(self._snaps))]

    def restore(self, text: str) -> bool:
        """Возвращает зоны, какими они были при таком тексте. False, если такого текста не помним."""
        snap = self._snaps.get(hash(text))
        if snap is None:
            return False
        self.items = list(snap)
        return True

    def clear(self, start: int, end: int) -> bool:
        """Снимает все метки со строк start..end. Зоны шире диапазона обрезаются."""
        out, changed = [], False
        for r in self.items:
            if r.end < start or r.start > end:
                out.append(r)
                continue
            changed = True
            if r.start < start:
                out.append(replace(r, end=start - 1))
            if r.end > end:
                out.append(replace(r, start=end + 1))
        self.items = out
        return changed

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
        out = []
        for r in self.items:
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
        self.items = out

    def save(self, path: Path, text: str):
        """Пишет файл зон. Если на диске уже то же самое, ничего не трогает (не будить git и наблюдателей)."""
        side = sidecar(path)
        if not self.items:
            side.unlink(missing_ok=True)
            return
        data = {"sha256": text_hash(text),
                "regions": [[r.level, r.start, r.end, *([r.note, r.note_en] if r.note or r.note_en else [])]
                            for r in self.items]}
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        try:
            if side.read_text(encoding="utf-8") == payload:
                return
        except OSError:
            pass
        side.write_text(payload, encoding="utf-8")

    def load(self, path: Path, text: str) -> bool:
        """Читает зоны. Возвращает False, если файл менялся вне редактора."""
        self.items = []
        side = sidecar(path)
        if not side.exists():
            return True
        data = json.loads(side.read_text(encoding="utf-8"))
        self.items = [Region(row[0], row[1], row[2], *(str(x) for x in row[3:5])) for row in data["regions"]]
        return data.get("sha256") == text_hash(text)
