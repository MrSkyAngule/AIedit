"""Установка Python-пакетов по просьбе ИИ (инструмент install_packages).

Ставит редактор, в окружение проекта (.venv), каждый раз только после «Установить» человека.
Принимаются только имена пакетов с PyPI (и версии): никаких ссылок, путей и флагов pip.
Ставятся только готовые сборки (--only-binary :all:): при установке wheel код пакета не выполняется,
а обычный pip install мог бы запустить setup.py из пакета прямо на машине человека.
Подделки под известные имена (typosquatting) так не поймать - поэтому человек видит список.
"""
import re
import shlex
from pathlib import Path
from typing import Callable

from . import checks
from .sandbox import project_venv

MAX_PACKAGES = 10
TIMEOUT = 300
MAX_TO_MODEL = 3000
SPEC = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]{1,100}\])?"
                  r"(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]{1,50}(?:,(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]{1,50})*)?")
OUT_DECLINED = "The user declined to install these packages. Continue without them or explain what is needed."


def parse(text: str) -> list[str] | str:
    """Список пакетов или текст ошибки для модели."""
    specs = [p for p in re.split(r"[\s,]+(?![=<>~!])", (text or "").strip()) if p]
    if not specs:
        return "ERROR: missing arguments: packages"
    if len(specs) > MAX_PACKAGES:
        return f"ERROR: at most {MAX_PACKAGES} packages at once"
    bad = [p for p in specs if not SPEC.fullmatch(p)]
    if bad:
        return ("ERROR: only package names from PyPI with optional versions are allowed (like flask or "
                f"requests==2.32.3), not URLs, paths or pip options: {', '.join(bad[:5])}")
    return list(dict.fromkeys(specs))


def pip_argv(python: Path, packages: list[str]) -> list[str]:
    return [str(python), "-m", "pip", "install", "--only-binary", ":all:", "--disable-pip-version-check",
            "--no-input", "--", *packages]


def name_of(spec: str) -> str:
    return re.split(r"[\[=<>~!]", spec, maxsplit=1)[0]


class Installer:
    """approve(папка окружения, пакеты) -> True/False спрашивает человека; show(результат) показывает вывод."""

    def __init__(self, root: Path, approve: Callable[[Path, list[str]], bool],
                 show: Callable[[checks.CheckResult], None] | None = None, runner=None, timeout: float = TIMEOUT):
        self.root, self.approve, self.show, self.runner, self.timeout = root, approve, show, runner, timeout

    def __call__(self, text: str) -> str:
        packages = parse(text)
        if isinstance(packages, str):
            return packages
        venv = project_venv(self.root)
        if venv is None:
            return ("ERROR: the project has no virtual environment (.venv or venv). Ask the user to create one "
                    "(python -m venv .venv) and try again.")
        if not self.approve(venv, packages):
            return OUT_DECLINED
        command = shlex.join(pip_argv(venv / "bin" / "python", packages))
        res = (self.runner or checks.run_command)(command, self.root, self.timeout)
        res.command = "pip install " + " ".join(packages)
        if self.show:
            self.show(res)
        status = ("could not start: " + res.error if res.error else "timed out" if res.timed_out
                  else f"exit code {res.code}")
        out = checks.scrub(res.output.strip())
        if len(out) > MAX_TO_MODEL:
            out = "[...]\n" + out[-MAX_TO_MODEL:]
        hint = ("\nNote: only prebuilt wheels are allowed. If a package has no wheel for this system, "
                "tell the user; they can install it themselves." if res.code not in (0, None) else "")
        return f"$ {res.command}\n{status}\n{out or '(no output)'}{hint}"
