"""Что проект сам говорит о своём окружении: файлы зависимостей, версия Python, установленные пакеты.

Берётся только то, что лежит в проекте и видно модели (закрытые файлы и зоны не показываются). Программы не
запускаются: версии читаются из файлов. Если ничего не нашлось, так и сказано, чтобы модель спросила человека,
а не додумывала.
"""
import json
import re

from .workspace import Workspace

MAX_CHARS = 3500
MAX_LINES = 40
MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "setup.cfg", "package.json",
             "go.mod", "Cargo.toml", ".python-version", ".nvmrc", ".tool-versions", "runtime.txt",
             "Dockerfile", "docker-compose.yml", "docker-compose.yaml")
_DIST = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)\.dist-info$")


def _text(ws: Workspace, name: str) -> str | None:
    real = ws.resolve(name)
    if real is None:
        return None
    st = ws.state(real)
    return None if isinstance(st, str) else ws.view(st).text


def _venv_info(ws: Workspace) -> list[str]:
    for name in (".venv", "venv"):
        venv = ws.root / name
        cfg = venv / "pyvenv.cfg"
        if not cfg.is_file():
            continue
        text = cfg.read_text(encoding="utf-8", errors="ignore")
        version = re.search(r"^version(?:_info)?\s*=\s*(\S+)", text, re.M)
        lines = [f"virtual environment `{name}`: Python {version[1] if version else 'version unknown'}"]
        installed = sorted({f"{m['name']}=={m['version']}" for site in venv.glob("lib/python*/site-packages")
                            for d in site.iterdir() if (m := _DIST.match(d.name))}, key=str.lower)
        shown = [p for p in installed if not p.lower().startswith(("pip==", "setuptools==", "wheel=="))]
        if shown:
            lines.append("installed: " + ", ".join(shown[:MAX_LINES]) + (f", ... and {len(shown) - MAX_LINES} more" if len(shown) > MAX_LINES else ""))
        return lines
    return []


def _manifest_lines(name: str, text: str) -> list[str]:
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return ["(not valid JSON)"]
        out = [f"{k}: " + ", ".join(f"{p} {v}" for p, v in (data.get(k) or {}).items())
               for k in ("engines", "dependencies", "devDependencies") if data.get(k)]
        return out or ["(no dependencies)"]
    if name == "Dockerfile":
        return [l.strip() for l in text.splitlines() if l.strip().upper().startswith(("FROM ", "ENV "))][:10]
    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return lines[:MAX_LINES] + ([f"... and {len(lines) - MAX_LINES} more lines"] if len(lines) > MAX_LINES else [])


def describe(ws: Workspace) -> str:
    sections: list[str] = []
    for name in MANIFESTS:
        text = _text(ws, name)
        if text is not None and text.strip():
            sections.append(f"{name}:\n" + "\n".join("  " + l for l in _manifest_lines(name, text)))
    sections += _venv_info(ws)
    if not sections:
        return ("The project declares nothing about its environment: no dependency files, no virtual environment. "
                "Versions and software (language, framework, database, operating system, how the app is run) are UNKNOWN. "
                "If the task depends on them, ask the user now (once, everything in one question) instead "
                "of assuming. package_version only tells what is newest, not what the user runs.")
    out = "\n".join(sections)
    return out[:MAX_CHARS] + ("\n[... cut]" if len(out) > MAX_CHARS else "")
