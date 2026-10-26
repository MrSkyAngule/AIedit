"""Проверка безопасности: независимая модель читает правки, находит ошибки и возвращает их автору.

Цикл: проверяющий смотрит набор правок и пишет замечания, серьёзные уходят автору кода на исправление.
Автор правит файлы замечаний и другие файлы этой же задачи (чтобы согласовать код между файлами), затем сам
запускает проверку в песочнице и перечитывает свой дифф. После этого проверяющий смотрит снова. Так до тех пор,
пока замечаний нет, но не больше MAX_ROUNDS кругов, и раньше, если автор не может исправить одно и то же.
Проверяющий работает в режиме «только чтение» и закрытого кода не видит, поэтому отчёт всегда называет,
что осталось непроверенным.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import agent
from .roster import Roster, Worker
from .workspace import Workspace

MAX_ROUNDS = 5
REVIEW_STEPS = 12
FIX_STEPS = 25
VERIFY_STEPS = 15
SEVERITIES = ("critical", "high", "medium", "low")
TASK_LIMIT = 6000  # сколько текста задачи видят проверяющий и автор (раньше 600: требования из конца ТЗ терялись)
ACTIONABLE = ("critical", "high", "medium")  # низкие попадают в отчёт, но автору автоматически не уходят

REVIEW_SYSTEM = """Mode: security review. You review proposed code changes for security and logic flaws. \
You work through read-only editor tools and cannot edit anything.

To call a tool write a block (several per reply are allowed):

@@tool NAME
argument: value
@@end

Tools: list_files (path, depth), read_file (path, start_line, end_line), search (query, path, case). \
The editor answers with `@@result NAME` ... `@@end` blocks.

Review the CHANGES diff first, then read the surrounding code and anything the change calls or is called by. \
Look for: injection, missing authorization or validation, unsafe handling of secrets, money or limit logic errors, \
race conditions, wrong error handling, and logic that does not do what the task asked.
Also check the code against the EXPLICIT requirements in the task text (endpoints, field names, headers, status codes, \
limits, behavior). Go through them one by one. Any requirement that is missing or implemented differently (for \
example a different header name or a missing check that the task demanded) is at least medium, and a missing access \
check is high. Do not mark such a deviation low.

When done, answer with exactly one block and nothing else:
@@findings
summary: <one short sentence>
@@finding f1
severity: critical|high|medium|low
file: <path>
line: <number, optional>
issue: <what is wrong and why it matters>
fix: <how to fix it, briefly>
@@end
If there is nothing to report, write `summary: no issues found` and no findings.

Rules:
- Report only real problems in the changed code or code it directly affects. No style remarks.
- critical: exploitable now or loses data or money. high: serious flaw. medium: real but limited. low: minor.
- Lines like <<ai-hidden:N:K lines>> are code you cannot see. Never guess what is inside; do not report on it.
- Be concrete: name the file and the line."""

FIX_PROMPT = """Fix these review findings in the proposed changes. You may edit the listed files and the other \
files this task already created or changed (so that routes, templates, names and imports stay consistent); \
make the smallest correct fix and do not refactor. If you are sure a finding is wrong, do not edit for it and \
explain why in your final answer.

Then check your own work before you answer:
1. Re-read your changes once and look for mismatches between files (a route and the template that calls it, \
a renamed function and its callers, imports, configuration names).
2. If the sandbox terminal is available, run the project's tests (or the program) and fix any failure your \
change caused. Do not weaken or delete a test to make it pass.
3. In your final answer say what you changed, what you ran and what the result was. Say plainly what you \
could not verify.
Original task: {task}
Findings:
{findings}"""

VERIFY_PROMPT = """You fixed review findings but did not run anything to check it. Run the project's tests (or the \
program) in the sandbox now. If something fails because of your changes, fix it and run again. Do not weaken or \
delete a test to make it pass. Answer with what you ran and the result, and say plainly what you could not verify.
Original task: {task}"""

_FIELD = re.compile(r"^(severity|file|line|issue|fix|summary)\s*:\s*(.*)$", re.I)


@dataclass
class Finding:
    id: str
    severity: str
    file: str
    line: int | None
    issue: str
    fix: str = ""

    def text(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"- {self.id} [{self.severity}] {where}: {self.issue}" + (f" Suggested: {self.fix}" if self.fix else "")


@dataclass
class Round:
    summary: str
    findings: list[Finding]
    unparsed: bool = False          # проверяющий ответил не в формате: замечаний мы не знаем
    fix_notes: list[str] = field(default_factory=list)  # что ответили авторы на замечания


@dataclass
class ReviewReport:
    reviewer: str
    rounds: list[Round] = field(default_factory=list)
    gaps: list[tuple[str, int, int]] = field(default_factory=list)  # (файл, скрытых строк, всего)
    skipped: str = ""
    stopped: str = ""
    stalled: str = ""  # причина, по которой цикл остановился, не добившись чистой проверки (повтор замечаний, лимит)
    fixes: int = 0     # сколько раз автор реально изменил код по замечаниям

    @property
    def remaining(self) -> list[Finding]:
        return self.rounds[-1].findings if self.rounds else []

    @property
    def unparsed(self) -> bool:
        return bool(self.rounds) and self.rounds[-1].unparsed

    @property
    def fixed_rounds(self) -> int:
        return self.fixes


def parse_findings(text: str, ws: Workspace) -> tuple[str, list[Finding], bool]:
    """(итог, замечания, разобрано ли). Замечания про закрытые или чужие файлы отбрасываются."""
    if "@@findings" not in text:
        return "", [], False
    body = text.partition("@@findings")[2].partition("@@end")[0]
    head, *blocks = re.split(r"^@@finding\b[^\n]*$", body, flags=re.M)
    summary = ""
    for line in head.split("\n"):
        m = _FIELD.match(line.strip())
        if m and m[1].lower() == "summary":
            summary = m[2].strip()
    found: list[Finding] = []
    for raw in blocks:
        f: dict[str, str] = {}
        key = None
        for line in raw.split("\n"):
            m = _FIELD.match(line.strip())
            if m:
                key = m[1].lower()
                f[key] = m[2].strip()
            elif key and line.strip() and not line.strip().startswith("@@"):
                f[key] += " " + line.strip()
        real = ws.resolve(f.get("file", ""))
        if not f.get("issue") or real is None:
            continue
        sev = f.get("severity", "").lower()
        line = re.search(r"\d+", f.get("line", ""))
        found.append(Finding(f"f{len(found) + 1}", sev if sev in SEVERITIES else "medium", ws.rel(real),
                             int(line[0]) if line else None, f["issue"][:500], f.get("fix", "")[:300]))
    return summary, found, True


def _provider_call(ws: Workspace, worker: Worker):
    saved = ws.model
    ws.model = worker.provider.name
    return saved


def review_once(task: str, ws: Workspace, reviewer: Worker, previous: list[Finding] | None = None,
                on_progress=None, should_stop=None, english_reply: bool = False) -> Round:
    """Один проход проверки. Рабочая область на это время становится «только чтение»."""
    prompt = ("Review these proposed changes for security and logic flaws.\n"
              f"Task the author was given: {task[:TASK_LIMIT]}\n\nCHANGES:\n{ws.review_diff() or '(none)'}")
    if previous:
        prompt += ("\n\nThese were reported before. Check whether each one is fixed and report it again if not:\n"
                   + "\n".join(f.text() for f in previous))
    ws.read_only, saved_model = True, _provider_call(ws, reviewer)
    try:
        res = agent.run(prompt, ws, reviewer.provider, None, None, on_progress, should_stop,
                        english_reply=english_reply, max_steps=REVIEW_STEPS, system=REVIEW_SYSTEM)
    finally:
        ws.read_only, ws.model = False, saved_model
    summary, findings, parsed = parse_findings(res.text if res.from_model else "", ws)
    return Round(summary, findings, unparsed=not parsed)


def _ran_command(res) -> bool:
    return any(c.startswith("запускаю в песочнице") for c in res.calls)


def fix_round(task: str, ws: Workspace, author: Worker, findings: list[Finding], on_progress=None,
              should_stop=None, english_reply: bool = False, terminal=None) -> str:
    """Автор исправляет замечания и сам проверяет результат. Править можно файлы из замечаний и остальные файлы,
    которые эта задача уже создала или изменила. Возвращает его ответ."""
    scope = {p for f in findings if (p := ws.resolve(f.file)) is not None} | {c.path for c in ws.changes()}
    prompt = FIX_PROMPT.format(task=task[:TASK_LIMIT], findings="\n".join(f.text() for f in findings))
    ws.edit_scope, saved_model = scope, _provider_call(ws, author)
    try:
        res = agent.run(prompt, ws, author.provider, None, None, on_progress, should_stop,
                        english_reply=english_reply, max_steps=FIX_STEPS, terminal=terminal)
        note = res.text if res.from_model else ""
        if terminal is not None and not _ran_command(res) and not (should_stop and should_stop()):
            on_progress and on_progress("автор не запускал проверку после исправления: прошу запустить")
            res2 = agent.run(VERIFY_PROMPT.format(task=task[:TASK_LIMIT]), ws, author.provider, None, None, on_progress,
                             should_stop, english_reply=english_reply, max_steps=VERIFY_STEPS, terminal=terminal)
            note = (note + "\n" + res2.text).strip() if res2.from_model else note
    finally:
        ws.edit_scope, ws.model = None, saved_model
    return note


def _signature(findings: list[Finding]) -> frozenset:
    """Те же замечания, что были: файл и начало текста (строки и формулировки у проверяющего чуть плавают)."""
    return frozenset((f.file, re.sub(r"\W+", " ", f.issue.lower())[:50]) for f in findings)


def run_cycle(task: str, ws: Workspace, roster: Roster, author_of: Callable[[Path], Worker],
              on_progress: Callable[[str], None] | None = None, should_stop: Callable[[], bool] | None = None,
              english_reply: bool = False, max_rounds: int = MAX_ROUNDS, terminal=None) -> ReviewReport:
    """Проверка, возврат серьёзных замечаний авторам (они исправляют и сами проверяют), повторная проверка, и так
    пока замечаний нет. Останавливается раньше, если автор ничего не меняет или замечания не уходят."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    reviewer = roster.reviewer()
    report = ReviewReport(reviewer.key if reviewer else "")
    if reviewer is None:
        report.skipped = "no reviewer connected"
        return report
    if not ws.changes():
        report.skipped = "no changes to review"
        return report
    previous: list[Finding] | None = None
    seen: list[frozenset] = []
    for round_no in range(max_rounds + 1):
        if stop():
            report.stopped = "остановлено пользователем"
            break
        say(f"безопасность: проверяет {reviewer.key}" + (f" (круг {round_no})" if round_no else ""))
        current = review_once(task, ws, reviewer, previous, on_progress, should_stop, english_reply)
        report.rounds.append(current)
        todo = [f for f in current.findings if f.severity in ACTIONABLE]
        if not todo:
            break
        if round_no == max_rounds:
            report.stalled = f"достигнут предел кругов проверки ({max_rounds}), замечания остались"
            break
        sig = _signature(todo)
        if sig in seen:
            report.stalled = "те же замечания повторились после исправления: автор не смог их убрать"
            break
        seen.append(sig)
        say(f"безопасность: замечаний к исправлению: {len(todo)}, возвращаю авторам")
        before = ws.review_diff()
        by_author: dict[str, tuple[Worker, list[Finding]]] = {}
        for f in todo:
            real = ws.resolve(f.file)
            worker = author_of(real) if real is not None else roster.strongest()
            by_author.setdefault(worker.key, (worker, []))[1].append(f)
        for worker, items in by_author.values():
            note = fix_round(task, ws, worker, items, on_progress, should_stop, english_reply, terminal)
            if note:
                current.fix_notes.append(f"{worker.key}: {note[:400]}")
        if ws.review_diff() == before:  # автор ничего не изменил (счёл замечания ложными): спорить дальше незачем
            report.stalled = "автор не изменил код: счёл замечания неверными"
            break
        report.fixes += 1
        previous = todo
    report.gaps = ws.coverage_gaps()
    return report
