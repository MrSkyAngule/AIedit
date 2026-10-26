"""Актуальные версии пакетов из публичных реестров (PyPI, npm): чтобы модель не закрепляла версии по памяти.

Из памяти модели берут давно устаревшие версии, а у них бывают известные уязвимости (Flask 3.0.0, Werkzeug 3.0.1
и другие). Здесь запрос идёт только к двум реестрам, имя пакета проверяется строгим шаблоном (в запрос не пронести
ни код, ни секреты), в ответ берутся только версия, дата, требуемый Python и известные уязвимости.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

PYPI = "https://pypi.org/pypi"
NPM = "https://registry.npmjs.org"
TIMEOUT = 10
MAX_BODY = 8_000_000
_PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,99}/)?[a-z0-9][a-z0-9._~-]{0,99}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+!-]{0,40}$")
_STABLE = re.compile(r"^\d+(\.\d+)*$")  # без alpha, beta, rc, dev


def parse(spec: str) -> tuple[str, str, str] | str:
    """'flask', 'flask==3.0.0', 'npm:express' -> (реестр, имя, версия или ''). Строка - текст ошибки для модели."""
    spec = (spec or "").strip()
    registry = "pypi"
    if spec.lower().startswith(("npm:", "pypi:")):
        registry, _, spec = spec.partition(":")
        registry = registry.lower()
    name, _, version = spec.partition("==")
    name, version = name.strip(), version.strip()
    if registry == "npm":
        if version or not _NPM_NAME.match(name):
            return "ERROR: give an npm package name like `npm:express`"
        return registry, name, ""
    if not _PYPI_NAME.match(name):
        return "ERROR: give a PyPI package name like `flask` or `flask==3.0.0`"
    if version and not _VERSION.match(version):
        return "ERROR: the version after == is not valid"
    return registry, name, version


def _get(url: str, opener) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "aiedit-versions"})
    with opener(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read(MAX_BODY))


def _stable_versions(releases: dict) -> list[str]:
    stable = [v for v, files in releases.items() if _STABLE.match(v) and files and not all(f.get("yanked") for f in files)]
    return sorted(stable, key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)


def _vulns(data: dict) -> str:
    found = data.get("vulnerabilities") or []
    if not found:
        return "none listed"
    items = []
    for v in found[:4]:
        fixed = ", ".join(v.get("fixed_in") or []) or "no fix listed"
        items.append(f"{v.get('id', '?')} (fixed in {fixed})")
    return "; ".join(items) + (f"; and {len(found) - 4} more" if len(found) > 4 else "")


def lookup(spec: str, opener=urllib.request.urlopen) -> str:
    """Ответ для модели: что сейчас стабильно и безопасно (или что не так с запрошенной версией)."""
    parsed = parse(spec)
    if isinstance(parsed, str):
        return parsed
    registry, name, version = parsed
    try:
        if registry == "npm":
            data = _get(f"{NPM}/{urllib.parse.quote(name, safe='@')}/latest", opener)
            node = (data.get("engines") or {}).get("node")
            return (f"npm {name}: latest stable {data.get('version', '?')}"
                    + (f", requires Node {node}" if node else "")
                    + ". Pin a version like this one; do not use one from memory.")
        main = _get(f"{PYPI}/{urllib.parse.quote(name)}/json", opener)
        info = main.get("info") or {}
        latest = info.get("version", "?")
        stable = _stable_versions(main.get("releases") or {})
        lines = [f"pypi {info.get('name', name)}: latest stable {latest}"
                 + (f", requires Python {info['requires_python']}" if info.get("requires_python") else ""),
                 "recent stable versions: " + ", ".join(stable[:5]),
                 f"known vulnerabilities in {latest}: {_vulns(main)}"]
        if version and version != latest:
            try:
                old = _get(f"{PYPI}/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json", opener)
                lines.append(f"{name}=={version}: known vulnerabilities: {_vulns(old)}"
                             + ("" if _vulns(old) == "none listed" else f". Use {latest} instead."))
            except urllib.error.HTTPError as e:
                lines.append(f"{name}=={version}: " + ("this version does not exist" if e.code == 404 else f"HTTP {e.code}"))
        elif version:
            lines.append(f"{name}=={version} is the latest stable version")
        lines.append(f"Pin a recent stable version, like {latest}, never an older one from memory.")
        return "\n".join(lines)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"ERROR: package `{name}` not found in {registry}. Check the spelling; do not guess another name."
        return f"ERROR: {registry} answered HTTP {e.code}. Say that the version is unverified."
    except (OSError, ValueError, urllib.error.URLError) as e:
        return f"ERROR: could not reach {registry} ({type(e).__name__}). Say that the versions are unverified."
