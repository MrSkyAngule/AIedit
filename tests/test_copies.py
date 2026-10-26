"""Закрытый код вне своих зон: файл переименовали или скопировали мимо редактора, копия проекта в .venv."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from aiedit import copies, outbound, sandbox
from aiedit.marks import Marks, sidecar
from aiedit.workspace import Workspace

SECRET = "CANARY_7731"
SHOP = f"""def total(x):
    return x


def price(user):
    rate = 0.85
    return '{SECRET}'


def tail():
    return 0
"""


def close(path: Path, *regions):
    m = Marks()
    for level, a, b in regions:
        m.add(level, a, b)
    m.save(path, path.read_text())


class CopiesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        self.root.mkdir()
        (self.root / "shop.py").write_text(SHOP)
        close(self.root / "shop.py", ("hidden", 4, 6))

    def tearDown(self):
        self.tmp.cleanup()

    def ws(self):
        return Workspace(self.root, log_enabled=False)


class FingerprintTest(CopiesCase):
    def test_sidecar_keeps_hashes_not_code(self):
        data = json.loads(sidecar(self.root / "shop.py").read_text())
        self.assertEqual(data["chunks"][0][0], "hidden")
        self.assertEqual(len(data["chunks"][0][1]), 3)
        self.assertNotIn(SECRET, json.dumps(data))
        self.assertNotIn("price", json.dumps(data))

    def test_short_chunks_are_not_fingerprinted(self):
        (self.root / "k.py").write_text("x = 1\nK = 42\n")
        close(self.root / "k.py", ("hidden", 1, 1))
        self.assertNotIn("chunks", json.loads(sidecar(self.root / "k.py").read_text()))

    def test_readonly_zones_are_not_fingerprinted(self):
        (self.root / "r.py").write_text(SHOP)
        close(self.root / "r.py", ("readonly", 4, 6))
        self.assertNotIn("chunks", json.loads(sidecar(self.root / "r.py").read_text()))


class RenameTest(CopiesCase):
    def test_renamed_outside_the_editor_stays_closed(self):
        os.rename(self.root / "shop.py", self.root / "store.py")
        ws = self.ws()
        out = ws.read_file("store.py")
        self.assertNotIn(SECRET, out)
        self.assertIn("<<ai-hidden:", out)
        self.assertNotIn(SECRET, ws.search("CANARY"))
        self.assertIn("def tail", out)  # остальной код виден как раньше

    def test_copy_is_closed_and_original_keeps_its_own_zone(self):
        shutil.copy(self.root / "shop.py", self.root / "copy.py")
        ws = self.ws()
        self.assertNotIn(SECRET, ws.read_file("copy.py"))
        self.assertNotIn(SECRET, ws.read_file("shop.py"))

    def test_chunk_pasted_into_another_file_with_other_indent(self):
        (self.root / "b.py").write_text("class A:\n    def price(user):\n        rate = 0.85\n"
                                        f"        return '{SECRET}'\n\nOK = 1\n")
        out = self.ws().read_file("b.py")
        self.assertNotIn(SECRET, out)
        self.assertIn("OK = 1", out)

    def test_interface_zone_is_not_turned_into_hidden_in_its_own_file(self):
        close(self.root / "shop.py", ("interface", 4, 6))
        out = self.ws().read_file("shop.py")
        self.assertIn("def price(user)", out)
        self.assertNotIn(SECRET, out)

    def test_model_cannot_edit_the_stray_code(self):
        os.rename(self.root / "shop.py", self.root / "store.py")
        ws = self.ws()
        res = ws.edit_file("store.py", "def tail():\n    return 0", "def tail():\n    return 1")
        self.assertTrue(res.startswith("edit accepted"), res)
        res = ws.edit_file("store.py", "<<ai-hidden:0:3 lines>>", "def price(user):\n    return 0")
        self.assertIn("rejected", res)

    def test_allowed_by_the_human_is_visible(self):
        os.rename(self.root / "shop.py", self.root / "store.py")
        found = copies.find(SHOP, [], copies.build(self.root))
        self.assertEqual([(m.start, m.end, m.level) for m in found], [(4, 6, "hidden")])
        m = Marks()
        m.allowed = [found[0].id]
        m.save(self.root / "store.py", SHOP)
        self.assertIn(SECRET, self.ws().read_file("store.py"))

    def test_live_tab_without_zones_is_closed_too(self):
        os.rename(self.root / "shop.py", self.root / "store.py")
        ws = Workspace(self.root, {self.root / "store.py": (SHOP, [])}, log_enabled=False)
        self.assertNotIn(SECRET, ws.read_file("store.py"))
        ws = Workspace(self.root, {self.root / "store.py": (SHOP, [], [copies.find(SHOP, [], ws.copies)[0].id])},
                       log_enabled=False)
        self.assertIn(SECRET, ws.read_file("store.py"))

    def test_sandbox_copy_has_no_stray_code(self):
        os.rename(self.root / "shop.py", self.root / "store.py")
        with tempfile.TemporaryDirectory() as d:
            sandbox.build_copy(self.ws(), Path(d))
            text = (Path(d) / "store.py").read_text()
        self.assertNotIn(SECRET, text)
        self.assertIn("def tail", text)

    def test_old_sidecar_without_fingerprints(self):
        side = sidecar(self.root / "shop.py")
        data = json.loads(side.read_text())
        del data["chunks"]
        side.write_text(json.dumps(data))
        m = Marks()
        self.assertTrue(m.load(self.root / "shop.py", SHOP))
        self.assertTrue(m.outdated)
        shutil.copy(self.root / "shop.py", self.root / "copy.py")  # оригинал на месте: отпечатки считаются по нему
        self.assertNotIn(SECRET, self.ws().read_file("copy.py"))

    def test_broken_sidecar_is_skipped(self):
        (self.root / "bad.py.aimarks.json").write_text("{not json")
        (self.root / "bad2.py.aimarks.json").write_text('{"regions": [["hidden"]]}')
        os.rename(self.root / "shop.py", self.root / "store.py")
        self.assertNotIn(SECRET, self.ws().read_file("store.py"))


class VenvShadowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "shop.py").write_text(SHOP)
        (self.root / "pkg" / "__init__.py").write_text("")
        self.site = self.root / ".venv" / "lib" / "python3.12" / "site-packages"
        (self.site / "__pycache__").mkdir(parents=True)
        (self.site / "requests").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def shadows(self):
        return sandbox.venv_shadows(self.root, self.root / ".venv")

    def test_project_modules_installed_as_copies_are_hidden(self):
        (self.site / "shop.py").write_text(SHOP)
        (self.site / "__pycache__" / "shop.cpython-312.pyc").write_bytes(b"x")
        (self.site / "pkg").mkdir()
        hidden = {p.relative_to(self.site).as_posix() for p in self.shadows()}
        self.assertEqual(hidden, {"shop.py", "__pycache__/shop.cpython-312.pyc", "pkg"})

    def test_package_installed_from_the_project_folder(self):
        (self.site / "lib_x").mkdir()
        dist = self.site / "x-1.0.dist-info"
        dist.mkdir()
        (dist / "direct_url.json").write_text(json.dumps({"url": self.root.as_uri(), "dir_info": {}}))
        (dist / "RECORD").write_text("lib_x/__init__.py,sha256=a,1\nx-1.0.dist-info/RECORD,,\n../../bin/x,,\n")
        self.assertIn(self.site / "lib_x", self.shadows())

    def test_editable_install_and_other_packages_stay(self):
        (self.site / "lib_x").mkdir()
        dist = self.site / "x-1.0.dist-info"
        dist.mkdir()
        (dist / "direct_url.json").write_text(json.dumps({"url": self.root.as_uri(), "dir_info": {"editable": True}}))
        (dist / "RECORD").write_text("lib_x/__init__.py,,\n")
        self.assertEqual(self.shadows(), [])

    def test_no_venv(self):
        self.assertEqual(sandbox.venv_shadows(self.root, None), [])

    def test_bwrap_mounts_empty_over_the_copies(self):
        (self.site / "shop.py").write_text(SHOP)
        (self.site / "pkg").mkdir()
        argv = sandbox.bwrap_argv("true", Path("/w"), self.root / ".venv", None, self.shadows())
        venv_at = argv.index(str(self.root / ".venv"))
        self.assertGreater(argv.index(str(self.site / "pkg")), venv_at)
        i = argv.index(str(self.site / "pkg"))
        self.assertEqual(argv[i - 1], "--tmpfs")
        j = argv.index(str(self.site / "shop.py"))
        self.assertEqual(argv[j - 2:j], ["--ro-bind", "/dev/null"])

    def test_seatbelt_denies_the_copies_last(self):
        (self.site / "shop.py").write_text(SHOP)
        prof = sandbox.seatbelt_profile(Path("/tmp/w"), Path("/tmp/t"), self.root / ".venv", hide=self.shadows())
        last = prof.strip().split("\n")[-1]
        self.assertTrue(last.startswith("(deny file-read*"))
        self.assertIn(str(self.site / "shop.py"), last)


@unittest.skipIf(sandbox.available(), f"песочница недоступна: {sandbox.available()}")
class RealVenvSandboxTest(unittest.TestCase):
    def test_installed_copy_is_not_readable_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "shop.py").write_text(SHOP)
            close(root / "shop.py", ("hidden", 4, 6))
            site = root / ".venv" / "lib" / "python3.12" / "site-packages"
            site.mkdir(parents=True)
            (root / ".venv" / "bin").mkdir()
            os.symlink(shutil.which("python3"), root / ".venv" / "bin" / "python")
            shutil.copy(root / "shop.py", site / "shop.py")
            shown = []
            t = sandbox.Terminal(Workspace(root, log_enabled=False), lambda c: "run", shown.append)
            try:
                t(f"cat {site}/shop.py; ls {site}")
            finally:
                t.close()
        self.assertNotIn(SECRET, shown[0].output)


class OutboundContextTest(unittest.TestCase):
    KEY = "sk-ant-api03-AbCdEf0123456789xyzQW"

    def test_finding_knows_where_it_is(self):
        text = f"почему не работает запрос с ключом {self.KEY}, подскажи"
        [f] = outbound.Gate().find(text)
        before, hit, after = f.where()
        self.assertIn("с ключом ", before)
        self.assertEqual(hit, f.preview())
        self.assertNotIn(self.KEY, before + hit + after)
        self.assertEqual(f.where(full=True)[1], self.KEY)
        self.assertIn("за твои деньги", f.explain())

    def test_context_is_one_trimmed_line(self):
        text = "первая строка\n" + "x" * 200 + " Acme " + "y" * 200 + "\nпоследняя"
        [f] = outbound.Gate(words=["acme"]).find(text)
        self.assertNotIn("\n", f.context)
        self.assertLess(len(f.context), 130)
        self.assertTrue(f.context.startswith("…") and f.context.endswith("…"))
        self.assertIn("слово из твоего списка", f.explain())

    def test_context_does_not_change_equality(self):
        self.assertEqual(outbound.Finding("k", "v", "a"), outbound.Finding("k", "v", "b"))


if __name__ == "__main__":
    unittest.main()
