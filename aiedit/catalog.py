"""Каталог моделей, которые можно подключить, и фабрика клиентов под них."""
import re
from dataclasses import dataclass

from .providers import AnthropicProvider, OpenAIProvider, Provider

CUSTOM = "custom"


@dataclass(frozen=True)
class ModelSpec:
    key: str          # стабильное имя: хранится в настройках и используется дирижёром в плане
    label: str        # что видит человек в списке
    service: str      # anthropic | openai | custom
    model_id: str
    tier: str         # cheap | mid | strong
    price_in: float   # $ за миллион токенов; 0 - бесплатно или неизвестно
    price_out: float
    strengths: str    # одна строка для дирижёра
    specialty: str = ""  # специализация по умолчанию при выборе этой модели в окне подключений
    default_extra: str = ""  # для service="compatible": "идентификатор | адрес" по умолчанию, человек может поправить


CATALOG: list[ModelSpec] = [
    ModelSpec("sonnet", "Claude Sonnet 5.5 (Anthropic)", "anthropic", "claude-sonnet-5-5", "mid", 2.0, 10.0,
              "balanced: ordinary features, multi-file edits, bug fixes, tests that need understanding"),
    ModelSpec("opus", "Claude Opus 5.5 (Anthropic)", "anthropic", "claude-opus-5-5", "strong", 4.0, 20.0,
              "strongest: hard debugging, architecture, security or money related code, edits next to protected zones",
              "backend"),
    ModelSpec("haiku", "Claude Haiku 5.5 (Anthropic)", "anthropic", "claude-haiku-5-5", "cheap", 0.10, 0.50,
              "fast and cheap: mechanical edits, renames, docs, boilerplate tests, read-only questions"),
    ModelSpec("gpt-4o", "GPT-4o (OpenAI)", "openai", "gpt-4o", "mid", 2.5, 10.0,
              "general purpose: ordinary features and bug fixes"),
    ModelSpec("gpt-4o-mini", "GPT-4o mini (OpenAI)", "openai", "gpt-4o-mini", "cheap", 0.15, 0.60,
              "fast and cheap: simple edits, docs, small tests, summaries, translation"),
]
CATALOG.append(
    # Идентификатор и адрес введены со слов пользователя и мной не проверены: поле в окне подключений редактируется.
    ModelSpec("glm", "GLM 5.3 (Z.ai, OpenAI-совместимый API)", "compatible", "glm-5.3", "mid", 0.0, 0.0,
              "independent reviewer from a different vendor, used for security and logic review", "security",
              "glm-5.3 | https://api.z.ai/api/paas/v4"))
CUSTOM_LABEL = "Своя модель (OpenAI-совместимый API)"
EXTRA_HINT = "идентификатор модели | адрес API, например  llama3 | http://localhost:11434/v1"
GATEWAY_HINT = "адрес шлюза, если не прямой API (необязательно), например  https://host/v1"


def by_key(key: str) -> ModelSpec | None:
    return next((m for m in CATALOG if m.key == key), None)


def parse_custom(extra: str) -> tuple[str, str]:
    """'model | http://host/v1' -> (model, url чата). ValueError с понятным текстом при ошибке."""
    model, _, base = (p.strip() for p in extra.partition("|"))
    if not model or not base:
        raise ValueError(f"для своей модели нужно: {EXTRA_HINT}")
    if not re.match(r"^https?://", base):
        raise ValueError("адрес API должен начинаться с http:// или https://")
    return model, base.rstrip("/") + "/chat/completions"


def worker_key(spec_key: str, extra: str = "") -> str:
    """Имя исполнителя в плане. У своей модели оно из её идентификатора."""
    if spec_key != CUSTOM:
        return spec_key
    model = extra.partition("|")[0].strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or CUSTOM


def spec_for(spec_key: str, extra: str = "") -> ModelSpec:
    spec = by_key(spec_key)
    if spec is not None:
        return spec
    if spec_key != CUSTOM:
        raise ValueError(f"неизвестная модель: {spec_key}")
    model, _ = parse_custom(extra)
    return ModelSpec(worker_key(CUSTOM, extra), model, "custom", model, "mid", 0.0, 0.0,
                     "user-defined model, strengths unknown")


def gateway_url(extra: str) -> str:
    """Адрес чата у шлюза (сервис-посредник в формате OpenAI), если человек его указал для каталожной модели."""
    base = extra.strip()
    if not re.match(r"^https?://", base):
        raise ValueError("адрес шлюза должен начинаться с http:// или https://")
    return base.rstrip("/") + "/chat/completions"


def _timeout(role: str) -> dict:
    return {"timeout": ROLE_TIMEOUT[role]} if role in ROLE_TIMEOUT else {}


ROLE_TIMEOUT = {"conductor": 90, "translator": 60}  # сколько ждать тишины на соединении; исполнителю - больше (WORKER_TIMEOUT)


def make_provider(spec_key: str, key: str, extra: str = "", role: str = "worker") -> Provider:
    """Клиент под модель. role: worker | conductor | translator - от роли зависят лимиты и температура.
    Для моделей каталога (Claude, GPT) extra - необязательный адрес шлюза: запросы идут через него, в формате
    OpenAI, а цены, сильные стороны и специализация остаются каталожными."""
    spec = spec_for(spec_key, extra)
    if spec.service in ("anthropic", "openai") and spec_key != CUSTOM and extra.strip():
        return OpenAIProvider(spec.model_id, key, url=gateway_url(extra),
                              temperature=0 if role == "translator" else None,
                              max_tokens=1500 if role == "conductor" else None, token_param="max_tokens",
                              **_timeout(role))
    if spec.service == "anthropic":
        if role == "conductor":  # план нужен быстро и коротко
            return AnthropicProvider(spec.model_id, key, effort="low", max_tokens=1500, timeout=60)
        return AnthropicProvider(spec.model_id, key)
    temperature = 0 if role == "translator" else None
    max_tokens = 1500 if role == "conductor" else None
    if spec.service == "openai":
        return OpenAIProvider(spec.model_id, key, temperature=temperature, max_tokens=max_tokens, **_timeout(role))
    model, url = parse_custom(extra or spec.default_extra)
    return OpenAIProvider(model, key, url=url, temperature=temperature, max_tokens=max_tokens, token_param="max_tokens",
                          **_timeout(role))
