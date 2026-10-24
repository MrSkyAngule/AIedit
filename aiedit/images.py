"""Картинки для модели: скриншот, приложенный человеком, или картинка из проекта (инструмент view_image).

Внутри редактора картинка - это короткая метка <<image:N>> в тексте сообщения. Сами байты хранятся
здесь, а провайдер перед отправкой превращает метку в блок картинки своего API. Так остальной путь
сообщения (фильтр исходящего, перевод, журнал) работает с обычным текстом.

Фильтр исходящего картинки не читает: что на ней написано, уходит модели как есть.
"""
import base64
import re
import threading

MAX_BYTES = 3_500_000  # после base64 это около 4.7 МБ: у Anthropic предел 5 МБ на картинку
_MARK = re.compile(r"<<image:(\d+)>>")
_MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif")]

_lock = threading.Lock()
_store: dict[str, tuple[str, str, str]] = {}  # метка -> (тип, base64, имя для человека)


def media_type(data: bytes) -> str | None:
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def register(data: bytes, name: str) -> str:
    """Метка для картинки или ValueError с причиной (по-английски: её может увидеть модель)."""
    kind = media_type(data)
    if kind is None:
        raise ValueError("not a PNG, JPEG, GIF or WebP image")
    if len(data) > MAX_BYTES:
        raise ValueError(f"image too large ({len(data) // 1000} KB, at most {MAX_BYTES // 1000} KB)")
    with _lock:
        mark = f"<<image:{len(_store) + 1}>>"
        _store[mark] = (kind, base64.b64encode(data).decode(), name)
    return mark


def name_of(mark: str) -> str:
    return _store.get(mark, ("", "", "?"))[2]


def marks_in(text: str) -> list[str]:
    return [m.group() for m in _MARK.finditer(text) if m.group() in _store]


def split(text: str) -> list[tuple[str, object]]:
    """[("text", str) | ("image", (тип, base64))] - для провайдера. Неизвестная метка остаётся текстом."""
    parts: list[tuple[str, object]] = []
    pos = 0
    for m in _MARK.finditer(text):
        if m.group() not in _store:
            continue
        if m.start() > pos:
            parts.append(("text", text[pos:m.start()]))
        kind, data, _ = _store[m.group()]
        parts.append(("image", (kind, data)))
        pos = m.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts


def has_images(text: str) -> bool:
    return bool(marks_in(text))


def strip(text: str) -> str:
    """Метки заменены словами: для модели без картинок и для истории разговора."""
    return _MARK.sub(lambda m: f"[image: {name_of(m.group())}]" if m.group() in _store else m.group(), text)
