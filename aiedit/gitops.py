"""Работа с git для человека (панель Git). ИИ git не пользуется: это инструмент человека.

Только локальные действия: состояние, дифф, коммит, создание репозитория. push и pull здесь нет:
это работа с удалённым сервером и паролями. git вызывается без оболочки, с таймаутом и без
вопросов в терминал (GIT_TERMINAL_PROMPT=0).
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 20
# служебные файлы редактора с историей чата и журналом отправок: в коммит по умолчанию не предлагаем
PRIVATE = (".aiedit-chat.json", ".aiedit-sends.jsonl")


@dataclass(frozen=True)
class Entry:
    code: str          # два символа git status: «M », « M», «??», «D », «R »...
    path: str          # путь от корня репозитория
    old_path: str = ""  # для переименования: откуда

    @property
    def label(self) -> str:
        """Для человека: изменён, новый, удалён..."""
        c = self.code.strip()
        if c == "??" or "A" in c:
            return "новый"
        if "D" in c:
            return "удалён"
        if "R" in c:
            return "переименован"
        if "U" in c:
            return "конфликт"
        return "изменён"

    @property
    def private(self) -> bool:
        return Path(self.path).name in PRIVATE


def run(root: Path, *args: str, timeout: float = TIMEOUT, raw: bool = False) -> tuple[int, str]:
    """(код, вывод). raw - не обрезать пробелы: в выводе git status они значимы."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        res = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True,
                             errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    out = res.stdout + res.stderr
    return res.returncode, out if raw else out.strip()


def toplevel(root: Path) -> Path | None:
    code, out = run(root, "rev-parse", "--show-toplevel")
    return Path(out) if code == 0 and out else None


def branch(root: Path) -> str:
    code, out = run(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        return out
    code, out = run(root, "symbolic-ref", "--short", "HEAD")  # репозиторий без коммитов
    return out if code == 0 else "?"


def status(root: Path) -> list[Entry]:
    code, out = run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", raw=True)
    if code != 0:
        return []
    parts = out.split("\0")
    entries, i = [], 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        old = ""
        if "R" in code or "C" in code:
            old = parts[i] if i < len(parts) else ""
            i += 1
        entries.append(Entry(code, path, old))
    return entries


def head_text(root: Path, path: str) -> str | None:
    """Текст файла в последнем коммите; None - если его там нет."""
    try:  # байты, а не run(): текст файла нельзя обрезать по краям
        res = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=root, capture_output=True, timeout=TIMEOUT,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.decode("utf-8", "replace") if res.returncode == 0 else None


def init(root: Path) -> tuple[bool, str]:
    code, out = run(root, "init")
    return code == 0, out


def commit(root: Path, paths: list[str], message: str) -> tuple[bool, str]:
    """Коммитит только выбранные файлы (и их удаление). Остальное в индексе не трогает."""
    if not paths:
        return False, "не выбрано ни одного файла"
    if not message.strip():
        return False, "нужно сообщение коммита"
    # путь, которого нет ни на диске, ни в индексе (старое имя после git mv), git add не принимает
    addable = [p for p in paths if (root / p).exists() or run(root, "ls-files", "--", p)[1]]
    if addable:
        code, out = run(root, "add", "-A", "--", *addable)
        if code != 0:
            return False, out
    code, out = run(root, "commit", "-m", message.strip(), "--", *paths)
    return code == 0, out
