import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from aiedit import analysis
from aiedit.analysis import Analyzer, Issue, analyze_once, build_shadow, find_binary, run_cycle
from aiedit.marks import Marks
from aiedit.policy import Rules
from aiedit.providers import Provider
from aiedit.roster import Worker
from aiedit.workspace import Workspace


def fake_parse(out):
    return [Issue("fake", m["f"].removeprefix("./"), int(m["l"]), "X1", m["m"])
            for m in re.finditer(r"^(?P<f>\S+):(?P<l>\d+): (?P<m>.+)$", out, re.M)]


FAKE = Analyzer("fake", (".py",), (), fake_parse)


class FakeRunner:
    """Читает файлы во временной копии и сообщает о строках с BAD: так видно, что именно анализируется."""
    def __init__(self):
        self.calls = []
        self.shadow_files = []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        self.shadow_files = sorted(str(p.relative_to(cwd)) for p in Path(cwd).rglob("*") if p.is_file())
        out = []
        for arg in argv[1:]:
            path = Path(cwd) / arg
            if path.is_file():
                for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                    if "BAD" in line:
                        out.append(f"{arg}:{n}: problem {line.strip().split('BAD')[-1].strip() or 'here'}")
        return (1 if out else 0), "\n".join(out)


class ParserTest(unittest.TestCase):
    def test_ruff_real_output(self):
        out = ("bad.py:1:8: F401 [*] `os` imported but unused\nbad.py:6:5: F841 Local variable `unused` is assigned "
               "to but never used\nFound 4 errors.\n[*] 2 fixable with `--fix`\n")
        self.assertEqual(analysis.parse_ruff(out), [Issue("ruff", "bad.py", 1, "F401", "`os` imported but unused"),
                                                    Issue("ruff", "bad.py", 6, "F841", "Local variable `unused` is assigned to but never used")])

    def test_mypy_real_output_notes_are_dropped(self):
        out = ('bad.py:7:12: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
               'bad.py:7:12: note: some hint\nbad.py:9:1: warning: unused ignore  [unused-ignore]\n')
        got = analysis.parse_mypy(out)
        self.assertEqual([(i.line, i.code) for i in got], [(7, "return-value"), (9, "unused-ignore")])

    def test_bandit_low_is_dropped(self):
        out = ("sec.py:1: LOW: B404 Consider possible security implications.\n"
               "sec.py:2: HIGH: B602 subprocess call with shell=True identified, security issue.\n")
        self.assertEqual(analysis.parse_bandit(out), [Issue("bandit", "sec.py", 2, "B602",
                                                            "[HIGH] subprocess call with shell=True identified, security issue.")])

    def test_eslint_and_tsc(self):
        self.assertEqual(analysis.parse_eslint("./a.js:3:5: 'x' is defined but never used. [Error/no-unused-vars]\n"),
                         [Issue("eslint", "a.js", 3, "no-unused-vars", "'x' is defined but never used.")])
        self.assertEqual(analysis.parse_tsc("src/a.ts(4,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"),
                         [Issue("tsc", "src/a.ts", 4, "TS2322", "Type 'string' is not assignable to type 'number'.")])

    def test_garbage_gives_nothing(self):
        for parse in (analysis.parse_ruff, analysis.parse_mypy, analysis.parse_bandit, analysis.parse_eslint, analysis.parse_tsc):
            self.assertEqual(parse("Traceback (most recent call last):\n  boom\n"), [])


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        for d in ("pkg", "secrets", ".git", "node_modules"):
            (self.root / d).mkdir(parents=True)
        files = {"main.py": "def run():\n    return 1\n", "pkg/pay.py": "KEY = 'k'\n\n\ndef fee(a):\n    return a  # BAD old\n",
                 "secrets/k.py": "TOP_SECRET = 1  # BAD\n", ".env": "A_SECRET=zzzzzzzz\n", ".git/config": "x",
                 "node_modules/m.js": "m"}
        for n, t in files.items():
            (self.root / n).write_text(t)
        Rules(self.root).set_denied(self.root / "secrets", True)
        self.ws = Workspace(self.root, log_enabled=False)
        self.runner = FakeRunner()

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self):
        return analyze_once(self.ws, [FAKE], self.runner)


class ShadowTest(Case):
    def test_copy_contains_visible_files_only(self):
        with tempfile.TemporaryDirectory() as d:
            build_shadow(self.ws, Path(d))
            names = sorted(str(p.relative_to(d)) for p in Path(d).rglob("*") if p.is_file() or p.is_symlink())
        self.assertEqual(names, ["main.py", "node_modules", "pkg/pay.py"])  # node_modules - ссылка
        self.assertNotIn("secrets", " ".join(names))

    def test_size_cap(self):
        old = analysis.MAX_FILES
        analysis.MAX_FILES = 1
        try:
            with tempfile.TemporaryDirectory() as d, self.assertRaises(analysis.TooBig):
                build_shadow(self.ws, Path(d))
        finally:
            analysis.MAX_FILES = old


class AnalyzeTest(Case):
    def test_only_new_problems_are_reported(self):
        self.ws.edit_file("pkg/pay.py", "def fee(a):\n    return a  # BAD old", "def fee(a):\n    return a * 2  # BAD old\n    # BAD fresh")
        rnd, ran, notes, skipped = self.analyze()
        self.assertEqual([(i.file, i.message) for i in rnd.issues], [("pkg/pay.py", "problem fresh")])
        self.assertEqual((ran, notes, skipped), ({"fake"}, [], ""))

    def test_baseline_uses_the_original_text_and_new_files_are_all_new(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD main")
        self.ws.create_file("pkg/new.py", "x = 1  # BAD created\n")
        rnd = self.analyze()[0]
        self.assertEqual(sorted((i.file, i.message) for i in rnd.issues),
                         [("main.py", "problem main"), ("pkg/new.py", "problem created")])

    def test_unchanged_or_unsupported_files_mean_nothing_to_analyze(self):
        self.assertEqual(self.analyze()[3], "no analyzable files changed")
        (self.root / "notes.txt").write_text("a\n")
        self.ws.edit_file("notes.txt", "a", "b")
        self.assertEqual(self.analyze()[0], None)
        self.assertEqual(self.runner.calls, [])

    def test_runner_never_sees_closed_files(self):
        self.ws.edit_file("main.py", "return 1", "return 2")
        self.analyze()
        self.assertTrue(self.runner.shadow_files)
        for name in self.runner.shadow_files:
            self.assertNotIn("secrets", name)
            self.assertNotIn(".env", name)
            self.assertNotIn(".git", name)

    def test_file_names_cannot_become_options(self):
        (self.root / "-x.py").write_text("a = 1\n")
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("-x.py", "a = 1", "a = 2")
        self.analyze()
        self.assertEqual(self.runner.calls[-1][1], "./-x.py")

    def test_problems_in_closed_zones_are_not_shown_to_the_author(self):
        m = Marks()
        m.add("hidden", 0, 0); m.add("readonly", 2, 2)
        m.save(self.root / "main.py", "def run():\n    return 1\n")
        (self.root / "main.py").write_text("X = 1  # BAD closed\ndef run():\n    return 1  # BAD ro\n\n\ndef free():\n    return 1\n")
        m = Marks(); m.add("hidden", 0, 0); m.add("readonly", 2, 2); m.save(self.root / "main.py", (self.root / "main.py").read_text())
        self.ws = Workspace(self.root, log_enabled=False)
        self.ws.edit_file("main.py", "def free():\n    return 1", "def free():\n    return 1  # BAD mine")

        def runner(argv, cwd):  # сообщение зависит от общего числа BAD в файле: правка меняет и «старые» замечания
            out = []
            for arg in argv[1:]:
                lines = (Path(cwd) / arg).read_text(encoding="utf-8").split("\n")
                total = sum("BAD" in l for l in lines)
                out += [f"{arg}:{n}: total {total}" for n, l in enumerate(lines, 1) if "BAD" in l]
            return 1, "\n".join(out)
        rnd = analyze_once(self.ws, [FAKE], runner)[0]
        self.assertEqual([i.line for i in rnd.issues], [7])  # строка автора
        self.assertEqual(rnd.ignored, 2)  # строки 1 и 3 в закрытых зонах

    def test_messages_that_quote_closed_code_are_hidden(self):
        Rules(self.root).set_denied(self.root / "secrets", True)
        (self.root / "secrets" / "k.py").write_text("def hidden_formula_function():\n    return 1\n")
        def runner(argv, cwd):  # замечание появляется только в новом коде
            new = "return 2" in (Path(cwd) / "main.py").read_text(encoding="utf-8")
            return 1, "main.py:2: undefined hidden_formula_function here" if new else ""
        self.ws.edit_file("main.py", "return 1", "return 2")
        rnd = analyze_once(self.ws, [FAKE], runner)[0]
        self.assertEqual(rnd.issues[0].message, "details hidden: refers to closed code")

    def test_missing_failing_and_project_wide_tools(self):
        self.ws.edit_file("main.py", "return 1", "return 2")
        missing = Analyzer("nope", (".py",), (), fake_parse)
        runner = lambda argv, cwd: (None, "")
        rnd, ran, notes, _ = analyze_once(self.ws, [FAKE], runner)
        self.assertEqual((rnd.issues, ran), ([], set()))
        self.assertIn("fake: не запустился или не уложился во время", notes)
        rnd, ran, notes, _ = analyze_once(self.ws, [missing], analysis.default_runner)
        self.assertIn("nope: программа не найдена", notes)
        tsc = Analyzer("tsc", (".py",), ("-p", "."), fake_parse, project_wide=True, needs="tsconfig.json")
        calls = []
        analyze_once(self.ws, [tsc], lambda argv, cwd: (calls.append(argv), (0, ""))[1])
        self.assertEqual(calls, [])  # без tsconfig.json проект-вайд анализатор не запускается


class RealProcessTest(Case):
    def test_default_runner_runs_a_real_program_on_the_copy(self):
        script = ("import sys\nfor f in sys.argv[1:]:\n"
                  "    for n, l in enumerate(open(f, encoding='utf-8'), 1):\n"
                  "        if 'BAD' in l: print(f'{f}:{n}: found bad')\n")
        real = Analyzer("script", (".py",), ("-c", script), fake_parse, binary=sys.executable)
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD x")
        rnd, ran, notes, _ = analyze_once(self.ws, [real])
        self.assertEqual(([(i.file, i.line, i.message) for i in rnd.issues], ran, notes),
                         ([("main.py", 2, "found bad")], {"script"}, []))
        self.assertFalse((self.root / "main.py").read_text().count("BAD"))  # проект на диске не тронут

    def test_find_binary_prefers_the_project_environment(self):
        venv = self.root / ".venv" / "bin"
        venv.mkdir(parents=True)
        tool = venv / "mytool123"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(find_binary("mytool123", self.root), str(tool))
        self.assertIsNone(find_binary("definitely-not-installed-xyz", self.root))


class Script(Provider):
    def __init__(self, name, replies):
        self.name, self.replies, self.seen = name, list(replies), []

    def complete(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        return self.replies.pop(0) if self.replies else "ok"

    def sent(self):
        return "\n".join(s + "\n" + "\n".join(m["content"] for m in ms) for s, ms in self.seen)


def edit(path, old, new):
    return f"@@tool edit_file\npath: {path}\n@@old\n{old}\n@@new\n{new}\n@@end"


class CycleTest(Case):
    def author(self, replies):
        self.prov = Script("author", replies)
        w = Worker("sonnet", self.prov, "mid", 2, 10, "mid")
        return lambda path: w

    def test_problem_goes_back_to_the_author_and_is_fixed(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD one")
        rep = run_cycle("add run", self.ws, self.author([edit("main.py", "# BAD one", "# fine"), "fixed"]),
                        analyzers=[FAKE], runner=self.runner)
        self.assertEqual([len(r.issues) for r in rep.rounds], [1, 0])
        self.assertEqual((rep.fixed_rounds, rep.tools, rep.remaining), (1, ["fake"], []))
        prompt = self.prov.seen[0][1][0]["content"]
        self.assertIn("Static analysis found NEW problems", prompt)
        self.assertIn("- fake X1 main.py:2: problem one", prompt)
        self.assertIn("Original task: add run", prompt)
        self.assertNotIn("BAD", self.ws.changes()[0].new)

    def test_author_edits_only_files_with_problems(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD one")
        rep = run_cycle("t", self.ws, self.author([edit("pkg/pay.py", "KEY = 'k'", "KEY = 'z'"), "gave up"]),
                        analyzers=[FAKE], runner=self.runner)
        self.assertIn("this step may only edit: main.py", self.prov.seen[1][1][-1]["content"])
        self.assertEqual(len(rep.rounds), 1)  # ничего не изменилось - цикл закончен
        self.assertEqual(len(rep.remaining), 1)

    def test_at_most_two_fix_rounds(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD a")
        replies = [edit("main.py", "# BAD a", "# BAD b"), "x", edit("main.py", "# BAD b", "# BAD c"), "x", "never"]
        rep = run_cycle("t", self.ws, self.author(replies), analyzers=[FAKE], runner=self.runner)
        self.assertEqual(len(rep.rounds), 3)
        self.assertEqual(rep.remaining[0].message, "problem c")

    def test_clean_code_and_nothing_to_analyze(self):
        self.ws.edit_file("main.py", "return 1", "return 2")
        rep = run_cycle("t", self.ws, self.author([]), analyzers=[FAKE], runner=self.runner)
        self.assertEqual((len(rep.rounds), rep.remaining, self.prov.seen), (1, [], []))
        empty = Workspace(self.root, log_enabled=False)
        self.assertTrue(run_cycle("t", empty, self.author([]), analyzers=[FAKE], runner=self.runner).skipped)

    def test_stop_flag_and_progress(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD")
        rep = run_cycle("t", self.ws, self.author([]), should_stop=lambda: True, analyzers=[FAKE], runner=self.runner)
        self.assertEqual((rep.rounds, rep.stopped), ([], "остановлено пользователем"))
        seen = []
        run_cycle("t", self.ws, self.author([edit("main.py", "# BAD", "# ok"), "ok"]), on_progress=seen.append,
                  analyzers=[FAKE], runner=self.runner)
        self.assertEqual(seen[0], "статанализ: запускаю программы")
        self.assertIn("статанализ: новых замечаний 1, возвращаю авторам", seen)
        self.assertIn("статанализ: запускаю программы (круг 1)", seen)

    def test_nothing_russian_or_closed_goes_to_the_author(self):
        self.ws.edit_file("main.py", "return 1", "return 2  # BAD one")
        run_cycle("t", self.ws, self.author(["ok"]), english_reply=True, analyzers=[FAKE], runner=self.runner)
        self.assertIsNone(re.search("[А-Яа-яЁё]", self.prov.sent()))
        self.assertTrue(self.prov.seen[0][0].endswith("Reply in English."))
        for leak in ("TOP_SECRET", "zzzzzzzz"):
            self.assertNotIn(leak, self.prov.sent())


if __name__ == "__main__":
    unittest.main()
