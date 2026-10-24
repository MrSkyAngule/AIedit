"""Поиск похожего на секреты: ключи, токены, пароли. Только подсказка человеку, не гарантия."""
import re
from dataclasses import dataclass
from pathlib import Path

from .guard import CONCEALED, Region, line_regions

_TOKENS = {
    "ключ API (sk-...)": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "токен GitHub": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "ключ AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "токен Slack": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}
_WORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|private[_-]?key|auth[_-]?key|access[_-]?key)"
# без ведущего [\w.-]*: он давал квадратичный перебор на длинных строках (минифицированный код)
_QUOTED = re.compile(rf"(?i){_WORDS}[\w.-]{{0,64}}[\"']?\s*[:=]\s*[\"']([^\"'\s]{{6,}})[\"']")
_DOTENV = re.compile(rf"(?i)^\s*(?:export\s+)?[A-Z][A-Z0-9_]{{0,100}}{_WORDS}[A-Z0-9_]{{0,64}}\s*=\s*([^\s#]{{6,}})")
_PRIVATE_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_PLACEHOLDER_VALUE = re.compile(
    r"(?i)^(?:<.*>|\$\{?\w+\}?|%\(\w+\)s|\{.*\}|x{4,}|\*{4,}|\.{3}|your[_-].*|changeme|example.*|"
    r"placeholder.*|os\.|none|null|true|false)")
_SUSPICIOUS_FILE = re.compile(r"(?i)^(?:\.env(?!\.(?:example|sample|template)$).*|.*\.(?:pem|key|p12|pfx)|"
                              r"id_(?:rsa|dsa|ecdsa|ed25519)|credentials.*\.json)$")


@dataclass(frozen=True)
class Finding:
    line: int  # с 0
    kind: str


def suspicious_file(path: Path | str) -> bool:
    """Имя файла говорит, что внутри, скорее всего, секреты (.env, ключи)."""
    return bool(_SUSPICIOUS_FILE.match(Path(path).name))


def scan(text: str) -> list[Finding]:
    found: dict[int, str] = {}
    in_key = False
    for i, line in enumerate(text.split("\n")):
        if in_key:
            found[i] = "приватный ключ"
            in_key = not _PRIVATE_END.search(line)
            continue
        if _PRIVATE_BEGIN.search(line):
            found[i] = "приватный ключ"
            in_key = not _PRIVATE_END.search(line)
            continue
        for kind, rx in _TOKENS.items():
            if rx.search(line):
                found[i] = kind
                break
        else:
            for rx in (_QUOTED, _DOTENV):
                m = rx.search(line)
                if m and not _PLACEHOLDER_VALUE.match(m[1]):
                    found[i] = "пароль или токен в значении"
                    break
    return [Finding(i, k) for i, k in sorted(found.items())]


def uncovered(findings: list[Finding], regions: list[Region], n_lines: int) -> list[Finding]:
    """Находки, которые ещё видны модели (не закрыты hidden или interface)."""
    lr = line_regions(n_lines, regions)
    return [f for f in findings if f.line >= n_lines or lr[f.line] is None or lr[f.line].level not in CONCEALED]


def to_ranges(findings: list[Finding]) -> list[tuple[int, int]]:
    """Склеивает подряд идущие строки в диапазоны."""
    ranges: list[list[int]] = []
    for f in findings:
        if ranges and f.line == ranges[-1][1] + 1:
            ranges[-1][1] = f.line
        else:
            ranges.append([f.line, f.line])
    return [(a, b) for a, b in ranges]
