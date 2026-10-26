"""Версии пакетов из реестров и описание окружения проекта: чтобы модель не брала версии из памяти."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from aiedit import environment, versions
from aiedit.marks import Marks
from aiedit.workspace import Workspace


class Resp:
    def __init__(self, data):
        self.buf = io.BytesIO(json.dumps(data).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.buf.read(n)


def opener(routes, calls):
    """Поддельный реестр: адрес -> ответ (словарь) или исключение."""
    def open_(req, timeout=None):
        calls.append(req.full_url)
        answer = routes.get(req.full_url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        return Resp(answer)
    return open_


def files(*names):
    return [{"yanked": False}] if names else []


FLASK = {"info": {"name": "Flask", "version": "3.1.3", "requires_python": ">=3.9"},
         "releases": {"3.1.3": files(1), "3.1.2": files(1), "3.1.0": files(1), "3.0.3": files(1), "3.0.0": files(1),
                      "3.2.0rc1": files(1), "3.2.0.dev0": files(1), "2.9.9": [{"yanked": True}], "0.1": files(1)},
         "vulnerabilities": []}
FLASK_300 = {"info": {"version": "3.0.0"}, "vulnerabilities": [
    {"id": "PYSEC-2026-2151", "fixed_in": ["3.1.3"]}, {"id": "GHSA-68rp-wp8r-4726", "fixed_in": ["3.1.3"]}]}


class ParseTest(unittest.TestCase):
    def test_good_specs(self):
        self.assertEqual(versions.parse("flask"), ("pypi", "flask", ""))
        self.assertEqual(versions.parse(" Flask==3.0.0 "), ("pypi", "Flask", "3.0.0"))
        self.assertEqual(versions.parse("pypi:requests"), ("pypi", "requests", ""))
        self.assertEqual(versions.parse("npm:express"), ("npm", "express", ""))
        self.assertEqual(versions.parse("npm:@types/node"), ("npm", "@types/node", ""))

    def test_nothing_but_a_package_name_can_be_sent(self):
        for bad in ("", "flask; rm -rf /", "flask os.system", "../../etc/passwd", "http://evil.example/x", "flask\nHost: x",
                    "a" * 101, "flask==1.0 && ls", "flask==../x", "npm:Express", "npm:express==1", "npm:", "-r requirements.txt"):
            self.assertTrue(str(versions.parse(bad)).startswith("ERROR"), repr(bad))
            calls = []
            self.assertTrue(versions.lookup(bad, opener({}, calls)).startswith("ERROR"))
            self.assertEqual(calls, [], repr(bad))  # в сеть не ушло ничего


class LookupTest(unittest.TestCase):
    def test_latest_stable_ignores_prereleases_and_yanked_and_stays_on_pypi(self):
        calls = []
        out = versions.lookup("flask", opener({f"{versions.PYPI}/flask/json": FLASK}, calls))
        self.assertIn("latest stable 3.1.3, requires Python >=3.9", out)
        self.assertIn("recent stable versions: 3.1.3, 3.1.2, 3.1.0, 3.0.3, 3.0.0", out)
        self.assertNotIn("rc1", out)
        self.assertNotIn("2.9.9", out)
        self.assertIn("known vulnerabilities in 3.1.3: none listed", out)
        self.assertEqual(calls, ["https://pypi.org/pypi/flask/json"])

    def test_old_pinned_version_is_checked_for_vulnerabilities(self):
        routes = {f"{versions.PYPI}/flask/json": FLASK, f"{versions.PYPI}/flask/3.0.0/json": FLASK_300}
        out = versions.lookup("flask==3.0.0", opener(routes, []))
        self.assertIn("flask==3.0.0: known vulnerabilities: PYSEC-2026-2151 (fixed in 3.1.3); GHSA-68rp-wp8r-4726", out)
        self.assertIn("Use 3.1.3 instead", out)

    def test_latest_and_unknown_pinned_versions(self):
        routes = {f"{versions.PYPI}/flask/json": FLASK}
        self.assertIn("flask==3.1.3 is the latest stable version", versions.lookup("flask==3.1.3", opener(routes, [])))
        self.assertIn("this version does not exist", versions.lookup("flask==9.9.9", opener(routes, [])))

    def test_unknown_package_and_network_failures_are_told_plainly(self):
        self.assertIn("not found in pypi", versions.lookup("nosuchpkg", opener({}, [])))
        down = opener({f"{versions.PYPI}/flask/json": urllib.error.URLError("dns")}, [])
        self.assertIn("could not reach pypi", versions.lookup("flask", down))
        self.assertIn("unverified", versions.lookup("flask", down))
        err = opener({f"{versions.PYPI}/flask/json": urllib.error.HTTPError("u", 503, "x", {}, None)}, [])
        self.assertIn("HTTP 503", versions.lookup("flask", err))

    def test_npm(self):
        calls = []
        out = versions.lookup("npm:express", opener({f"{versions.NPM}/express/latest": {"version": "5.3.0", "engines": {"node": ">= 18"}}}, calls))
        self.assertIn("npm express: latest stable 5.3.0, requires Node >= 18", out)
        self.assertEqual(calls, ["https://registry.npmjs.org/express/latest"])


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = Workspace(self.root, log_enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_project_says_everything_is_unknown_and_to_ask(self):
        out = environment.describe(self.ws)
        self.assertIn("UNKNOWN", out)
        self.assertIn("ask the user now", out)

    def test_manifests_and_venv_are_summarised_without_running_anything(self):
        (self.root / "requirements.txt").write_text("# web\nflask==3.1.3\nrequests>=2.32\n")
        (self.root / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\ndependencies = ["rich"]\n')
        (self.root / "package.json").write_text(json.dumps({"engines": {"node": ">=18"}, "dependencies": {"react": "^18.2.0"}}))
        (self.root / "Dockerfile").write_text("FROM python:3.12-slim\nRUN pip install x\nENV MODE=prod\n")
        site = self.root / ".venv" / "lib" / "python3.14" / "site-packages"
        (site / "flask-3.1.3.dist-info").mkdir(parents=True)
        (site / "werkzeug-3.1.9.dist-info").mkdir()
        (site / "pip-26.0.dist-info").mkdir()
        (self.root / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.14.0\n")
        out = environment.describe(self.ws)
        for expected in ("requirements.txt:", "flask==3.1.3", "requests>=2.32", "requires-python", "engines: node >=18",
                         "react ^18.2.0", "FROM python:3.12-slim", "ENV MODE=prod", "virtual environment `.venv`: Python 3.14.0",
                         "Flask==3.1.3".lower(), "werkzeug==3.1.9"):
            self.assertIn(expected, out.replace("Flask==3.1.3", "flask==3.1.3"), expected)
        self.assertNotIn("# web", out)  # комментарии не нужны
        self.assertNotIn("pip==", out)
        self.assertNotIn("RUN pip install", out)

    def test_closed_lines_do_not_get_into_the_summary(self):
        text = "flask==3.1.3\nsecret-internal-lib==9.9.9\nrequests\n"
        (self.root / "requirements.txt").write_text(text)
        m = Marks()
        m.add("hidden", 1, 1)
        m.save(self.root / "requirements.txt", text)
        out = Workspace(self.root, log_enabled=False).project_environment()
        self.assertIn("flask==3.1.3", out)
        self.assertNotIn("secret-internal-lib", out)

    def test_output_is_capped(self):
        (self.root / "requirements.txt").write_text("\n".join(f"package{i}==1.{i}.0" for i in range(500)))
        out = environment.describe(self.ws)
        self.assertLessEqual(len(out), environment.MAX_CHARS + 20)
        self.assertIn("more lines", out)
