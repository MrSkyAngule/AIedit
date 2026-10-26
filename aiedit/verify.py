"""Ворота зелёных тестов: написал -> запустил -> упало -> поправил -> запустил, пока не позеленеет.

Проверку запускает редактор, а не модель: модель может сказать «готово» при красных тестах (так и было). Если запуск
красный, вывод (уже очищенный от секретов и закрытого кода) возвращается автору на исправление, потом проверка идёт
снова. Круги ограничены, одна и та же ошибка дважды подряд останавливает цикл. Итог, зелёный он или красный, остаётся
в отчёте, и человек видит его рядом с ответом модели.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import agent
from .roster import Worker
from .workspace import Workspace

MAX_ROUNDS = 6
TAIL = 3500
FIX_STEPS = 25
COMPILE = 'python -m compileall -q -x "(^|/)(\\.?venv[^/]*|node_modules|\\.git)(/|$)" .'
PYTEST = "python -m pytest -q -p no:cacheprovider"
UNITTEST = "python -m unittest discover -q"

FIX_PROMPT = """The editor ran `{command}` after your work and it FAILED. Fix the cause in the code and make it pass.
Rules:
- Read the output below, find the real cause, fix it. You may edit any file of this task.
- Do NOT delete a test or weaken an assertion to get green. If a test itself is wrong, fix the test and say why.
- If the failure is a missing package, use install_packages (when available) or say what must be installed.
- You may run the command yourself to check, the editor will run it again after you answer.
- Answer with what you changed and what you could not fix.
Original task: {task}
Output of the failed run:
{output}"""


@dataclass
class VerifyReport:
    command: str = ""
    source: str = ""             # откуда команда: настройки проекта или определена по файлам
    runs: int = 0
    fixes: int = 0               # сколько раз автор реально менял код по красной проверке
    green: bool | None = None    # None - проверка не шла
    skipped: str = ""
    stalled: str = ""
    output: str = ""             # хвост последнего запуска
    notes: list[str] = field(default_factory=list)


def _files(ws: Workspace) -> list[str]:
    return [ws.rel(p) for p in ws._walk_files(ws.root)]


def _text(ws: Workspace, rel: str) -> str:
    real = ws.resolve(rel)
    st = ws.state(real) if real is not None else None
    return "" if st is None or isinstance(st, str) else st.text


def _test_files(names: list[str]) -> list[str]:
    return [n for n in names if n.endswith(".py") and (Path(n).name.startswith("test_") or Path(n).name.endswith("_test.py")
                                                       or "tests/" in n)]


def detect_command(ws: Workspace, configured: str = "") -> tuple[str, str]:
    """(команда, откуда). Пустая команда: проверять нечем. Без тестов хотя бы проверяем, что код компилируется."""
    if configured.strip():
        return configured.strip(), "настройки проекта"
    names = _files(ws)
    tests = _test_files(names)
    if tests:
        body = "\n".join(_text(ws, n) for n in tests + [n for n in names if Path(n).name in ("conftest.py", "pytest.ini")])
        if "unittest" in body and "pytest" not in body and "conftest.py" not in names:
            return UNITTEST, "найдены тесты unittest"
        return PYTEST, "найдены тесты pytest"
    if "package.json" in names and "node_modules" in {n.split("/")[0] for n in names}:
        if '"test"' in _text(ws, "package.json"):
            return "npm test --silent", "package.json"
    if any(n.endswith(".py") for n in names):
        return COMPILE, "тестов нет: проверяю хотя бы, что код компилируется"
    return "", "нечего проверять"


def count_checks(ws: Workspace) -> int:
    """Сколько тестов и утверждений в тестовых файлах: чтобы заметить, что автор «починил» красное, удалив проверки."""
    return sum(len(re.findall(r"^\s*(?:async\s+)?def test_|\bassert\b", _text(ws, n), re.M)) for n in _test_files(_files(ws)))


def _signature(output: str) -> str:
    return re.sub(r"0x[0-9a-f]+|\d+(?:\.\d+)?|/tmp/\S+", "N", output)[-800:]


def _fix(task: str, ws: Workspace, author: Worker, command: str, output: str, on_progress, should_stop,
         english_reply: bool, terminal, helpers: dict) -> None:
    prompt = FIX_PROMPT.format(command=command, task=task[:6000], output=output[-TAIL:])
    saved = ws.model
    ws.model = author.provider.name
    try:
        agent.run(prompt, ws, author.provider, None, None, on_progress, should_stop, english_reply=english_reply,
                  max_steps=FIX_STEPS, terminal=terminal, **helpers)
    finally:
        ws.model = saved


def loop(task: str, ws: Workspace, author: Worker, terminal: Callable[[str], str], command: str, source: str = "",
         on_progress=None, should_stop=None, english_reply: bool = False, rounds: int = MAX_ROUNDS,
         helpers: dict | None = None) -> VerifyReport:
    """Запуск проверки, при красной возврат автору, повтор. Редактор меняет только код: тесты он не запускает на хосте,
    только через переданный terminal (песочница, с одобрением человека)."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    report = VerifyReport(command=command, source=source)
    seen: list[str] = []
    checks_before = count_checks(ws)
    for n in range(rounds + 1):
        if stop():
            report.stalled = "остановлено пользователем"
            break
        say(f"проверка: запускаю {command}" + (f" (после исправления {n})" if n else ""))
        out = terminal(command)
        report.runs += 1
        passed = agent.command_passed(out)
        if passed is None:  # человек не разрешил запуск или песочница недоступна
            report.skipped = " ".join(out.split())[:200]
            break
        report.output = out[-TAIL:]
        if passed:
            report.green = True
            break
        report.green = False
        if n == rounds:
            report.stalled = f"предел кругов проверки ({rounds}), тесты остались красными"
            break
        sig = _signature(out)
        if sig in seen:
            report.stalled = "та же ошибка повторилась после исправления: автор не смог её убрать"
            break
        seen.append(sig)
        say("проверка красная: возвращаю вывод автору на исправление")
        before = ws.review_diff()
        _fix(task, ws, author, command, out, on_progress, should_stop, english_reply, terminal, helpers or {})
        if ws.review_diff() == before:
            report.stalled = "автор ничего не изменил"
            break
        report.fixes += 1
    after = count_checks(ws)
    if checks_before and after < checks_before * 0.8:
        report.notes.append(f"число тестов и утверждений уменьшилось с {checks_before} до {after}: проверьте, не удалены ли проверки")
    return report
