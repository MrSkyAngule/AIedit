"""Связанные файлы и поиск по проекту. Закрытые от ИИ файлы здесь не появляются."""
import ast
import os
from dataclasses import dataclass
from pathlib import Path

from . import policy

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vscode"}
MAX_FILE_BYTES = 1_000_000
MAX_HITS = 200


def _module_file(base: Path, parts: list[str]) -> Path | None:
    target = base.joinpath(*parts) if parts else base
    for cand in (target.with_suffix(".py"), target / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def python_imports(text: str, file: Path, root: Path) -> list[Path]:
    """Файлы проекта, которые импортирует этот Python-файл. Не выходит за root, закрытые пропускает."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    root = Path(os.path.realpath(root))
    found: list[Path] = []

    def add(p: Path | None):
        if p is None:
            return
        real = Path(os.path.realpath(p))
        if real.is_relative_to(root) and real != Path(os.path.realpath(file)) \
                and real not in found and not policy.is_denied(real):
            found.append(real)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(_module_file(root, a.name.split(".")) or _module_file(file.parent, a.name.split(".")))
        elif isinstance(node, ast.ImportFrom):
            base = file.parent
            for _ in range(max(node.level - 1, 0)):
                base = base.parent
            parts = node.module.split(".") if node.module else []
            where = base if node.level else root
            mod = _module_file(where, parts) or (_module_file(file.parent, parts) if not node.level else None)
            add(mod)
            for a in node.names:  # from pkg import submodule
                add(_module_file(where, parts + [a.name]))
    return found


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int  # с 1
    text: str


def search(root: Path, query: str, case: bool = False, limit: int = MAX_HITS) -> list[Hit]:
    """Ищет строку в текстовых файлах проекта. Закрытые от ИИ файлы не читаются и не показываются."""
    if not query:
        return []
    needle = query if case else query.lower()
    hits: list[Hit] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not policy.is_denied(Path(dirpath) / d))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if policy.is_private_name(name) or policy.is_denied(p):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
                lines = p.read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                if needle in (line if case else line.lower()):
                    hits.append(Hit(p, i, line.strip()[:200]))
                    if len(hits) >= limit:
                        return hits
    return hits
