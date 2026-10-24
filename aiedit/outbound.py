"""Фильтр исходящего: последняя проверка всего, что уходит модели, прямо перед отправкой.

Зоны и запреты защищают файлы, а этот фильтр - всё остальное: текст задачи и чата, историю,
заметки, вывод проверок. Через него идёт каждый запрос к любой модели (исполнители, дирижёр,
проверяющий, переводчик), поэтому новый текст, который начнёт уходить наружу, тоже его пройдёт.

Ищет секреты, слова из списка человека (имена клиентов, адреса серверов) и строки закрытого кода,
которые человек сам вставил в задачу. Про каждое новое значение спрашивает человека: скрыть,
отправить как есть или отменить. Скрытое заменяется меткой <<masked-N>>, а в ответе модели метки
возвращаются обратно, поэтому правки с такими местами применяются к настоящему тексту.

Фильтр узнаёт только то, что умеет узнать. Пересказ секрета своими словами он не поймает.
"""
import re
from dataclasses import dataclass, replace
from typing import Callable

from . import secrets_scan

MIN_WORD = 2    # слова короче ловили бы всё подряд
MIN_LINE = 12   # строки кода короче слишком часто совпадают с обычным кодом
_LABEL = re.compile(r"<<masked-(\d+)>>")

CHOICES = ("mask", "send", "cancel")


class SendCancelled(Exception):
    """Человек отменил отправку в окне фильтра."""


@dataclass(frozen=True)
class Finding:
    kind: str   # для человека: «ключ API», «слово из списка»...
    value: str  # само найденное; в журнал не пишется

    def preview(self) -> str:
        """Обрезанное значение для окна: узнать можно, переписать с экрана целиком нельзя."""
        v = " ".join(self.value.split())
        if self.kind == "слово из списка":
            return v
        return v[:6] + "…" + v[-3:] if len(v) > 12 else v[:3] + "…"


class Gate:
    """decide(findings, model) -> "mask" | "send" | "cancel" спрашивает человека про новые находки.
    Ответ запоминается до reset(): про одно значение дважды не спрашиваем. Отмена не запоминается."""

    def __init__(self, words=(), decide: Callable[[list[Finding], str], str] | None = None,
                 log: Callable[[dict], None] | None = None, enabled: bool = True):
        self.set_words(words)
        self.decide = decide or (lambda findings, model: "mask")
        self.log = log
        self.enabled = enabled
        self.closed: set[str] = set()      # строки закрытого кода, замеченные в тексте человека
        self.choice: dict[str, str] = {}   # значение -> mask | send
        self.labels: dict[str, str] = {}   # значение -> метка
        self.values: dict[str, str] = {}   # метка -> значение

    def set_words(self, words):
        self.words = sorted({w.strip() for w in words if len(w.strip()) >= MIN_WORD}, key=len, reverse=True)

    def reset(self):
        self.closed.clear()
        self.choice.clear()

    def watch_closed(self, text: str, closed_lines) -> int:
        """Запоминает строки закрытого кода, которые есть в тексте человека. Возвращает, сколько нашлось."""
        lines = {l.strip() for l in text.split("\n")}
        hits = {l.strip() for l in closed_lines if len(l.strip()) >= MIN_LINE and l.strip() in lines}
        self.closed |= hits
        return len(hits)

    def find(self, text: str) -> list[Finding]:
        found: dict[str, str] = {}
        for a, b, kind in secrets_scan.spans(text):
            found.setdefault(text[a:b], kind)
        for line in self.closed:
            if line in text:
                found.setdefault(line, "строка закрытого кода")
        for w in self.words:
            for m in re.finditer(re.escape(w), text, re.IGNORECASE):
                found.setdefault(m.group(), "слово из списка")
        return [Finding(k, v) for v, k in found.items() if v.strip() and not _LABEL.fullmatch(v)]

    def review(self, texts: list[str], model: str) -> list[str]:
        """Проверяет тексты одного запроса. Возвращает значения, которые надо скрыть (длинные первыми).
        SendCancelled, если человек отменил."""
        if not self.enabled:
            return []
        found: dict[str, Finding] = {}
        for t in texts:
            for f in self.find(t):
                found.setdefault(f.value, f)
        new = [f for v, f in found.items() if v not in self.choice]
        if new:
            answer = self.decide(new, model)
            answer = answer if answer in CHOICES else "cancel"
            self._log(model, new, answer)
            if answer == "cancel":
                raise SendCancelled("отправка отменена в фильтре исходящего")
            for f in new:
                self.choice[f.value] = answer
        return sorted((v for v in found if self.choice.get(v) == "mask"), key=len, reverse=True)

    def _log(self, model: str, findings: list[Finding], answer: str):
        if self.log:
            kinds: dict[str, int] = {}
            for f in findings:
                kinds[f.kind] = kinds.get(f.kind, 0) + 1
            self.log({"mode": "filter", "model": model, "decision": answer, "found": kinds})

    def label(self, value: str) -> str:
        if value not in self.labels:
            lab = f"<<masked-{len(self.labels) + 1}>>"
            self.labels[value], self.values[lab] = lab, value
        return self.labels[value]

    def mask(self, text: str, values: list[str]) -> str:
        for v in values:  # длинные первыми: слово внутри ключа не испортит замену ключа
            text = text.replace(v, self.label(v))
        return text

    def unmask(self, text: str) -> str:
        return _LABEL.sub(lambda m: self.values.get(m.group(), m.group()), text)

    # --- обёртки: через них идут все запросы ---
    def wrap(self, provider) -> "GuardedProvider":
        return provider if isinstance(provider, GuardedProvider) else GuardedProvider(provider, self)

    def wrap_roster(self, roster):
        from .roster import Roster
        return Roster([replace(w, provider=self.wrap(w.provider)) for w in roster.workers.values()])

    def wrap_transport(self, send: Callable[[dict], str], model: str) -> Callable[[dict], str]:
        """Для переводчика: запрос в форме chat/completions."""
        def guarded(payload: dict) -> str:
            msgs = payload["messages"]
            values = self.review([m["content"] for m in msgs], model)
            if not values:
                return send(payload)
            out = send({**payload, "messages": [{**m, "content": self.mask(m["content"], values)} for m in msgs]})
            return self.unmask(out)
        return guarded


class GuardedProvider:
    """Провайдер за фильтром. Снаружи выглядит как обычный: name, complete и всё остальное от внутреннего."""

    def __init__(self, inner, gate: Gate):
        self.inner, self.gate = inner, gate

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, system: str, messages: list[dict]) -> str:
        g = self.gate
        values = g.review([system] + [m["content"] for m in messages], self.inner.name)
        if not values:
            return self.inner.complete(system, messages)
        reply = self.inner.complete(g.mask(system, values),
                                    [{**m, "content": g.mask(m["content"], values)} for m in messages])
        return g.unmask(reply)
