import tempfile
import unittest
from pathlib import Path

from tests.test_gui import QApplication

if QApplication:
    from aiedit.folder_dialog import FolderDialog


@unittest.skipUnless(QApplication, "нет Qt")
class FolderDialogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "a" / "inner").mkdir(parents=True)
        (self.root / "b").mkdir()
        self.dlg = FolderDialog(None, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, name):
        return next(self.dlg.listing.item(i) for i in range(self.dlg.listing.count())
                    if self.dlg.listing.item(i).text() == name)

    def test_activating_a_folder_enters_it_once(self):
        self.dlg.enter(self.item("a"))
        self.assertEqual(self.dlg.current, self.root / "a")
        self.assertIn("inner", [self.dlg.listing.item(i).text() for i in range(self.dlg.listing.count())])

    def test_item_gone_after_redraw_is_ignored(self):
        self.dlg.enter(self.item("a"))
        self.dlg.enter(None)  # так приходит второй сигнал двойного клика после перерисовки списка
        self.assertEqual(self.dlg.current, self.root / "a")

    def test_up_goes_to_parent(self):
        self.dlg.enter(self.item("a"))
        self.dlg.enter(self.item(".."))
        self.assertEqual(self.dlg.current, self.root)
