"""Настройки пользователя по проектам. Лежат в профиле пользователя, а не в проекте:
команду из чужого репозитория запускать нельзя."""
import json
import os
from pathlib import Path


def config_file() -> Path:
    base = os.environ.get("AIEDIT_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "aiedit")
    return Path(base) / "settings.json"


def _read() -> dict:
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _key(root: Path | str) -> str:
    return os.path.realpath(root)


def get_project(root: Path | str) -> dict:
    data = _read().get("projects", {}).get(_key(root), {})
    return data if isinstance(data, dict) else {}


def get_user() -> dict:
    data = _read().get("user", {})
    return data if isinstance(data, dict) else {}


def set_user(**values) -> bool:
    data = _read()
    data.setdefault("user", {}).update(values)
    try:
        f = config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        return False
    return True


def set_project(root: Path | str, **values) -> bool:
    data = _read()
    data.setdefault("projects", {}).setdefault(_key(root), {}).update(values)
    try:
        f = config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        return False
    return True
