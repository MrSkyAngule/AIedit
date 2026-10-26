"""Конвейер проверки правок ИИ.

    автор (Claude) пишет -> редактор запускает тесты -> красные: автор правит, снова запуск, пока не позеленеет
    -> проверяющий (GLM) -> автор исправляет и сам проверяет -> проверяющий -> ... пока замечаний нет
    -> тесты ещё раз, если GLM что-то заставил поменять
    -> статанализ: его замечания тоже возвращаются автору -> если он что-то поправил, тесты и проверяющий ещё раз

Человеку правки отдаются только после этого, вместе с честным итогом: зелёные тесты или красные. Каждый цикл ограничен (review.MAX_ROUNDS, analysis.MAX_ROUNDS) и
останавливается раньше, если автор не может убрать одно и то же замечание: тогда причина попадает в отчёт.
"""
from pathlib import Path
from typing import Callable

from . import analysis, review as security, verify
from .roster import Roster, Worker
from .workspace import Workspace

def fix_helpers(helpers: dict) -> dict:
    """Инструменты, которые автор получает на кругах исправления (терминал передаётся отдельно)."""
    return {k: v for k, v in helpers.items() if k in ("web", "install", "search", "versions", "nav") and v is not None}


MAX_PASSES = 2  # «ревью, затем статанализ» повторяется, только если статанализ изменил код


def _merge_review(total: security.ReviewReport | None, part: security.ReviewReport) -> security.ReviewReport:
    if total is None:
        return part
    total.rounds += part.rounds
    total.fixes += part.fixes
    total.gaps, total.stalled, total.stopped = part.gaps, part.stalled, part.stopped
    return total


def _merge_analysis(total: analysis.AnalysisReport | None, part: analysis.AnalysisReport) -> analysis.AnalysisReport:
    if total is None:
        return part
    total.rounds += part.rounds
    total.fixes += part.fixes
    total.tools = sorted(set(total.tools) | set(part.tools))
    total.notes += [n for n in part.notes if n not in total.notes]
    total.stalled, total.stopped, total.skipped = part.stalled, part.stopped, part.skipped
    return total


def run(task: str, ws: Workspace, roster: Roster | None, author_of: Callable[[Path | None], Worker],
        on_progress: Callable[[str], None] | None = None, should_stop: Callable[[], bool] | None = None,
        english_reply: bool = False, review_on: bool = True, analyze_on: bool = True,
        terminal=None, analyzers=None, runner=None, verify_command: str = "", verify_on: bool = True,
        helpers: dict | None = None):
    """Возвращает (отчёт статанализа, отчёт проверяющего, отчёт проверки тестами); None, если этап не шёл.
    helpers - инструменты автора для исправлений (install, versions, web ...), без terminal."""
    say = on_progress or (lambda _: None)
    stop = should_stop or (lambda: False)
    do_review = review_on and roster is not None and roster.reviewer() is not None
    review_report = analysis_report = None
    state = {"report": None, "diff": None}

    def gate():
        """Тесты до зелёного. Запускаются, только если код изменился с прошлой проверки."""
        if terminal is None or not verify_on or not ws.changes() or stop():
            return
        diff = ws.review_diff()
        if diff == state["diff"]:
            return
        command, source = verify.detect_command(ws, verify_command)
        if not command:
            state["diff"] = diff
            return
        part = verify.loop(task, ws, author_of(None), terminal, command, source, on_progress, should_stop,
                           english_reply, helpers=helpers)
        state["diff"] = ws.review_diff()
        total = state["report"]
        if total is not None:
            part.runs, part.fixes = total.runs + part.runs, total.fixes + part.fixes
            part.notes = total.notes + [n for n in part.notes if n not in total.notes]
        state["report"] = part

    gate()
    for n in range(MAX_PASSES):
        if not ws.changes() or stop():
            break
        if do_review:
            review_report = _merge_review(review_report, security.run_cycle(
                task, ws, roster, author_of, on_progress, should_stop, english_reply, terminal=terminal))
            if review_report.stopped:
                break
            gate()  # GLM мог заставить автора что-то поменять: тесты ещё раз
        if analyze_on and ws.changes():
            part = analysis.run_cycle(task, ws, author_of, on_progress, should_stop, english_reply, terminal=terminal,
                                      analyzers=analyzers, runner=runner)
            analysis_report = _merge_analysis(analysis_report, part)
            if part.stopped:
                break
            changed = part.fixes > 0
            gate()
        else:
            changed = False
        if not (changed and do_review) or n == MAX_PASSES - 1:
            break
        say("статанализ внёс правки: проверяющий смотрит ещё раз")
    return analysis_report, review_report, state["report"]
