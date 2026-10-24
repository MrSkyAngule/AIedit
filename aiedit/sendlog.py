"""Журнал отправок: что и какой модели ушло. Содержимое файлов в журнал не пишется."""
import json
import os
from datetime import datetime
from pathlib import Path

from .policy import RULES_NAME

LOG_NAME = ".aiedit-sends.jsonl"


def project_dir(source: Path | str | None) -> Path | None:
    """Папка проекта: ближайшая сверху с файлом правил или с .git, иначе папка самого файла."""
    if source is None:
        return None
    p = Path(os.path.realpath(source))
    for d in p.parents:
        if (d / RULES_NAME).is_file() or (d / ".git").exists():
            return d
    return p.parent


def log_path(source: Path | str | None, root: Path | str | None = None) -> Path | None:
    d = Path(os.path.realpath(root)) if root else project_dir(source)
    return d / LOG_NAME if d else None


def record(source: Path | str | None, entry: dict, root: Path | str | None = None) -> bool:
    """Дописывает запись. Ошибка записи не должна ломать работу редактора.
    root - открытая папка проекта (если известна), иначе она определяется по файлу."""
    path = log_path(source, root)
    if path is None:
        return False
    entry = {"time": datetime.now().isoformat(timespec="seconds"), **entry}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def read(source_or_dir: Path | str, limit: int = 200) -> list[dict]:
    """Последние записи журнала проекта (принимает файл проекта или папку)."""
    p = Path(source_or_dir)
    path = p / LOG_NAME if p.is_dir() else log_path(p)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except (OSError, AttributeError):
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def format_entry(e: dict) -> str:
    files = ", ".join(
        f"{f['name']}" + (f" (скрыто: {f['hidden_lines']} строк в {f['hidden_chunks']} кусках)"
                          if f.get("hidden_chunks") else "")
        for f in e.get("files", []))
    return (f"{e.get('time', '?')}  {e.get('model', '?')}  [{e.get('mode', '?')}]  "
            f"{e.get('chars', 0)} симв., реплик {e.get('messages', 0)}\n    {files}")
