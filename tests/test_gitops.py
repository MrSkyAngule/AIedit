import os
import tempfile
import unittest
from pathlib import Path

from aiedit import gitops

IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.root.mkdir()
        self.saved = {k: os.environ.get(k) for k in IDENT}
        os.environ.update(IDENT)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def repo_with_commit(self):
        gitops.init(self.root)
        (self.root / "a.py").write_text("x = 1\n")
        (self.root / "b.py").write_text("y = 1\n")
        self.assertTrue(gitops.commit(self.root, ["a.py", "b.py"], "first")[0])


class GitOpsTest(GitCase):
    def test_not_a_repo_then_init(self):
        self.assertIsNone(gitops.toplevel(self.root))
        ok, _ = gitops.init(self.root)
        self.assertTrue(ok)
        self.assertEqual(gitops.toplevel(self.root), self.root)

    def test_status_labels_and_head_text(self):
        self.repo_with_commit()
        (self.root / "a.py").write_text("x = 2\n")
        (self.root / "b.py").unlink()
        (self.root / "new dir").mkdir()
        (self.root / "new dir" / "c.py").write_text("z\n")
        (self.root / ".aiedit-chat.json").write_text("{}")
        got = {e.path: (e.label, e.private) for e in gitops.status(self.root)}
        self.assertEqual(got, {"a.py": ("изменён", False), "b.py": ("удалён", False),
                               "new dir/c.py": ("новый", False), ".aiedit-chat.json": ("новый", True)})
        self.assertEqual(gitops.head_text(self.root, "a.py"), "x = 1\n")
        self.assertIsNone(gitops.head_text(self.root, "new dir/c.py"))

    def test_commit_only_chosen_files(self):
        self.repo_with_commit()
        (self.root / "a.py").write_text("x = 2\n")
        (self.root / "b.py").write_text("y = 2\n")
        ok, _ = gitops.commit(self.root, ["a.py"], "only a")
        self.assertTrue(ok)
        self.assertEqual([e.path for e in gitops.status(self.root)], ["b.py"])

    def test_commit_needs_files_and_message(self):
        self.repo_with_commit()
        self.assertEqual(gitops.commit(self.root, [], "m"), (False, "не выбрано ни одного файла"))
        self.assertEqual(gitops.commit(self.root, ["a.py"], "  "), (False, "нужно сообщение коммита"))

    def test_rename_is_one_entry(self):
        self.repo_with_commit()
        gitops.run(self.root, "mv", "a.py", "moved.py")
        (e,) = gitops.status(self.root)
        self.assertEqual((e.path, e.old_path, e.label), ("moved.py", "a.py", "переименован"))
        self.assertTrue(gitops.commit(self.root, [e.path, e.old_path], "move")[0])
        self.assertEqual(gitops.status(self.root), [])

    def test_branch_even_without_commits(self):
        gitops.init(self.root)
        self.assertNotEqual(gitops.branch(self.root), "?")


if __name__ == "__main__":
    unittest.main()
