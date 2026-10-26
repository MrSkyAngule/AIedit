"""Закрытый код, который оказался вне своих зон: файл переименовали, скопировали или вставили кусок в другой файл.

Зоны лежат рядом с файлом, в `имя.aimarks.json`. Если переименовать файл мимо редактора (mv, git mv, файловый
менеджер), его файл зон останется со старым именем, и у нового файла зон не будет: весь код ушёл бы модели.
Поэтому в файле зон хранятся отпечатки закрытых кусков (marks.fingerprints: хэши строк, не сам код), а здесь
редактор ищет эти отпечатки во всех файлах проекта. Нашёлся закрытый кусок там, где его не закрывает своя зона, -
эти строки скрываются от модели, пока человек не решит иначе (вернуть зоны или разрешить оставить открытым).

Ищется только кусок целиком и строка в строку (отступы не важны). Если закрытый код в копии изменить,
отпечаток не совпадёт: это лучшее усилие, а не гарантия.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .guard import CONCEALED, Region, line_regions
from .marks import fingerprints, line_hash, text_hash

SIDECAR_SUFFIX = ".aimarks.json"
MAX_SIDECARS = 2000


@dataclass(frozen=True)
class Chunk:
    level: str
    hashes: tuple[str, ...]
    id: str


@dataclass(frozen=True)
class Match:
    start: int  # строки с 0, включительно
    end: int
    level: str
    id: str


def chunk_id(hashes) -> str:
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()[:16]


class Index:
    """Отпечатки закрытых кусков проекта, по первой строке куска."""

    def __init__(self, chunks=()):
        self.by_first: dict[str, list[Chunk]] = {}
        for c in chunks:
            if not c.hashes:
                continue
            bucket = self.by_first.setdefault(c.hashes[0], [])
            if all(o.id != c.id for o in bucket):
                bucket.append(c)

    def __bool__(self) -> bool:
        return bool(self.by_first)


def build(root: Path | str, skip=frozenset()) -> Index:
    """Читает отпечатки из всех файлов зон под root. Испорченные файлы зон пропускает."""
    chunks: list[Chunk] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if not name.endswith(SIDECAR_SUFFIX):
                continue
            seen += 1
            if seen > MAX_SIDECARS:
                return Index(chunks)
            try:
                side = Path(dirpath) / name
                data = json.loads(side.read_text(encoding="utf-8"))
                if "chunks" not in data:
                    data["chunks"] = _old_sidecar(side, data)
                for level, hashes in data["chunks"]:
                    if level in CONCEALED and hashes:
                        hashes = tuple(str(h) for h in hashes)
                        chunks.append(Chunk(level, hashes, chunk_id(hashes)))
            except (OSError, ValueError, TypeError, AttributeError, IndexError):
                continue
    return Index(chunks)


def _old_sidecar(side: Path, data: dict) -> list:
    """Файл зон, записанный до отпечатков: считаем их по самому файлу, если он на месте и не менялся."""
    base = side.with_name(side.name[:-len(SIDECAR_SUFFIX)])
    try:
        text = base.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if data.get("sha256") != text_hash(text):
        return []
    return fingerprints(text, [Region(str(r[0]), int(r[1]), int(r[2])) for r in data.get("regions", [])])


def find(text: str, regions: list[Region], index: Index, allowed=()) -> list[Match]:
    """Закрытые куски из index в этом тексте, не накрытые его собственными закрытыми зонами."""
    if not index:
        return []
    lines = text.split("\n")
    hashes = [line_hash(l) for l in lines]
    own = line_regions(len(lines), regions)
    allowed = set(allowed)
    out: list[Match] = []
    for i, h in enumerate(hashes):
        for c in index.by_first.get(h, ()):
            n = len(c.hashes)
            if c.id in allowed or tuple(hashes[i:i + n]) != c.hashes:
                continue
            if all(own[j] is not None and own[j].level in CONCEALED for j in range(i, i + n)):
                continue  # это и есть свой закрытый код на своём месте
            out.append(Match(i, i + n - 1, c.level, c.id))
    return out


def regions(matches: list[Match]) -> list[Region]:
    """Найденное скрываем целиком (hidden): у копии нет заметок и сигнатур, которые человек разрешил показывать."""
    return [Region("hidden", m.start, m.end) for m in matches]
