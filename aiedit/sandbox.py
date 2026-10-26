"""Терминал для ИИ: команды выполняет редактор, в песочнице, на копии проекта.

Копия: запрещённых файлов в ней нет, закрытый код заменён. В копии ровно то, что модель и так видит:
- interface: заглушки - имя, аргументы и декораторы остаются, тело бросает NotImplementedError;
- hidden и строки с секретами: ничего, только pass или raise, чтобы файл остался рабочим.
Поэтому, что бы ни запустила модель (хоть программу, которая печатает исходники в base64),
закрытого кода в выводе быть не может.
В копию попадают и правки, которые агент уже предложил: он проверяет то, что написал.

Песочница: на Linux - bubblewrap (bwrap), на macOS - встроенный sandbox-exec (экспериментально).
Видна только копия, системные папки на чтение, без сети, без домашней папки, с пустым окружением
(ключей API там нет) и ограничением по времени. То, что команда поменяла в копии, на диск не
попадает. Каждую команду одобряет человек. На Windows песочницы нет: редактор можно запустить в WSL.
"""
import ast
import json
import re
import os
import shlex
import shutil
import sys
import tempfile
import textwrap
import urllib.parse
from pathlib import Path
from typing import Callable

from . import checks, guard, policy, secrets_scan
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


SECRET_VALUE = "<hidden>"  # такое значение secrets_scan считает заглушкой: вывод с ним не вычищается


def mask_secret_values(line: str) -> str | None:
    """Строка, где каждое похожее на секрет значение в кавычках заменено на SECRET_VALUE. Так строка
    `"password": "secret123",` внутри словаря остаётся правильным Python, а raise на её месте ломал бы файл.
    None, если хоть одно значение не в кавычках (или его не найти): тогда строка прячется целиком."""
    spans = secrets_scan.spans(line)
    if not spans:
        return None
    out, pos = [], 0
    for a, b, _ in spans:
        quote = line[a - 1] if a > 0 else ""
        if a < pos or quote not in ("'", '"') or b >= len(line) or line[b] != quote:
            return None
        out += [line[pos:a], SECRET_VALUE]
        pos = b
    return "".join(out) + line[pos:]


_DEF_NAME = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)|^(\w+) = \.\.\.")


def closed_names(code: str) -> list[str]:
    """Имена из куска interface: они модели и так видны в заглушке, значит, их можно назвать в подсказке."""
    names: list[str] = []
    for sig in guard.signatures(code):
        m = _DEF_NAME.match(sig)
        name = m and (m[1] or m[2])
        if name and name not in names:
            names.append(name)
    return names


def closed_notice(closed: dict) -> str:
    """Что в копии проекта закрыто, для модели: иначе None и NameError она принимает за ошибку своего кода
    и подставляет свои значения, чтобы запуск «прошёл»."""
    rows = []
    for name, e in sorted(closed.items())[:10]:
        parts = []
        if e["names"]:
            parts.append(", ".join(e["names"][:20])
                         + " (real values are not available: variables are None, functions raise NotImplementedError)")
        if e["hidden"]:
            parts.append(f"{e['hidden']} hidden line(s) are gone")
        if e.get("masked"):
            parts.append(f'{e["masked"]} line(s) had secret-looking values replaced with "{SECRET_VALUE}"')
        if parts:
            rows.append(f"- {name}: " + "; ".join(parts))
    if not rows:
        return ""
    return ("\nclosed in this sandbox (the real values are NOT here):\n" + "\n".join(rows)
            + "\nUse these names as they are (call the functions, pass the real variable): the code will have the real "
              "values. But do not invent your own values for them, and do not count a run that depends on them as proof "
              "that the code works. If you need sample data for a test, call it sample data. Say what you could not verify.")


def sandbox_text(ws: Workspace, st: FileState, closed: dict | None = None) -> str:
    """Текст файла для копии: закрытые зоны и секреты заменены. Число строк по возможности то же,
    чтобы номера строк в ошибках совпадали с тем, что видит модель.
    closed (если дан) пополняется: что именно в этом файле закрыто (для подсказки модели)."""
    lines = st.text.split("\n")
    entry = {"names": [], "hidden": 0, "masked": 0}
    masked: set[guard.Region] = set()  # найденные секреты, где хватило заменить само значение
    for r in ws.secret_regions(st):
        new = [mask_secret_values(line) for line in lines[r.start:r.end + 1]]
        if all(n is not None for n in new):
            lines[r.start:r.end + 1] = new
            masked.add(r)
            entry["masked"] += len(new)
    regions = sorted((r for r in ws.view_regions(st) if r.level in guard.CONCEALED and r not in masked),
                     key=lambda r: r.start)
    if closed is not None:
        closed.pop(ws.rel(st.path), None)  # файл могли пересобрать: считаем заново, а не добавляем
    if not regions and not masked:
        return st.text
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
        if r.level == "interface" and python:
            entry["names"] += [n for n in closed_names(code) if n not in entry["names"]]
        else:
            entry["hidden"] += size
        out += stub + [""] * max(0, size - len(stub))
        pos = r.end + 1
    if closed is not None and (entry["names"] or entry["hidden"] or entry["masked"]):
        closed[ws.rel(st.path)] = entry
    return "\n".join(out + lines[pos:])


# --- копия проекта ---
def _file_text(ws: Workspace, real: Path, closed: dict | None = None) -> str | None | bool:
    """Текст для копии; None - копировать как есть (не текст); False - не копировать."""
    cached = real in ws.states
    st = ws.state(real)
    if not cached and real in ws.states:
        del ws.states[real]  # копия не должна менять рабочую область агента
    if isinstance(st, str):
        return None if "not a text file" in st else False  # большие и устаревшие - не копируем
    return sandbox_text(ws, st, closed)


def build_copy(ws: Workspace, dest: Path, closed: dict | None = None) -> list[str]:
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
            if (not real.is_relative_to(ws.root) or not ws.visible(real) or policy.is_private_name(name)
                    or ws.pending_delete(real)):
                continue
            files += 1
            size += real.stat().st_size
            if files > MAX_FILES or size > MAX_BYTES:
                raise TooBig()
            target = dest / real.relative_to(ws.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            text = _file_text(ws, real, closed)
            if text is False:
                skipped.append(ws.rel(real))
            elif text is None:
                shutil.copy2(real, target)
            else:
                target.write_text(text, encoding="utf-8")
    return skipped


# --- песочница ---
SEATBELT = "/usr/bin/sandbox-exec"


def kind(platform: str | None = None) -> str:
    """Какая песочница у этой системы: bwrap, seatbelt (macOS) или пусто."""
    platform = platform or sys.platform
    return "bwrap" if platform.startswith("linux") else "seatbelt" if platform == "darwin" else ""


def available() -> str:
    """Пусто, если песочница работает; иначе причина для человека."""
    global _available
    if _available is None:
        backend = kind()
        if backend == "bwrap":
            if not shutil.which("bwrap"):
                _available = "не установлен bubblewrap (пакет bwrap)"
            else:
                res = checks.run_command(shlex.join(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"]),
                                         Path("/"), 10)
                _available = "" if res.passed else f"bwrap не запускается: {res.output.strip() or res.error}"[:200]
        elif backend == "seatbelt":
            if not Path(SEATBELT).exists():
                _available = "в системе нет sandbox-exec"
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    res = run_seatbelt("true", Path(tmp), timeout=10)
                _available = "" if res.passed else f"sandbox-exec не запускается: {res.output.strip() or res.error}"[:200]
        elif sys.platform.startswith("win"):
            _available = "на Windows песочницы нет: запусти редактор в WSL (Linux внутри Windows), там она работает"
        else:
            _available = "песочница есть только на Linux и macOS"
    return _available


def experimental() -> str:
    """Предупреждение для человека, если песочница этой системы проверена хуже."""
    return ("песочница macOS (sandbox-exec) экспериментальная: её проверяли только тестами профиля, "
            "не на настоящем Mac" if kind() == "seatbelt" else "")


def _sb(path) -> str:
    """Путь для профиля sandbox-exec: настоящий (macOS прячет /var за /private/var), в кавычках."""
    real = os.path.realpath(path)
    return '"' + real.replace("\\", "\\\\").replace('"', '\\"') + '"'


# системные папки macOS, которые нужны, чтобы запускались sh и Python (только чтение)
SEATBELT_SYSTEM = ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/etc", "/private/var/db/dyld",
                   "/dev", "/opt/homebrew", "/usr/local")
# службы macOS, без которых не работают простые программы; буфер обмена и связка ключей сюда не входят
SEATBELT_SERVICES = ("com.apple.system.logger", "com.apple.system.notification_center",
                     "com.apple.system.opendirectoryd.libinfo")


def seatbelt_profile(work: Path, tmp: Path, venv: Path | None = None, extra_ro=(), hide=()) -> str:
    """Профиль sandbox-exec: запрещено всё, кроме перечисленного. Ошибка в списке ломает запуск, а не защиту.
    hide - что в разрешённом прятать (venv_shadows): правило запрета идёт последним, оно сильнее."""
    readable = [*SEATBELT_SYSTEM, work, tmp, *extra_ro]
    if venv is not None:
        readable.append(venv)
        base = venv_base(venv)
        if base is not None:
            readable.append(base)
    reads = " ".join(f"(subpath {_sb(p)})" for p in readable)
    services = " ".join(f'(global-name "{s}")' for s in SEATBELT_SERVICES)
    return "\n".join([
        "(version 1)", "(deny default)",
        "(allow process-fork) (allow process-exec) (allow signal (target same-sandbox))",
        "(allow sysctl-read) (allow file-read-metadata) (allow ipc-posix-shm)",
        f"(allow mach-lookup {services})",
        f'(allow file-read* (literal "/") {reads})',
        f'(allow file-write* (subpath {_sb(work)}) (subpath {_sb(tmp)}) (literal "/dev/null") (literal "/dev/zero")'
        ' (regex #"^/dev/tty"))',
        "(deny network*)",
        *([f"(deny file-read* {' '.join(('(subpath ' if Path(p).is_dir() else '(literal ') + _sb(p) + ')' for p in hide)})"]
          if hide else []),
    ])


def seatbelt_argv(command: str, work: Path, tmp: Path, venv: Path | None = None, extra_ro=(), hide=()) -> list[str]:
    path = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    env = [f"HOME={os.path.realpath(tmp)}", f"TMPDIR={os.path.realpath(tmp)}", "LANG=en_US.UTF-8",
           "PYTHONDONTWRITEBYTECODE=1"]
    if venv is not None:
        path = f"{venv}/bin:{path}"
        env.append(f"VIRTUAL_ENV={venv}")
    return ["/usr/bin/env", "-i", f"PATH={path}", *env, SEATBELT, "-p",
            seatbelt_profile(work, tmp, venv, extra_ro, hide), "/bin/sh", "-c", command]


def run_seatbelt(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None,
                 timeout: float = TIMEOUT, hide=()) -> checks.CheckResult:
    links = []
    for name, real in (extra_ro or {}).items():  # node_modules: ссылкой, читать можно по разрешению в профиле
        link = work / name
        if not link.exists():
            link.symlink_to(real)
            links.append(link)
    with tempfile.TemporaryDirectory(prefix="aiedit-tmp-") as tmp:
        res = checks.run_command(shlex.join(seatbelt_argv(command, work, Path(tmp), venv,
                                                          list((extra_ro or {}).values()), hide)), work, timeout)
    for link in links:
        link.unlink(missing_ok=True)
    res.command = command
    return res


def run_sandboxed(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None,
                  timeout: float = TIMEOUT, hide=()) -> checks.CheckResult:
    return (run_seatbelt if kind() == "seatbelt" else run_bwrap)(command, work, venv, extra_ro, timeout, hide=hide)


def bwrap_argv(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None,
               hide=()) -> list[str]:
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
        for p in hide:  # копии кода самого проекта в venv: папку закрываем пустой, файл - пустым файлом
            argv += ["--tmpfs", str(p)] if Path(p).is_dir() else ["--ro-bind", "/dev/null", str(p)]
    for name, real in (extra_ro or {}).items():
        argv += ["--ro-bind", str(real), f"{WORK}/{name}"]
    return argv + ["--chdir", WORK, "--clearenv", "--setenv", "PATH", path, *env, "/bin/sh", "-c", command]


def run_bwrap(command: str, work: Path, venv: Path | None = None, extra_ro: dict[str, Path] | None = None,
              timeout: float = TIMEOUT, hide=()) -> checks.CheckResult:
    res = checks.run_command(shlex.join(bwrap_argv(command, work, venv, extra_ro, hide)), work, timeout)
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


def project_names(root: Path) -> set[str]:
    """Имена модулей и пакетов верхнего уровня самого проекта: в корне и в src/."""
    names: set[str] = set()
    for base in (root, root / "src"):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            if e.is_file() and e.suffix in PY_SUFFIXES:
                names.add(e.stem)
            elif e.is_dir() and any(e.glob("*.py")):
                names.add(e.name)
    return names


def _installed_from(dist: Path, root: Path) -> bool:
    """Пакет поставлен из папки проекта копией (обычный pip install ., не -e)."""
    try:
        data = json.loads((dist / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    url = str(data.get("url", ""))
    if not url.startswith("file://") or (data.get("dir_info") or {}).get("editable"):
        return False
    src = Path(os.path.realpath(urllib.parse.unquote(urllib.parse.urlsplit(url).path)))
    return src.is_relative_to(root)


def _record_names(dist: Path) -> set[str]:
    """Что пакет положил в site-packages (по его списку файлов RECORD): имена верхнего уровня."""
    try:
        rows = (dist / "RECORD").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    tops = {row.split(",", 1)[0].split("/", 1)[0] for row in rows}
    return {t.split(".")[0] for t in tops if t and t not in ("..", "__pycache__") and not t.endswith(".dist-info")}


def venv_shadows(root: Path, venv: Path | None) -> list[Path]:
    """Копии кода самого проекта в его окружении, которые песочница прячет.

    Обычный `pip install .` (не -e) кладёт в site-packages полную копию проекта, с закрытыми зонами и закрытыми
    файлами. Окружение песочница подключает целиком (там библиотеки для тестов), поэтому такую копию надо закрыть,
    иначе `cat .venv/lib/.../модуль.py` покажет модели всё. Узнаём копию по имени (как модуль или пакет в корне
    проекта) и по записи pip, что пакет поставлен из папки проекта. Тесты в песочнице при этом импортируют
    модули из копии проекта: текущая папка в пути поиска стоит раньше site-packages."""
    if venv is None:
        return []
    root = Path(os.path.realpath(root))
    names = project_names(root)
    out: list[Path] = []
    for site in sorted(venv.glob("lib/python*/site-packages")):
        own = set(names)
        for dist in site.glob("*.dist-info"):
            if _installed_from(dist, root):
                own |= _record_names(dist)
        if not own:
            continue
        for e in sorted(site.iterdir()):
            if e.name == "__pycache__":
                out += [c for c in sorted(e.iterdir()) if c.name.split(".")[0] in own]
            elif not e.name.endswith((".dist-info", ".pth")) and (e.name if e.is_dir() else e.name.split(".")[0]) in own:
                out.append(e)
    return out


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


class ProjectCopy:
    """Копия проекта без закрытого кода (build_copy) с правками агента. Создаётся при первом prepare()
    и живёт до close(). Ей пользуются терминал и навигация по коду, каждый своей."""

    def __init__(self, ws: Workspace, prefix: str = "aiedit-sandbox-"):
        self.ws, self.prefix = ws, prefix
        self.tmp: tempfile.TemporaryDirectory | None = None
        self.synced: dict[Path, str | None] = {}  # что записано в копию; None - удалён
        self.skipped: list[str] = []
        self.closed: dict[str, dict] = {}  # файл -> {"names": закрытые имена interface, "hidden": строк hidden}

    @property
    def path(self) -> Path:
        return Path(self.tmp.name)

    def prepare(self) -> list[Path]:
        """Создаёт копию (если её ещё нет) и переносит в неё правки агента. Возвращает изменённые файлы копии."""
        if self.tmp is None:
            self.tmp = tempfile.TemporaryDirectory(prefix=self.prefix)
            self.skipped = build_copy(self.ws, self.path, self.closed)
        changed = []
        for path, st in self.ws.states.items():
            target = self.path / path.relative_to(self.ws.root)
            if st.deleted:
                if self.synced.get(path, "") is not None:
                    target.unlink(missing_ok=True)
                    self.synced[path] = None
                    changed.append(target)
                continue
            if st.base is not None and st.text == st.base and path not in self.synced:
                continue  # не менялся (если правку откатили, копию надо вернуть - тогда он в synced)
            text = sandbox_text(self.ws, st, self.closed)
            if self.synced.get(path) != text:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                self.synced[path] = text
                changed.append(target)
        return changed

    def close(self):
        if self.tmp is not None:
            self.tmp.cleanup()
            self.tmp = None


class Terminal:
    """Инструмент run_command. approve(команда) -> "run" | "deny" спрашивает человека;
    show(результат) показывает ему вывод. Копия создаётся при первой команде и живёт до close()."""

    def __init__(self, ws: Workspace, approve: Callable[[str], str], show: Callable[[checks.CheckResult], None] | None = None,
                 runner=None, timeout: float = TIMEOUT):
        self.ws, self.approve, self.show, self.runner, self.timeout = ws, approve, show, runner, timeout
        self.copy = ProjectCopy(ws)
        self.runs = 0

    @property
    def tmp(self):
        return self.copy.tmp

    @property
    def work(self) -> Path:
        return self.copy.path

    @property
    def skipped(self) -> list[str]:
        return self.copy.skipped

    def _prepare(self):
        self.copy.prepare()

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
        venv = project_venv(self.ws.root)
        if self.runner is not None:
            res = self.runner(command, self.work, venv, extra, self.timeout)
        else:
            res = run_sandboxed(command, self.work, venv, extra, self.timeout, hide=venv_shadows(self.ws.root, venv))
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
        return f"$ {res.command}\n{status}\n{output or '(no output)'}{note}{closed_notice(self.copy.closed)}"

    def close(self):
        self.copy.close()
