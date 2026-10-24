"""Точки отката: как выглядели файлы до того, как применили правки ИИ.

Хранятся в профиле пользователя, а не в проекте: в репозиторий и в git ничего не попадает, а модель
их не видит. На каждый применённый набор правок - одна точка: для каждого файла текст до и после
и файл зон до. Откат возвращает «до», если файл с тех пор не трогали; иначе спрашивает человека.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .settings import config_file

MAX_POINTS = 30
MAX_FILE = 2_000_000


@dataclass
class FileBefore:
    path: str             # абсолютный путь
    before: str | None    # None - файла не было (ИИ его создал)
    after: str | None     # None - ИИ его удалил
    marks_before: str | None = None  # файл зон до правки, если был


@dataclass
class Point:
    id: str
    time: str
    title: str
    files: list[FileBefore] = field(default_factory=list)

    def label(self) -> str:
        names = ", ".join(Path(f.path).name for f in self.files[:4]) + (" ..." if len(self.files) > 4 else "")
        return f"{self.time} · {self.title[:60]} · файлов: {len(self.files)} ({names})"


def folder(root: Path | str) -> Path:
    key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]
    return config_file().parent / "checkpoints" / key


def save(root: Path | str, title: str, files: list[FileBefore]) -> Point | None:
    """Записывает точку. None, если записать не вышло (это не должно мешать применению правок)."""
    files = [f for f in files if len(f.before or "") <= MAX_FILE and len(f.after or "") <= MAX_FILE]
    if not files:
        return None
    now = datetime.now()
    point = Point(now.strftime("%Y%m%d-%H%M%S-%f"), now.strftime("%Y-%m-%d %H:%M:%S"), " ".join(title.split()), files)
    d = folder(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d.parent, 0o700)
        (d / f"{point.id}.json").write_text(json.dumps({
            "time": point.time, "title": point.title, "root": os.path.realpath(root),
            "files": [vars(f) for f in files]}, ensure_ascii=False), encoding="utf-8")
        for old in sorted(d.glob("*.json"))[:-MAX_POINTS]:
            old.unlink(missing_ok=True)
    except OSError:
        return None
    return point


def load(root: Path | str) -> list[Point]:
    """Точки проекта, новые первыми."""
    out = []
    for p in sorted(folder(root).glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append(Point(p.stem, data["time"], data["title"], [FileBefore(**f) for f in data["files"]]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def forget(root: Path | str, point: Point):
    (folder(root) / f"{point.id}.json").unlink(missing_ok=True)
