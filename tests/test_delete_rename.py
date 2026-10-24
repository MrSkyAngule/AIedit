"""Удаление и перенос файлов агентом: только предложения, файлы с зонами - только человек."""
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, sandbox
from aiedit.marks import Marks
from aiedit.policy import Rules
from aiedit.workspace import Workspace
from tests.test_agent import Script, tool


class MoveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "secret").mkdir()
        (self.root / "old.py").write_text("def f():\n    return 1\n")
        (self.root / "pkg" / "util.py").write_text("x = 1\n")
        (self.root / "zoned.py").write_text("a = 1\nb = 2\n")
        m = Marks()
        m.add("readonly", 0, 0)
        m.save(self.root / "zoned.py", "a = 1\nb = 2\n")
        (self.root / "keys.py").write_text('K = "sk-ant-api03-AbCdEf0123456789xyz"\n')
        (self.root / "secret" / "s.py").write_text("S = 1\n")
        Rules(self.root).set_denied(self.root / "secret", True)
        self.ws = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class DeleteTest(MoveCase):
    def test_delete_is_a_proposal_and_file_disappears_for_the_model(self):
        self.assertEqual(self.ws.delete_file("old.py"), "file will be deleted: old.py")
        self.assertTrue((self.root / "old.py").exists())  # на диске ничего не тронуто
        (ch,) = self.ws.changes()
        self.assertTrue(ch.deleted)
        self.assertEqual((ch.old, ch.new), ("def f():\n    return 1\n", ""))
        self.assertTrue(self.ws.read_file("old.py").startswith("ERROR: file not found"))
        self.assertNotIn("old.py", self.ws.list_files())
        self.assertEqual(self.ws.search("return"), "no matches")
        self.assertTrue(self.ws.edit_file("old.py", "return 1", "return 2").startswith("ERROR"))

    def test_protected_secret_and_closed_files_cannot_be_deleted(self):
        self.assertIn("protected zones (readonly)", self.ws.delete_file("zoned.py"))
        self.assertIn("protected zones (hidden)", self.ws.delete_file("keys.py"))  # строка с ключом скрыта
        self.assertTrue(self.ws.delete_file("secret/s.py").startswith("ERROR: file not found"))
        self.assertTrue(self.ws.delete_file("../x.py").startswith("ERROR"))
        self.assertEqual(self.ws.changes(), [])

    def test_deleting_a_new_file_just_drops_it(self):
        self.ws.create_file("tmp.py", "y = 1\n")
        self.assertEqual(self.ws.delete_file("tmp.py"), "new file dropped: tmp.py")
        self.assertEqual(self.ws.changes(), [])

    def test_create_after_delete_replaces_content(self):
        self.ws.delete_file("old.py")
        self.assertEqual(self.ws.create_file("old.py", "new\n"), "file will be created: old.py")
        (ch,) = self.ws.changes()
        self.assertFalse(ch.deleted)
        self.assertEqual(ch.new, "new\n")

    def test_read_only_and_edit_scope_apply(self):
        self.ws.read_only = True
        self.assertIn("read-only", self.ws.delete_file("old.py"))
        self.ws.read_only = False
        self.ws.edit_scope = {self.root / "pkg" / "util.py"}
        self.assertIn("may only edit", self.ws.delete_file("old.py"))


class RenameTest(MoveCase):
    def test_rename_moves_current_text_including_pending_edits(self):
        self.ws.edit_file("old.py", "return 1", "return 7")
        self.assertEqual(self.ws.rename_file("old.py", "pkg/new.py"),
                         "file will be moved: old.py -> pkg/new.py (edit it at the new path)")
        changes = {self.ws.rel(c.path): c for c in self.ws.changes()}
        self.assertTrue(changes["old.py"].deleted)
        self.assertTrue(changes["pkg/new.py"].created)
        self.assertIn("return 7", changes["pkg/new.py"].new)
        self.assertIn("return 7", self.ws.read_file("pkg/new.py"))

    def test_rename_target_rules(self):
        self.assertIn("already exists", self.ws.rename_file("old.py", "pkg/util.py"))
        self.assertIn("cannot create a file here", self.ws.rename_file("old.py", "secret/x.py"))
        self.assertIn("cannot create a file here", self.ws.rename_file("old.py", ".env"))
        self.assertIn("outside", self.ws.rename_file("old.py", "../x.py"))
        self.assertIn("already exists", self.ws.rename_file("old.py", "old.py"))
        self.assertIn("protected zones", self.ws.rename_file("zoned.py", "z2.py"))
        self.assertEqual(self.ws.changes(), [])

    def test_rename_new_file_leaves_no_trace_at_old_path(self):
        self.ws.create_file("a.py", "1\n")
        self.ws.rename_file("a.py", "b.py")
        self.assertEqual([(self.ws.rel(c.path), c.created) for c in self.ws.changes()], [("b.py", True)])


class AgentMoveTest(MoveCase):
    def test_agent_tools(self):
        prov = Script([tool("delete_file", path="pkg/util.py") + "\n" + tool("rename_file", path="old.py", new_path="lib/f.py"),
                       "Done."])
        res = agent.run("tidy", self.ws, prov)
        self.assertEqual(sorted((self.ws.rel(c.path), c.deleted) for c in res.changes),
                         [("lib/f.py", False), ("old.py", True), ("pkg/util.py", True)])
        self.assertIn("предлагаю удалить pkg/util.py", res.calls)
        self.assertIn("delete_file", prov.seen[0][0])
        self.assertIn("rename_file", prov.seen[0][0])


class SandboxMoveTest(MoveCase):
    def test_copy_follows_deletes_and_moves(self):
        seen = []

        def run(command, work, venv, extra, timeout):
            from aiedit import checks
            seen.append(sorted(p.relative_to(work).as_posix() for p in work.rglob("*.py")))
            return checks.CheckResult(command, 0, "", 0)
        t = sandbox.Terminal(self.ws, lambda c: "run", runner=run)
        self.addCleanup(t.close)
        t("ls")
        self.ws.rename_file("old.py", "pkg/new.py")
        t("ls")
        self.ws.create_file("old.py", "again\n")
        t("ls")
        self.assertIn("old.py", seen[0])
        self.assertNotIn("old.py", seen[1])
        self.assertIn("pkg/new.py", seen[1])
        self.assertIn("old.py", seen[2])


if __name__ == "__main__":
    unittest.main()
