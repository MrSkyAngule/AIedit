"""Запуск проверки (тестов) после правки ИИ и безопасная подготовка вывода для ИИ."""
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import policy, secrets_scan
from .policy import Rules

MAX_OUTPUT = 20000
MAX_SHOWN = 1500
_FRAME = re.compile(r'^\s*File "(.+?)", line \d+')
_TOKEN = re.compile(r"[\w\-+/=.]{8,}")
MAX_PROTECTED_FILE = 200_000


@dataclass
class CheckResult:
    command: str
    code: int | None  # None - не запустилась или не уложилась во время
    output: str
    seconds: float
    timed_out: bool = False
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.code == 0 and not self.timed_out

    def status_en(self) -> str:
        """Для модели (в память разговора)."""
        if self.error:
            return "could not run"
        if self.timed_out:
            return "timed out"
        return "passed" if self.passed else f"failed (exit {self.code})"

    def status(self) -> str:
        if self.error:
            return f"не запустилась ({self.error})"
        if self.timed_out:
            return f"прервана по времени ({self.seconds:.0f} с)"
        return "прошла" if self.passed else f"не прошла (код {self.code})"


def run_command(command: str, cwd: Path, timeout: float = 120) -> CheckResult:
    """Запускает команду без оболочки (shell), с таймаутом; при таймауте убивает всю группу процессов."""
    start = time.monotonic()
    try:
        argv = shlex.split(command)
        if not argv:
            return CheckResult(command, None, "", 0, error="пустая команда")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", start_new_session=True)
    except (OSError, ValueError) as e:
        return CheckResult(command, None, "", time.monotonic() - start, error=str(e))
    try:
        out, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, _ = proc.communicate()
        timed_out = True
    return CheckResult(command, proc.returncode, (out or "")[-MAX_OUTPUT:], time.monotonic() - start, timed_out)


def tail(output: str, limit: int = MAX_SHOWN) -> str:
    return output if len(output) <= limit else "...\n" + output[-limit:]


def protected_tokens(root: Path, concealed_lines: set[str] | None = None) -> set[str]:
    """Длинные токены из закрытых от ИИ файлов и скрытых зон, а также пути закрытых файлов.
    Любая строка вывода с таким токеном считается утечкой."""
    tokens: set[str] = set()
    for line in concealed_lines or ():
        tokens.update(_TOKEN.findall(line))
    rules = Rules(root)
    for rel in rules.denied:
        target = rules.root / rel
        tokens.update((rel, str(target)))
        files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else []
        for f in files:
            try:
                if f.stat().st_size <= MAX_PROTECTED_FILE:
                    tokens.update(_TOKEN.findall(f.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    return tokens


def scrub(output: str, concealed: set[str] | None = None, tokens: set[str] | None = None) -> str:
    """Убирает из вывода то, что нельзя отдавать ИИ: кадры трейсбека из закрытых файлов,
    строки с секретами, строки совпадающие со скрытым кодом (concealed) и строки с токенами
    из закрытых файлов (tokens). Это лучшее усилие, а не гарантия."""
    concealed = concealed or set()
    tokens = tokens or set()
    secret_lines = {f.line for f in secrets_scan.scan(output)}
    lines, out, skip_next = output.split("\n"), [], False
    for i, line in enumerate(lines):
        if skip_next:  # строка исходника сразу после закрытого кадра
            skip_next = False
            continue
        m = _FRAME.match(line)
        if m and policy.is_denied(m[1]):
            out.append('  [frame from a closed file hidden]')
            skip_next = True
            continue
        if (i in secret_lines or (len(line.strip()) >= 4 and line.strip() in concealed)
                or any(t in line for t in tokens)):
            out.append("[line hidden]")
            continue
        out.append(line)
    return "\n".join(out)
