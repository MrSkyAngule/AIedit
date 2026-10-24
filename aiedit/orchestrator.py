"""Оркестратор: по зонам и задаче выбирает модель, прячет hidden-код, проверяет ответ."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import guard, policy, prompts, sendlog
from .guard import Region
from .providers import Provider

SYSTEM = prompts.ASK_SYSTEM
EXPLAIN_SYSTEM = prompts.EXPLAIN_SYSTEM
_FENCE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.S)


@dataclass
class Turn:
    """Реплика разговора. source - файл, о котором шла речь (для проверки доступа)."""
    role: str  # "user" или "assistant"
    text: str  # как видит человек
    source: Path | None = None
    en: str | None = None  # английская версия для модели (перевод задачи или оригинал ответа)

    @property
    def for_model(self) -> str:
        return self.en or self.text


MAX_TURNS = 20
MAX_CHARS = 12000


def build_messages(history: list[Turn] | None, content: str) -> list[dict]:
    """История + текущий запрос. Реплики о файлах, закрытых сейчас, в запрос не попадают."""
    kept = [t for t in (history or []) if t.source is None or not policy.is_denied(t.source)]
    kept = kept[-MAX_TURNS:]
    while kept and sum(len(t.for_model) for t in kept) > MAX_CHARS:
        kept.pop(0)
    while kept and kept[0].role != "user":
        kept.pop(0)
    messages: list[dict] = []
    for t in [*kept, Turn("user", content)]:
        if messages and messages[-1]["role"] == t.role:
            messages[-1]["content"] += "\n\n" + t.for_model
        else:
            messages.append({"role": t.role, "content": t.for_model})
    return messages


@dataclass
class Result:
    new_text: str
    verdict: guard.Verdict
    provider: Provider
    reason: str
    hidden_count: int
    note: str = ""  # пояснение модели вне блока кода (человеку, возможно переведённое)
    note_en: str = ""  # то же, как ответила модель
    task_en: str = ""  # перевод задачи, если был
    notes: list[str] = field(default_factory=list)  # замечания про перевод для человека


@dataclass
class Explanation:
    text: str
    provider: Provider
    zones: int
    text_en: str = ""
    task_en: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class FileInput:
    path: Path | None
    text: str
    regions: list[Region]
    root: Path | None = None  # открытая папка проекта: от неё считаются подписи и журнал

    @property
    def label(self) -> str:
        if self.path is None:
            return "(без файла)"
        base = os.path.realpath(self.root) if self.root else sendlog.project_dir(self.path)
        return os.path.relpath(os.path.realpath(self.path), base)


@dataclass
class Request:
    """Всё, что уйдёт провайдеру. Один и тот же объект идёт и в предпросмотр, и в отправку."""
    mode: str  # "ask" или "explain"
    provider: Provider
    reason: str
    system: str
    messages: list[dict]
    files: list[tuple[FileInput, guard.Redacted]]  # первый - основной, остальные - контекст
    zones: int = 0


MAX_CONTEXT_CHARS = 30000


def pick(regions: list[Region], task: str, providers: dict[str, Provider]) -> tuple[Provider, str]:
    levels = {r.level for r in regions if r.level != "explain"}
    if levels:
        return providers["strong"], f"есть зоны ({', '.join(sorted(levels))})"
    if len(task) > 300:
        return providers["strong"], "длинная задача"
    return providers["cheap"], "зон нет, задача короткая"


def _zone_lines(regions: list[Region], red: guard.Redacted, levels: set[str]) -> list[str]:
    zones = []
    for r in regions:
        if r.level in levels and r.start < len(red.line_map):
            a, b = red.line_map[r.start] + 1, red.line_map[min(r.end, len(red.line_map) - 1)] + 1
            zones.append((a, b, r.level))
    return [prompts.zone_line(lv, a, b) for a, b, lv in sorted(set(zones))]


def _context_block(context: list[tuple[FileInput, guard.Redacted]]) -> str:
    if not context:
        return ""
    parts, left = [prompts.SEC_CONTEXT], MAX_CONTEXT_CHARS
    for f, red in context:
        body = red.text[:max(left, 0)]
        if len(body) < len(red.text):
            body += "\n" + prompts.TRUNCATED
        left -= len(body)
        parts.append(f"--- {f.label} ---\n{body}")
    return "\n".join(parts) + "\n"


def _require_all(inputs: list[FileInput]):
    for f in inputs:
        policy.require(f.path)


def prepare_ask(primary: FileInput, task: str, providers: dict[str, Provider],
                history: list[Turn] | None = None,
                context: list[FileInput] | None = None, english_reply: bool = False) -> Request:
    inputs = [primary, *(context or [])]
    _require_all(inputs)
    provider, reason = pick([r for f in inputs for r in f.regions], task, providers)
    files = [(f, guard.redact(f.text, f.regions)) for f in inputs]
    red = files[0][1]
    zones = "\n".join(_zone_lines(primary.regions, red, {"confirm", "readonly"})) or prompts.NONE
    content = (f"{task}\n{prompts.SEC_ZONES}\n{zones}\n{_context_block(files[1:])}"
               f"{prompts.SEC_FILE}\n{red.text}")
    return Request("ask", provider, reason, prompts.with_reply(SYSTEM, english_reply),
                   build_messages(history, content), files)


def prepare_explain(primary: FileInput, providers: dict[str, Provider],
                    history: list[Turn] | None = None,
                    context: list[FileInput] | None = None, english_reply: bool = False) -> Request | None:
    """None, если объяснять нечего (нет зон explain или они целиком скрыты)."""
    inputs = [primary, *(context or [])]
    _require_all(inputs)
    files = [(f, guard.redact(f.text, f.regions)) for f in inputs]
    red = files[0][1]
    red_lines = red.text.split("\n")
    zones = []
    for r in primary.regions:
        if r.level != "explain" or r.start >= len(red.line_map):
            continue
        a = red.line_map[r.start]
        b = red.line_map[min(r.end, len(red.line_map) - 1)]
        if all(guard.PLACEHOLDER.match(l) for l in red_lines[a:b + 1]):
            continue  # зона целиком скрыта
        zones.append((a + 1, b + 1))
    if not zones:
        return None
    numbered = "\n".join(f"{i + 1}| {l}" for i, l in enumerate(red_lines))
    listing = "\n".join(f"Zone {n + 1}: lines {a}-{b}" for n, (a, b) in enumerate(sorted(set(zones))))
    content = f"Explain these zones:\n{listing}\n{_context_block(files[1:])}{prompts.SEC_FILE}\n{numbered}"
    return Request("explain", providers["cheap"], "объяснение", prompts.with_reply(EXPLAIN_SYSTEM, english_reply),
                   build_messages(history, content), files, len(zones))


def send(req: Request) -> str:
    """Единственное место, где данные уходят провайдеру: проверка доступа, журнал, вызов."""
    for f, _ in req.files:
        policy.require(f.path)
    sendlog.record(req.files[0][0].path, {
        "mode": req.mode,
        "model": req.provider.name,
        "files": [{"name": f.label, "hidden_chunks": len(red.chunks),
                   "hidden_lines": sum(c.count("\n") + 1 for c in red.chunks)}
                  for f, red in req.files],
        "chars": len(req.system) + sum(len(m["content"]) for m in req.messages),
        "messages": len(req.messages),
    }, req.files[0][0].root)
    return req.provider.complete(req.system, req.messages)


def describe(req: Request) -> str:
    """Текст для предпросмотра: ровно то, что получит провайдер."""
    out = [f"Модель: {req.provider.name} ({req.reason})", "Файлы: " + ", ".join(
        f"{f.label}" + (" [контекст]" if i else "") + (f", скрыто кусков: {len(red.chunks)}" if red.chunks else "")
        for i, (f, red) in enumerate(req.files)), "", "=== SYSTEM ===", req.system]
    for m in req.messages:
        out += ["", f"=== {m['role'].upper()} ===", m["content"]]
    return "\n".join(out)


def finish_ask(req: Request, reply: str) -> Result:
    primary, red = req.files[0]
    m = _FENCE.search(reply)
    new_text = guard.restore(m[1] if m else reply, red.chunks)
    note = (reply[:m.start()] + reply[m.end():]).strip() if m else ""
    return Result(new_text, guard.check_edit(primary.text, new_text, primary.regions),
                  req.provider, req.reason, len(red.chunks), note)


def ask(text: str, task: str, regions: list[Region], providers: dict[str, Provider],
        source: Path | None = None, history: list[Turn] | None = None,
        context: list[FileInput] | None = None, root: Path | None = None,
        english_reply: bool = False) -> Result:
    req = prepare_ask(FileInput(source, text, regions, root), task, providers, history, context, english_reply)
    return finish_ask(req, send(req))


def explain(text: str, regions: list[Region], providers: dict[str, Provider],
            source: Path | None = None, history: list[Turn] | None = None,
            context: list[FileInput] | None = None, root: Path | None = None,
            english_reply: bool = False) -> Explanation:
    req = prepare_explain(FileInput(source, text, regions, root), providers, history, context, english_reply)
    if req is None:
        policy.require(source)
        return Explanation("Нет зон для объяснения (или они целиком скрыты).", providers["cheap"], 0)
    return Explanation(send(req).strip(), req.provider, req.zones)
