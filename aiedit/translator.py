"""Переводчик задач (ru->en) и ответов (en->ru) через gpt-4o-mini от OpenAI.

Зачем: модель-исполнитель получает английский, это дешевле по токенам; человек пишет и читает по-русски.
Переводчику уходит только проза. Код, пути, идентификаторы, ссылки и строки с секретами заменяются
метками {{n}} локально и вставляются обратно после перевода. Если перевод неудачен (сеть, ключ,
потеряна метка), возвращается оригинал: задача не должна исказиться молча.
"""
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import secrets_scan
from .outbound import SendCancelled

MODEL = "gpt-4o-mini"
URL = "https://api.openai.com/v1/chat/completions"
MAX_CHUNK = 3000
MAX_TEXT = 30_000

SYSTEMS = {
    "ru-en": ("You are a translation engine. Translate the user's message from Russian to English for a coding "
              "assistant. Keep the meaning exact; add, drop and explain nothing. Tokens like {{1}} are protected: "
              "copy each one unchanged, exactly once. Output only the translation."),
    "en-ru": ("You are a translation engine. Translate the user's message from English to Russian for a developer. "
              "Keep the meaning exact; add, drop and explain nothing. Tokens like {{1}} are protected: "
              "copy each one unchanged, exactly once. Output only the translation."),
}

_CYR = re.compile("[А-Яа-яЁё]")
_LAT = re.compile("[A-Za-z]")
_TOKEN = re.compile(r"\{\{(\d+)\}\}")
_PROTECT = re.compile("|".join([
    r"```.*?```",                                        # блоки кода
    r"`[^`\n]+`",                                        # код в строке
    r"https?://\S+",                                     # ссылки
    r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]*",                 # пути с папками
    r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|json|md|txt|toml|yml|yaml|html|css|rs|go|java|cpp|c|h|sh)\b",  # имена файлов
    r"--?[A-Za-z][\w-]*",                                # флаги
    r"\b[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b",      # snake_case
    r"\b[a-z]+[A-Z][A-Za-z0-9]*\b",                      # camelCase
    r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b",            # PascalCase
    r"\b[A-Za-z_]\w*(?:\.\w+)+\b",                       # a.b.c
    r"\b\w+\(\)",                                        # f()
]), re.S)


@dataclass
class Translation:
    text: str            # то, что использовать дальше: перевод или оригинал
    translated: bool     # был ли реальный перевод
    note: str = ""       # почему не переведено или что пошло не так (для человека)
    sent_chars: int = 0  # сколько символов реально ушло переводчику


def has_cyrillic(text: str, share: float = 0.0) -> bool:
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    return cyr > 0 and cyr >= share * (cyr + lat)


def mask(text: str) -> tuple[str, list[str]]:
    """Заменяет защищённые куски метками {{n}}. Возвращает (текст с метками, список оригиналов)."""
    saved: list[str] = []

    def put(piece: str) -> str:
        saved.append(piece)
        return "{{%d}}" % len(saved)

    lines = text.split("\n")
    for f in secrets_scan.scan(text):  # строки с секретами целиком остаются у нас
        lines[f.line] = put(lines[f.line])
    return _PROTECT.sub(lambda m: put(m.group()), "\n".join(lines)), saved


def unmask(text: str, saved: list[str]) -> str | None:
    """Возвращает исходные куски на места меток. None, если метка потеряна или задвоена."""
    found = [int(n) for n in _TOKEN.findall(text)]
    if sorted(found) != list(range(1, len(saved) + 1)):
        return None
    return _TOKEN.sub(lambda m: saved[int(m[1]) - 1], text)


def chunks(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Режет по абзацам, чтобы не рвать метки и фразы."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > limit:  # один огромный абзац - режем по строкам
            cut = cur.rfind("\n", 0, limit) if "\n" in cur[:limit] else limit
            out.append(cur[:cut])
            cur = cur[cut:].lstrip("\n")
    return out + [cur] if cur else out


def openai_transport(api_key: str, url: str = URL, timeout: float = 30) -> Callable[[dict], str]:
    """Обычный HTTPS-запрос к chat/completions. Без SDK, чтобы не тянуть зависимости."""
    def send(payload: dict) -> str:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]
    return send


def provider_transport(provider) -> Callable[[dict], str]:
    """Даёт переводчику любую модель: принимает запрос в форме chat/completions и зовёт provider.complete."""
    def send(payload: dict) -> str:
        msgs = payload["messages"]
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        return provider.complete(system, [m for m in msgs if m["role"] != "system"])
    return send


class Translator:
    def __init__(self, transport: Callable[[dict], str], log: Callable[[str, int], None] | None = None,
                 model: str = MODEL):
        self.transport, self.log, self.model = transport, log, model
        self.cache: dict[tuple[str, str], Translation] = {}

    @classmethod
    def from_env(cls, log=None) -> "Translator | None":
        key = os.environ.get("OPENAI_API_KEY")
        return cls(openai_transport(key), log) if key else None

    def ru_to_en(self, text: str) -> Translation:
        return self._run("ru-en", text, needs=lambda t: has_cyrillic(t))

    def en_to_ru(self, text: str) -> Translation:
        return self._run("en-ru", text, needs=lambda t: not has_cyrillic(t, 0.2) and bool(_LAT.search(t)))

    def _run(self, direction: str, text: str, needs) -> Translation:
        if not text.strip() or not needs(text):
            return Translation(text, False, "перевод не нужен")
        if len(text) > MAX_TEXT:
            return Translation(text, False, "текст слишком длинный для перевода")
        if _TOKEN.search(text):
            return Translation(text, False, "в тексте есть {{n}}, перевод может перепутать метки")
        key = (direction, text)
        if key in self.cache:
            return self.cache[key]
        masked, saved = mask(text)
        prose = _TOKEN.sub("", masked)
        if not (_CYR.search(prose) if direction == "ru-en" else _LAT.search(prose)):
            return Translation(text, False, "переводить нечего: в тексте только код")
        sent = 0
        try:
            parts = []
            for piece in chunks(masked):
                reply = self.transport({"model": self.model, "temperature": 0, "messages": [
                    {"role": "system", "content": SYSTEMS[direction]}, {"role": "user", "content": piece}]})
                sent += len(piece)
                if self.log:
                    self.log(direction, len(piece))
                parts.append(reply.strip())
            result = unmask("\n\n".join(parts), saved)
        except SendCancelled:  # человек отменил отправку: идти дальше с оригиналом нельзя
            raise
        except Exception as e:  # сеть, ключ, лимиты, формат ответа
            return Translation(text, False, f"переводчик недоступен: {type(e).__name__}: {e}", sent)
        if result is None or not result.strip() or len(result) > 4 * len(text) + 200:
            return Translation(text, False, "перевод отброшен: потерялись защищённые куски", sent)
        out = Translation(result, True, "", sent)
        self.cache[key] = out
        return out
