"""Подсветка синтаксиса: Python, C-подобные языки, JS/TS, shell/yaml/toml, JSON."""
import re
from dataclasses import dataclass

from .qt import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

PY_KEYWORDS = set("""False None True and as assert async await break class continue def del elif else except
finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case""".split())
PY_BUILTINS = set("""print len range int str float bool list dict set tuple open type isinstance super self cls
enumerate zip map filter sorted sum min max abs any all repr id input iter next object Exception ValueError
TypeError KeyError OSError""".split())
C_KEYWORDS = set("""if else for while do switch case default break continue return goto sizeof typedef struct union enum
class interface extends implements new delete this super static const final public private protected void int long
short char float double bool boolean byte unsigned signed var let function async await import export from package
namespace using try catch finally throw throws fn pub mut impl trait use mod match loop func type chan go defer
range select null nil true false undefined in of typeof instanceof as is where override abstract virtual""".split())
SH_KEYWORDS = set("if then else elif fi for while do done case esac function in return export local echo cd".split())


@dataclass(frozen=True)
class Lang:
    keywords: frozenset
    builtins: frozenset = frozenset()
    line_comment: str = "#"
    block: tuple | None = None  # (начало, конец) многострочных строк или комментариев
    quotes: str = "\"'"
    decorators: bool = False
    defs: frozenset = frozenset()  # слова, после которых идёт имя (def, class)


PYTHON = Lang(frozenset(PY_KEYWORDS), frozenset(PY_BUILTINS), "#", None, "\"'", True, frozenset({"def", "class"}))
CLIKE = Lang(frozenset(C_KEYWORDS), frozenset(), "//", ("/*", "*/"), "\"'`", False,
             frozenset({"function", "class", "fn", "func", "struct", "interface", "enum", "trait"}))
SHELL = Lang(frozenset(SH_KEYWORDS), frozenset(), "#", None, "\"'", False, frozenset({"function"}))
DATA = Lang(frozenset({"true", "false", "null", "True", "False", "None"}), frozenset(), "#", None, "\"'")
JSON = Lang(frozenset({"true", "false", "null"}), frozenset(), "", None, '"')

BY_SUFFIX = {
    ".py": PYTHON, ".pyw": PYTHON,
    **{s: CLIKE for s in (".js", ".jsx", ".ts", ".tsx", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".go",
                          ".rs", ".cs", ".kt", ".swift", ".php", ".scala", ".dart")},
    ".sh": SHELL, ".bash": SHELL, ".zsh": SHELL,
    ".yml": DATA, ".yaml": DATA, ".toml": DATA, ".ini": DATA, ".cfg": DATA, ".env": DATA,
    ".json": JSON,
}


def language_for(suffix: str) -> Lang | None:
    return BY_SUFFIX.get(suffix.lower())


def _token_regex(lang: Lang) -> re.Pattern:
    parts = []
    if lang.block:
        parts.append(rf"(?P<mstart>{re.escape(lang.block[0])})")
    elif lang is PYTHON:
        parts.append(r"(?P<mstart>\"\"\"|''')")
    if lang.line_comment:
        parts.append(rf"(?P<comment>{re.escape(lang.line_comment)}.*)")
    if lang.quotes:
        q = "".join(re.escape(c) for c in lang.quotes)
        parts.append(rf"(?P<string>(?P<q>[{q}])(?:\\.|(?!(?P=q)).)*(?:(?P=q)|$))")
    parts.append(r"(?P<number>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)")
    if lang.decorators:
        parts.append(r"(?P<deco>@[A-Za-z_][\w.]*)")
    parts.append(r"(?P<word>[A-Za-z_][\w]*)")
    return re.compile("|".join(parts))


def colors(dark: bool) -> dict[str, QColor]:
    if dark:
        return {"keyword": "#c678dd", "builtin": "#56b6c2", "string": "#98c379", "comment": "#7f848e",
                "number": "#d19a66", "deco": "#e5c07b", "def": "#61afef"}
    return {"keyword": "#7a3e9d", "builtin": "#0b7285", "string": "#2a7a2a", "comment": "#7a7f87",
            "number": "#b35900", "deco": "#9a6700", "def": "#1f5fbf"}


class Highlighter(QSyntaxHighlighter):
    def __init__(self, document, dark: bool = False):
        super().__init__(document)
        self.lang: Lang | None = None
        self.rx: re.Pattern | None = None
        self.formats: dict[str, QTextCharFormat] = {}
        for name, color in colors(dark).items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if name in ("keyword", "def"):
                fmt.setFontWeight(QFont.Weight.Bold)
            if name == "comment":
                fmt.setFontItalic(True)
            self.formats[name] = fmt

    def set_language(self, suffix: str):
        self.lang = language_for(suffix)
        self.rx = _token_regex(self.lang) if self.lang else None
        self.rehighlight()

    def highlightBlock(self, text: str):
        self.setCurrentBlockState(0)
        lang, rx = self.lang, self.rx
        if lang is None:
            return
        block_end = lang.block[1] if lang.block else None
        pos = 0
        if self.previousBlockState() > 0:  # внутри многострочной строки или комментария
            end = self.previousBlockState_end(lang)
            idx = text.find(end)
            if idx < 0:
                self.setFormat(0, len(text), self.formats["string" if lang is PYTHON else "comment"])
                self.setCurrentBlockState(self.previousBlockState())
                return
            pos = idx + len(end)
            self.setFormat(0, pos, self.formats["string" if lang is PYTHON else "comment"])
        prev_word = ""
        while True:
            m = rx.search(text, pos)
            if m is None:
                break
            pos = m.end()
            kind = m.lastgroup
            if kind == "mstart":
                start = m.start()
                end = block_end or m.group()
                idx = text.find(end, m.end())
                style = "string" if lang is PYTHON else "comment"
                if idx < 0:
                    self.setFormat(start, len(text) - start, self.formats[style])
                    self.setCurrentBlockState(2 if m.group() == "'''" else 1)
                    return
                pos = idx + len(end)  # закрылось на этой же строке - дальше идём после закрытия
                self.setFormat(start, pos - start, self.formats[style])
            elif kind == "word":
                w = m.group()
                if prev_word in lang.defs:
                    self.setFormat(m.start(), len(w), self.formats["def"])
                elif w in lang.keywords:
                    self.setFormat(m.start(), len(w), self.formats["keyword"])
                elif w in lang.builtins:
                    self.setFormat(m.start(), len(w), self.formats["builtin"])
                prev_word = w
                continue
            elif kind == "string":
                self.setFormat(m.start(), len(m.group()), self.formats["string"])
            elif kind in self.formats:
                self.setFormat(m.start(), len(m.group()), self.formats[kind])
            prev_word = ""

    def previousBlockState_end(self, lang: Lang) -> str:
        """Чем закрывается многострочный фрагмент, начатый в предыдущих строках."""
        if lang.block:
            return lang.block[1]
        return "'''" if self.previousBlockState() == 2 else '"""'
