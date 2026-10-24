import json
import tempfile
import unittest
from pathlib import Path

from aiedit import chatstore, policy
from aiedit.orchestrator import Turn


class ChatStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        msgs = [("user", "привет", ""), ("ai", "ответ", "model · причина"), ("system", "готово", "")]
        hist = [Turn("user", "привет", self.root / "a.py"), Turn("assistant", "ответ", None)]
        self.assertTrue(chatstore.save(self.root, msgs, hist))
        got_msgs, got_hist = chatstore.load(self.root)
        self.assertEqual(got_msgs, msgs)
        self.assertEqual(got_hist, hist)

    def test_missing_or_broken_file_gives_empty_chat(self):
        self.assertEqual(chatstore.load(self.root), ([], []))
        chatstore.chat_path(self.root).write_text("{ broken")
        self.assertEqual(chatstore.load(self.root), ([], []))
        chatstore.chat_path(self.root).write_text(json.dumps({"messages": [1], "history": []}))
        self.assertEqual(chatstore.load(self.root), ([], []))

    def test_sources_are_relative_so_project_can_move(self):
        chatstore.save(self.root, [], [Turn("user", "q", self.root / "src" / "a.py")])
        data = json.loads(chatstore.chat_path(self.root).read_text())
        self.assertEqual(data["history"][0]["source"], "src/a.py")

    def test_message_count_is_bounded(self):
        msgs = [("user", str(i), "") for i in range(chatstore.MAX_MESSAGES + 30)]
        chatstore.save(self.root, msgs, [])
        self.assertEqual(len(chatstore.load(self.root)[0]), chatstore.MAX_MESSAGES)

    def test_chat_file_is_private_for_ai(self):
        self.assertTrue(policy.is_denied(chatstore.chat_path(self.root)))

    def test_forget(self):
        chatstore.save(self.root, [], [])
        chatstore.forget(self.root)
        self.assertFalse(chatstore.chat_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
