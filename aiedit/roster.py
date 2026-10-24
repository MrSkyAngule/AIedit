"""Реестр исполнителей: какие модели есть, в чём сильны и сколько стоят.
Дирижёр читает его описание и по нему раздаёт шаги плана."""
import os
from dataclasses import dataclass

from .providers import AnthropicProvider, DemoProvider, Provider

CONDUCTOR_MODEL = "claude-sonnet-5-5"

# Специализации: ключ хранится в настройках, подпись видит человек, описание читает дирижёр.
SPECIALTIES: dict[str, tuple[str, str]] = {
    "": ("Универсал", ""),
    "backend": ("Backend", "server-side code: APIs, business logic, databases, auth, background jobs, integrations"),
    "frontend": ("Frontend", "UI code: components, styles, client-side logic, accessibility"),
    "tests": ("Тесты и документация", "tests, test data, docs, comments, READMEs"),
    "devops": ("DevOps", "build, CI/CD, containers, deployment, config, infrastructure scripts"),
    # проверяющий: читает и пишет замечания, код не пишет. В таблицу исполнителей дирижёра не попадает
    "security": ("Безопасность (только проверка)", "reviews code for security and logic flaws, never edits"),
}
REVIEWER = "security"


@dataclass(frozen=True)
class Worker:
    key: str              # имя в плане дирижёра, например sonnet
    provider: Provider
    strengths: str        # одна строка для дирижёра
    price_in: float       # $ за миллион входных токенов (для показа и выбора)
    price_out: float
    tier: str             # "cheap" | "mid" | "strong"
    specialty: str = ""   # ключ из SPECIALTIES; пусто - универсал

    @property
    def is_reviewer(self) -> bool:
        return self.specialty == REVIEWER


class Roster:
    def __init__(self, workers: list[Worker]):
        if not workers:
            raise ValueError("пустой реестр исполнителей")
        self.workers = {w.key.lower(): w for w in workers}

    def get(self, key: str) -> Worker | None:
        return self.workers.get((key or "").strip().lower())

    def get_builder(self, key: str) -> Worker | None:
        """Исполнитель, которому можно отдать шаг с правками. Проверяющий (security) не подходит."""
        w = self.get(key)
        return w if w is not None and w in self.builders() else None

    def builders(self) -> list[Worker]:
        """Кто пишет код. Если подключены одни проверяющие, пишут они: лучше так, чем пустой реестр."""
        built = [w for w in self.workers.values() if not w.is_reviewer]
        return built or list(self.workers.values())

    def reviewer(self) -> Worker | None:
        """Проверяющий безопасность, если он подключён и есть кому писать код (иначе он нужен как исполнитель)."""
        found = next((w for w in self.workers.values() if w.is_reviewer), None)
        return found if found is not None and any(not w.is_reviewer for w in self.workers.values()) else None

    def keys(self) -> list[str]:
        return list(self.workers)

    def by_price(self) -> list[Worker]:
        return sorted(self.builders(), key=lambda w: (w.price_in + w.price_out))

    def cheapest(self) -> Worker:
        return self.by_price()[0]

    def strongest(self) -> Worker:
        return self.by_price()[-1]

    def mid(self) -> Worker:
        ranked = self.by_price()
        return next((w for w in ranked if w.tier == "mid"), ranked[len(ranked) // 2])

    def for_risk(self, risk: str) -> Worker:
        """Запасной выбор, если дирижёр не назвал исполнителя: по риску шага."""
        return {"low": self.cheapest, "high": self.strongest}.get((risk or "").lower(), self.mid)()

    def providers(self) -> dict[str, Provider]:
        """Совместимость с режимами без дирижёра: cheap - самый дешёвый, strong - средний."""
        return {"cheap": self.cheapest().provider, "strong": self.mid().provider}

    def labels(self) -> str:
        """Для человека: sonnet, opus (Backend)."""
        return ", ".join(w.key + (f" ({SPECIALTIES[w.specialty][0]})" if w.specialty in SPECIALTIES and w.specialty else "")
                         for w in self.workers.values())

    def describe(self) -> str:
        """Компактная таблица для дирижёра: ничего лишнего, он читает её каждый раз."""
        def line(w: Worker) -> str:
            spec = f" [specialty: {w.specialty} - {SPECIALTIES[w.specialty][1]}]" if w.specialty in SPECIALTIES and w.specialty else ""
            price = f"${w.price_in:g}/${w.price_out:g} per 1M tokens" if w.price_in or w.price_out else "price unknown"
            return f"- {w.key} ({price}){spec}: {w.strengths}"
        return "\n".join(line(w) for w in self.by_price())


STRENGTHS = {
    "sonnet": "balanced: ordinary features, multi-file edits, bug fixes, tests that need understanding",
    "opus": "strongest: hard debugging, architecture, security or money related code, edits next to protected zones",
}


def default_roster() -> Roster:
    """Sonnet - универсал, Opus - Backend. Чтобы добавить других, дописать строку в spec."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    spec = [("sonnet", "claude-sonnet-5-5", 2.0, 10.0, "mid", ""),
            ("opus", "claude-opus-5-5", 4.0, 20.0, "strong", "backend")]
    # без ключа - демо-исполнители: цены условные, но по ним проходит та же раздача
    return Roster([Worker(k, AnthropicProvider(model, key) if key else DemoProvider(f"demo-{k}"),
                          STRENGTHS[k], pin, pout, tier, specialty) for k, model, pin, pout, tier, specialty in spec])


def default_conductor() -> Provider:
    """Дирижёр - Sonnet 5.5 с низким effort и коротким ответом: план нужен быстро."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicProvider(CONDUCTOR_MODEL, key, effort="low", max_tokens=1500, timeout=60)
    return DemoProvider("demo-conductor")
