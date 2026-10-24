"""Правила проекта для ИИ: файл AGENTS.md (или AIEDIT.md, CLAUDE.md) в корне проекта.

Туда пишут то, что модель должна знать в каждой задаче: стек, как запускать тесты, стиль кода,
чего не трогать. Редактор добавляет файл к первому сообщению агента и к запросу дирижёра.
Файл читается через рабочую область: если он закрыт от ИИ, его нет; зоны и секреты в нём скрыты.
Правила - это текст для модели, а не замок: защиту дают зоны и запреты, а не просьбы в этом файле.
"""
from .workspace import Workspace

NAMES = ("AGENTS.md", "AIEDIT.md", "CLAUDE.md")
MAX_CHARS = 8000
TEMPLATE = """# Правила проекта для ИИ

Этот файл ИИ читает перед каждой задачей. Пиши коротко, только то, что важно всегда.

- Стек:
- Как запускать: 
- Тесты: 
- Стиль кода: 
- Чего не трогать: 
"""


def find(ws: Workspace) -> tuple[str, str] | None:
    """(имя файла, текст для модели) или None."""
    for name in NAMES:
        real = ws.resolve(name)
        if real is None or not real.is_file():
            continue
        st = ws.state(real)
        if isinstance(st, str):
            continue
        text = ws.view(st).text.strip()
        if not text:
            return None
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n[... rules cut: keep this file short ...]"
        ws.sent.add(real)
        return ws.rel(real), text
    return None


def section(ws: Workspace, heading: str = "=== PROJECT RULES ({name}) ===") -> str:
    found = find(ws)
    return f"\n\n{heading.format(name=found[0])}\n{found[1]}" if found else ""
