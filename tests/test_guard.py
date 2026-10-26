import unittest

from aiedit import guard, orchestrator
from aiedit.guard import Region
from aiedit.marks import Marks
from aiedit.providers import DemoProvider, Provider

SRC = """\
def a():
    return 1

SECRET = "sk-123"
def check(x):
    return x == SECRET

def pay():
    return 42

def risky():
    return 0

def free():
    return 2
"""
REGIONS = [Region("hidden", 3, 5), Region("readonly", 7, 8),
           Region("confirm", 10, 11), Region("explain", 13, 14)]


class Spy(Provider):
    name = "spy"

    def __init__(self, fn=lambda c: c):
        self.fn = fn
        self.seen = []

    def complete(self, system, messages):
        user = messages[-1]["content"]
        self.seen.append(user)
        self.messages = messages
        code = user.partition("\n=== FILE ===\n")[2]
        return "```\n" + self.fn(code) + "\n```"


def run(fn, regions=REGIONS):
    p = Spy(fn)
    return orchestrator.ask(SRC, "task", regions, {"cheap": p, "strong": p}), p


class GuardTest(unittest.TestCase):
    def test_redact_hides_secret(self):
        red = guard.redact(SRC, REGIONS)
        self.assertNotIn("sk-123", red.text)
        self.assertNotIn("check", red.text)
        self.assertIn("<<ai-hidden:0:3 lines>>", red.text)
        self.assertEqual(guard.restore(red.text, red.chunks), SRC)

    def test_secret_never_sent(self):
        _, p = run(lambda c: c)
        self.assertNotIn("sk-123", p.seen[0])

    def test_free_edit_ok(self):
        r, _ = run(lambda c: c.replace("return 2", "return 3"))
        self.assertTrue(r.verdict.ok)
        self.assertEqual(r.verdict.needs_confirm, [])
        self.assertIn("sk-123", r.new_text)

    def test_readonly_edit_rejected(self):
        r, _ = run(lambda c: c.replace("return 42", "return 0"))
        self.assertFalse(r.verdict.ok)

    def test_readonly_insertion_rejected(self):
        r, _ = run(lambda c: c.replace("def pay():\n", "def pay():\n    print('x')\n"))
        self.assertFalse(r.verdict.ok)

    def test_confirm_requires_confirmation(self):
        r, _ = run(lambda c: c.replace("return 0", "return 1"))
        self.assertTrue(r.verdict.ok)
        self.assertEqual(len(r.verdict.needs_confirm), 1)

    def test_dropped_placeholder_rejected(self):
        r, _ = run(lambda c: "\n".join(l for l in c.split("\n") if "ai-hidden" not in l))
        self.assertFalse(r.verdict.ok)

    def test_fake_instead_of_placeholder_rejected(self):
        r, _ = run(lambda c: c.replace("<<ai-hidden:0:3 lines>>", "SECRET = 'fake'"))
        self.assertFalse(r.verdict.ok)

    def test_explain_zone_is_not_protected(self):
        r, _ = run(lambda c: c.replace("return 2", "return 9"), [Region("explain", 13, 14)])
        self.assertTrue(r.verdict.ok)
        self.assertEqual(r.verdict.needs_confirm, [])

    def test_zones_listed_in_redacted_numbering(self):
        _, p = run(lambda c: c)
        # hidden (3 строки) свёрнут в одну: readonly 7-8 -> 5-6 (с 1: 6-7)
        self.assertIn("readonly: lines 6-7", p.seen[0])

    def test_routing(self):
        cheap, strong = DemoProvider("c"), DemoProvider("s")
        provs = {"cheap": cheap, "strong": strong}
        self.assertIs(orchestrator.pick([], "fix", provs)[0], cheap)
        self.assertIs(orchestrator.pick([Region("explain", 0, 1)], "fix", provs)[0], cheap)
        self.assertIs(orchestrator.pick(REGIONS, "fix", provs)[0], strong)

    def test_explain_hides_secret_and_skips_hidden_zone(self):
        p = Spy()
        provs = {"cheap": p, "strong": p}
        orchestrator.explain(SRC, [Region("hidden", 3, 5), Region("explain", 3, 5),
                                   Region("explain", 13, 14)], provs)
        self.assertNotIn("sk-123", p.seen[0])
        self.assertIn("Zone 1: lines 12-13", p.seen[0])
        self.assertNotIn("Zone 2", p.seen[0])

    def test_explain_without_zones_makes_no_call(self):
        p = Spy()
        res = orchestrator.explain(SRC, [Region("readonly", 0, 1)], {"cheap": p, "strong": p})
        self.assertEqual(p.seen, [])
        self.assertEqual(res.zones, 0)


class MarksTest(unittest.TestCase):
    def test_insert_above_shifts(self):
        m = Marks(); m.add("readonly", 5, 7)
        m.on_change(2, 3)
        self.assertEqual(m.items, [Region("readonly", 8, 10)])

    def test_insert_inside_grows(self):
        m = Marks(); m.add("readonly", 5, 7)
        m.on_change(6, 2)
        self.assertEqual(m.items, [Region("readonly", 5, 9)])

    def test_insert_below_ignored(self):
        m = Marks(); m.add("readonly", 5, 7)
        m.on_change(7 + 1, 2)
        self.assertEqual(m.items, [Region("readonly", 5, 7)])

    def test_delete_above_shifts_back(self):
        m = Marks(); m.add("hidden", 5, 7)
        m.on_change(1, -2)  # убраны строки 2-3
        self.assertEqual(m.items, [Region("hidden", 3, 5)])

    def test_delete_whole_zone_drops_it(self):
        m = Marks(); m.add("confirm", 5, 6)
        m.on_change(4, -3)  # убраны строки 5-7
        self.assertEqual(m.items, [])

    def test_delete_overlap_start_clamps(self):
        m = Marks(); m.add("confirm", 5, 8)
        m.on_change(4, -2)  # убраны 5-6
        self.assertEqual(m.items, [Region("confirm", 4, 6)])

    def test_new_mark_replaces_old(self):
        m = Marks()
        for lv in ("hidden", "confirm", "readonly"):
            m.add(lv, 2, 4)
        self.assertEqual(m.items, [Region("readonly", 2, 4)])

    def test_clear_removes_all_at_once(self):
        m = Marks(); m.add("hidden", 2, 4); m.add("explain", 6, 7)
        self.assertTrue(m.clear(2, 4))
        self.assertEqual(m.items, [Region("explain", 6, 7)])
        self.assertFalse(m.clear(20, 21))

    def test_mark_inside_zone_splits_it(self):
        m = Marks(); m.add("readonly", 0, 10); m.add("confirm", 4, 5)
        self.assertEqual(sorted((r.start, r.end, r.level) for r in m.items),
                         [(0, 3, "readonly"), (4, 5, "confirm"), (6, 10, "readonly")])

    def test_clear_part_of_zone_trims_it(self):
        m = Marks(); m.add("readonly", 0, 10); m.clear(0, 2)
        self.assertEqual(m.items, [Region("readonly", 3, 10)])

    def test_save_load_roundtrip_and_hash(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.py"; f.write_text("a\nb\n")
            m = Marks(); m.add("hidden", 0, 0); m.save(f, "a\nb\n")
            m2 = Marks()
            self.assertTrue(m2.load(f, "a\nb\n"))
            self.assertEqual(m2.items, m.items)
            self.assertFalse(Marks().load(f, "changed\n"))

    def test_remap_after_edit(self):
        old = "a\nb\nc\nd"
        new = "x\na\nb\nc\nd"
        out = guard.remap([Region("readonly", 1, 2)], old, new)
        self.assertEqual(out, [Region("readonly", 2, 3)])


if __name__ == "__main__":
    unittest.main()


class InterfaceCommentsTest(unittest.TestCase):
    CODE = "ars = ['sds', 'asdasd', 'sdgf']\n#эта переменная со списком слов\n\narss = 12 # эта переменная с числами\n"

    def view(self, code, level="interface", end=None):
        n = len(code.split("\n"))
        return guard.redact(code, [guard.Region(level, 0, (end if end is not None else n - 2))]).text.split("\n")[0]

    def test_comments_stay_next_to_the_names_they_describe_values_stay_hidden(self):
        line = self.view(self.CODE, end=3)
        self.assertEqual(line, "<<ai-hidden:0:4 lines | ars = ... ; # эта переменная со списком слов ; "
                               "arss = ... ; # эта переменная с числами>>")
        self.assertNotIn("sdgf", line)
        self.assertNotIn("12", line)

    def test_comments_inside_function_bodies_are_shown_but_not_the_code(self):
        code = "def fee(x):\n    # комиссия в копейках\n    return x * 0.07  # ставка семь процентов\n"
        line = self.view(code, end=2)
        self.assertIn("def fee(x): ; # комиссия в копейках ; # ставка семь процентов", line.replace("\n", " "))
        self.assertNotIn("0.07", line)
        self.assertNotIn("return", line)

    def test_hash_inside_a_string_is_not_a_comment_and_docstring_is_not_shown(self):
        code = 'url = "http://x/#anchor"\n\ndef f():\n    """внутреннее описание"""\n    return 1\n'
        line = self.view(code, end=4)
        self.assertNotIn("anchor", line)
        self.assertNotIn("внутреннее описание", line)

    def test_cut_off_selection_still_shows_its_comments(self):
        code = "x = foo(\n    1,  # первый аргумент\n"  # скобка не закрыта
        line = guard.redact(code, [guard.Region("interface", 0, 1)]).text.split("\n")[0]
        self.assertIn("# первый аргумент", line)

    def test_hidden_zone_shows_no_comments_at_all(self):
        self.assertEqual(self.view(self.CODE, "hidden", end=3), "<<ai-hidden:0:4 lines>>")

    def test_a_comment_cannot_break_the_placeholder_or_restore(self):
        code = "a = 1  # конец >> и <<ai-hidden:7:1 lines>> подделка\nb = 2\n"
        red = guard.redact(code, [guard.Region("interface", 0, 0), guard.Region("hidden", 1, 1)])
        self.assertEqual(guard.restore(red.text, red.chunks), code)  # заглушки не перепутались
        self.assertEqual(len(red.chunks), 2)

    def test_long_comments_are_cut(self):
        code = "a = 1  # " + "слово " * 400 + "\n"
        self.assertLessEqual(len(self.view(code, end=0)), 1300)

    def test_agent_read_file_shows_the_comments(self):
        import tempfile
        from pathlib import Path
        from aiedit.marks import Marks
        from aiedit.workspace import Workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "sd.py").write_text(self.CODE)
            m = Marks()
            m.add("interface", 0, 3)
            m.save(root / "sd.py", self.CODE)
            out = Workspace(root, model="x").read_file("sd.py")
            self.assertIn("# эта переменная со списком слов", out)
            self.assertIn("# эта переменная с числами", out)
            self.assertNotIn("sdgf", out)
