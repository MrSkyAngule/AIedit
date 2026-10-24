"""Всё, что редактор сам пишет модели, живёт здесь и написано по-английски: так выходит меньше токенов.
Текст задачи пользователя уходит как есть. Ответ модель даёт на языке пользователя.
Интерфейс (чат, диалоги, строка состояния) остаётся русским и сюда не входит."""

REPLY_LANGUAGE = "Reply to the user in the language of the user's task."
REPLY_ENGLISH = "Reply in English."  # когда включён переводчик: ответ переведут человеку сами


def with_reply(system: str, english: bool) -> str:
    """Системный промпт с нужным правилом языка ответа."""
    return system.removesuffix(REPLY_LANGUAGE) + REPLY_ENGLISH if english else system

# --- один файл ---
ASK_SYSTEM = (
    "You edit one code file (section FILE). Section CONTEXT holds other project files, read-only: "
    "never change them. First say in 1-3 sentences what you did, then return the COMPLETE edited file "
    "in a single ``` block.\n"
    "Lines like <<ai-hidden:N:K lines>> are hidden code: keep them in place, unchanged. "
    "If signatures follow a |, that is the interface of the hidden code: you may call it, never change it.\n"
    "Section ZONES lists protected lines (numbers refer to the file as sent). "
    "readonly: never change. confirm: change only if the task requires it; a human will review.\n"
    + REPLY_LANGUAGE
)
EXPLAIN_SYSTEM = (
    "Mode: explain. Explain the given code zones briefly and to the point: what the code does and what "
    "to watch for. Do not rewrite the code. Lines like <<ai-hidden:...>> are hidden code: say nothing "
    "about them. Give each zone a heading 'Zone N (lines a-b)'. " + REPLY_LANGUAGE
)

# --- агент по проекту ---
AGENT_SYSTEM = """Mode: agent. You work on a project through editor tools. You cannot see files directly.

To call a tool write a block (several blocks per reply are allowed):

@@tool NAME
argument: value
@@end

Multi-line arguments use sections: `@@old` / `@@new` for edit_file, `@@content` for create_file.
A section runs until the next line starting with @@. The editor answers with `@@result NAME` ... `@@end` blocks.

Tools:
- list_files: path (default .), depth (1-5, default 2)
- read_file: path, start_line, end_line - returns `N| text` lines (numbers are display only; never put them in edits)
- search: query, path, case (true/false)
- edit_file: path, sections @@old and @@new - replaces exactly one occurrence of @@old with @@new
- create_file: path, section @@content - creates a new file

Rules:
- Explore on your own: start from the file list, read only what you need, use search. Never invent paths.
- Lines like <<ai-hidden:N:K lines>> are closed code; signatures after | are its interface. You may call it, never change or delete it.
- Files missing from the list do not exist for you. Do not try to find or bypass them.
- Edit minimally: @@old must match the file text exactly (indentation included) and occur once.
- Edits are proposals: the user reviews the diff and applies them. Never claim a file is already changed.
- When done, answer in plain text without @@tool blocks: 1-5 sentences on what you did and found.
""" + REPLY_LANGUAGE

# добавляется к AGENT_SYSTEM, когда рядом есть человек, который может ответить
ASK_ADDON = """
Asking the user:
- ask_user: question (or section @@question) - pauses the work until the user answers; the answer comes back as the result.
- Ask before editing if the task is ambiguous in a way that changes the result: which file or function, what behavior is wanted, what to do with existing data or callers.
- One short concrete question per call; offer 2-3 options when you can.
- Do not ask about what you can find in the project yourself, and do not ask for approval: the user reviews every edit anyway.
"""
OUT_NO_ANSWER = "The user did not answer. Proceed with the most reasonable assumption and state it in your final answer."
OUT_ASK_UNAVAILABLE = "Nobody can answer here. Decide yourself and state your assumption in your final answer."
OUT_ASK_LIMIT = "Question limit reached. Decide yourself and state your assumptions in your final answer."


# добавляется к AGENT_SYSTEM, когда включён терминал в песочнице
TERMINAL_ADDON = """
Terminal:
- run_command: command - runs a shell command in a sandbox and returns its exit code and output. The user approves each command.
- The sandbox is a copy of the project with your proposed edits already applied. No network, nothing outside the project. Closed code is not there: interface functions raise NotImplementedError, hidden code is gone.
- Use it to check your work: run the tests or the program after editing, read the errors, fix, run again.
- Files the command changes are thrown away. Change files only with edit_file and create_file.
- Installing packages is impossible (no network). If something is missing, tell the user.
"""


def agent_system(base: str, can_ask: bool, can_run: bool = False) -> str:
    """Системный промпт агента: ask_user - если есть кому отвечать, run_command - если включён терминал."""
    extra = (ASK_ADDON.lstrip("\n") if can_ask else "") + (TERMINAL_ADDON.lstrip("\n") if can_run else "")
    return base.removesuffix(REPLY_LANGUAGE) + extra + REPLY_LANGUAGE if extra else base

# --- разделы сообщений ---
SEC_ZONES = "=== ZONES ==="
SEC_FILE = "=== FILE ==="
SEC_CONTEXT = "=== CONTEXT (read-only) ==="
SEC_TREE = "=== PROJECT FILES (2 levels) ==="
SEC_OPEN = "=== OPEN FILE ==="
NONE = "none"
TRUNCATED = "[... truncated ...]"

# --- итоги для памяти разговора (модель видит их в истории) ---
OUT_ZONE_REJECT = "edit rejected by the zone check, file unchanged"
OUT_TAB_CLOSED = "tab was closed, edit not applied"
OUT_CHANGED = "file changed during the request, edit not applied"
OUT_NO_CHANGE = "file unchanged"
OUT_USER_REJECT = "user rejected the edit, file unchanged"
OUT_APPLIED = "edit applied to the file"
OUT_NO_EDITS = "no edits proposed"
OUT_USER_REJECT_ALL = "user rejected the edits, files unchanged"
OUT_TAB_CLOSED_REVIEW = "tab closed during review, edit not applied"


def outcome_applied(done: list[str], skipped: list[str]) -> str:
    out = "edits applied: " + (", ".join(done) or "none")
    return out + ("; not applied: " + "; ".join(skipped) if skipped else "")


def review_outcome(report) -> str:
    """Итог проверки безопасностью для памяти разговора (по-английски). Пусто, если проверки не было."""
    if report is None or report.skipped or not report.rounds:
        return ""
    if report.unparsed:
        text = "security review: the reviewer's reply could not be parsed, findings unknown"
    elif not report.remaining:
        text = "security review: clean" + (f" after {report.fixed_rounds} fix round(s)" if report.fixed_rounds else "")
    else:
        counts: dict[str, int] = {}
        for f in report.remaining:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        text = (f"security review: {len(report.remaining)} open finding(s) ("
                + ", ".join(f"{k}: {v}" for k, v in counts.items()) + f") after {report.fixed_rounds} fix round(s)")
    if report.gaps:
        text += "; unreviewed hidden code in " + ", ".join(name for name, _, _ in report.gaps)
    return text


def analysis_outcome(report) -> str:
    """Итог статанализа для памяти разговора (по-английски). Пусто, если анализа не было."""
    if report is None or report.skipped or not report.rounds:
        return ""
    if not report.tools:
        return "static analysis: no analyzers available, nothing was checked"
    names = ", ".join(report.tools)
    if not report.remaining:
        return f"static analysis ({names}): clean" + (f" after {report.fixed_rounds} fix round(s)" if report.fixed_rounds else "")
    return f"static analysis ({names}): {len(report.remaining)} open issue(s) after {report.fixed_rounds} fix round(s)"


def zone_line(level: str, a: int, b: int) -> str:
    return f"{level}: lines {a}-{b}"
