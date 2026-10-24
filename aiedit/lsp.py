"""Минимальный клиент Language Server Protocol: запуск сервера, initialize, запросы и ответы по stdin/stdout.

Только то, что нужно навигации по коду: definition и references. Сервер запускается на копии
проекта без закрытого кода (sandbox.ProjectCopy), с окружением без ключей API.
"""
import json
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

TIMEOUT = 20


class LspError(Exception):
    pass


def uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


def path_of(u: str) -> Path:
    return Path(unquote(urlparse(u).path))


def clean_env() -> dict:
    """Окружение без ключей и токенов: серверу они не нужны. Go не должен ничего скачивать."""
    secret = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in secret)}
    env.update({"GOFLAGS": "-mod=readonly", "GOPROXY": "off"})
    return env


class LspClient:
    def __init__(self, argv: list[str], root: Path, timeout: float = TIMEOUT):
        self.root, self.timeout = root, timeout
        self.proc = subprocess.Popen(argv, cwd=root, env=clean_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.next_id = 0
        self.results: dict[int, dict] = {}
        self.cond = threading.Condition()
        self.lock = threading.Lock()  # запись в stdin
        self.dead = False
        threading.Thread(target=self._read_loop, daemon=True).start()

    # --- обмен сообщениями ---
    def _send(self, msg: dict):
        data = json.dumps(msg).encode()
        with self.lock:
            try:
                self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise LspError(f"language server stopped: {e}") from e

    def _read_loop(self):
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.decode("ascii", "replace").partition(":")
                    if name.lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                msg = json.loads(out.read(length))
                if "id" in msg and "method" in msg:  # запрос от сервера: отвечаем, иначе некоторые зависают
                    params = msg.get("params") or {}
                    result = [None] * len(params.get("items", [])) if msg["method"] == "workspace/configuration" else None
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                elif "id" in msg:
                    with self.cond:
                        self.results[msg["id"]] = msg
                        self.cond.notify_all()
        except (EOFError, OSError, ValueError, LspError):
            with self.cond:
                self.dead = True
                self.cond.notify_all()

    def request(self, method: str, params: dict, timeout: float | None = None):
        with self.cond:
            self.next_id += 1
            rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        with self.cond:
            ok = self.cond.wait_for(lambda: rid in self.results or self.dead, timeout or self.timeout)
            msg = self.results.pop(rid, None)
        if msg is None:
            raise LspError("language server stopped" if ok else f"language server did not answer {method} in time")
        if "error" in msg:
            raise LspError(str(msg["error"].get("message", msg["error"]))[:200])
        return msg.get("result")

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # --- жизненный цикл ---
    def start(self):
        self.request("initialize", {
            "processId": os.getpid(), "rootUri": uri(self.root),
            "workspaceFolders": [{"uri": uri(self.root), "name": self.root.name}],
            "capabilities": {"textDocument": {"definition": {}, "references": {}, "synchronization": {}},
                             "workspace": {"configuration": True, "workspaceFolders": True}}},
            timeout=max(self.timeout, 30))
        self.notify("initialized", {})

    def close(self):
        try:
            if not self.dead:
                self.request("shutdown", {}, timeout=3)
                self.notify("exit", {})
        except LspError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
