"""Статический анализ правок настоящими программами (ruff, mypy, bandit, eslint, tsc).

Правки лежат в рабочей области и на диск ещё не записаны, поэтому анализ идёт на временной копии проекта:
сначала на исходном коде, потом на новом. Автору возвращаются только НОВЫЕ замечания в изменённых файлах,
чтобы он не правил чужие старые проблемы. Замечания в закрытых зонах отбрасываются, а в тексте остальных
закрытый код вычищается так же, как из вывода тестов.
"""
import os
import re
import shlex
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import agent, checks, guard, policy
from .workspace import SKIP_DIRS, Workspace

MAX_FILES = 3000
MAX_BYTES = 30_000_000
TIMEOUT = 60


@dataclass(frozen=True)
class Issue:
    tool: str
    file: str   # относительно корня проекта
    line: int
    code: str
    message: str

    def key(self) -> tuple:
        """Для сравнения «было / стало»: без номера строки, он сдвигается от правок."""
        return (self.tool, self.file, self.code, self.message)

    def text(self) -> str:
        return f"- {self.tool} {self.code} {self.file}:{self.line}: {self.message}"


def _lines(text: str, rx: str, make: Callable[[re.Match], Issue | None]) -> list[Issue]:
    found = []
    for m in re.finditer(rx, text, re.M):
        issue = make(m)
        if issue is not None:
            found.append(issue)
    return found


def _norm(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def parse_ruff(out: str) -> list[Issue]:
    return _lines(out, r"^(?P<f>.+?):(?P<l>\d+):\d+: (?P<c>[A-Z]+\d+) (?:\[\*\] )?(?P<m>.+)$",
                  lambda m: Issue("ruff", _norm(m["f"]), int(m["l"]), m["c"], m["m"].strip()))


def parse_mypy(out: str) -> list[Issue]:
    def make(m):
        if m["k"] == "note":
            return None
        return Issue("mypy", _norm(m["f"]), int(m["l"]), m["c"] or m["k"], m["m"].strip())
    return _lines(out, r"^(?P<f>.+?):(?P<l>\d+):(?:\d+:)? (?P<k>error|warning|note): (?P<m>.*?)(?:  \[(?P<c>[\w-]+)\])?$", make)


def parse_bandit(out: str) -> list[Issue]:
    def make(m):
        return None if m["s"] == "LOW" else Issue("bandit", _norm(m["f"]), int(m["l"]), m["c"], f"[{m['s']}] {m['m'].strip()}")
    return _lines(out, r"^(?P<f>.+?):(?P<l>\d+): (?P<s>LOW|MEDIUM|HIGH): (?P<c>B\d+) (?P<m>.+)$", make)


def parse_eslint(out: str) -> list[Issue]:
    return _lines(out, r"^(?P<f>.+?):(?P<l>\d+):\d+: (?P<m>.+?) \[(?:Error|Warning)/(?P<c>[^\]]+)\]$",
                  lambda m: Issue("eslint", _norm(m["f"]), int(m["l"]), m["c"], m["m"].strip()))


def parse_tsc(out: str) -> list[Issue]:
    return _lines(out, r"^(?P<f>.+?)\((?P<l>\d+),\d+\): error (?P<c>TS\d+): (?P<m>.+)$",
                  lambda m: Issue("tsc", _norm(m["f"]), int(m["l"]), m["c"], m["m"].strip()))


@dataclass(frozen=True)
class Analyzer:
    key: str
    suffixes: tuple[str, ...]
    args: tuple[str, ...]           # аргументы перед списком файлов
    parse: Callable[[str], list[Issue]]
    binary: str = ""                # имя программы; по умолчанию key
    project_wide: bool = False      # tsc проверяет весь проект, а не список файлов
    needs: str = ""                 # файл настройки, без которого запускать бессмысленно (tsconfig.json)

    @property
    def exe(self) -> str:
        return self.binary or self.key


ANALYZERS: list[Analyzer] = [
    Analyzer("ruff", (".py",), ("check", "--output-format=concise", "--no-cache"), parse_ruff),
    Analyzer("mypy", (".py",), ("--ignore-missing-imports", "--no-error-summary", "--show-column-numbers",
                                "--no-color-output", "--no-incremental"), parse_mypy),
    Analyzer("bandit", (".py",), ("-q", "-f", "custom", "--msg-template", "{relpath}:{line}: {severity}: {test_id} {msg}"),
             parse_bandit),
    Analyzer("eslint", (".js", ".jsx", ".ts", ".tsx", ".mjs"), ("--format", "unix", "--no-warn-ignored"), parse_eslint),
    Analyzer("tsc", (".ts", ".tsx"), ("--noEmit", "--pretty", "false", "-p", "."), parse_tsc,
             project_wide=True, needs="tsconfig.json"),
]


def find_binary(name: str, root: Path) -> str | None:
    """Программа из окружения проекта (.venv, node_modules) или из PATH."""
    for cand in (root / ".venv" / "bin" / name, root / "node_modules" / ".bin" / name):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which(name)


class TooBig(Exception):
    pass


def build_shadow(ws: Workspace, dest: Path) -> None:
    """Копия видимых файлов проекта. Закрытые, секретные и служебные файлы в неё не попадают."""
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(ws.root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d == "node_modules":  # тяжёлая папка нужна eslint/tsc: подключаем ссылкой, не копируем
                (dest / here.relative_to(ws.root) / d).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(here / d, dest / here.relative_to(ws.root) / d)
            elif d not in SKIP_DIRS and ws.visible(Path(os.path.realpath(here / d))):
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            src = here / name
            real = Path(os.path.realpath(src))
            if not real.is_relative_to(ws.root) or not ws.visible(real) or policy.is_private_name(name):
                continue
            files += 1
            size += src.stat().st_size
            if files > MAX_FILES or size > MAX_BYTES:
                raise TooBig()
            target = dest / src.relative_to(ws.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def _argv(an: Analyzer, binary: str, rels: list[str], shadow: Path) -> list[str] | None:
    if an.project_wide:
        return [binary, *an.args] if (shadow / an.needs).is_file() else None
    files = [f"./{r}" for r in rels if r.endswith(an.suffixes)]  # ./ защищает от файлов, начинающихся с «-»
    return [binary, *an.args, *files] if files else None


def default_runner(argv: list[str], cwd: Path) -> tuple[int | None, str]:
    res = checks.run_command(shlex.join(argv), cwd, TIMEOUT)
    return (None if res.error or res.timed_out else res.code), res.output


def run_tools(shadow: Path, rels: list[str], analyzers: list[Analyzer], root: Path, runner, notes: list[str],
              ran: set[str]) -> list[Issue]:
    """Запускает доступные программы на копии. Вывод берётся только по изменённым файлам."""
    found: list[Issue] = []
    for an in analyzers:
        binary = find_binary(an.exe, root) if runner is default_runner else an.exe
        argv = _argv(an, binary, rels, shadow) if binary else None
        if binary is None:
            if any(r.endswith(an.suffixes) for r in rels):
                note = f"{an.key}: программа не найдена"
                if note not in notes:
                    notes.append(note)
            continue
        if argv is None:
            continue
        code, out = runner(argv, shadow)
        if code is None:
            note = f"{an.key}: не запустился или не уложился во время"
            if note not in notes:
                notes.append(note)
            continue
        ran.add(an.key)
        found += [i for i in an.parse(out) if i.file in rels]
    return found


@dataclass
class AnalysisRound:
    issues: list[Issue]
    ignored: int = 0  # сколько замечаний пришлось на закрытые зоны и не показано автору


@dataclass
class AnalysisReport:
    rounds: list[AnalysisRound] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)   # какие программы реально отработали
    notes: list[str] = field(default_factory=list)   # что не нашлось или не запустилось (для человека)
    skipped: str = ""
    stopped: str = ""
    stalled: str = ""  # почему цикл остановился, не добившись чистого анализа (то же замечание повторилось, лимит)
    fixes: int = 0     # сколько раз автор реально изменил код по замечаниям

    @property
    def remaining(self) -> list[Issue]:
        return self.rounds[-1].issues if self.rounds else []

    @property
    def fixed_rounds(self) -> int:
        return self.fixes


def _rels(ws: Workspace, analyzers: list[Analyzer]) -> tuple[list, list[str], list[str]]:
    """(изменения, пути для анализа, из них изменённые, а не новые)."""
    suffixes = tuple(sfx for an in analyzers for sfx in an.suffixes)
    changes = [c for c in ws.changes() if c.path.name.endswith(suffixes) and not c.deleted]
    rels = [ws.rel(c.path) for c in changes]
    return changes, rels, [ws.rel(c.path) for c in changes if not c.created]


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _closed_lines(ws: Workspace, rel: str, n_lines: int) -> set[int]:
    """Строки файла (с 1), которые автор менять не может: hidden, interface, readonly."""
    st = ws.states.get(ws.root / rel)
    if st is None:
        return set()
    lr = guard.line_regions(n_lines, ws.view_regions(st))
    return {i + 1 for i, r in enumerate(lr) if r is not None and (r.level in guard.CONCEALED or r.level == "readonly")}


def analyze_once(ws: Workspace, analyzers: list[Analyzer] | None = None, runner=None
                 ) -> tuple[AnalysisRound | None, set[str], list[str], str]:
    """(раунд, какие программы отработали, заметки, причина пропуска). Раунд None, если анализировать нечего."""
    analyzers = ANALYZERS if analyzers is None else analyzers
    runner = runner or default_runner
    changes, rels, modified = _rels(ws, analyzers)
    if not changes:
        return None, set(), [], "no analyzable files changed"
    notes: list[str] = []
    ran: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="aiedit-shadow-") as tmp:
        shadow = Path(tmp)
        try:
            build_shadow(ws, shadow)
        except TooBig:
            return None, set(), [], "project is too big to copy for analysis"
        for c in changes:  # исходный вид изменённых файлов; новые файлы в копии ещё не существуют
            if c.created:
                (shadow / ws.rel(c.path)).unlink(missing_ok=True)
            else:
                _write(shadow, ws.rel(c.path), c.old or "")
        before = Counter(i.key() for i in run_tools(shadow, modified, analyzers, ws.root, runner, notes, ran))
        for c in changes:
            _write(shadow, ws.rel(c.path), c.new)
        current = run_tools(shadow, rels, analyzers, ws.root, runner, notes, ran)
    fresh: list[Issue] = []
    for issue in current:
        if before[issue.key()] > 0:
            before[issue.key()] -= 1  # такое замечание было и до правки
        else:
            fresh.append(issue)
    texts = {ws.rel(c.path): c.new for c in changes}
    ignored, kept = 0, []
    closed = {rel: _closed_lines(ws, rel, len(text.split("\n"))) for rel, text in texts.items()}
    concealed = {l.strip() for st in ws.states.values() for l in _concealed_text(ws, st)}
    tokens = checks.protected_tokens(ws.root, concealed)
    for issue in fresh:
        if issue.line in closed.get(issue.file, set()):
            ignored += 1
            continue
        clean = checks.scrub(issue.message, concealed, tokens)
        kept.append(issue if clean == issue.message else
                    Issue(issue.tool, issue.file, issue.line, issue.code, "details hidden: refers to closed code"))
    return AnalysisRound(kept, ignored), ran, notes, ""


def _concealed_text(ws: Workspace, st) -> list[str]:
    lines = st.text.split("\n")
    lr = guard.line_regions(len(lines), ws.view_regions(st))
    return [l for l, r in zip(lines, lr) if r is not None and r.level in guard.CONCEALED]


MAX_ROUNDS = 5

FIX_PROMPT = """Static analysis found NEW problems introduced by your changes. Fix them with the smallest correct \
edits. You may edit the listed files and the other files this task already created or changed. Do not hide a \
warning with noqa or ignore comments unless the warning is wrong, and then say so in your final answer.
When you are done, check your own work: if the sandbox terminal is available, run the project's tests or the \
program and make sure your fixes did not break anything. Say plainly what you ran and what you could not verify.
Original task: {task}
Problems:
{issues}"""


def _fix(task: str, ws: Workspace, author, issues: list[Issue], on_progress, should_stop, english_reply: bool,
         terminal=None) -> str:
    scope = {p for i in issues if (p := ws.resolve(i.file)) is not None} | {c.path for c in ws.changes()}
    prompt = FIX_PROMPT.format(task=task[:6000], issues="\n".join(i.text() for i in issues))
    ws.edit_scope, saved = scope, ws.model
    ws.model = author.provider.name
    try:
        res = agent.run(prompt, ws, author.provider, None, None, on_progress, should_stop,
                        english_reply=english_reply, max_steps=25, terminal=terminal)
    finally:
        ws.edit_scope, ws.model = None, saved
    return res.text if res.from_model else ""


def run_cycle(task: str, ws: Workspace, author_of, on_progress: Callable[[str], None] | None = None,
              should_stop: Callable[[], bool] | None = None, english_reply: bool = False, max_rounds: int = MAX_ROUNDS,
              analyzers: list[Analyzer] | None = None, runner=None, terminal=None) -> AnalysisReport:
    """Анализ, возврат новых замечаний авторам (Claude), повторный анализ, и так пока новых замечаний нет.
    Раньше останавливается, если те же замечания не уходят или автор ничего не меняет."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    report = AnalysisReport()
    seen: list[frozenset] = []
    for round_no in range(max_rounds + 1):
        if stop():
            report.stopped = "остановлено пользователем"
            break
        say("статанализ: запускаю программы" + (f" (круг {round_no})" if round_no else ""))
        rnd, ran, notes, skipped = analyze_once(ws, analyzers, runner)
        report.notes += [n for n in notes if n not in report.notes]
        report.tools = sorted(set(report.tools) | ran)
        if rnd is None:
            report.skipped = skipped
            break
        report.rounds.append(rnd)
        if not rnd.issues:
            break
        if round_no == max_rounds:
            report.stalled = f"достигнут предел кругов статанализа ({max_rounds}), замечания остались"
            break
        sig = frozenset(i.key() for i in rnd.issues)
        if sig in seen:
            report.stalled = "те же замечания статанализа повторились после исправления"
            break
        seen.append(sig)
        say(f"статанализ: новых замечаний {len(rnd.issues)}, возвращаю авторам")
        before = ws.review_diff()
        by_author: dict[str, tuple] = {}
        for issue in rnd.issues:
            real = ws.resolve(issue.file)
            worker = author_of(real) if real is not None else author_of(None)
            by_author.setdefault(worker.key, (worker, []))[1].append(issue)
        for worker, items in by_author.values():
            _fix(task, ws, worker, items, on_progress, should_stop, english_reply, terminal)
        if ws.review_diff() == before:
            report.stalled = "автор не изменил код"
            break
        report.fixes += 1
    return report
