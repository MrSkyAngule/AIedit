"""Сохранение чата и памяти ИИ в папке проекта. У каждого проекта свой чат."""
import json
import os
from pathlib import Path

from .orchestrator import Turn

CHAT_NAME = ".aiedit-chat.json"
MAX_MESSAGES = 200


def chat_path(root: Path) -> Path:
    return Path(root) / CHAT_NAME


def save(root: Path, messages: list[tuple], history: list[Turn]) -> bool:
    """messages - список (kind, text, meta). Ошибка записи не ломает работу."""
    def rel(p: Path | None):
        if p is None:
            return None
        try:
            return os.path.relpath(os.path.realpath(p), os.path.realpath(root))
        except ValueError:
            return None
    data = {
        "messages": [list(m) for m in messages[-MAX_MESSAGES:]],
        "history": [{"role": t.role, "text": t.text, "source": rel(t.source), "en": t.en} for t in history],
    }
    try:
        chat_path(root).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


def load(root: Path) -> tuple[list[tuple], list[Turn]]:
    try:
        data = json.loads(chat_path(root).read_text(encoding="utf-8"))
        messages = [(m[0], m[1], m[2] if len(m) > 2 else "") for m in data["messages"]]
        history = [Turn(t["role"], t["text"], Path(root) / t["source"] if t.get("source") else None, t.get("en"))
                   for t in data["history"]]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return [], []
    return messages, history


def forget(root: Path):
    chat_path(root).unlink(missing_ok=True)
