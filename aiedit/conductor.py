"""Дирижёр: один быстрый запрос к Sonnet 5.5 разбивает задачу на шаги и раздаёт их исполнителям.

План - короткий текстовый блок, поэтому разбор терпим к мусору: любой сбой даёт запасной план
из одного шага, а не ошибку. Дирижёр не читает файлы и ничего не правит: он видит только задачу,
реестр исполнителей, список файлов и обзор защищённых файлов.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from typing import Callable

from . import agent, analysis, orchestrator, policy, prompts, review as security
from .outbound import SendCancelled
from .providers import Provider
from .roster import Roster, Worker
from .workspace import Workspace

MAX_TASKS = 6
MAX_GOAL = 600

CONDUCTOR_SYSTEM = """Mode: conductor. Split the user's request into the fewest steps and assign each step to one worker. \
Be fast: no analysis, no prose, output only the plan block.

Output format, exactly:
@@plan
summary: <one short sentence>
@@task t1
worker: <worker name from WORKERS>
goal: <imperative, self-contained, one or two sentences>
files: <comma-separated paths from PROJECT FILES, optional>
needs: <comma-separated task ids, optional>
risk: low|medium|high
@@task t2
...
@@end

Rules:
- 1 to 6 steps. A small request is ONE step. Never split what a single worker does well in one pass.
- A worker with a specialty is the best choice for steps in that area: give it every such step, even a small one. \
Use generalists for the rest.
- Among generalists choose the cheapest that can do the step well. Use the strongest for hard reasoning, security \
or money related code, and edits in or next to PROTECTED files. Use the cheapest for mechanical edits, docs, \
simple tests and read-only questions.
- Add `needs` only for a real dependency, such as a step that needs files another step creates. Steps without \
`needs` are independent.
- Mention only paths that appear in PROJECT FILES or that a step will create. Never invent others.
- Goals are concrete and in English. The worker sees only its goal, so include what it needs to know.
""" + prompts.REPLY_ENGLISH

_FIELD = re.compile(r"^(worker|goal|files|needs|risk|summary)\s*:\s*(.*)$", re.I)


@dataclass
class Task:
    id: str
    worker: str
    goal: str
    files: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    risk: str = "medium"


@dataclass
class Plan:
    summary: str
    tasks: list[Task]
    notes: list[str] = field(default_factory=list)  # что пришлось поправить в плане (для человека)
    fallback: bool = False
    raw: str = ""


def build_request(task: str, ws: Workspace, roster: Roster, open_file: Path | None = None) -> str:
    """Сообщение дирижёру: задача, исполнители, защищённые файлы, дерево проекта."""
    overview = ws.protected_overview() or prompts.NONE
    tree = ws.list_files(".", 2)
    opened = f"\nOPEN FILE: {ws.rel(open_file)}" if open_file and ws.resolve(str(open_file)) else ""
    return (f"REQUEST:\n{task}\n\nWORKERS:\n{roster.describe()}\n\nPROTECTED (zones):\n{overview}\n\n"
            f"PROJECT FILES (2 levels):\n{tree}{opened}")


def _fields(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    key = None
    for line in block.split("\n"):
        m = _FIELD.match(line.strip())
        if m:
            key = m[1].lower()
            out[key] = m[2].strip()
        elif key and line.strip() and not line.strip().startswith("@@"):
            out[key] += " " + line.strip()  # перенос длинной цели на следующую строку
    return out


def _split_list(value: str) -> list[str]:
    return [p for p in re.split(r"[,\s;]+", value or "") if p and p.lower() not in ("none", "-", "n/a")]


def _acyclic(tasks: list[Task]) -> bool:
    done: set[str] = set()
    pending = list(tasks)
    while pending:
        ready = [t for t in pending if all(n in done for n in t.needs)]
        if not ready:
            return False
        done.update(t.id for t in ready)
        pending = [t for t in pending if t.id not in done]
    return True


def parse_plan(text: str, ws: Workspace, roster: Roster, request: str, protected: bool = False) -> Plan:
    notes: list[str] = []
    body = text.partition("@@plan")[2] if "@@plan" in text else ""
    body = body.partition("@@end")[0]
    head, *blocks = re.split(r"^@@task\b[^\n]*$", body, flags=re.M)
    summary = _fields(head).get("summary", "")
    tasks: list[Task] = []
    for raw in blocks:
        f = _fields(raw)
        goal = f.get("goal", "").strip()
        if not goal:
            continue
        risk = f.get("risk", "").lower()
        risk = risk if risk in ("low", "medium", "high") else "medium"
        worker = roster.get_builder(f.get("worker", ""))
        if worker is None:
            worker = roster.for_risk(risk)
            notes.append(f"unknown worker {f.get('worker', '')!r} replaced by {worker.key}")
        files = [p for p in _split_list(f.get("files", "")) if ws.resolve(p) is not None]
        tasks.append(Task(f"t{len(tasks) + 1}", worker.key, goal[:MAX_GOAL], files, [], risk))
        tasks[-1].needs = _split_list(f.get("needs", ""))
        if len(tasks) == MAX_TASKS:
            break
    ids = {t.id for t in tasks}
    for t in tasks:
        t.needs = [n.lower() for n in t.needs if n.lower() in ids and n.lower() != t.id]
    if not _acyclic(tasks):
        for t in tasks:
            t.needs = []
        notes.append("circular dependencies dropped")
    if not tasks:
        worker = roster.for_risk("high" if protected else "medium")
        return Plan(request[:MAX_GOAL], [Task("t1", worker.key, request[:MAX_GOAL], [], [], "medium")],
                    ["plan was unreadable, running the request as one step"], True, text)
    return Plan(summary or tasks[0].goal[:120], tasks, notes, False, text)


def ordered(plan: Plan) -> list[Task]:
    """Порядок выполнения: зависимости раньше, иначе как в плане."""
    done: set[str] = set()
    out: list[Task] = []
    pending = list(plan.tasks)
    while pending:
        nxt = next(t for t in pending if all(n in done for n in t.needs))
        out.append(nxt)
        done.add(nxt.id)
        pending.remove(nxt)
    return out


def make_plan(task: str, ws: Workspace, roster: Roster, provider: Provider, history: list | None = None,
              open_file: Path | None = None) -> Plan:
    """Один запрос дирижёру. Любая ошибка разбора даёт запасной план, а сбой связи пробрасывается."""
    request = build_request(task, ws, roster, open_file)
    messages = orchestrator.build_messages(history, request)
    saved, ws.model = ws.model, provider.name
    ws.log("conductor", "", None, chars=len(CONDUCTOR_SYSTEM) + sum(len(m["content"]) for m in messages))
    ws.model = saved
    reply = provider.complete(CONDUCTOR_SYSTEM, messages)
    return parse_plan(reply, ws, roster, task, protected=bool(ws.protected_overview()))


MAX_TOTAL_STEPS = 60


@dataclass
class TaskResult:
    task: Task
    status: str  # done | failed | skipped | stopped
    text: str = ""
    steps: int = 0


@dataclass
class ConductResult(agent.AgentResult):
    plan: Plan | None = None
    results: list[TaskResult] = field(default_factory=list)


def task_prompt(task: str, plan: Plan, t: Task, index: int, done: list[TaskResult]) -> str:
    """Что видит исполнитель: свою цель, общий запрос и итоги уже сделанных шагов. Больше ничего."""
    lines = [t.goal]
    if t.files:
        lines.append("Files: " + ", ".join(t.files))
    lines.append(f"Overall request: {task[:500]}")
    lines.append(f"Plan: {plan.summary}. You are step {index} of {len(plan.tasks)}; do only this step.")
    finished = [r for r in done if r.status == "done" and r.text]
    if finished:
        lines.append("Done so far:\n" + "\n".join(f"- {r.task.id} ({r.task.worker}): {r.text[:300]}" for r in finished[-4:]))
    return "\n".join(lines)


def summarize(plan: Plan, results: list[TaskResult]) -> str:
    if len(results) == 1 and results[0].status == "done":
        return results[0].text
    lines = [f"Plan: {plan.summary}"]
    for r in results:
        head = f"- {r.task.id} [{r.task.worker}] "
        if r.status == "done":
            lines.append(head + (r.text.strip().split("\n\n")[0][:400] or "done"))
        elif r.status == "skipped":
            lines.append(head + "SKIPPED: an earlier step it depends on did not finish")
        elif r.status == "stopped":
            lines.append(head + "STOPPED")
        else:
            lines.append(head + "FAILED: " + (r.text[:200] or "error"))
    return "\n".join(lines)


def run(task: str, ws: Workspace, roster: Roster, conductor: Provider, history: list | None = None,
        open_file: Path | None = None, on_progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None, english_reply: bool = False,
        review_on: bool = True, analyze_on: bool = True, ask=None, terminal=None) -> ConductResult:
    """План от дирижёра, затем шаги по порядку у назначенных исполнителей на общей рабочей области.
    Потом, если подключён проверяющий, он смотрит все правки, а серьёзные замечания уходят их авторам.
    ask - вопрос человеку: исполнитель может переспросить посреди своего шага.
    terminal - песочница для команд, общая для всех шагов (копия с правками предыдущих шагов)."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    say("дирижёр составляет план...")
    plan = make_plan(task, ws, roster, conductor, history, open_file)
    say(f"план: {plan.summary}")
    for note in plan.notes:
        say(f"план: {note}")
    for t in plan.tasks:
        say(f"{t.id} → {t.worker}: {t.goal[:110]}")
    out = ConductResult("", [], plan=plan)
    results: list[TaskResult] = []
    status: dict[str, str] = {}
    authors: dict[Path, "Worker"] = {}  # кто последним менял файл: ему вернут замечания
    for index, t in enumerate(ordered(plan), 1):
        if stop():
            out.stopped = "остановлено пользователем"
            results.append(TaskResult(t, "stopped"))
            status[t.id] = "stopped"
            continue
        if any(status.get(n) != "done" for n in t.needs):
            results.append(TaskResult(t, "skipped"))
            status[t.id] = "skipped"
            continue
        worker = roster.get_builder(t.worker) or roster.for_risk(t.risk)
        left = MAX_TOTAL_STEPS - out.steps
        if left <= 0:
            out.stopped = out.stopped or "достигнут общий лимит шагов"
            results.append(TaskResult(t, "stopped"))
            status[t.id] = "stopped"
            continue
        ws.model = worker.provider.name
        before = {p: st.text for p, st in ws.states.items()}
        try:
            r = agent.run(task_prompt(task, plan, t, index, results), ws, worker.provider, None, open_file,
                          on_progress=lambda note, tid=t.id: say(f"{tid}: {note}"), should_stop=should_stop,
                          english_reply=english_reply, max_steps=min(agent.MAX_STEPS, left), ask=ask,
                          terminal=terminal)
        except (policy.AccessDenied, SendCancelled):
            raise
        except Exception as e:  # сбой одного исполнителя не должен ронять весь план
            results.append(TaskResult(t, "failed", f"{type(e).__name__}: {e}"))
            status[t.id] = "failed"
            say(f"{t.id}: ошибка исполнителя {worker.key}: {type(e).__name__}: {e}")
            continue
        out.steps += r.steps
        authors.update({p: worker for p, st in ws.states.items() if st.text != before.get(p, st.base)})
        out.calls += [f"{t.id}: {c}" for c in r.calls]
        text = r.text if r.from_model else f"stopped: {r.stopped}"
        results.append(TaskResult(t, "stopped" if r.stopped == "остановлено пользователем" else "done", text, r.steps))
        status[t.id] = results[-1].status
        if r.stopped == "остановлено пользователем":
            out.stopped = r.stopped
    out.results = results
    out.text = summarize(plan, results)
    out.text_en = ""
    if analyze_on and not out.stopped and ws.changes():  # сначала настоящие программы, потом безопасник
        out.analysis = analysis.run_cycle(task, ws, lambda p: authors.get(p) or roster.strongest(),
                                          on_progress, should_stop, english_reply)
    if review_on and not out.stopped and roster.reviewer() is not None and ws.changes():
        out.review = security.run_cycle(task, ws, roster, lambda p: authors.get(p) or roster.strongest(),
                                        on_progress, should_stop, english_reply)
    out.changes = ws.changes()
    out.authors = {ws.rel(p): w.key for p, w in authors.items()}
    return out
