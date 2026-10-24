import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from aiedit import checks, settings
from aiedit.policy import Rules


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def py(self, code):
        return shlex.join([sys.executable, "-c", code])

    def test_pass_and_fail(self):
        ok = checks.run_command(self.py("print('ok')"), self.root)
        self.assertTrue(ok.passed)
        self.assertEqual(ok.output.strip(), "ok")
        bad = checks.run_command(self.py("import sys; print('boom'); sys.exit(3)"), self.root)
        self.assertEqual((bad.code, bad.passed, bad.status()), (3, False, "не прошла (код 3)"))
        self.assertIn("boom", bad.output)

    def test_stderr_is_captured_and_cwd_used(self):
        r = checks.run_command(self.py("import os, sys; sys.stderr.write(os.getcwd())"), self.root)
        self.assertEqual(Path(r.output).resolve(), self.root)

    def test_timeout_kills_whole_group(self):
        marker = self.root / "child_alive"
        inner = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w')"
        code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {inner!r}]); time.sleep(30)"
        r = checks.run_command(self.py(code), self.root, timeout=0.5)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.passed)
        import time
        time.sleep(2)
        self.assertFalse(marker.exists(), "дочерний процесс пережил таймаут")

    def test_missing_program_and_empty_command(self):
        self.assertIn("не запустилась", checks.run_command("no-such-program-xyz", self.root).status())
        self.assertIn("не запустилась", checks.run_command("   ", self.root).status())

    def test_no_shell_so_metacharacters_are_literal(self):
        r = checks.run_command(f"{sys.executable} -c 'print(1)' ; echo hacked", self.root)
        self.assertNotIn("hacked", r.output.split("\n")[-1] if r.output else "")

    def test_api_key_not_passed_to_child(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-not-leak"
        try:
            r = checks.run_command(self.py("import os; print(os.environ.get('ANTHROPIC_API_KEY'))"), self.root)
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertEqual(r.output.strip(), "None")

    def test_output_is_capped(self):
        r = checks.run_command(self.py("print('x' * 100000)"), self.root)
        self.assertLessEqual(len(r.output), checks.MAX_OUTPUT)
        self.assertLessEqual(len(checks.tail(r.output)), checks.MAX_SHOWN + 5)


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "secrets").mkdir()
        Rules(self.root).set_denied(self.root / "secrets", True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_frames_from_denied_files_are_removed_with_their_source_line(self):
        tb = (f'Traceback (most recent call last):\n  File "{self.root}/main.py", line 3, in <module>\n    run()\n'
              f'  File "{self.root}/secrets/keys.py", line 9, in load\n    return API_PASSWORD_VALUE\n'
              "ValueError: bad")
        out = checks.scrub(tb)
        self.assertIn("main.py", out)
        self.assertIn("run()", out)
        self.assertNotIn("keys.py", out)
        self.assertNotIn("API_PASSWORD_VALUE", out)
        self.assertIn("ValueError: bad", out)

    def test_secret_lines_and_concealed_code_removed(self):
        out = checks.scrub('FAIL\nkey = "sk-ant-api03-AbCdEf0123456789xyz"\nreturn hidden_formula(x)\nok',
                           concealed={"return hidden_formula(x)"})
        self.assertNotIn("sk-ant", out)
        self.assertNotIn("hidden_formula", out)
        self.assertIn("FAIL", out)
        self.assertEqual(out.count("[line hidden]"), 2)


    def test_tokens_from_denied_files_and_hidden_zones_are_removed(self):
        (self.root / "secrets" / "k.py").write_text("def get():\n    return 'API_PASSWORD_VALUE'\n")
        tokens = checks.protected_tokens(self.root, {"return hidden_formula(x)"})
        out = checks.scrub("AssertionError: API_PASSWORD_VALUE\nsee secrets/k.py\nmath: hidden_formula(x)\nplain failure",
                           tokens=tokens)
        self.assertNotIn("API_PASSWORD_VALUE", out)
        self.assertNotIn("secrets/k.py", out)
        self.assertNotIn("hidden_formula", out)
        self.assertIn("plain failure", out)

    def test_no_denied_files_means_nothing_protected(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(checks.protected_tokens(Path(d)), set())


class SettingsTest(unittest.TestCase):
    def test_per_project_values_in_user_config(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["AIEDIT_CONFIG_DIR"] = d
            try:
                proj = Path(d) / "proj"; proj.mkdir()
                self.assertEqual(settings.get_project(proj), {})
                settings.set_project(proj, test_command="pytest -q")
                settings.set_project(proj, auto=True)
                self.assertEqual(settings.get_project(proj), {"test_command": "pytest -q", "auto": True})
                self.assertEqual(settings.get_project(Path(d) / "other"), {})
                self.assertFalse((proj / "settings.json").exists())
            finally:
                del os.environ["AIEDIT_CONFIG_DIR"]


if __name__ == "__main__":
    unittest.main()
