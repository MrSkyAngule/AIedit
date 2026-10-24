"""Провайдеры ИИ. Каждый умеет одно: complete(system, messages) -> текст.

messages - список {"role": "user"|"assistant", "content": str}, последняя реплика от user.
"""
import json
import re
import urllib.request


class Provider:
    name = "base"
    last_usage: dict | None = None  # расход последнего запроса: input, cache_read, cache_write, output (токены)

    def complete(self, system: str, messages: list[dict]) -> str:
        raise NotImplementedError


class DemoProvider(Provider):
    """Офлайн-заглушка: возвращает файл как есть и дописывает строку. Для проверки без ключей."""

    def __init__(self, name="demo"):
        self.name = name

    def _agent_step(self, messages: list[dict], system: str = "") -> str:
        """Простой сценарий без модели: посмотреть проект, прочитать файл, при слове «комментар» добавить строку."""
        first = messages[0]["content"]
        task = first.split("\n\n=== PROJECT FILES")[0]
        steps = sum(1 for m in messages if m["role"] == "assistant")
        listing = first.partition("=== PROJECT FILES (2 levels) ===\n")[2].partition("\n=== OPEN FILE")[0]
        top = [l for l in listing.split("\n") if l and not l.startswith(" ") and not l.endswith("/")]
        target = next((l for l in top if l.endswith(".py")), top[0] if top else None)
        if target is None:
            return f"[{self.name}] Демо-агент: в проекте нет файлов."
        if "спроси" in task.lower() and "ask_user" in system:  # показать вопрос человеку без ключей
            if steps == 0:
                return f"@@tool ask_user\nquestion: Демо-вопрос: какой файл посмотреть? Например, {target}\n@@end"
            answer = messages[-1]["content"].partition("@@result ask_user\n")[2].partition("\n@@end")[0]
            return f"[{self.name}] Демо-агент: ты ответил «{answer}». С ключами здесь продолжилась бы работа."
        if steps == 0:
            return f"Сначала прочитаю {target}.\n@@tool read_file\npath: {target}\n@@end"
        read = messages[-1]["content"]
        line = next((l.partition("| ")[2] for l in read.split("\n") if l.startswith("1| ")), "")
        if steps == 1 and "комментар" in task.lower() and line:
            return (f"@@tool edit_file\npath: {target}\n@@old\n{line}\n@@new\n# [{self.name}] {task.strip()[:60]}\n{line}\n@@end")
        return f"[{self.name}] Демо-агент: посмотрел проект и прочитал {target}."

    @staticmethod
    def _conductor_plan(messages: list[dict]) -> str:
        """Демо-дирижёр: режет запрос по «;» и « then », раздаёт шаги по ключевым словам
        среди исполнителей из реестра (в таблице WORKERS они идут от дешёвого к дорогому)."""
        content = messages[-1]["content"]
        request = content.partition("REQUEST:\n")[2].partition("\n\nWORKERS:")[0].strip()
        table = content.partition("WORKERS:\n")[2].partition("\n\n")[0]
        workers = re.findall(r"^- (\S+) \(", table, re.M) or ["worker"]
        backend = re.findall(r"^- (\S+) \([^)]*\) \[specialty: backend", table, re.M)
        general = [w for w in workers if w not in re.findall(r"^- (\S+) \([^)]*\) \[specialty:", table, re.M)] or workers
        parts = [p.strip() for p in re.split(r";|\bthen\b", request) if p.strip()][:3] or [request]
        out = ["@@plan", f"summary: {request[:80]}"]
        for i, part in enumerate(parts, 1):
            low = part.lower()
            if backend and any(w in low for w in ("api", "server", "backend", "database", "sql", "endpoint", "auth")):
                worker, risk = backend[0], "medium"
            elif any(w in low for w in ("test", "doc", "comment", "rename", "readme", "list")):
                worker, risk = general[0], "low"
            elif any(w in low for w in ("security", "auth", "payment", "fix", "bug", "refactor", "architect")):
                worker, risk = workers[-1], "high"
            else:
                worker, risk = general[len(general) // 2], "medium"
            out += [f"@@task t{i}", f"worker: {worker}", f"goal: {part}", f"risk: {risk}"]
        return "\n".join(out + ["@@end"])

    def complete(self, system: str, messages: list[dict]) -> str:
        if "Mode: conductor" in system:
            return self._conductor_plan(messages)
        if "Mode: agent" in system:
            return self._agent_step(messages, system)
        task, _, code = messages[-1]["content"].partition("\n=== FILE ===\n")
        memory = f" Реплик в памяти: {len(messages) - 1}."
        if "Mode: explain" in system:
            return f"[{self.name}] демо-объяснение.{memory} {task.strip()}"
        task = task.partition("\n=== ZONES ===\n")[0]
        return (f"[{self.name}] Демо: дописал строку в конец файла.{memory}\n"
                f"```\n{code.rstrip(chr(10))}\n# [{self.name}] {task.strip()}\n```")


class AnthropicProvider(Provider):
    """Messages API по HTTPS. effort ("low"..."max") ограничивает глубину рассуждений: для быстрых
    коротких задач вроде планирования ставим "low". Параметры sampling не передаём: их новые модели не принимают.

    Кэширование промпта: метка на системном промпте (он одинаковый на всех шагах и у всех исполнителей
    одной модели) и автоматическая метка на конце переписки - каждый шаг агента читает из кэша всё,
    что было до него. Чтение из кэша стоит около 0.1 обычной цены, запись - 1.25."""
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str, api_key: str, effort: str | None = None, max_tokens: int = 8192,
                 url: str | None = None, timeout: float = 120, cache: bool = True):
        self.cache = cache
        self.name = model
        self.model = model
        self.api_key = api_key
        self.effort = effort
        self.max_tokens = max_tokens
        self.url = url or self.URL
        self.timeout = timeout

    def request_body(self, system: str, messages: list[dict]) -> dict:
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": messages}
        if self.cache:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            body["cache_control"] = {"type": "ephemeral"}  # автоматически: на последнем блоке переписки
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        return body

    @staticmethod
    def usage(data: dict) -> dict:
        u = data.get("usage") or {}
        return {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens") or 0,
                "cache_write": u.get("cache_creation_input_tokens") or 0, "output": u.get("output_tokens", 0)}

    def complete(self, system: str, messages: list[dict]) -> str:
        req = urllib.request.Request(
            self.url, data=json.dumps(self.request_body(system, messages)).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        self.last_usage = self.usage(data)
        return "".join(p.get("text", "") for p in data["content"] if p.get("type", "text") == "text")


class OpenAIProvider(Provider):
    """chat/completions: OpenAI и любые совместимые сервисы (Ollama, OpenRouter, Groq и другие).
    Ключ необязателен для локальных серверов. temperature не шлём, пока её не попросили явно."""
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model: str, api_key: str = "", url: str | None = None, temperature: float | None = None,
                 max_tokens: int | None = None, token_param: str = "max_completion_tokens", timeout: float = 120):
        self.name = model
        self.model = model
        self.api_key = api_key
        self.url = url or self.URL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.token_param = token_param  # у старых совместимых серверов это max_tokens
        self.timeout = timeout

    def request_body(self, system: str, messages: list[dict]) -> dict:
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages]}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.max_tokens:
            body[self.token_param] = self.max_tokens
        return body

    def complete(self, system: str, messages: list[dict]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(self.request_body(system, messages)).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.load(resp)
        self.last_usage = self.usage(data)
        return data["choices"][0]["message"].get("content") or ""

    @staticmethod
    def usage(data: dict) -> dict:
        """OpenAI кэширует длинные одинаковые начала запросов сама; сколько взято из кэша - в prompt_tokens_details."""
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return {"input": max(0, (u.get("prompt_tokens") or 0) - cached), "cache_read": cached, "cache_write": 0,
                "output": u.get("completion_tokens") or 0}


def default_providers() -> dict[str, Provider]:
    """Пара cheap/strong для режимов без дирижёра. Строится из реестра исполнителей."""
    from .roster import default_roster
    return default_roster().providers()
