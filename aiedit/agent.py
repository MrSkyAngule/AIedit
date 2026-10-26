"""Цикл агента: модель смотрит проект через инструменты, не зная про API провайдера.

Протокол текстовый, поэтому подходит любой модели и любому сервису. Модель пишет блоки

    @@tool read_file
    path: main.py
    @@end

а для многострочных аргументов (старый и новый текст, содержимое файла) - секции:

    @@tool edit_file
    path: main.py
    @@old
    ...
    @@new
    ...
    @@end

Ответ редактора приходит блоками `@@result <имя>` ... `@@end`. Ответ без блоков `@@tool`
считается итогом.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import context, environment, orchestrator, policy, project_rules, prompts
from .providers import Provider
from .workspace import Change, Workspace

MAX_STEPS = 40  # потом редактор спрашивает человека, продолжать ли (extend)
MAX_CALLS_PER_REPLY = 8
MAX_NOTES = 4000
MAX_QUESTIONS = 3  # больше - значит модель перекладывает работу на человека
ENV_IN_FIRST_MESSAGE = 1200  # сколько символов описания окружения проекта кладём в первое сообщение
HINT_AFTER_PASSES = 2  # проверка прошла столько раз подряд: напоминаем, что пора заканчивать
LOOP_PASSES = 4        # ...а столько раз подряд и агент всё ещё правит: останавливаем сами
LOOP_SAME_REPLY = 3    # столько раз подряд один и тот же набор вызовов
_EXIT = re.compile(r"^exit code (-?\d+)\s*$", re.M)

SYSTEM = prompts.AGENT_SYSTEM

_BLOCK = re.compile(r"^@@tool[ \t]+(\w+)[ \t]*\n(.*?)^@@end[ \t]*$", re.S | re.M)
_SECTION = re.compile(r"^@@(old|new|content|question|notes)[ \t]*$", re.M)
# слабые модели пишут `@@old|текст` или `@@old: текст` (привычка из YAML): текст после маркера переносим на следующую строку
# ответ редактора модель писать не должна, но иногда дописывает его сама, прямо за @@end, иногда с хвостом </think>
_END_TAIL = re.compile(r"^(@@end)(?:[ \t]*</?[A-Za-z]+>)+[ \t]*", re.M)
_RESULT_LINE = re.compile(r"^@@result\b", re.M)
_GLUED = re.compile(r"^(@@(?:old|new|content|question|notes))[ \t]*[|:](.*)$", re.M)

TOOLS: dict[str, dict] = {
    "list_files": {"path": str, "depth": int},
    "read_file": {"path": str, "start_line": int, "end_line": int},
    "search": {"query": str, "path": str, "case": bool},
    "edit_file": {"path": str, "old_text": str, "new_text": str},
    "create_file": {"path": str, "content": str},
    "delete_file": {"path": str},
    "rename_file": {"path": str, "new_path": str},
    "update_notes": {"notes": str},
    "go_to_definition": {"path": str, "line": int, "symbol": str},  # навигация (codenav.Navigator)
    "find_references": {"path": str, "line": int, "symbol": str},  # план агента: редактор хранит его и возвращает после сжатия
    "ask_user": {"question": str},  # не инструмент рабочей области: отвечает человек
    "run_command": {"command": str},  # выполняет редактор, в песочнице (sandbox.Terminal)
    "fetch_url": {"url": str},  # открывает редактор, только сайты из списка или с одобрения (web.Web)
    "web_search": {"query": str},  # поиск через Brave (search.Search)
    "install_packages": {"packages": str},  # ставит редактор, с одобрения (packages.Installer)
    "project_environment": {},  # что проект сам говорит о версиях и ПО (environment.describe), без запуска программ
    "package_version": {"name": str},  # актуальная версия пакета из реестра (versions.lookup), если интернет разрешён
}
FORMAT_HINTS = {
    "edit_file": "@@tool edit_file\npath: main.py\n@@old\nexact old text\n@@new\nnew text\n@@end",
    "create_file": "@@tool create_file\npath: new.py\n@@content\nfile text\n@@end",
}
_SECTION_ARG = {"old": "old_text", "new": "new_text", "content": "content", "question": "question", "notes": "notes"}


@dataclass
class Call:
    name: str
    args: dict
    error: str = ""


@dataclass
class AgentResult:
    text: str
    changes: list[Change]
    steps: int = 0
    text_en: str = ""  # итог как ответила модель (text может быть переводом)
    task_en: str = ""  # перевод задачи, если был
    notes: list[str] = field(default_factory=list)  # замечания про перевод для человека
    calls: list[str] = field(default_factory=list)  # что делал агент, для показа человеку
    stopped: str = ""  # причина досрочной остановки, если была
    from_model: bool = True  # text - ответ модели, а не наше сообщение об остановке
    review: object | None = None  # review.ReviewReport, если шла проверка безопасностью
    analysis: object | None = None  # analysis.AnalysisReport, если шёл статанализ
    verification: object | None = None  # verify.VerifyReport: что показал запуск тестов редактором (зелёный или красный)
    authors: dict[str, str] = field(default_factory=dict)  # путь -> кто последним писал файл (для журнала правок)
    sent: int = 0  # сколько символов ушло модели за все запросы (для человека: расход)
    usage: dict = field(default_factory=dict)  # точный расход по ответам API: input, cache_read, cache_write, output
    plan: str = ""  # последний план агента (update_notes)
    compactions: int = 0  # сколько раз сжимали переписку


def _coerce(value: str, kind):
    value = value.strip()
    if kind is int:
        return int(value)
    if kind is bool:
        return value.lower() in ("1", "true", "yes", "да")
    return value


def _hint(name: str) -> str:
    """Что дописать к ошибке разбора: пример правильного вызова, чтобы модель сама исправилась."""
    example = FORMAT_HINTS.get(name, f"@@tool {name}\nargument: value\n@@end")
    return ("\nCorrect format (each @@ marker alone on its own line, text starts on the next line, "
            "no | or : after the marker):\n" + example)


def _unglue(body: str) -> str:
    return _GLUED.sub(lambda m: m[1] + ("\n" + m[2] if m[2].strip() else ""), body)


def strip_fake_results(reply: str) -> tuple[str, bool]:
    """Убирает то, что модель дописала за редактор: блоки @@result и всё после них (оно построено на выдумке).
    Внутри блока @@tool слова @@result - просто текст, например в правке файла, там ничего не режем.
    Возвращает (чистый ответ, был ли выдуманный результат)."""
    reply = _END_TAIL.sub(lambda m: m[1] + "\n", reply)
    spans = [m.span() for m in _BLOCK.finditer(reply)]
    for m in _RESULT_LINE.finditer(reply):
        if not any(a <= m.start() < b for a, b in spans):
            return reply[:m.start()].rstrip() + "\n", True
    return reply, False


def parse_calls(reply: str) -> tuple[list[Call], str]:
    """Достаёт вызовы инструментов. Возвращает (вызовы, текст вне блоков)."""
    calls = []
    reply, _ = strip_fake_results(reply)
    for m in _BLOCK.finditer(reply):
        name, body = m[1], m[2]
        if name not in TOOLS:
            calls.append(Call(name, {}, f"unknown tool {name!r}; available: {', '.join(TOOLS)}"))
            continue
        head, *rest = _SECTION.split(_unglue(body))
        args: dict = {}
        error = ""
        for line in head.split("\n"):
            if not line.strip():
                continue
            key, sep, val = line.partition(":")
            key = key.strip()
            if not sep or key not in TOOLS[name]:
                error = f"bad or extra argument line: {line.strip()[:60]!r}" + _hint(name)
                break
            try:
                args[key] = _coerce(val, TOOLS[name][key])
            except ValueError:
                error = f"argument {key}: a number is expected"
                break
        for sec, text in zip(rest[0::2], rest[1::2]):
            arg = _SECTION_ARG[sec]
            if arg not in TOOLS[name]:
                error = error or f"section @@{sec} does not fit {name}"
            else:
                text = text[1:] if text.startswith("\n") else text  # переводы строк вокруг секции - от разметки
                args[arg] = text[:-1] if text.endswith("\n") else text
        calls.append(Call(name, args, error))
    text = _BLOCK.sub("", reply).strip()
    return calls[:MAX_CALLS_PER_REPLY], text


def execute(ws: Workspace, call: Call) -> str:
    if call.error:
        return f"ERROR: {call.error}"
    required = {"read_file": ["path"], "search": ["query"], "edit_file": ["path", "old_text", "new_text"],
                "create_file": ["path", "content"],
                "delete_file": ["path"], "rename_file": ["path", "new_path"]}.get(call.name, [])
    missing = [r for r in required if r not in call.args]
    if missing:
        return f"ERROR: missing arguments: {', '.join(missing)}" + (_hint(call.name) if call.name in FORMAT_HINTS else "")
    try:
        return getattr(ws, call.name)(**call.args)
    except policy.AccessDenied:
        raise
    except Exception as e:  # ошибка инструмента не должна ронять цикл
        return f"ERROR: {type(e).__name__}: {e}"


def describe_call(call: Call) -> str:
    a = call.args
    if call.name == "list_files":
        return f"смотрю список файлов: {a.get('path', '.')}"
    if call.name == "read_file":
        return f"читаю {a.get('path', '?')}"
    if call.name == "search":
        return f"ищу «{a.get('query', '')}»"
    if call.name == "edit_file":
        return f"предлагаю правку в {a.get('path', '?')}"
    if call.name == "create_file":
        return f"предлагаю новый файл {a.get('path', '?')}"
    if call.name == "delete_file":
        return f"предлагаю удалить {a.get('path', '?')}"
    if call.name == "rename_file":
        return f"предлагаю перенести {a.get('path', '?')} → {a.get('new_path', '?')}"
    if call.name == "update_notes":
        return "обновляю план"
    if call.name in ("go_to_definition", "find_references"):
        what = "где определено" if call.name == "go_to_definition" else "где используется"
        return f"{what}: {a.get('symbol', '?')} ({a.get('path', '?')}:{a.get('line', '?')})"
    if call.name == "ask_user":
        return "задаю вопрос"
    if call.name == "run_command":
        return f"запускаю в песочнице: {a.get('command', '?')[:120]}"
    if call.name == "fetch_url":
        return f"читаю страницу: {a.get('url', '?')[:120]}"
    if call.name == "web_search":
        return f"ищу в интернете: {a.get('query', '?')[:120]}"
    if call.name == "install_packages":
        return f"прошу установить: {a.get('packages', '?')[:120]}"
    if call.name == "project_environment":
        return "смотрю версии и ПО проекта"
    if call.name == "package_version":
        return f"проверяю версию пакета: {a.get('name', '?')[:60]}"
    return call.name


# инструменты, которые выполняет не рабочая область, а помощник от редактора: имя -> (аргумент, ответ без помощника)
HELPED = {
    "run_command": ("command", "ERROR: no terminal here. Check your work by reading the code instead."),
    "fetch_url": ("url", "ERROR: no web access here. Rely on what you know and say what you are unsure about."),
    "install_packages": ("packages", "ERROR: installing packages is not available here. Tell the user what to install."),
    "web_search": ("query", "ERROR: web search is not set up. Rely on what you know or ask the user for a link."),
    "package_version": ("name", "ERROR: version lookup is not available here (the internet is off). Do not present "
                                "remembered versions as current: say they are unverified, or ask the user which versions they use."),
}


def run_helper(call: Call, helper: Callable[[str], str] | None) -> str:
    if call.error:
        return f"ERROR: {call.error}"
    arg, unavailable = HELPED[call.name]
    return helper(call.args.get(arg, "")) if helper is not None else unavailable


def ask_human(call: Call, ask: Callable[[str], str | None] | None, asked: int) -> tuple[str, bool]:
    """Вопрос человеку. Возвращает (что увидит модель: ответ или почему его нет, был ли вопрос задан)."""
    if call.error:
        return f"ERROR: {call.error}", False
    question = call.args.get("question", "").strip()
    if not question:
        return "ERROR: missing arguments: question", False
    if ask is None:
        return prompts.OUT_ASK_UNAVAILABLE, False
    if asked >= MAX_QUESTIONS:
        return prompts.OUT_ASK_LIMIT, False
    answer = ask(question)
    return (answer.strip() if answer and answer.strip() else prompts.OUT_NO_ANSWER), True


def command_passed(out: str) -> bool | None:
    """Результат run_command: прошла ли команда. None - в тексте нет кода выхода (не результат команды)."""
    m = _EXIT.search(out[:400])
    return None if m is None else int(m[1]) == 0


ACTING = ("edit_file", "create_file", "delete_file", "rename_file", "run_command", "install_packages")


def call_signature(calls: list[Call]) -> tuple:
    """Отпечаток правок и запусков в ответе. Чтение и поиск не считаем: их повторы ограничивает лимит шагов."""
    return tuple((c.name, tuple(sorted((k, str(v)) for k, v in c.args.items()))) for c in calls if c.name in ACTING)


def add_usage(total: dict, part: dict | None):
    for k, v in (part or {}).items():
        total[k] = total.get(k, 0) + v


def usage_text(usage: dict, sent_chars: int = 0) -> str:
    """Для человека: точный расход, если API его сообщил, иначе прикидка по символам."""
    if not usage:
        return f"ушло ~{max(1, sent_chars // 4000)} тыс. токенов" if sent_chars else ""
    k = lambda n: f"{n / 1000:.1f}".rstrip("0").rstrip(".") + " тыс."  # noqa: E731
    total_in = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
    text = f"токены: вход {k(total_in)}"
    if usage.get("cache_read"):
        text += f" (из кэша {k(usage['cache_read'])}, {round(100 * usage['cache_read'] / max(total_in, 1))}%)"
    return text + f", ответ {k(usage.get('output', 0))}"


def first_message(task: str, ws: Workspace, open_file: Path | None = None) -> str:
    """Первое сообщение модели: задача, список файлов проекта, имя открытого файла и правила проекта."""
    tree = ws.list_files(".", 2)
    opened = f"\n{prompts.SEC_OPEN}\n{ws.rel(open_file)}" if open_file and ws.resolve(str(open_file)) else ""
    if ws.read_only:  # проверяющему окружение и «новый проект» ни к чему
        kind = ""
    elif not ws._walk_files(ws.root):
        kind = f"\n{prompts.SEC_KIND}\n{prompts.KIND_NEW}"
    else:  # существующий проект: версии и ПО лежат перед глазами у модели, а не только в инструменте, который она может не вызвать
        kind = f"\n{prompts.SEC_ENV}\n{environment.describe(ws)[:ENV_IN_FIRST_MESSAGE]}"
    return f"{task}\n\n{prompts.SEC_TREE}\n{tree}{opened}{kind}{project_rules.section(ws)}"


def run(task: str, ws: Workspace, provider: Provider, history: list | None = None,
        open_file: Path | None = None, on_progress: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None, english_reply: bool = False,
        max_steps: int = MAX_STEPS, system: str | None = None,
        ask: Callable[[str], str | None] | None = None,
        terminal: Callable[[str], str] | None = None, web: Callable[[str], str] | None = None,
        install: Callable[[str], str] | None = None,
        extend: Callable[[int, int], bool] | None = None,
        search: Callable[[str], str] | None = None, nav: Callable[[str, dict], str] | None = None,
        versions: Callable[[str], str] | None = None) -> AgentResult:
    """Крутит цикл: запрос к модели, выполнение инструментов, ответ редактора, пока не будет итога.
    ask(вопрос) -> ответ человека или None; без ask модель не знает про ask_user и решает сама.
    terminal(команда) -> вывод для модели (sandbox.Terminal); без него run_command нет.
    web(адрес) -> текст страницы (web.Web); без него fetch_url нет.
    install(пакеты) -> вывод pip (packages.Installer). Проверяющему (read_only) помощники не даются.
    extend(шагов сделано, символов отправлено) -> продолжать ли, когда кончились max_steps шагов.
    Длинная переписка сжимается (context.compact), так что задача не упирается в размер контекста."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    first = first_message(task, ws, open_file)
    messages = orchestrator.build_messages(history, first)
    start = len(messages)  # история и задача: при сжатии не трогаем
    result = AgentResult("", [])
    asked = 0
    if ws.read_only:
        terminal = web = install = search = versions = None
    helpers = {"run_command": terminal, "fetch_url": web, "install_packages": install, "web_search": search,
               "package_version": versions}
    system = prompts.agent_system(system or SYSTEM, [addon for on, addon in (
        (ask is not None, prompts.ASK_ADDON), (terminal is not None, prompts.TERMINAL_ADDON),
        (web is not None, prompts.WEB_ADDON), (search is not None and web is not None, prompts.SEARCH_ADDON),
        (install is not None, prompts.INSTALL_ADDON), (nav is not None, prompts.NAV_ADDON),
        (versions is not None, prompts.VERSIONS_ADDON)) if on])
    full_system = prompts.with_reply(system, english_reply)
    budget, step = max_steps, 0
    passes = repeats = 0     # проверка прошла подряд; один и тот же набор вызовов подряд
    last_signature: tuple = ()
    while True:
        if stop():
            result.stopped = "остановлено пользователем"
            break
        if step >= budget:
            if extend is not None and extend(step, result.sent):
                budget += max_steps
            else:
                result.stopped = f"достигнут лимит шагов ({step})"
                break
        step += 1
        ws.recheck()
        if context.compact(full_system, messages, start):
            result.compactions += 1
            say("переписка сжата: старые результаты убраны, план и список правок сохранены")
            last = messages[-1]
            messages[-1] = {**last, "content": last["content"] + "\n\n" + context.state_block(
                ws.changes(), result.plan, ws.rel)}
        sent = context.size(full_system, messages)
        result.sent += sent
        ws.log("agent", "", None, chars=sent)
        reply = provider.complete(full_system, messages)
        add_usage(result.usage, getattr(provider, "last_usage", None))
        result.steps = step
        reply, invented = strip_fake_results(reply)  # в переписке остаётся только то, что модель вправе была написать
        calls, text = parse_calls(reply)
        if not calls:
            result.text = text or reply.strip()
            break
        messages.append({"role": "assistant", "content": reply})
        blocks = []
        for call in calls:
            note = describe_call(call)
            result.calls.append(note)
            if call.name != "ask_user":  # сам вопрос человек и так увидит
                say(note)
            if call.name == "ask_user":
                out, used = ask_human(call, ask, asked)
                asked += used
            elif call.name in ("go_to_definition", "find_references"):
                out = (f"ERROR: {call.error}" if call.error else nav(call.name, call.args) if nav is not None
                       else "ERROR: code navigation is not available here; use search")
            elif call.name == "update_notes":
                result.plan = call.args.get("notes", "").strip()[:MAX_NOTES]
                out = "notes saved" if result.plan else "ERROR: missing arguments: notes"
            elif call.name in HELPED:
                out = run_helper(call, helpers[call.name])
                if call.name == "run_command" and (ok := command_passed(out)) is not None:
                    passes = passes + 1 if ok else 0
            else:
                out = execute(ws, call)
            blocks.append(f"@@result {call.name}\n{out}\n@@end")
        if invented:
            blocks.append(prompts.OUT_INVENTED_RESULT)
        if passes == HINT_AFTER_PASSES:
            blocks.append(prompts.OUT_PASSED_TWICE)
        messages.append({"role": "user", "content": "\n\n".join(blocks)})
        signature = call_signature(calls)
        repeats = repeats + 1 if signature and signature == last_signature else (1 if signature else 0)
        last_signature = signature
        if passes >= LOOP_PASSES or repeats >= LOOP_SAME_REPLY:
            result.stopped = ("похоже, зациклилось: проверка уже прошла " + f"{passes} раз подряд, а агент всё правит"
                              if passes >= LOOP_PASSES else
                              f"похоже, зациклилось: агент {repeats} раза подряд повторил одно и то же")
            say(result.stopped)
            break
    result.changes = ws.changes()
    if result.stopped and not result.text:
        result.text = f"Работа остановлена: {result.stopped}."
        result.from_model = False
    return result
