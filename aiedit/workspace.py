"""Рабочая область проекта для ИИ: просмотр, поиск и правка файлов через инструменты.

Модель не видит диск напрямую. Каждый инструмент проходит через те же проверки:
запреты по папкам, зоны (hidden/interface/readonly/confirm), скрытие секретов.
Закрытые от ИИ файлы для модели просто не существуют. Правки копятся в наборе
изменений и на диск не пишутся: их применяет человек после просмотра диффа.
"""
import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import guard, images, policy, secrets_scan, sendlog
from .guard import Region
from .marks import Marks, sidecar

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".idea", ".vscode"}
MAX_FILE_BYTES = 1_000_000
MAX_LIST = 300
MAX_READ_LINES = 400
MAX_READ_CHARS = 60_000
MAX_HITS = 100
MAX_NEW_FILE = 200_000


@dataclass
class FileState:
    path: Path
    text: str  # текущий текст с учётом принятых правок
    regions: list[Region]
    base: str | None  # исходный текст; None для нового файла
    live: bool = False  # файл открыт во вкладке (его текст и зоны берутся оттуда)
    confirm: list[str] = field(default_factory=list)
    base_regions: list[Region] = field(default_factory=list)  # зоны для base (до правок)
    deleted: bool = False  # агент предложил удалить файл (или перенести в другое место)


@dataclass
class Change:
    path: Path
    old: str | None
    new: str
    regions: list[Region]
    needs_confirm: list[str]
    live: bool
    deleted: bool = False  # new тогда пустой: файл удаляется

    @property
    def created(self) -> bool:
        return self.old is None


class Workspace:
    def __init__(self, root: Path, live: dict | None = None, model: str = "", log_enabled: bool = True):
        self.root = Path(os.path.realpath(root))
        self.log_enabled = log_enabled  # предпросмотр ничего никуда не отправляет, значит и не пишет в журнал
        self.live = {os.path.realpath(p): v for p, v in (live or {}).items()}  # путь -> (текст, зоны)
        self.model = model
        self.states: dict[Path, FileState] = {}
        self.sent: set[Path] = set()  # файлы, содержимое которых уже ушло модели
        self.read_only = False  # режим проверки: инструменты правки отказывают
        self.edit_scope: set[Path] | None = None  # если задано, править можно только эти файлы

    # --- доступ ---
    def resolve(self, rel: str) -> Path | None:
        """Путь внутри проекта или None. Закрытые от ИИ пути неотличимы от несуществующих."""
        rel = (rel or ".").strip()
        p = Path(rel) if os.path.isabs(rel) else self.root / rel
        real = Path(os.path.realpath(p))
        if real != self.root and not real.is_relative_to(self.root):
            return None
        return real if self.visible(real) else None

    def visible(self, real: Path) -> bool:
        if policy.is_denied(real) or secrets_scan.suspicious_file(real):
            return False
        return not any(part in SKIP_DIRS for part in real.relative_to(self.root).parts)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    def recheck(self):
        """Перед каждым запросом к модели: ничего из уже отправленного не должно стать закрытым."""
        for p in self.sent:
            policy.require(p)

    # --- состояние файлов ---
    def state(self, real: Path) -> FileState | str:
        """Состояние файла или текст ошибки для модели."""
        if real in self.states:
            st = self.states[real]
            return f"ERROR: file not found: {self.rel(real)}" if st.deleted else st
        if not real.exists() or not real.is_file():
            return f"ERROR: file not found: {self.rel(real)}"
        key = str(real)
        if key in self.live:
            text, regions = self.live[key]
            st = FileState(real, text, list(regions), text, live=True, base_regions=list(regions))
        else:
            try:
                if real.stat().st_size > MAX_FILE_BYTES:
                    return f"ERROR: file too large: {self.rel(real)}"
                text = real.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return f"ERROR: not a text file: {self.rel(real)}"
            marks = Marks()
            if not marks.load(real, text):
                return (f"ERROR: file unavailable: {self.rel(real)} changed outside the editor, so its "
                        "protection zones are stale. Ask the user to open and save it in the editor.")
            st = FileState(real, text, list(marks.items), text, base_regions=list(marks.items))
        self.states[real] = st
        return st

    @staticmethod
    def view_regions(st: FileState) -> list[Region]:
        """Зоны файла плюс скрытые на лету секреты: их модель не видит и менять не может."""
        lines = st.text.split("\n")
        found = secrets_scan.uncovered(secrets_scan.scan(st.text), st.regions, len(lines))
        return st.regions + [Region("hidden", a, b) for a, b in secrets_scan.to_ranges(found)]

    def view(self, st: FileState) -> guard.Redacted:
        return guard.redact(st.text, self.view_regions(st))

    # --- журнал ---
    def log(self, tool: str, result: str, files: list[tuple[Path, guard.Redacted | None]] | None = None,
            chars: int | None = None):
        if not self.log_enabled:
            return
        sendlog.record(None, {
            "mode": tool if tool in ("agent", "conductor") else f"tool:{tool}", "model": self.model,
            "files": [{"name": self.rel(p), "hidden_chunks": len(r.chunks) if r else 0,
                       "hidden_lines": sum(c.count("\n") + 1 for c in r.chunks) if r else 0}
                      for p, r in files or []],
            "chars": len(result) if chars is None else chars, "messages": 0}, self.root)

    # --- инструменты ---
    def list_files(self, path: str = ".", depth: int = 2) -> str:
        base = self.resolve(path)
        if base is None or not base.is_dir():
            return f"ERROR: folder not found: {path}"
        depth = max(1, min(int(depth), 5))
        lines: list[str] = []

        def walk(d: Path, level: int):
            try:
                entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for e in entries:
                real = Path(os.path.realpath(e))
                if not real.is_relative_to(self.root) or not self.visible(real) or self.pending_delete(real):
                    continue
                if len(lines) >= MAX_LIST:
                    return
                if e.is_dir():
                    lines.append("  " * level + e.name + "/")
                    if level + 1 < depth:
                        walk(e, level + 1)
                elif not policy.is_private_name(e.name):
                    lines.append("  " * level + e.name)

        walk(base, 0)
        out = "\n".join(lines) or "(empty)"
        if len(lines) >= MAX_LIST:
            out += f"\n[... first {MAX_LIST} entries shown]"
        self.log("list_files", out)
        return out

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        real = self.resolve(path)
        st = self.state(real) if real is not None else f"ERROR: file not found: {path}"
        if isinstance(st, str):
            return st
        red = self.view(st)
        lines = red.text.split("\n")
        total = len(lines)
        a = max(int(start_line or 1), 1)
        b = min(int(end_line) if end_line else a + MAX_READ_LINES - 1, total, a + MAX_READ_LINES - 1)
        body = "\n".join(f"{i}| {lines[i - 1]}" for i in range(a, b + 1))
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS] + "\n[... truncated by size]"
        note = f"[{self.rel(real)}: lines {a}-{b} of {total}]"
        if b < total:
            note += f" Use start_line={b + 1} to continue."
        out = note + "\n" + body
        self.sent.add(real)
        self.log("read_file", out, [(real, red)])
        return out

    def search(self, query: str, path: str = ".", case: bool = False) -> str:
        base = self.resolve(path)
        if not query or base is None:
            return "ERROR: need a non-empty query and an existing folder or file"
        needle = query if case else query.lower()
        files = [base] if base.is_file() else self._walk_files(base)
        hits: list[str] = []
        touched: list[tuple[Path, guard.Redacted]] = []
        for real in files:
            st = self.state(real)
            if isinstance(st, str):
                continue
            red = self.view(st)
            matched = False
            for i, line in enumerate(red.text.split("\n"), 1):
                if needle in (line if case else line.lower()):
                    hits.append(f"{self.rel(real)}:{i}: {line.strip()[:200]}")
                    matched = True
                    if len(hits) >= MAX_HITS:
                        break
            if matched:
                touched.append((real, red))
                self.sent.add(real)
            if len(hits) >= MAX_HITS:
                break
        out = "\n".join(hits) or "no matches"
        if len(hits) >= MAX_HITS:
            out += f"\n[... first {MAX_HITS} matches shown]"
        self.log("search", out, touched)
        return out

    def _walk_files(self, base: Path) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if self.visible(Path(os.path.realpath(Path(dirpath) / d))))
            for name in sorted(filenames):
                real = Path(os.path.realpath(Path(dirpath) / name))
                if real.is_relative_to(self.root) and self.visible(real) and not policy.is_private_name(name):
                    out.append(real)
        return out

    def _edit_refusal(self, real: Path | None) -> str:
        """Текст отказа, если правка сейчас запрещена режимом или рамками шага."""
        if self.read_only:
            return "ERROR: read-only review mode, editing is not allowed"
        if self.edit_scope is not None and real is not None and real not in self.edit_scope:
            allowed = ", ".join(sorted(self.rel(p) for p in self.edit_scope)) or "nothing"
            return f"ERROR: this step may only edit: {allowed}"
        return ""

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        real = self.resolve(path)
        if refused := self._edit_refusal(real):
            return refused
        st = self.state(real) if real is not None else f"ERROR: file not found: {path}"
        if isinstance(st, str):
            return st
        if not old_text:
            return "ERROR: old_text is empty"
        if old_text == new_text:
            return "ERROR: old_text and new_text are identical"
        regions = self.view_regions(st)
        red = guard.redact(st.text, regions)
        count = red.text.count(old_text)
        if count != 1:
            return ("ERROR: old_text not found in the file (check indentation and compare with read_file)"
                    if count == 0 else
                    f"ERROR: old_text occurs {count} times, add neighboring lines to make it unique")
        new_full = guard.restore(red.text.replace(old_text, new_text, 1), red.chunks)
        verdict = guard.check_edit(st.text, new_full, regions)
        if not verdict.ok:
            return "ERROR: edit rejected by protection: " + "; ".join(verdict.violations_en)
        st.regions = guard.remap(st.regions, st.text, new_full)
        st.text = new_full
        st.confirm += [c for c in verdict.needs_confirm if c not in st.confirm]
        self.sent.add(real)
        return f"edit accepted: {self.rel(real)}" + (
            f" (needs user confirmation: {', '.join(verdict.needs_confirm_en)})" if verdict.needs_confirm else "")

    def view_image(self, path: str) -> str:
        """Картинка из проекта для модели: метка, которую провайдер превратит в картинку."""
        real = self.resolve(path)
        if real is None or not real.is_file() or policy.is_private_name(real.name):
            return f"ERROR: file not found: {path}"
        try:
            size = real.stat().st_size
            if size > images.MAX_BYTES:
                return f"ERROR: {self.rel(real)}: image too large ({size // 1000} KB, at most {images.MAX_BYTES // 1000} KB)"
            data = real.read_bytes()
            mark = images.register(data, self.rel(real))
        except (OSError, ValueError) as e:
            return f"ERROR: {self.rel(real)}: {e}"
        out = f"image {self.rel(real)}:\n{mark}"
        self.sent.add(real)
        self.log("view_image", out, [(real, None)], chars=len(data))
        return out

    def pending_delete(self, real: Path) -> bool:
        st = self.states.get(real)
        return st is not None and st.deleted

    def _new_target(self, path: str) -> Path | str:
        """Куда можно положить новый файл, или текст ошибки для модели."""
        rel = (path or "").strip()
        if not rel or rel.endswith("/"):
            return "ERROR: a file path is required"
        target = Path(os.path.realpath(self.root / rel))
        if not target.is_relative_to(self.root) or target == self.root:
            return "ERROR: path is outside the project"
        if refused := self._edit_refusal(target):
            return refused
        if (policy.is_denied(target) or secrets_scan.suspicious_file(target) or policy.is_private_name(target.name)
                or any(part in SKIP_DIRS for part in target.relative_to(self.root).parts)):
            return f"ERROR: cannot create a file here: {rel}"
        if (target.exists() or target in self.states) and not self.pending_delete(target):
            return f"ERROR: file already exists: {rel} (use edit_file)"
        return target

    def _put_new(self, target: Path, content: str):
        st = self.states.get(target)
        if st is not None and st.deleted:  # удаление отменяется: на этом месте будет новый текст
            st.deleted, st.text, st.regions = False, content, []
        else:
            self.states[target] = FileState(target, content, [], None)

    def create_file(self, path: str, content: str) -> str:
        target = self._new_target(path)
        if isinstance(target, str):
            return target
        if len(content) > MAX_NEW_FILE:
            return "ERROR: file too large"
        self._put_new(target, content)
        return f"file will be created: {self.rel(target)}"

    def _movable(self, path: str) -> FileState | str:
        """Файл, который ИИ может удалить или перенести. Файлы с зонами защиты и секретами - только человек."""
        real = self.resolve(path)
        if refused := self._edit_refusal(real):
            return refused
        st = self.state(real) if real is not None else f"ERROR: file not found: {path}"
        if isinstance(st, str):
            return st
        levels = sorted({r.level for r in self.view_regions(st)} - {"explain"})
        if levels:
            return (f"ERROR: {self.rel(real)} has protected zones ({', '.join(levels)}); "
                    "only the user can delete or move it. Tell the user if it is needed.")
        return st

    def delete_file(self, path: str) -> str:
        st = self._movable(path)
        if isinstance(st, str):
            return st
        if st.base is None:  # создан в этом же запуске: просто забываем
            del self.states[st.path]
            return f"new file dropped: {self.rel(st.path)}"
        st.deleted = True
        return f"file will be deleted: {self.rel(st.path)}"

    def rename_file(self, path: str, new_path: str) -> str:
        st = self._movable(path)
        if isinstance(st, str):
            return st
        target = self._new_target(new_path)
        if isinstance(target, str):
            return target
        text = st.text
        if st.base is None:
            del self.states[st.path]
        else:
            st.deleted = True
        self._put_new(target, text)
        return f"file will be moved: {self.rel(st.path)} -> {self.rel(target)} (edit it at the new path)"

    def protected_overview(self, limit: int = 30, scan_limit: int = 5000) -> str:
        """Файлы с зонами защиты: по ним дирижёр оценивает риск шагов. Только видимые модели файлы."""
        rows: dict[Path, list[Region]] = {}
        for key, (_, regions) in self.live.items():
            real = Path(key)
            if real.is_relative_to(self.root) and self.visible(real):
                rows[real] = regions
        for n, real in enumerate(self._walk_files(self.root)):
            if n >= scan_limit or len(rows) >= limit:
                break
            if real not in rows and sidecar(real).exists():
                st = self.state(real)
                if not isinstance(st, str):
                    rows[real] = st.regions
        out = []
        for real, regions in sorted(rows.items()):
            counts: dict[str, int] = {}
            for r in regions:
                if r.level != "explain":
                    counts[r.level] = counts.get(r.level, 0) + 1
            if counts:
                out.append(f"{self.rel(real)}: " + ", ".join(f"{lv} x{n}" for lv, n in sorted(counts.items())))
        return "\n".join(out[:limit])

    # --- материалы для проверяющего ---
    def _concealed_lines(self, text: str, regions: list[Region]) -> int:
        st = FileState(Path("x"), text, regions, text)
        lr = guard.line_regions(len(text.split("\n")), self.view_regions(st))
        return sum(1 for r in lr if r is not None and r.level in guard.CONCEALED)

    def review_diff(self, limit: int = 40_000) -> str:
        """Дифф всех правок для проверяющего. Строится по виду для модели: закрытый код в нём заглушками."""
        parts: list[str] = []
        for st in self.states.values():
            if st.text == st.base:
                continue
            old_st = FileState(st.path, st.base or "", list(st.base_regions), st.base)
            old = guard.redact(old_st.text, self.view_regions(old_st)).text if st.base is not None else ""
            new = self.view(st).text
            name = self.rel(st.path)
            parts.append("\n".join(difflib.unified_diff(
                old.split("\n") if st.base is not None else [], new.split("\n"),
                f"a/{name}" if st.base is not None else "/dev/null", f"b/{name}", n=3, lineterm="")))
        out = "\n\n".join(parts)
        return out if len(out) <= limit else out[:limit] + "\n[... diff truncated ...]"

    def coverage_gaps(self) -> list[tuple[str, int, int]]:
        """Что проверяющий не мог увидеть: (файл, скрытых строк, всего строк) для изменённых и прочитанных файлов."""
        paths = {st.path for st in self.states.values() if st.text != st.base} | set(self.sent)
        out = []
        for real in sorted(paths):
            st = self.states.get(real) or self.state(real)
            if isinstance(st, str):
                continue
            hidden = self._concealed_lines(st.text, st.regions)
            if hidden:
                out.append((self.rel(real), hidden, len(st.text.split("\n"))))
        return out

    # --- набор изменений ---
    def changes(self) -> list[Change]:
        return [Change(st.path, st.base, "" if st.deleted else st.text, st.regions, st.confirm, st.live, st.deleted)
                for st in self.states.values() if st.deleted or st.text != st.base]


def write_change(change: Change) -> None:
    """Записывает изменение файла на диск вместе с файлом зон. Для файлов без вкладки."""
    change.path.parent.mkdir(parents=True, exist_ok=True)
    change.path.write_text(change.new, encoding="utf-8")
    marks = Marks()
    marks.items = list(change.regions)
    marks.save(change.path, change.new)
    if not change.regions:
        sidecar(change.path).unlink(missing_ok=True)
