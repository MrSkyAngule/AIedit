"""Ядро защиты: зоны (метки по строкам), скрытие кода от модели, проверка правок.

Метки хранятся вне текста: зона - это уровень и диапазон строк.
Уровни (от слабого к строгому):
  explain  - не защита: ИИ по запросу объясняет этот код
  confirm  - модель может менять, но только после подтверждения человека
  readonly - модель видит код, но менять его нельзя
  interface - модель видит сигнатуры (имя, типы) и комментарии # зоны, тела и значения скрыты, менять нельзя
  hidden   - модель код не видит вообще (уходит заглушка), менять нельзя
"""
import ast
import difflib
import io
import re
import textwrap
import tokenize
from dataclasses import dataclass, field, replace

SEVERITY = {"explain": 0, "confirm": 1, "readonly": 2, "interface": 3, "hidden": 4}
CONCEALED = ("interface", "hidden")  # уровни, чей код модель не получает
PLACEHOLDER = re.compile(r"^\s*<<ai-hidden:(\d+):\d+ lines(?: \|.*)?>>\s*$")
MAX_SIGNATURE = 400
MAX_COMMENTS = 800  # дополнительное место в заглушке interface под комментарии зоны
MAX_NOTE = 300


@dataclass(frozen=True)
class Region:
    level: str
    start: int  # первая строка зоны (с 0)
    end: int    # последняя строка зоны, включительно
    note: str = ""     # заметка человека: что про этот код можно знать модели (для interface и hidden)
    note_en: str = ""  # её английский перевод, если был


@dataclass
class Verdict:
    violations: list[str]  # для человека, по-русски
    needs_confirm: list[str]
    violations_en: list[str] = field(default_factory=list)  # то же для модели, по-английски
    needs_confirm_en: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass
class Redacted:
    text: str
    chunks: list[str]
    line_map: list[int]  # строка оригинала -> строка текста для модели


def line_regions(n_lines: int, regions: list[Region]) -> list[Region | None]:
    """Для каждой строки - самая строгая зона, которая её накрывает."""
    result: list[Region | None] = [None] * n_lines
    for r in regions:
        for i in range(max(r.start, 0), min(r.end, n_lines - 1) + 1):
            cur = result[i]
            if cur is None or SEVERITY[r.level] > SEVERITY[cur.level]:
                result[i] = r
    return result


def _signature_items(code: str) -> list[tuple[int, str]]:
    """Сигнатуры верхнего уровня (Python) с номером строки: имя, аргументы, типы. Без тел, значений и докстрингов."""
    try:
        tree = ast.parse(textwrap.dedent(code))
    except (SyntaxError, ValueError):
        return []
    dots = ast.Constant(value=...)

    def header(node) -> str:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            args.defaults = [dots for _ in args.defaults]
            args.kw_defaults = [None if d is None else dots for d in args.kw_defaults]
            clone = type(node)(name=node.name, args=args, body=[ast.Expr(dots)],
                               decorator_list=[], returns=node.returns, type_comment=None)
        else:
            clone = ast.ClassDef(name=node.name, bases=node.bases, keywords=[],
                                 body=[ast.Expr(dots)], decorator_list=[])
        return ast.unparse(ast.fix_missing_locations(clone)).splitlines()[0]

    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.lineno, header(node)))
            if isinstance(node, ast.ClassDef):
                found += [(n.lineno, "  " + header(n)) for n in node.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [(node.lineno, f"{t.id} = ...") for t in targets if isinstance(t, ast.Name)]
    return found


def signatures(code: str) -> list[str]:
    return [text for _, text in _signature_items(code)]


def comments(code: str) -> list[tuple[int, str]]:
    """Комментарии # зоны: (номер строки, текст). Читает токены, поэтому # внутри строки комментарием не считается.
    Докстринги - не комментарии и сюда не попадают."""
    found: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(textwrap.dedent(code)).readline):
            if tok.type == tokenize.COMMENT and tok.string.lstrip("#").strip():
                found.append((tok.start[0], "# " + tok.string.lstrip("#").strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # кусок оборван посреди скобки или отступа: берём то, что успели прочитать
    return found


def interface_view(code: str) -> tuple[list[str], bool]:
    """Что видит модель в зоне interface: сигнатуры и комментарии в том порядке, в каком они стоят в коде.
    Так видно, к чему относится комментарий. Второе значение: есть ли среди них комментарии."""
    notes = comments(code)
    merged = sorted([(line, 0, text) for line, text in _signature_items(code)]
                    + [(line, 1, text) for line, text in notes])  # на одной строке сигнатура идёт раньше комментария
    return [text for _, _, text in merged], bool(notes)


def _clean(text: str, limit: int) -> str:
    return " ".join(text.replace(">>", "> >").split())[:limit]


def notes_for(regions: list[Region], a: int, b: int) -> list[str]:
    """Заметки закрытых зон, которые пересекают строки a..b-1. Модели идёт английская версия, если она есть."""
    seen: list[str] = []
    for r in sorted(regions, key=lambda r: r.start):
        text = (r.note_en or r.note).strip()
        if r.level in CONCEALED and r.start < b and r.end >= a and text and text not in seen:
            seen.append(text)
    return seen


def _placeholder(k: int, code: str, n: int, with_signatures: bool, notes: list[str] | None = None) -> str:
    sigs, has_comments = interface_view(code) if with_signatures else ([], False)
    parts = []
    if sigs:
        parts.append(_clean(" ; ".join(s.strip() if i else s for i, s in enumerate(sigs)),
                            MAX_SIGNATURE + (MAX_COMMENTS if has_comments else 0)))
    if notes:
        parts.append("note: " + _clean(" / ".join(notes), MAX_NOTE))
    return f"<<ai-hidden:{k}:{n} lines{''.join(' | ' + p for p in parts)}>>"


def redact(text: str, regions: list[Region]) -> Redacted:
    """Заменяет hidden- и interface-код заглушками (у interface в заглушке есть сигнатуры)."""
    lines = text.split("\n")
    lr = line_regions(len(lines), regions)

    def level(i):
        return lr[i].level if lr[i] is not None and lr[i].level in CONCEALED else None

    out: list[str] = []
    chunks: list[str] = []
    line_map = [0] * len(lines)
    i = 0
    while i < len(lines):
        if level(i):
            j = i
            while j < len(lines) and level(j) == level(i):
                line_map[j] = len(out)
                j += 1
            chunks.append("\n".join(lines[i:j]))
            out.append(_placeholder(len(chunks) - 1, chunks[-1], j - i, level(i) == "interface",
                                    notes_for(regions, i, j)))
            i = j
        else:
            line_map[i] = len(out)
            out.append(lines[i])
            i += 1
    return Redacted("\n".join(out), chunks, line_map)


def restore(model_text: str, chunks: list[str]) -> str:
    """Подставляет скрытые куски на место заглушек. Лишние дубли заглушек отбрасываются."""
    out, used = [], set()
    for line in model_text.split("\n"):
        m = PLACEHOLDER.match(line)
        if not m:
            out.append(line)
            continue
        k = int(m[1])
        if k < len(chunks) and k not in used:
            used.add(k)
            out.append(chunks[k])
    return "\n".join(out)


def check_edit(old: str, new: str, regions: list[Region]) -> Verdict:
    """Сверяет правку с зонами исходного текста."""
    a, b = old.split("\n"), new.split("\n")
    violations: list[str] = []
    confirm: list[str] = []
    violations_en: list[str] = []
    confirm_en: list[str] = []
    guarded = [r for r in regions if r.level != "explain"]

    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for r in guarded:
            if i1 == i2:  # чистая вставка между строками i1-1 и i1
                touched = r.start <= i1 - 1 and i1 <= r.end
            else:
                touched = r.start < i2 and i1 <= r.end
            if not touched:
                continue
            msg = f"{r.level}-зона (строки {r.start + 1}-{r.end + 1})"
            msg_en = f"{r.level} zone (lines {r.start + 1}-{r.end + 1})"
            if r.level == "confirm":
                if msg not in confirm:
                    confirm.append(msg)
                    confirm_en.append(msg_en)
            elif f"правка внутри {msg}" not in violations:
                violations.append(f"правка внутри {msg}")
                violations_en.append(f"edit inside {msg_en}")
    return Verdict(violations, confirm, violations_en, confirm_en)


def remap(regions: list[Region], old: str, new: str) -> list[Region]:
    """Переносит зоны на новый текст по диффу (после применённой правки ИИ)."""
    a, b = old.split("\n"), new.split("\n")
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()

    def mapped(i: int, is_end: bool) -> int:
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= i < i2:
                if tag == "equal":
                    return j1 + i - i1
                if tag == "delete":
                    return j1 - 1 if is_end else j1
                return j2 - 1 if is_end else j1
        return i

    result = []
    for r in regions:
        s, e = mapped(r.start, False), mapped(r.end, True)
        if e >= s and s < len(b):
            result.append(replace(r, start=s, end=min(e, len(b) - 1)))
    return result
