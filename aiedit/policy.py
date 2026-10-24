"""Правила доступа ИИ к файлам и папкам.

Правила лежат в `.aiedit-rules.json` в корне проекта: список запрещённых путей
относительно этого корня. Запрет на папку действует на всё внутри.

Проверка is_denied() не зависит от состояния редактора: она идёт вверх от файла,
находит файлы правил в родительских папках и сверяет путь (и с симлинками, и без).
Если файл правил повреждён, доступ закрывается.
"""
import json
import os
from pathlib import Path

RULES_NAME = ".aiedit-rules.json"
# служебные файлы редактора: ИИ их не отдаём и в дереве не показываем
PRIVATE_NAMES = {RULES_NAME, ".aiedit-sends.jsonl", ".aiedit-chat.json"}
PRIVATE_SUFFIXES = (".aimarks.json",)


def is_private_name(name: str) -> bool:
    return name in PRIVATE_NAMES or name.endswith(PRIVATE_SUFFIXES)


class AccessDenied(Exception):
    pass


def _covered(rel: str, entries) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in entries)


def is_denied(path: Path | str) -> bool:
    """Закрыт ли путь для ИИ правилами любой из родительских папок."""
    for candidate in {os.path.realpath(path), os.path.abspath(path)}:
        p = Path(candidate)
        if is_private_name(p.name):
            return True
        for d in (p, *p.parents):
            rules = d / RULES_NAME
            if not rules.is_file() or p == d:
                continue
            try:
                entries = json.loads(rules.read_text(encoding="utf-8"))["denied"]
            except (OSError, ValueError, KeyError, TypeError):
                return True  # не смогли прочитать правила - закрываем
            if _covered(p.relative_to(d).as_posix(), entries):
                return True
    return False


def require(path: Path | str | None):
    """Точка контроля перед отправкой чего-либо провайдеру."""
    if path is not None and is_denied(path):
        raise AccessDenied(f"{Path(path).name}: закрыто от ИИ правилами проекта")


class Rules:
    """Правила одной папки проекта для редактирования из интерфейса."""

    def __init__(self, root: Path):
        self.root = Path(os.path.realpath(root))
        self.denied: list[str] = []
        self.load()

    @property
    def file(self) -> Path:
        return self.root / RULES_NAME

    def load(self):
        try:
            self.denied = list(json.loads(self.file.read_text(encoding="utf-8"))["denied"])
        except (OSError, ValueError, KeyError, TypeError):
            self.denied = []

    def save(self):
        if self.denied:
            self.file.write_text(json.dumps({"denied": sorted(self.denied)}, ensure_ascii=False,
                                            indent=1), encoding="utf-8")
        else:
            self.file.unlink(missing_ok=True)

    def rel(self, path: Path | str) -> str | None:
        """Путь относительно корня (с учётом симлинков) или None, если он вне корня."""
        try:
            return Path(os.path.realpath(path)).relative_to(self.root).as_posix()
        except ValueError:
            return None

    def covers(self, path: Path | str) -> bool:
        rel = self.rel(path)
        return rel is not None and rel != "." and _covered(rel, self.denied)

    def is_explicit(self, path: Path | str) -> bool:
        return self.rel(path) in self.denied

    def moved(self, old_rel: str, new_rel: str):
        """Переносит запреты вместе с переименованным файлом или папкой."""
        out = []
        for e in self.denied:
            if e == old_rel:
                out.append(new_rel)
            elif e.startswith(old_rel + "/"):
                out.append(new_rel + e[len(old_rel):])
            else:
                out.append(e)
        if out != self.denied:
            self.denied = out
            self.save()

    def set_denied(self, path: Path | str, flag: bool) -> bool:
        """Запрещает или разрешает путь. False, если путь вне корня, сам корень или закрыт сверху."""
        rel = self.rel(path)
        if rel is None or rel == ".":
            return False
        if flag:
            if rel not in self.denied:
                self.denied = [e for e in self.denied if not e.startswith(rel + "/")] + [rel]
        else:
            if rel not in self.denied:
                return False  # закрыт через родителя - снять можно только с родителя
            self.denied.remove(rel)
        self.save()
        return True
