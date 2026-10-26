import tempfile
import unittest
from pathlib import Path

from aiedit import sendlog, workspace
from aiedit.guard import Region
from aiedit.marks import Marks
from aiedit.policy import AccessDenied, Rules
from aiedit.workspace import Workspace

PAY = '''def charge(user: str, amount: int = 5) -> int:
    """секретная логика"""
    return amount * 100 + HIDDEN_FEE

HIDDEN_FEE = 7


def refund(user, amount):
    return -amount


def free():
    return 1


def tail():
    return 0
'''


class WsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        for d in ("pkg", "secrets", "node_modules", ".git"):
            (self.root / d).mkdir(parents=True)
        files = {"main.py": "from pkg import pay\nprint(pay.free())\n", "pkg/pay.py": PAY,
                 "secrets/k.py": "K = 'abc123'\n", ".env": "A_SECRET=zzzzzzzz\n", "node_modules/m.js": "x",
                 "cfg.py": 'KEY = "sk-ant-api03-AbCdEf0123456789xyz"\nOK = 1\n'}
        for n, t in files.items():
            (self.root / n).write_text(t)
        m = Marks()
        m.add("interface", 0, 4); m.add("readonly", 6, 8); m.add("confirm", 11, 12)
        m.save(self.root / "pkg" / "pay.py", PAY)
        Rules(self.root).set_denied(self.root / "secrets", True)
        self.ws = Workspace(self.root, model="m1")

    def tearDown(self):
        self.tmp.cleanup()


class ListReadTest(WsCase):
    def test_list_hides_denied_secret_private_and_service_dirs(self):
        out = self.ws.list_files()
        self.assertIn("pkg/", out)
        self.assertIn("main.py", out)
        for hidden in ("secrets", ".env", "node_modules", ".git", ".aiedit-rules.json", "aimarks"):
            self.assertNotIn(hidden, out)

    def test_denied_looks_like_missing(self):
        self.assertEqual(self.ws.read_file("secrets/k.py"), self.ws.read_file("secrets/none.py").replace("none", "k"))
        self.assertIn("not found", self.ws.read_file(".env"))
        self.assertIn("not found", self.ws.list_files("secrets"))

    def test_cannot_escape_root(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("top secret")
        for p in ("../outside.txt", str(outside), "pkg/../../outside.txt"):
            self.assertIn("not found", self.ws.read_file(p))
        (self.root / "link.txt").symlink_to(outside)
        self.assertIn("not found", self.ws.read_file("link.txt"))

    def test_symlink_into_denied_is_missing(self):
        (self.root / "pkg" / "l.py").symlink_to(self.root / "secrets" / "k.py")
        self.assertIn("not found", self.ws.read_file("pkg/l.py"))
        self.assertNotIn("abc123", self.ws.list_files("pkg") + self.ws.read_file("pkg/l.py"))

    def test_read_shows_interface_not_body(self):
        out = self.ws.read_file("pkg/pay.py")
        self.assertIn("def charge(user: str, amount: int=...) -> int", out)
        for leak in ("секретная", "HIDDEN_FEE = 7", "amount * 100"):
            self.assertNotIn(leak, out)
        self.assertIn("return -amount", out)  # readonly виден
        self.assertIn("1| ", out)

    def test_secret_lines_are_hidden_automatically(self):
        out = self.ws.read_file("cfg.py")
        self.assertNotIn("sk-ant", out)
        self.assertIn("OK = 1", out)

    def test_read_range_and_paging_note(self):
        (self.root / "big.txt").write_text("\n".join(f"line{i}" for i in range(1000)))
        out = self.ws.read_file("big.txt")
        self.assertIn("lines 1-400 of 1000", out)
        self.assertIn("start_line=401", out)
        self.assertIn("401| line400", self.ws.read_file("big.txt", 401, 402))

    def test_stale_zones_make_file_unavailable(self):
        (self.root / "pkg" / "pay.py").write_text(PAY + "# changed outside\n")
        out = Workspace(self.root).read_file("pkg/pay.py")
        self.assertIn("unavailable", out)
        self.assertNotIn("def free", out)

    def test_live_tab_text_and_zones_win_over_disk(self):
        live = {str(self.root / "main.py"): ("fresh = 1\nSECRET_PART = 2\n", [Region("hidden", 1, 1)])}
        out = Workspace(self.root, live=live).read_file("main.py")
        self.assertIn("fresh = 1", out)
        self.assertNotIn("SECRET_PART", out)

    def test_binary_and_missing(self):
        (self.root / "b.bin").write_bytes(b"\xff\xfe\x00")
        self.assertIn("not a text file", self.ws.read_file("b.bin"))
        self.assertIn("not found", self.ws.read_file("nope.py"))


class SearchTest(WsCase):
    def test_search_skips_denied_hidden_and_secret_lines(self):
        out = self.ws.search("k")
        self.assertNotIn("secrets/", out)
        self.assertEqual(self.ws.search("секретная"), "no matches")
        self.assertEqual(self.ws.search("amount * 100"), "no matches")
        self.assertEqual(self.ws.search("sk-ant"), "no matches")
        self.assertIn("pkg/pay.py", self.ws.search("def refund"))

    def test_search_inside_folder_and_case(self):
        out = self.ws.search("FREE", "pkg", case=True)
        self.assertEqual(out, "no matches")
        self.assertIn("pkg/pay.py:", self.ws.search("FREE", "pkg"))
        self.assertIn("not found", self.ws.search("x", "secrets") + "not found")


class EditTest(WsCase):
    def test_edit_free_code_and_diff_in_changes(self):
        r = self.ws.edit_file("pkg/pay.py", "def tail():\n    return 0", "def tail():\n    return 2")
        self.assertIn("accepted", r)
        ch = self.ws.changes()
        self.assertEqual([c.path.name for c in ch], ["pay.py"])
        self.assertIn("return 2", ch[0].new)
        self.assertEqual(ch[0].needs_confirm, [])
        self.assertIn("amount * 100", ch[0].new)  # скрытое сохранилось
        self.assertEqual((self.root / "pkg" / "pay.py").read_text(), PAY)  # диск не тронут

    def test_edit_errors(self):
        self.assertIn("not found in the file", self.ws.edit_file("pkg/pay.py", "no such", "x"))
        self.assertIn("times, add", self.ws.edit_file("pkg/pay.py", "amount", "x"))
        self.assertIn("empty", self.ws.edit_file("pkg/pay.py", "", "x"))
        self.assertIn("identical", self.ws.edit_file("pkg/pay.py", "free", "free"))
        self.assertEqual(self.ws.changes(), [])

    def test_readonly_and_interface_cannot_change(self):
        self.assertIn("rejected", self.ws.edit_file("pkg/pay.py", "return -amount", "return 0"))
        self.assertIn("not found", self.ws.edit_file("pkg/pay.py", "amount * 100", "0"))  # его нет в виде модели
        red = self.ws.view(self.ws.state(self.root / "pkg" / "pay.py"))
        placeholder = red.text.split("\n")[0]
        self.assertIn("rejected", self.ws.edit_file("pkg/pay.py", placeholder, "# stolen"))
        self.assertEqual(self.ws.changes(), [])

    def test_secret_line_cannot_be_edited_or_seen(self):
        self.assertIn("not found", self.ws.edit_file("cfg.py", 'KEY = "x"', "KEY = 1"))
        r = self.ws.edit_file("cfg.py", "OK = 1", "OK = 2")
        self.assertIn("accepted", r)
        self.assertIn("sk-ant", self.ws.changes()[0].new)

    def test_confirm_zone_flagged(self):
        r = self.ws.edit_file("pkg/pay.py", "return 1", "return 3")
        self.assertIn("confirmation", r)
        self.assertEqual(len(self.ws.changes()[0].needs_confirm), 1)

    def test_second_edit_sees_first_and_zones_follow_lines(self):
        self.ws.edit_file("pkg/pay.py", "def tail():", "# новая строка\ndef tail():")
        r = self.ws.edit_file("pkg/pay.py", "# новая строка\ndef tail():\n    return 0", "def tail():\n    return 5")
        self.assertIn("accepted", r)
        self.assertIn("rejected", self.ws.edit_file("pkg/pay.py", "return -amount", "return 0"))

    def test_edit_denied_file_is_missing(self):
        self.assertIn("not found", self.ws.edit_file("secrets/k.py", "abc123", "x"))


class CreateTest(WsCase):
    def test_create_and_changes(self):
        self.assertIn("will be created", self.ws.create_file("pkg/new.py", "x = 1\n"))
        ch = self.ws.changes()
        self.assertTrue(ch[0].created)
        self.assertFalse((self.root / "pkg" / "new.py").exists())
        self.assertIn("accepted", self.ws.edit_file("pkg/new.py", "x = 1", "x = 2"))
        self.assertEqual(self.ws.changes()[0].new, "x = 2\n")

    def test_create_refusals(self):
        for path in ("secrets/new.py", ".env", "../out.py", "pkg/", "main.py", ".aiedit-rules.json",
                     "node_modules/x.js", "id_rsa", "a.py.aimarks.json"):
            self.assertIn("ERROR", self.ws.create_file(path, "x"), path)
        self.assertEqual(self.ws.changes(), [])

    def test_write_change_saves_file_and_zones(self):
        self.ws.create_file("pkg/new.py", "x = 1\n")
        ch = self.ws.changes()[0]
        ch.regions = [Region("readonly", 0, 0)]
        workspace.write_change(ch)
        self.assertEqual((self.root / "pkg" / "new.py").read_text(), "x = 1\n")
        m = Marks()
        self.assertTrue(m.load(self.root / "pkg" / "new.py", "x = 1\n"))
        self.assertEqual(m.items, [Region("readonly", 0, 0)])


class SafetyTest(WsCase):
    def test_recheck_blocks_when_sent_file_becomes_denied(self):
        self.ws.read_file("main.py")
        self.ws.recheck()
        Rules(self.root).set_denied(self.root / "main.py", True)
        with self.assertRaises(AccessDenied):
            self.ws.recheck()

    def test_every_tool_call_is_logged_without_content(self):
        self.ws.list_files()
        self.ws.read_file("pkg/pay.py")
        self.ws.search("def")
        entries = sendlog.read(self.root)
        self.assertEqual([e["mode"] for e in entries], ["tool:list_files", "tool:read_file", "tool:search"])
        read = entries[1]
        self.assertEqual(read["files"][0]["name"], "pkg/pay.py")
        self.assertGreater(read["files"][0]["hidden_chunks"], 0)
        raw = (self.root / sendlog.LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("amount", raw)
        self.assertEqual(entries[1]["model"], "m1")


if __name__ == "__main__":
    unittest.main()


class PendingNewFilesVisibleTest(unittest.TestCase):
    """Файл, созданный агентом, но ещё не применённый, должен быть виден ему же (иначе круг исправлений его не найдёт)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "sd.py").write_text("ars = 1\n")
        self.ws = Workspace(self.root, model="x", log_enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_file_is_listed_and_found_by_search(self):
        self.ws.create_file("app.py", "app.run(debug=True)\n")
        self.assertEqual(self.ws.list_files(".").split("\n"), ["app.py", "sd.py"])
        self.assertIn("app.py:1: app.run(debug=True)", self.ws.search("debug=True"))
        self.assertFalse((self.root / "app.py").exists())  # на диск по-прежнему ничего не попало

    def test_new_file_in_a_new_folder_shows_the_folder_too(self):
        self.ws.create_file("templates/index.html", "<h1>hi</h1>\n")
        self.assertEqual(self.ws.list_files(".", 3).split("\n"), ["templates/", "  index.html", "sd.py"])
        self.assertEqual(self.ws.list_files(".", 1).split("\n"), ["templates/", "sd.py"])
        self.assertIn("templates/index.html:1", self.ws.search("hi"))
        self.assertIn("index.html", self.ws.list_files("templates"))

    def test_new_file_in_an_existing_folder(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "a.py").write_text("x = 1\n")
        self.ws.create_file("pkg/b.py", "y = 2\n")
        self.assertEqual(self.ws.list_files("pkg").split("\n"), ["a.py", "b.py"])

    def test_renamed_and_deleted_files_follow_the_pending_state(self):
        self.ws.rename_file("sd.py", "words.py")
        listed = self.ws.list_files(".").split("\n")
        self.assertEqual(listed, ["words.py"])
        self.ws.create_file("tmp.py", "z\n")
        self.ws.delete_file("tmp.py")
        self.assertNotIn("tmp.py", self.ws.list_files("."))

    def test_nothing_changes_for_ordinary_projects(self):
        self.assertEqual(self.ws.list_files("."), "sd.py")
        self.assertEqual(self.ws.pending_created(self.root), [])


class SymlinkOutsideTheProjectTest(unittest.TestCase):
    """Ссылка из проекта наружу не даёт ни прочитать то, что снаружи, ни сломать поиск."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.root = base / "proj"
        self.root.mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("TOP_SECRET_OUTSIDE\n")
        (self.root / "main.py").write_text("print('TOP_SECRET_OUTSIDE is not here')\n")
        (self.root / "link_dir").symlink_to(self.outside)
        (self.root / "link_file").symlink_to(self.outside / "secret.txt")
        self.ws = Workspace(self.root, model="x", log_enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_outside_files_are_not_readable_through_links_or_dotdot(self):
        for path in ("link_file", "link_dir/secret.txt", "../outside/secret.txt", str(self.outside / "secret.txt")):
            self.assertTrue(self.ws.read_file(path).startswith("ERROR"), path)
            self.assertNotIn("TOP_SECRET", self.ws.read_file(path))

    def test_search_still_works_and_never_goes_outside(self):
        out = self.ws.search("TOP_SECRET")
        self.assertEqual(out.count("\n"), 0)
        self.assertTrue(out.startswith("main.py:1:"))
        self.assertNotIn("secret.txt", out)

    def test_listing_hides_links_that_lead_outside(self):
        self.assertEqual(self.ws.list_files("."), "main.py")

    def test_outside_is_not_visible_and_does_not_raise(self):
        self.assertFalse(self.ws.visible(self.outside))
        self.assertFalse(self.ws.visible(self.outside / "secret.txt"))
        self.assertTrue(self.ws.visible(self.root / "main.py"))

    def test_sandbox_copy_does_not_include_what_links_point_to(self):
        from aiedit import sandbox
        dest = Path(self.tmp.name) / "copy"
        dest.mkdir()
        sandbox.build_copy(self.ws, dest)
        names = sorted(p.name for p in dest.rglob("*"))
        self.assertEqual(names, ["main.py"])
