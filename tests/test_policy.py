import tempfile
import unittest
from pathlib import Path

from aiedit import orchestrator, policy
from aiedit.guard import Region
from aiedit.policy import RULES_NAME, AccessDenied, Rules
from aiedit.providers import Provider


class Spy(Provider):
    name = "spy"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        return "```\nx\n```"


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("secrets", "src/core", "src/ok"):
            (self.root / d).mkdir(parents=True)
        for f in ("secrets/key.txt", "src/core/pay.py", "src/ok/a.py", ".env", "main.py"):
            (self.root / f).write_text("x")
        self.rules = Rules(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_denied_by_default(self):
        self.assertFalse(policy.is_denied(self.root / "main.py"))

    def test_folder_denies_everything_inside(self):
        self.rules.set_denied(self.root / "secrets", True)
        self.assertTrue(policy.is_denied(self.root / "secrets" / "key.txt"))
        self.assertTrue(policy.is_denied(self.root / "secrets"))
        self.assertFalse(policy.is_denied(self.root / "src" / "ok" / "a.py"))

    def test_file_rule_and_similar_name_not_confused(self):
        self.rules.set_denied(self.root / ".env", True)
        self.assertTrue(policy.is_denied(self.root / ".env"))
        (self.root / ".env.example").write_text("x")
        self.assertFalse(policy.is_denied(self.root / ".env.example"))

    def test_symlink_into_denied_is_denied(self):
        self.rules.set_denied(self.root / "secrets", True)
        link = self.root / "src" / "ok" / "link.txt"
        link.symlink_to(self.root / "secrets" / "key.txt")
        self.assertTrue(policy.is_denied(link))

    def test_dotdot_path_is_denied(self):
        self.rules.set_denied(self.root / "secrets", True)
        sneaky = self.root / "src" / "ok" / ".." / ".." / "secrets" / "key.txt"
        self.assertTrue(policy.is_denied(sneaky))

    def test_allow_again(self):
        self.rules.set_denied(self.root / "secrets", True)
        self.assertTrue(self.rules.set_denied(self.root / "secrets", False))
        self.assertFalse(policy.is_denied(self.root / "secrets" / "key.txt"))
        self.assertFalse(self.rules.file.exists())

    def test_child_cannot_override_parent_deny(self):
        self.rules.set_denied(self.root / "secrets", True)
        self.assertFalse(self.rules.set_denied(self.root / "secrets" / "key.txt", False))
        self.assertTrue(policy.is_denied(self.root / "secrets" / "key.txt"))

    def test_denying_parent_absorbs_child_entries(self):
        self.rules.set_denied(self.root / "src" / "core", True)
        self.rules.set_denied(self.root / "src", True)
        self.assertEqual(self.rules.denied, ["src"])

    def test_broken_rules_file_fails_closed(self):
        (self.root / RULES_NAME).write_text("{ not json")
        self.assertTrue(policy.is_denied(self.root / "main.py"))

    def test_rules_persist_and_nested_project_rules_found(self):
        self.rules.set_denied(self.root / "src" / "core", True)
        again = Rules(self.root)
        self.assertEqual(again.denied, ["src/core"])
        self.assertTrue(policy.is_denied(self.root / "src" / "core" / "pay.py"))

    def test_rules_file_itself_is_denied(self):
        self.rules.set_denied(self.root / "secrets", True)
        self.assertTrue(policy.is_denied(self.root / RULES_NAME))

    def test_root_cannot_be_denied(self):
        self.assertFalse(self.rules.set_denied(self.root, True))

    def test_orchestrator_never_calls_provider_for_denied_file(self):
        self.rules.set_denied(self.root / "secrets", True)
        spy = Spy()
        provs = {"cheap": spy, "strong": spy}
        target = self.root / "secrets" / "key.txt"
        with self.assertRaises(AccessDenied):
            orchestrator.ask("x", "t", [], provs, source=target)
        with self.assertRaises(AccessDenied):
            orchestrator.explain("x", [Region("explain", 0, 0)], provs, source=target)
        self.assertEqual(spy.calls, 0)
        orchestrator.ask("x", "t", [], provs, source=self.root / "main.py")
        self.assertEqual(spy.calls, 1)


if __name__ == "__main__":
    unittest.main()
