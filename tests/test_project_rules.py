import tempfile
import unittest
from pathlib import Path

from aiedit import agent, conductor, project_rules
from aiedit.marks import Marks
from aiedit.policy import Rules
from aiedit.roster import Roster, Worker
from aiedit.workspace import Workspace
from tests.test_agent import Script


class RulesFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        self.root.mkdir()
        (self.root / "main.py").write_text("x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def ws(self):
        return Workspace(self.root)

    def test_agents_md_goes_into_first_message(self):
        (self.root / "AGENTS.md").write_text("Stack: Flask. Tests: pytest -q. 4 spaces.\n")
        prov = Script(["ok"])
        agent.run("add a page", self.ws(), prov)
        first = prov.seen[0][1][0]["content"]
        self.assertIn("=== PROJECT RULES (AGENTS.md) ===\nStack: Flask. Tests: pytest -q. 4 spaces.", first)
        self.assertTrue(first.startswith("add a page"))
        self.assertIn("PROJECT RULES", prov.seen[0][0])

    def test_order_of_names_and_none(self):
        self.assertIsNone(project_rules.find(self.ws()))
        (self.root / "CLAUDE.md").write_text("claude rules")
        self.assertEqual(project_rules.find(self.ws()), ("CLAUDE.md", "claude rules"))
        (self.root / "AGENTS.md").write_text("agents rules")
        self.assertEqual(project_rules.find(self.ws())[0], "AGENTS.md")
        (self.root / "AGENTS.md").write_text("   \n")
        self.assertIsNone(project_rules.find(self.ws()))

    def test_closed_file_zones_and_secrets_are_respected(self):
        text = 'Deploy to prod-db-7.\nTOKEN = "sk-ant-api03-AbCdEf0123456789xyz"\nUse pytest.\n'
        (self.root / "AGENTS.md").write_text(text)
        m = Marks()
        m.add("hidden", 0, 0)
        m.save(self.root / "AGENTS.md", text)
        found = project_rules.find(self.ws())[1]
        self.assertNotIn("prod-db-7", found)
        self.assertNotIn("sk-ant", found)
        self.assertIn("Use pytest.", found)
        Rules(self.root).set_denied(self.root / "AGENTS.md", True)
        self.assertIsNone(project_rules.find(self.ws()))

    def test_long_rules_are_cut(self):
        (self.root / "AGENTS.md").write_text("r" * (project_rules.MAX_CHARS + 100))
        self.assertIn("keep this file short", project_rules.find(self.ws())[1])

    def test_conductor_sees_rules(self):
        (self.root / "AGENTS.md").write_text("Only small steps.")
        roster = Roster([Worker("sonnet", Script([]), "x", 1, 1, "mid")])
        self.assertIn("PROJECT RULES (AGENTS.md):\nOnly small steps.", conductor.build_request("t", self.ws(), roster))


if __name__ == "__main__":
    unittest.main()
