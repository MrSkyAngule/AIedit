"""Терминал для ИИ: команды выполняет редактор, в песочнице, на копии проекта.

Копия: запрещённых файлов в ней нет, закрытый код заменён. В копии ровно то, что модель и так видит:
- interface: заглушки - имя, аргументы и декораторы остаются, тело бросает NotImplementedError;
- hidden и строки с секретами: ничего, только pass или raise, чтобы файл остался рабочим.
Поэтому, что бы ни запустила модель (хоть программу, которая печатает исходники в base64),
закрытого кода в выводе быть не может.
В копию попадают и правки, которые агент уже предложил: он проверяет то, что написал.

Песочница - bubblewrap (bwrap, только Linux): видна только копия, системные папки на чтение,
без сети, без домашней папки, с пустым окружением (ключей API там нет) и ограничением по времени.
То, что команда поменяла в копии, на диск не попадает. Каждую команду одобряет человек.
"""
import ast
import os
import shlex
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

from . import checks, guard, policy
from .workspace import SKIP_DIRS, FileState, Workspace

TIMEOUT = 60
MAX_FILES = 5000
MAX_BYTES = 200_000_000
MAX_COMMAND = 2000
MAX_TO_MODEL = 6000
WORK = "/work"
STUB = 'raise NotImplementedError("hidden from AI")'
PY_SUFFIXES = (".py", ".pyi")
OUT_DECLINED = "The user declined to run this command. Do not retry it; continue without it or ask the user."

_available: str | None = None


class TooBig(Exception):
    pass


# --- заглушки вместо закрытого кода ---
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)


def _stub_node(node: ast.stmt) -> list[str]:
    """Заглушка одного оператора: сигнатуры и декораторы остаются, тела и значения - нет."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        args.defaults = [ast.Constant(value=...) for _ in args.defaults]
        args.kw_defaults = [None if d is None else ast.Constant(value=...) for d in args.kw_defaults]
        node.body = [ast.parse(STUB).body[0]]
        return ast.unparse(node).split("\n")
    if isinstance(node, ast.ClassDef):
        body: list[str] = []
        for child in node.body:
            body += _stub_node(child)
        node.body = [ast.Pass()]
        head = ast.unparse(node).split("\n")[:-1]  # без «pass»
        return head + ["    " + line for line in (body or ["pass"])]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [f"{t.id} = None" for t in targets if isinstance(t, ast.Name)]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [ast.unparse(node)]  # нужны, чтобы модуль загрузился; имена библиотек и так видны в интерфейсе
    return []


def stub_python(code: str) -> list[str]:
    """Строки-заглушки для куска Python. Если кусок не разбирается (середина функции), - одна строка raise."""
    indent = next((l[:len(l) - len(l.lstrip())] for l in code.split("\n") if l.strip()), "")
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return [indent + STUB]
    lines: list[str] = []
    for node in tree.body:
        lines += _stub_node(node)
    if indent and not all(isinstance(n, _DEFINITIONS) for n in tree.body):
        lines.append(STUB)  # кусок тела функции: вызов не должен притворяться успешным
    return [indent + l if l else l for l in lines] or [indent + "pass"]


def blank_python(code: str) -> list[str]:
    """Замена hidden-куска без единого имени из него. pass там, где были определения (уровень модуля
    или класса), raise там, где был исполняемый код (тело функции): вызов не притворится успешным."""
    indent = next((l[:len(l) - len(l.lstrip())] for l in code.split("\n") if l.strip()), "")
    if not indent:
        return ["pass"]
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return [indent + STUB]
    return [indent + ("pass" if all(isinstance(n, _DEFINITIONS) for n in tree.body) else STUB)]


def sandbox_text(ws: Workspace, st: FileState) -> str:
    """Текст файла для копии: закрытые зоны и секреты заменены. Число строк по возможности то же,
    чтобы номера строк в ошибках совпадали с тем, что видит модель."""
    regions = sorted((r for r in ws.view_regions(st) if r.level in guard.CONCEALED), key=lambda r: r.start)
    if not regions:
        return st.text
    lines = st.text.split("\n")
    python = st.path.suffix in PY_SUFFIXES
    out, pos = [], 0
    for r in regions:
        if r.start < pos:  # пересекающиеся зоны: часть уже заменена
            r = guard.Region(r.level, pos, r.end)
        if r.start > r.end:
            continue
        out += lines[pos:r.start]
        size = r.end - r.start + 1
        code = "\n".join(lines[r.start:r.end + 1])
        stub = (stub_python(code) if r.level == "interface" else blank_python(code)) if python else []
        out += stub + [""] * max(0, size - len(stub))
        pos = r.end + 1
    return "\n".join(out + lines[pos:])


# --- копия проекта ---
def _file_text(ws: Workspace, real: Path) -> str | None | bool:
    """Текст для копии; None - копировать как есть (не текст); False - не копировать."""
    cached = real in ws.states
    st = ws.state(real)
    if not cached and real in ws.states:
        del ws.states[real]  # копия не должна менять рабочую область агента
    if isinstance(st, str):
        return None if "not a text file" in st else False  # большие и устаревшие - не копируем
    return sandbox_text(ws, st)


def build_copy(ws: Workspace, dest: Path) -> list[str]:
    """Копия видимых файлов с заглушками. Возвращает заметки о пропущенном (для человека)."""
    skipped: list[str] = []
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(ws.root):
        here = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in SKIP_DIRS and ws.visible(Path(os.path.realpath(here / d)))]
        for name in sorted(filenames):
            src = here / name
            real = Path(os.path.realpath(src))
            if not real.is_relative_to(ws.root) or not ws.visible(real) or policy.is_private_name(name):
                continue
            files += 1
            size += real.stat().st_size
            if files > MAX_FILES or size > MAX_BYTES:
                raise TooBig()
            target = dest / real.relative_to(ws.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            text = _file_text(ws, real)
            if text is False:
                skipped.append(ws.rel(real))
            elif text is None:
                shutil.copy2(real, target)
            else:
                target.write_text(text, encoding="utf-8")
    return skipped


# --- песочница ---
def available() -> str:
    """Пусто, если песочница работает; иначе причина для человека."""
    global _available
    if _available is None:
        if not sys.platform.startswith("linux"):
            _available = "песочница пока есть только на Linux"
        elif not shutil.which("bwrap"):
            _available = "не установлен bubblewrap (пакет bwrap)"
        else:
            res = checks.run_command(shlex.join(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"]),
                                     Path("/"), 10)
            _available = "" if res.passed else f"bwrap не запускается: {res.output.strip() or res.error}"[:200]
    return _available


def bwrap_argv(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None) -> list[str]:
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr"]
    for d in ("bin", "sbin", "lib", "lib32", "lib64"):
        p = Path("/") / d
        if p.is_symlink():
            argv += ["--symlink", os.readlink(p), str(p)]
        elif p.is_dir():
            argv += ["--ro-bind", str(p), str(p)]
    for f in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/localtime", "/etc/passwd", "/etc/group"):
        argv += ["--ro-bind-try", f, f]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(work), WORK]
    path = "/usr/local/bin:/usr/bin:/bin"
    env = ["--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1"]
    if venv is not None:  # окружение проекта: на чтение, по тому же пути (в скриптах venv пути абсолютные)
        argv += ["--ro-bind", str(venv), str(venv)]
        base = venv_base(venv)
        if base is not None:  # интерпретатор, на который ссылается venv (uv, pyenv), - тоже только на чтение
            argv += ["--ro-bind", os.path.realpath(base), str(base)]
        path = f"{venv}/bin:{path}"
        env += ["--setenv", "VIRTUAL_ENV", str(venv)]
    for name, real in (extra_ro or {}).items():
        argv += ["--ro-bind", str(real), f"{WORK}/{name}"]
    return argv + ["--chdir", WORK, "--clearenv", "--setenv", "PATH", path, *env, "/bin/sh", "-c", command]


def run_bwrap(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None,
              timeout: float = TIMEOUT) -> checks.CheckResult:
    res = checks.run_command(shlex.join(bwrap_argv(command, work, venv, extra_ro)), work, timeout)
    res.command = command
    return res


def venv_base(venv: Path) -> Path | None:
    """Папка установки Python, от которого сделан venv (строка home в pyvenv.cfg), если она не в /usr."""
    try:
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    home = next((l.partition("=")[2].strip() for l in cfg.splitlines() if l.partition("=")[0].strip() == "home"), "")
    if not home:
        return None
    base = Path(home).parent
    return None if base == Path("/") or Path(os.path.realpath(base)).is_relative_to("/usr") or not base.is_dir() else base


def project_venv(root: Path) -> Path | None:
    for name in (".venv", "venv"):
        p = root / name
        if (p / "bin" / "python").exists() and Path(os.path.realpath(p)).is_relative_to(root):
            return p
    return None


_SHELL_META = set(";&|`$<>()\n\\*?{}[]~")


def program(command: str) -> str | None:
    """Имя программы, если команда простая (без ;, |, $(...), перенаправлений и масок).
    Только такие можно разрешить навсегда: «pytest» не должно разрешать «pytest; что-то ещё»."""
    if _SHELL_META & set(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv[0] if argv else None


def preapproved(command: str, allowed) -> bool:
    name = program(command)
    return name is not None and name in set(allowed)


class Terminal:
    """Инструмент run_command. approve(команда) -> "run" | "deny" спрашивает человека;
    show(результат) показывает ему вывод. Копия создаётся при первой команде и живёт до close()."""

    def __init__(self, ws: Workspace, approve: Callable[[str], str], show: Callable[[checks.CheckResult], None] | None = None,
                 runner=None, timeout: float = TIMEOUT):
        self.ws, self.approve, self.show, self.runner, self.timeout = ws, approve, show, runner, timeout
        self.tmp: tempfile.TemporaryDirectory | None = None
        self.synced: dict[Path, str] = {}
        self.skipped: list[str] = []
        self.runs = 0

    @property
    def work(self) -> Path:
        return Path(self.tmp.name)

    def _prepare(self):
        if self.tmp is None:
            self.tmp = tempfile.TemporaryDirectory(prefix="aiedit-sandbox-")
            self.skipped = build_copy(self.ws, self.work)
        for path, st in self.ws.states.items():  # правки агента - в копию
            if st.base is not None and st.text == st.base and path not in self.synced:
                continue  # не менялся (если правку откатили, копию надо вернуть - тогда он в synced)
            text = sandbox_text(self.ws, st)
            if self.synced.get(path) != text:
                target = self.work / path.relative_to(self.ws.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                self.synced[path] = text

    def __call__(self, command: str) -> str:
        command = (command or "").strip()
        if not command:
            return "ERROR: missing arguments: command"
        if len(command) > MAX_COMMAND or "\x00" in command:
            return "ERROR: command too long"
        if self.approve(command) != "run":
            return OUT_DECLINED
        try:
            self._prepare()
        except TooBig:
            return "ERROR: the project is too large for the sandbox"
        extra = {d: self.ws.root / d for d in ("node_modules",) if (self.ws.root / d).is_dir()}
        res = (self.runner or run_bwrap)(command, self.work, project_venv(self.ws.root), extra, self.timeout)
        self.runs += 1
        if self.show:
            self.show(res)
        return self.for_model(res)

    def for_model(self, res: checks.CheckResult) -> str:
        if res.error:
            status = f"could not start: {res.error}"
        elif res.timed_out:
            status = f"timed out after {self.timeout:g} s and was killed"
        else:
            status = f"exit code {res.code}"
        output = checks.scrub(res.output.strip())  # закрытого кода в копии нет; это вторая линия защиты
        if len(output) > MAX_TO_MODEL:
            output = "[... earlier output cut ...]\n" + output[-MAX_TO_MODEL:]
        note = ""
        if self.skipped:
            note = "\nnote: some files are missing from the sandbox copy: " + ", ".join(self.skipped[:10])
        return f"$ {res.command}\n{status}\n{output or '(no output)'}{note}"

    def close(self):
        if self.tmp is not None:
            self.tmp.cleanup()
            self.tmp = None
