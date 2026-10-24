"""Операции с файлами проекта. Зоны (.aimarks.json) и запреты переезжают вместе с файлом."""
import shutil
from pathlib import Path

from .marks import sidecar
from .policy import Rules


class FileOpError(Exception):
    pass


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise FileOpError("недопустимое имя")
    return name


def create_file(folder: Path, name: str) -> Path:
    target = folder / check_name(name)
    if target.exists():
        raise FileOpError(f"«{target.name}» уже существует")
    target.touch()
    return target


def create_folder(folder: Path, name: str) -> Path:
    target = folder / check_name(name)
    if target.exists():
        raise FileOpError(f"«{target.name}» уже существует")
    target.mkdir()
    return target


def rename(path: Path, new_name: str, rules: Rules | None = None) -> Path:
    """Переименовывает внутри той же папки. Зоны файла и запреты переносятся."""
    target = path.with_name(check_name(new_name))
    if target == path:
        return path
    if target.exists():
        raise FileOpError(f"«{target.name}» уже существует")
    old_rel = rules.rel(path) if rules else None
    side = sidecar(path)
    path.rename(target)
    if target.is_file() and side.exists():
        side.rename(sidecar(target))
    if rules and old_rel not in (None, "."):
        new_rel = rules.rel(target)
        if new_rel is not None:
            rules.moved(old_rel, new_rel)
    return target


def count_inside(path: Path) -> int:
    return sum(1 for p in path.rglob("*") if p.is_file()) if path.is_dir() else 1


def delete(path: Path):
    """Удаляет файл или папку вместе с файлами зон. Запреты в правилах остаются: так
    заново созданный файл с тем же именем (например .env) не станет доступен ИИ."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
        sidecar(path).unlink(missing_ok=True)
