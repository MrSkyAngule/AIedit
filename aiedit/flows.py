"""Связка запроса с переводчиком: задача ru->en на входе, ответ en->ru на выходе.

Без переводчика (translator=None) функции ведут себя как прямые вызовы агента и оркестратора.
С переводчиком модель получает английскую задачу и отвечает по-английски, а человек видит русский
ответ. Если перевод не вышел, запрос идёт с оригиналом, а причина попадает в notes.
"""
from pathlib import Path
from typing import Callable

from . import agent, analysis, conductor, orchestrator, review as security
from .translator import Translator

_BENIGN = ("перевод не нужен", "переводить нечего")


def _say(on_progress, text: str):
    if on_progress:
        on_progress(text)


def _task_to_en(task: str, tr: Translator | None, notes: list[str], on_progress=None) -> str:
    if tr is None:
        return task
    res = tr.ru_to_en(task)
    if res.translated:
        _say(on_progress, "задача переведена: " + res.text.replace("\n", " ")[:160])
        return res.text
    if res.note and res.note not in _BENIGN:
        notes.append(f"{res.note}. Задача ушла как есть.")
    return task


def _answer_to_ru(text: str, tr: Translator | None, notes: list[str], on_progress=None) -> str:
    """Возвращает текст для показа человеку. Пустой и непереведённый возвращаются как есть."""
    if tr is None or not text.strip():
        return text
    _say(on_progress, "перевожу ответ...")
    res = tr.en_to_ru(text)
    if res.translated:
        return res.text
    if res.note and res.note not in _BENIGN:
        notes.append(f"{res.note}. Ответ показан как есть.")
    return text


def _asker(ask, tr: Translator | None, notes: list[str]):
    """Вопрос модели человеку на его языке, ответ человека модели - на её."""
    if ask is None:
        return None

    def asked(question: str) -> str | None:
        answer = ask(_answer_to_ru(question, tr, notes))
        return None if answer is None else _task_to_en(answer, tr, notes)
    return asked


def run_agent(task: str, ws, provider, history: list | None = None, open_file: Path | None = None,
              translator: Translator | None = None, on_progress: Callable[[str], None] | None = None,
              should_stop: Callable[[], bool] | None = None, roster=None, review_on: bool = False,
              author=None, analyze_on: bool = False, ask=None, terminal=None) -> agent.AgentResult:
    """Один агент. Если дан реестр с проверяющим и review_on, правки после работы проверяются, а замечания
    возвращаются автору (author - его исполнитель, по умолчанию та модель, что писала код).
    ask(вопрос) -> ответ человека или None: если дан, агент может переспросить, когда задача неоднозначна.
    terminal - sandbox.Terminal: агент может запускать команды в песочнице и проверять свои правки."""
    notes: list[str] = []
    en_task = _task_to_en(task, translator, notes, on_progress)
    res = agent.run(en_task, ws, provider, history, open_file, on_progress, should_stop,
                    english_reply=translator is not None, ask=_asker(ask, translator, notes), terminal=terminal)
    from .roster import Worker
    who = author or Worker("agent", provider, "", 0, 0, "mid")  # автор правок - та модель, что реально писала код
    if analyze_on and not res.stopped and ws.changes():
        res.analysis = analysis.run_cycle(en_task, ws, lambda p: who, on_progress, should_stop,
                                          english_reply=translator is not None)
        res.changes = ws.changes()
    if review_on and roster is not None and roster.reviewer() is not None and not res.stopped and ws.changes():
        res.review = security.run_cycle(en_task, ws, roster, lambda p: who, on_progress, should_stop,
                                        english_reply=translator is not None)
        res.changes = ws.changes()
    res.authors = {ws.rel(c.path): provider.name for c in ws.changes()}
    res.task_en = en_task if en_task != task else ""
    if res.from_model:
        res.text_en = res.text
        res.text = _answer_to_ru(res.text_en, translator, notes, on_progress)
    res.notes = notes
    return res


def run_conductor(task: str, ws, roster, conductor_provider, history: list | None = None,
                  open_file: Path | None = None, translator: Translator | None = None,
                  on_progress: Callable[[str], None] | None = None,
                  should_stop: Callable[[], bool] | None = None, review_on: bool = True,
                  analyze_on: bool = True, ask=None, terminal=None) -> conductor.ConductResult:
    """Дирижёр с переводом: задача уходит по-английски, итог плана возвращается человеку по-русски."""
    notes: list[str] = []
    en_task = _task_to_en(task, translator, notes, on_progress)
    res = conductor.run(en_task, ws, roster, conductor_provider, history, open_file, on_progress, should_stop,
                        english_reply=translator is not None, review_on=review_on, analyze_on=analyze_on,
                        ask=_asker(ask, translator, notes), terminal=terminal)
    res.task_en = en_task if en_task != task else ""
    res.text_en = res.text
    res.text = _answer_to_ru(res.text_en, translator, notes, on_progress)
    res.notes = notes
    return res


def run_ask(translator: Translator | None, text: str, task: str, regions, providers, source=None,
            history=None, context=None, root=None, on_progress=None) -> orchestrator.Result:
    notes: list[str] = []
    en_task = _task_to_en(task, translator, notes, on_progress)
    res = orchestrator.ask(text, en_task, regions, providers, source, history, context, root,
                           english_reply=translator is not None)
    res.task_en = en_task if en_task != task else ""
    res.note_en = res.note
    res.note = _answer_to_ru(res.note_en, translator, notes, on_progress)
    res.notes = notes
    return res


def run_explain(translator: Translator | None, text: str, regions, providers, source=None,
                history=None, context=None, root=None, on_progress=None) -> orchestrator.Explanation:
    notes: list[str] = []
    res = orchestrator.explain(text, regions, providers, source, history, context, root,
                               english_reply=translator is not None)
    if res.zones:
        res.text_en = res.text
        res.text = _answer_to_ru(res.text_en, translator, notes, on_progress)
    res.notes = notes
    return res
