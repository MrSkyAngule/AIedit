"""Сжатие переписки агента с моделью, чтобы длинная задача не упиралась в размер контекста.

Когда переписка разрастается, из старых сообщений убираются результаты инструментов (прочитанные
файлы, вывод команд) и длинные куски старых правок. Задача, рассуждения модели и последние шаги
остаются, а редактор добавляет сводку: какие файлы уже изменены и план, который ведёт сам агент
(update_notes). Понадобится старый файл - модель перечитает его. Модель для сжатия не нужна:
это дешевле и ничего не искажает.
"""
import re

COMPACT_AT = 160_000   # символов в запросе (~40 тыс. токенов): дальше сжимаем
COMPACT_TO = 90_000    # до скольких сжимать
HARD_LIMIT = 400_000   # если и после сжатия больше - режем результаты последнего шага
KEEP_LAST = 2          # последний ответ модели и результаты к нему не трогаем
STUB = "[cleared to save context: {n} chars. Call the tool again if you need this]"
_RESULT = re.compile(r"(@@result [\w-]+\n)(.*?)(\n@@end)", re.S)
_SECTION = re.compile(r"(^@@(?:old|new|content|notes|question)[ \t]*\n)(.*?)(?=^@@)", re.S | re.M)
STATE_TOOL = "editor_state"


def size(system: str, messages: list[dict]) -> int:
    return len(system) + sum(len(m["content"]) for m in messages)


def _shrink(m: dict) -> dict:
    if m["role"] == "user":
        text = _RESULT.sub(lambda r: r[0] if len(r[2]) <= 200 else r[1] + STUB.format(n=len(r[2])) + r[3], m["content"])
    else:
        text = _SECTION.sub(lambda s: s[0] if len(s[2]) <= 300 else f"{s[1]}[... {len(s[2])} chars ...]\n", m["content"])
    return {**m, "content": text}


def compact(system: str, messages: list[dict], start: int) -> bool:
    """Сжимает сообщения от start (до него - история и задача, их не трогаем). True, если что-то убрано."""
    if size(system, messages) <= COMPACT_AT:
        return False
    changed = False
    for i in range(start, len(messages) - KEEP_LAST):
        if size(system, messages) <= COMPACT_TO:
            break
        smaller = _shrink(messages[i])
        if smaller["content"] != messages[i]["content"]:
            messages[i] = smaller
            changed = True
    if size(system, messages) > HARD_LIMIT and messages and messages[-1]["role"] == "user":
        over = size(system, messages) - HARD_LIMIT
        last = messages[-1]["content"]
        bodies = [len(r[2]) for r in _RESULT.finditer(last)]
        if bodies:
            cut = max(500, max(bodies) - over)  # самые большие результаты укорачиваем до этой длины
            messages[-1] = {**messages[-1], "content": _RESULT.sub(
                lambda r: r[0] if len(r[2]) <= cut else
                r[1] + r[2][:cut] + f"\n[... cut: {len(r[2]) - cut} more chars, read a smaller range ...]" + r[3], last)}
            changed = True
    return changed


def state_block(changes, notes: str, rel) -> str:
    """Сводка от редактора после сжатия: что уже сделано и план агента."""
    edited = [rel(c.path) for c in changes if not c.created and not c.deleted]
    created = [rel(c.path) for c in changes if c.created]
    deleted = [rel(c.path) for c in changes if c.deleted]
    lines = ["Older tool results were cleared to save context. Current state:"]
    for label, items in (("edited", edited), ("created", created), ("deleted", deleted)):
        if items:
            lines.append(f"- {label}: {', '.join(items[:40])}" + (" ..." if len(items) > 40 else ""))
    if len(lines) == 1:
        lines.append("- no pending changes yet")
    lines.append("Your notes:\n" + notes if notes.strip() else "No notes yet: keep a plan with update_notes.")
    return f"@@result {STATE_TOOL}\n" + "\n".join(lines) + "\n@@end"
