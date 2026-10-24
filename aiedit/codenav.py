"""Навигация по коду для агента: go_to_definition и find_references.

Если установлен языковой сервер (pylsp, pyright, jedi-language-server, typescript-language-server,
gopls), спрашиваем его. Сервер работает на копии проекта без закрытого кода (sandbox.ProjectCopy),
поэтому ни в одном ответе закрытого кода быть не может. Без сервера для Python редактор сам
разбирает код: определения по синтаксическому дереву, использования по токенам - строки и
комментарии с тем же словом не считаются. Это точнее текстового поиска, но слабее сервера:
одноимённые функции из разных модулей не различаются.

Номера строк - те, что модель видит в read_file (там закрытые куски свёрнуты в одну строку).
"""
import ast
import io
import os
import re
import shutil
import tokenize
from pathlib import Path

from . import guard
from .lsp import LspClient, LspError, path_of, uri
from .sandbox import ProjectCopy, project_venv
from .workspace import Workspace

MAX_RESULTS = 50
SERVERS = {
    "python": [["pylsp"], ["pyright-langserver", "--stdio"], ["jedi-language-server"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "go": [["gopls"]],
}
LANG = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "typescript",
        ".jsx": "typescript", ".mjs": "typescript", ".cjs": "typescript", ".go": "go"}
LANG_ID = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescriptreact",
           ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript", ".go": "go"}


def find_server(lang: str, root: Path) -> list[str] | None:
    venv = project_venv(root)
    for argv in SERVERS.get(lang, []):
        local = venv / "bin" / argv[0] if venv else None
        if local is not None and local.exists():
            return [str(local), *argv[1:]]
        found = shutil.which(argv[0])
        if found:
            return [found, *argv[1:]]
    return None


class Navigator:
    """Вызывается как nav(имя инструмента, аргументы) -> текст для модели. close() останавливает серверы."""

    def __init__(self, ws: Workspace, server_for=find_server):
        self.ws, self.server_for = ws, server_for
        self.copy = ProjectCopy(ws, "aiedit-nav-")
        self.clients: dict[str, LspClient | None] = {}
        self.versions: dict[Path, int] = {}

    def __call__(self, tool: str, args: dict) -> str:
        path, symbol = str(args.get("path", "")), str(args.get("symbol", "")).strip()
        line = args.get("line")
        if not path or not symbol or not isinstance(line, int):
            return "ERROR: missing arguments: path, line, symbol"
        real = self.ws.resolve(path)
        st = self.ws.state(real) if real is not None else f"ERROR: file not found: {path}"
        if isinstance(st, str):
            return st
        red = self.ws.view(st)
        orig = [i for i, r in enumerate(red.line_map) if r == line - 1]
        if not orig:
            return f"ERROR: line {line} is outside the file"
        lr = guard.line_regions(len(red.line_map), self.ws.view_regions(st))
        if lr[orig[0]] is not None and lr[orig[0]].level in guard.CONCEALED:
            return f"ERROR: line {line} is closed code"
        text = st.text.split("\n")[orig[0]]
        m = re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", text)
        if m is None:
            return f"ERROR: {symbol!r} is not on line {line} of {path}"
        try:
            self.copy.prepare()
        except Exception as e:  # слишком большой проект и т. п.
            return f"ERROR: code navigation is unavailable: {e}"
        rel = self.ws.rel(real)
        locations, how = self._lsp(tool, rel, orig[0], m.start() + len(symbol) // 2)
        if locations is None:
            if real.suffix not in (".py", ".pyi"):
                return (f"ERROR: no language server for {real.suffix or 'this file'} is installed; "
                        "use search instead")
            locations, how = self._python(tool, rel, symbol), "found by name: no language server is installed, " \
                "same-named symbols from different modules are not told apart"
        out = self._format(locations)
        self.ws.log(tool, out)
        what = "definition" if tool == "go_to_definition" else "references"
        if not out:
            out = ("nothing found. It may be closed code (hidden from you on purpose: use it as described in its "
                   "note, do not look for it) or something defined outside the project."
                   if tool == "go_to_definition" else "nothing found")
        return f"[{what} of {symbol}, {how}]\n" + out

    # --- языковой сервер ---
    def _client(self, lang: str) -> LspClient | None:
        if lang not in self.clients:
            argv = self.server_for(lang, self.ws.root)
            client = None
            if argv:
                try:
                    client = LspClient(argv, self.copy.path)
                    client.start()
                except (OSError, LspError):
                    if client is not None:
                        client.close()
                    client = None
            self.clients[lang] = client
        return self.clients[lang]

    def _lsp(self, tool: str, rel: str, line: int, col: int):
        suffix = Path(rel).suffix
        lang = LANG.get(suffix)
        client = self._client(lang) if lang else None
        if client is None:
            return None, ""
        target = self.copy.path / rel
        try:
            text = target.read_text(encoding="utf-8")
            ver = self.versions.get(target, 0) + 1
            if ver == 1:
                client.notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri(target), "languageId": LANG_ID[suffix], "version": ver, "text": text}})
            else:
                client.notify("textDocument/didChange", {"textDocument": {"uri": uri(target), "version": ver},
                                                         "contentChanges": [{"text": text}]})
            self.versions[target] = ver
            params = {"textDocument": {"uri": uri(target)}, "position": {"line": line, "character": col}}
            if tool == "find_references":
                params["context"] = {"includeDeclaration": False}
                result = client.request("textDocument/references", params)
            else:
                result = client.request("textDocument/definition", params)
        except (OSError, LspError):
            return None, ""
        items = result if isinstance(result, list) else [result] if result else []
        locations = []
        for it in items:
            u = it.get("uri") or it.get("targetUri")
            rng = it.get("range") or it.get("targetSelectionRange") or {}
            if u:
                locations.append((path_of(u), (rng.get("start") or {}).get("line", 0)))
        return locations, f"via language server {Path(client.proc.args[0]).name}"

    # --- без сервера: Python по синтаксическому дереву и токенам ---
    def _py_files(self) -> list[Path]:
        return sorted(p for p in self.copy.path.rglob("*.py") if p.is_file())

    def _python(self, tool: str, rel: str, symbol: str) -> list[tuple[Path, int]]:
        found: list[tuple[Path, int]] = []
        here = self.copy.path / rel
        for f in self._py_files():
            try:
                src = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if tool == "go_to_definition":
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names = [node.name]
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        names = [t.id for t in targets if isinstance(t, ast.Name)]
                    if symbol in names:
                        found.append((f, node.lineno - 1))
            else:
                try:
                    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                        if tok.type == tokenize.NAME and tok.string == symbol:
                            found.append((f, tok.start[0] - 1))
                except (tokenize.TokenError, SyntaxError, IndentationError):
                    continue
        found = list(dict.fromkeys(found))
        return sorted(found, key=lambda x: (x[0] != here, str(x[0]), x[1]))  # сначала тот же файл

    # --- ответ модели ---
    def _format(self, locations) -> str:
        lines, root = [], self.copy.path.resolve()
        for path, line in locations[:MAX_RESULTS]:
            p = Path(os.path.realpath(path))
            if not p.is_relative_to(root):
                tail = "/".join(p.parts[-3:])  # стандартная библиотека или пакет: содержимое не нужно
                lines.append(f"[library] {tail}:{line + 1}")
                continue
            rel = p.relative_to(root).as_posix()
            real = self.ws.resolve(rel)
            st = self.ws.state(real) if real is not None else None
            if st is None or isinstance(st, str):
                continue
            red = self.ws.view(st)
            if line >= len(red.line_map):
                continue
            shown = red.line_map[line]
            lr = guard.line_regions(len(red.line_map), self.ws.view_regions(st))
            closed = lr[line] is not None and lr[line].level in guard.CONCEALED
            code = "(closed code)" if closed else red.text.split("\n")[shown].strip()[:160]
            lines.append(f"{rel}:{shown + 1}: {code}")
        more = f"\n[... {len(locations) - MAX_RESULTS} more]" if len(locations) > MAX_RESULTS else ""
        return "\n".join(dict.fromkeys(lines)) + more

    def close(self):
        for client in self.clients.values():
            if client is not None:
                client.close()
        self.clients.clear()
        self.copy.close()
