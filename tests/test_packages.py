import shlex
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, checks, packages
from tests.test_agent import AgentCase, Script, tool


class ParseTest(unittest.TestCase):
    def test_names_versions_extras(self):
        self.assertEqual(packages.parse("flask requests==2.32.3, uvicorn[standard]>=0.30,<1  Django~=5.0"),
                         ["flask", "requests==2.32.3", "uvicorn[standard]>=0.30,<1", "Django~=5.0"])
        self.assertEqual(packages.parse("flask flask"), ["flask"])

    def test_only_pypi_names(self):
        for bad in ("https://evil.example/pkg.whl", "./local", "-e .", "--index-url=x flask", "git+https://x/y",
                    "pkg;rm -rf", "../x", "flask==1.0 ; os_name", "a" * 120, "pkg@ https://x"):
            out = packages.parse(bad)
            self.assertIsInstance(out, str, bad)
            self.assertTrue(out.startswith("ERROR"), bad)
        self.assertTrue(packages.parse("").startswith("ERROR"))
        self.assertIn("at most", packages.parse(" ".join(f"p{i}" for i in range(11))))

    def test_pip_command_uses_wheels_only_and_ends_options(self):
        argv = packages.pip_argv(Path("/v/bin/python"), ["flask"])
        self.assertEqual(argv[:4], ["/v/bin/python", "-m", "pip", "install"])
        self.assertIn("--only-binary", argv)
        self.assertEqual(argv[argv.index("--only-binary") + 1], ":all:")
        self.assertEqual(argv[-2:], ["--", "flask"])
        self.assertEqual(packages.name_of("uvicorn[standard]>=0.30"), "uvicorn")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ran, self.asked, self.shown = [], [], []

    def tearDown(self):
        self.tmp.cleanup()

    def make_venv(self):
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").write_text("")

    def installer(self, answer=True, code=0, output="Successfully installed flask-3.0"):
        def run(command, cwd, timeout):
            self.ran.append(shlex.split(command))
            return checks.CheckResult(command, code, output, 1.0)
        return packages.Installer(self.root, lambda v, p: (self.asked.append((v, p)), answer)[1], self.shown.append, run)

    def test_installs_after_approval(self):
        self.make_venv()
        out = self.installer()("flask")
        self.assertEqual(self.asked, [(self.root / ".venv", ["flask"])])
        self.assertEqual(self.ran[0][0], str(self.root / ".venv" / "bin" / "python"))
        self.assertIn("--only-binary", self.ran[0])
        self.assertTrue(out.startswith("$ pip install flask\nexit code 0\nSuccessfully installed"))
        self.assertEqual(self.shown[0].command, "pip install flask")

    def test_declined_or_bad_or_no_venv(self):
        self.assertIn("no virtual environment", self.installer()("flask"))
        self.make_venv()
        self.assertEqual(self.installer(answer=False)("flask"), packages.OUT_DECLINED)
        self.assertTrue(self.installer()("https://x/y").startswith("ERROR"))
        self.assertEqual(self.ran, [])
        self.assertEqual(len(self.asked), 1)  # про плохой список даже не спрашиваем

    def test_failure_hint(self):
        self.make_venv()
        out = self.installer(code=1, output="ERROR: No matching distribution")("weird")
        self.assertIn("exit code 1", out)
        self.assertIn("prebuilt wheels", out)


class AgentInstallTest(AgentCase):
    def test_tool(self):
        from aiedit.workspace import Workspace
        prov = Script([tool("install_packages", packages="flask"), "ok"])
        res = agent.run("t", Workspace(self.root), prov, install=lambda p: f"installed {p}")
        self.assertIn("install_packages", prov.seen[0][0])
        self.assertIn("@@result install_packages\ninstalled flask\n@@end", prov.everything())
        self.assertIn("прошу установить: flask", res.calls)
        prov = Script([tool("install_packages", packages="flask"), "ok"])
        agent.run("t", Workspace(self.root), prov)
        self.assertIn("ERROR: installing packages is not available", prov.everything())


if __name__ == "__main__":
    unittest.main()
