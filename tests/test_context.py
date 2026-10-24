"""Длинные задачи: сжатие переписки, план агента, продолжение с согласия человека."""
import unittest

from aiedit import agent, context
from tests.test_agent import AgentCase, tool


def result(name, body):
    return f"@@result {name}\n{body}\n@@end"


class CompactTest(unittest.TestCase):
    def msgs(self):
        return [{"role": "user", "content": "TASK " + "t" * 1000},
                {"role": "assistant", "content": "@@tool read_file\npath: a.py\n@@end"},
                {"role": "user", "content": result("read_file", "A" * 80_000)},
                {"role": "assistant", "content": "@@tool edit_file\npath: a.py\n@@old\n" + "o" * 5000 + "\n@@new\n"
                 + "n" * 5000 + "\n@@end"},
                {"role": "user", "content": result("edit_file", "edit accepted: a.py")},
                {"role": "assistant", "content": "@@tool read_file\npath: b.py\n@@end"},
                {"role": "user", "content": result("read_file", "B" * 80_000)}]

    def test_small_conversation_untouched(self):
        m = [{"role": "user", "content": "hi"}]
        self.assertFalse(context.compact("s", m, 1))

    def test_old_results_and_edit_bodies_go_task_and_last_step_stay(self):
        m = self.msgs()
        self.assertTrue(context.compact("s", m, 1))
        self.assertTrue(m[0]["content"].startswith("TASK"))                 # задача
        self.assertIn("[cleared to save context: 80000 chars", m[2]["content"])
        self.assertIn("@@old\n[... 5001 chars ...]\n@@new\n[... 5001 chars ...]\n@@end", m[3]["content"])
        self.assertIn("edit accepted: a.py", m[4]["content"])               # короткий результат остался
        self.assertEqual(m[6]["content"], result("read_file", "B" * 80_000))  # последний шаг целиком
        self.assertLessEqual(context.size("s", m), context.COMPACT_AT)

    def test_history_before_start_is_never_touched(self):
        m = [{"role": "user", "content": result("read_file", "H" * 200_000)}] + self.msgs()
        context.compact("s", m, 2)
        self.assertEqual(m[0]["content"], result("read_file", "H" * 200_000))

    def test_huge_last_step_is_cut(self):
        m = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "@@tool read_file\n@@end"},
             {"role": "user", "content": result("read_file", "Z" * 300_000) + "\n\n" + result("read_file", "Y" * 300_000)}]
        self.assertTrue(context.compact("s", m, 1))
        self.assertLessEqual(context.size("s", m), context.HARD_LIMIT + 1000)
        self.assertIn("read a smaller range", m[2]["content"])

    def test_state_block(self):
        class C:
            def __init__(self, p, created=False, deleted=False):
                self.path, self.created, self.deleted = p, created, deleted
        text = context.state_block([C("a.py"), C("b.py", created=True), C("c.py", deleted=True)], "1. done\n2. left",
                                   lambda p: p)
        self.assertIn("- edited: a.py", text)
        self.assertIn("- created: b.py", text)
        self.assertIn("- deleted: c.py", text)
        self.assertIn("Your notes:\n1. done", text)
        self.assertIn("update_notes", context.state_block([], "", str))


class LongTaskTest(AgentCase):
    def test_notes_survive_compaction(self):
        (self.root / "big.txt").write_text("x" * 59000)
        replies = ["@@tool update_notes\n@@notes\n1. read big.txt\n2. edit main.py\n@@end"]
        replies += [tool("read_file", path="big.txt")] * 5 + ["done"]
        res, prov, _ = self.go(replies)
        self.assertEqual(res.plan, "1. read big.txt\n2. edit main.py")
        self.assertGreater(res.compactions, 0)
        later = "\n".join(m["content"] for m in prov.seen[-1][1])
        self.assertIn("@@result editor_state", later)
        self.assertIn("1. read big.txt", later)
        self.assertIn("обновляю план", res.calls)
        self.assertIn("update_notes", prov.seen[0][0])

    def test_extend_asks_and_continues(self):
        asked = []
        replies = [tool("list_files")] * (agent.MAX_STEPS + 3) + ["done"]
        res, _, _ = self.go(replies, extend=lambda steps, sent: (asked.append((steps, sent)), True)[1])
        self.assertEqual(res.text, "done")
        self.assertEqual(res.steps, agent.MAX_STEPS + 4)
        self.assertEqual(asked[0][0], agent.MAX_STEPS)
        self.assertGreater(asked[0][1], 0)

    def test_extend_declined_stops(self):
        res, _, _ = self.go([tool("list_files")] * (agent.MAX_STEPS + 3), extend=lambda s, n: False)
        self.assertEqual(res.steps, agent.MAX_STEPS)
        self.assertIn("лимит шагов", res.stopped)

    def test_sent_is_counted(self):
        res, prov, _ = self.go([tool("list_files"), "done"])
        self.assertEqual(res.sent, sum(len(sys) + sum(len(m["content"]) for m in ms) for sys, ms in prov.seen))


if __name__ == "__main__":
    unittest.main()
