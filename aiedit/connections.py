"""Подключения ИИ: какие модели и с какими ключами включил пользователь.

Строки подключений (модель + ключ) лежат в профиле пользователя, а не в проекте, в файле с правами 600.
Из них собираются реестр исполнителей, дирижёр и переводчик. Пока подключений нет, работают переменные
окружения ANTHROPIC_API_KEY и OPENAI_API_KEY, а без них - демо.
"""
import json
import os
import tempfile
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog
from .providers import OpenAIProvider, Provider
from .roster import Roster, Worker, default_conductor, default_roster
from .settings import config_file

AUTO, OFF = "", "off"
MAX_ROWS = 4  # столько подключений помещается в окно


@dataclass
class Connection:
    model: str                              # ключ каталога или "custom"
    key: str = field(default="", repr=False)  # ключ не попадает в repr, а значит и в логи и отладку
    extra: str = ""                         # для своей модели: "идентификатор | адрес"


@dataclass
class Roles:
    conductor: str = AUTO   # имя исполнителя из подключённых; пусто - выбрать самому
    translator: str = AUTO  # имя исполнителя, "off" или пусто - выбрать самому


@dataclass
class Setup:
    roster: Roster
    conductor: Provider
    translator: Provider | None
    problems: list[str] = field(default_factory=list)
    source: str = "connections"  # connections | env | demo


def file_path() -> Path:
    return config_file().parent / "connections.json"


def load() -> tuple[list[Connection], Roles]:
    try:
        data = json.loads(file_path().read_text(encoding="utf-8"))
        rows = [Connection(str(r["model"]), str(r.get("key", "")), str(r.get("extra", "")))
                for r in data.get("connections", []) if isinstance(r, dict) and r.get("model")]
        roles = data.get("roles")
        roles = roles if isinstance(roles, dict) else {}  # испорченные роли не должны ронять подключения
        return rows[:MAX_ROWS], Roles(str(roles.get("conductor", AUTO)), str(roles.get("translator", AUTO)))
    except (OSError, ValueError, AttributeError, TypeError):
        return [], Roles()


def save(conns: list[Connection], roles: Roles) -> bool:
    """Пишет файл сразу с правами 600: ключ не должен хоть на миг лежать открытым."""
    path = file_path()
    payload = json.dumps({"connections": [{"model": c.model, "key": c.key, "extra": c.extra} for c in conns],
                          "roles": {"conductor": roles.conductor, "translator": roles.translator}},
                         ensure_ascii=False, indent=1)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".connections-")  # mkstemp создаёт файл с правами 600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def env_setup() -> Setup:
    """Без сохранённых подключений: ключи из окружения, иначе демо."""
    roster = default_roster()
    translator = None
    if os.environ.get("OPENAI_API_KEY"):
        translator = OpenAIProvider("gpt-4o-mini", os.environ["OPENAI_API_KEY"], temperature=0)
    demo = os.environ.get("ANTHROPIC_API_KEY") is None
    return Setup(roster, default_conductor(), translator, [], "demo" if demo and translator is None else "env")


def build(conns: list[Connection], roles: Roles | None = None) -> Setup:
    """Собирает всё из заполненных строк. Неподходящие строки пропускает и пишет причину в problems."""
    roles = roles or Roles()
    problems: list[str] = []
    workers: list[Worker] = []
    rows: dict[str, Connection] = {}
    for c in conns:
        try:
            spec = catalog.spec_for(c.model, c.extra)
            if spec.service != "custom" and not c.key.strip():
                raise ValueError("не указан ключ")
            key = catalog.worker_key(c.model, c.extra)
            if key in rows:
                raise ValueError("эта модель уже подключена выше")
            provider = catalog.make_provider(c.model, c.key.strip(), c.extra)
        except ValueError as e:
            problems.append(f"{catalog.by_key(c.model).label if catalog.by_key(c.model) else c.model}: {e}")
            continue
        rows[key] = c
        # назначение модели задаёт каталог: Opus - Backend, GLM - проверка безопасности, остальные универсалы
        workers.append(Worker(key, provider, spec.strengths, spec.price_in, spec.price_out, spec.tier, spec.specialty))
    if not workers:
        fallback = env_setup()
        fallback.problems = problems
        return fallback
    roster = Roster(workers)
    chosen = roles.conductor if roles.conductor in rows else roster.mid().key
    c = rows[chosen]
    conductor = catalog.make_provider(c.model, c.key.strip(), c.extra, role="conductor")
    return Setup(roster, conductor, _translator(rows, roles), problems, "connections")


def _translator(rows: dict[str, Connection], roles: Roles) -> Provider | None:
    if roles.translator == OFF:
        return None
    key = roles.translator if roles.translator in rows else None
    if key is None:  # сам выбираем только проверенных переводчиков; свои и остальные модели - только вручную
        key = next((k for k in ("gpt-4o-mini", "gpt-4o") if k in rows), None)
    if key is None:
        return None
    c = rows[key]
    return catalog.make_provider(c.model, c.key.strip(), c.extra, role="translator")


def is_quota_error(text: str) -> bool:
    """Сервис (чаще шлюз-посредник) отказал из-за денег или лимита ключа, а не из-за неверного ключа."""
    low = text.lower()
    return any(w in low for w in ("insufficient", "quota", "balance", "exceeded your current", "额度", "余额"))


def ping(provider: Provider) -> tuple[bool, str]:
    """Проверка подключения одним коротким запросом. Текст ошибки без ключа. Без повторов: проверка отвечает сразу."""
    retries = getattr(provider, "retries", None)
    if retries is not None:
        provider.retries = 0
    try:
        provider.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}])
    except urllib.error.HTTPError as e:
        e.close()
        hint = {401: "ключ не принят", 403: "доступ запрещён", 404: "модель или адрес не найдены",
                429: "лимит запросов"}.get(e.code, "ошибка сервиса")
        reason = str(e.reason)
        if is_quota_error(reason):
            hint = "на ключе закончились деньги или лимит"
        detail = reason.partition(": ")[2][:160]  # причина от самого сервиса, если он её назвал
        return False, f"{hint} (HTTP {e.code})" + (f": {detail}" if detail and e.code not in (401, 404) else "")
    except Exception as e:  # сеть, таймаут, неожиданный ответ
        return False, f"{type(e).__name__}: {e}"
    finally:
        if retries is not None:
            provider.retries = retries
    return True, "работает"
