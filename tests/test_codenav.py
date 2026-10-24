"""Навигация по коду: определения и использования без утечки закрытого кода."""
import shutil
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, codenav
from aiedit.marks import Marks
from aiedit.policy import Rules
from aiedit.workspace import Workspace
from tests.test_agent import Script

PAY = '''import json


def fee(amount):
    return amount * 0.07  # SECRET_RATE


def charge(amount):
    total = amount + fee(amount)
    return json.dumps({"total": total})


def rate_card(kind):
    return {"vip": 1}[kind]
'''
MAIN = '''from pkg.pay import charge, fee, rate_card

print(charge(10))
print(fee(5))
# charge in a comment
s = "charge in a string"
'''


class NavCase(unittest.TestCase):
    server = staticmethod(lambda lang, root: None)  # по умолчанию - без языкового сервера

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "secret").mkdir()
        (self.root / "pkg" / "pay.py").write_text(PAY)
        (self.root / "main.py").write_text(MAIN)
        (self.root / "secret" / "s.py").write_text("def charge():\n    return 'LEAK'\n")
        m = Marks()
        m.add("hidden", 3, 4)      # fee
        m.add("interface", 12, 13)  # rate_card
        m.save(self.root / "pkg" / "pay.py", PAY)
        Rules(self.root).set_denied(self.root / "secret", True)
        self.ws = Workspace(self.root)
        self.nav = codenav.Navigator(self.ws, self.server)

    def tearDown(self):
        self.nav.close()
        self.tmp.cleanup()

    def go(self, tool, path, line, symbol):
        return self.nav(tool, {"path": path, "line": line, "symbol": symbol})


class FallbackTest(NavCase):
    def test_definition_uses_model_line_numbers(self):
        out = self.go("go_to_definition", "main.py", 3, "charge")
        self.assertIn("found by name", out)
        self.assertIn("pkg/pay.py:7: def charge(amount):", out)  # на диске 8-я строка, fee свёрнута
        self.assertNotIn("secret", out)
        self.assertIn("7| def charge(amount):", self.ws.read_file("pkg/pay.py"))

    def test_hidden_definition_is_not_found_and_says_why(self):
        out = self.go("go_to_definition", "main.py", 4, "fee")
        self.assertIn("It may be closed code", out)
        self.assertNotIn("0.07", out)

    def test_interface_definition_shows_closed_marker(self):
        out = self.go("go_to_definition", "main.py", 1, "rate_card")
        self.assertIn("pkg/pay.py:12: (closed code)", out)
        self.assertNotIn("vip", out)

    def test_references_skip_comments_strings_and_closed_files(self):
        out = self.go("find_references", "main.py", 3, "charge")
        self.assertIn("main.py:1:", out)
        self.assertIn("main.py:3:", out)
        self.assertIn("pkg/pay.py:7:", out)
        self.assertNotIn("main.py:5", out)
        self.assertNotIn("main.py:6", out)
        self.assertNotIn("secret", out)

    def test_pending_edits_are_seen(self):
        self.ws.edit_file("main.py", "print(charge(10))", "print(charge(10))\nprint(charge(20))")
        self.assertIn("main.py:4: print(charge(20))", self.go("find_references", "main.py", 3, "charge"))

    def test_bad_requests(self):
        self.assertIn("is closed code", self.go("go_to_definition", "pkg/pay.py", 4, "fee"))
        self.assertIn("is not on line", self.go("go_to_definition", "main.py", 2, "charge"))
        self.assertIn("outside the file", self.go("go_to_definition", "main.py", 99, "charge"))
        self.assertIn("file not found", self.go("go_to_definition", "secret/s.py", 1, "charge"))
        self.assertIn("missing arguments", self.nav("find_references", {"path": "main.py"}))
        (self.root / "app.ts").write_text("const x = 1;\n")
        self.assertIn("no language server", self.go("go_to_definition", "app.ts", 1, "x"))

    def test_navigation_is_logged_and_copy_removed(self):
        self.go("go_to_definition", "main.py", 3, "charge")
        copy = self.nav.copy.path
        self.nav.close()
        self.assertFalse(copy.exists())


class AgentNavTest(NavCase):
    def test_agent_tool_and_reviewer_can_navigate(self):
        call = "@@tool go_to_definition\npath: main.py\nline: 3\nsymbol: charge\n@@end"
        prov = Script([call, "ok"])
        res = agent.run("t", self.ws, prov, nav=self.nav)
        self.assertIn("go_to_definition", prov.seen[0][0])
        self.assertIn("pkg/pay.py:7: def charge(amount):", prov.everything())
        self.assertIn("где определено: charge (main.py:3)", res.calls)
        ws = Workspace(self.root)
        ws.read_only = True
        prov = Script([call, "ok"])
        reviewer_nav = codenav.Navigator(ws, self.server)
        self.addCleanup(reviewer_nav.close)
        agent.run("t", ws, prov, nav=reviewer_nav)
        self.assertIn("def charge", prov.everything())  # только чтение - проверяющему можно
        prov = Script([call, "ok"])
        agent.run("t", Workspace(self.root), prov)
        self.assertIn("ERROR: code navigation is not available", prov.everything())


PYLSP = shutil.which("pylsp")


@unittest.skipUnless(PYLSP, "pylsp не установлен")
class PylspTest(NavCase):
    server = staticmethod(lambda lang, root: [PYLSP] if lang == "python" else None)

    def test_definition_and_library(self):
        out = self.go("go_to_definition", "main.py", 3, "charge")
        self.assertIn("via language server pylsp", out)
        self.assertIn("pkg/pay.py:7: def charge(amount):", out)
        self.assertIn("[library]", self.go("go_to_definition", "pkg/pay.py", 9, "json"))
        self.assertIn("It may be closed code", self.go("go_to_definition", "main.py", 4, "fee"))


if __name__ == "__main__":
    unittest.main()
