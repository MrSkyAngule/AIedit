"""Окно «Подключения ИИ»: четыре строки (модель + ключ), роли дирижёра и переводчика, проверка ключей."""
from . import catalog, connections
from .connections import AUTO, OFF, MAX_ROWS, Connection, Roles
from .qt import (QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                 QPushButton, QThread, QVBoxLayout, Signal)

NONE_LABEL = "- не выбрано -"


def default_ping(conn: Connection) -> tuple[bool, str]:
    try:
        return connections.ping(catalog.make_provider(conn.model, conn.key.strip(), conn.extra))
    except ValueError as e:
        return False, str(e)


class PingWorker(QThread):
    result = Signal(int, bool, str)

    def __init__(self, items: list[tuple[int, Connection]], ping_fn):
        super().__init__()
        self.items, self.ping_fn = items, ping_fn

    def run(self):
        for row, conn in self.items:
            ok, text = self.ping_fn(conn)
            self.result.emit(row, ok, text)


class ConnectionsDialog(QDialog):
    def __init__(self, parent=None, conns: list[Connection] | None = None, roles: Roles | None = None,
                 ping_fn=default_ping):
        super().__init__(parent)
        self.setWindowTitle("Подключения ИИ")
        self.resize(820, 470)
        self.ping_fn = ping_fn
        self.worker: PingWorker | None = None
        conns, roles = conns or [], roles or Roles()

        info = QLabel("Выбери модель и вставь её API-ключ. Хватит одной строки: например, только OpenAI. "
                      f"Ключи хранятся в {connections.file_path()} (доступ только владельцу) и в проект не попадают.")
        info.setWordWrap(True)
        grid = QGridLayout()
        self.combos: list[QComboBox] = []
        self.keys: list[QLineEdit] = []
        self.extras: list[QLineEdit] = []
        self.status: list[QLabel] = []
        for i in range(MAX_ROWS):
            combo = QComboBox()
            combo.addItem(NONE_LABEL, "")
            for m in catalog.CATALOG:
                combo.addItem(m.label, m.key)
            combo.addItem(catalog.CUSTOM_LABEL, catalog.CUSTOM)
            key = QLineEdit()
            key.setPlaceholderText("API-ключ")
            key.setEchoMode(QLineEdit.EchoMode.Password)
            extra = QLineEdit()
            extra.setPlaceholderText(catalog.EXTRA_HINT)
            extra.hide()
            status = QLabel("")
            combo.currentIndexChanged.connect(lambda _=0, n=i: self.on_row_changed(n))
            key.textChanged.connect(lambda _="", n=i: self.refresh_roles())
            extra.textChanged.connect(lambda _="": self.refresh_roles())
            for col, w in enumerate((QLabel(f"{i + 1}."), combo, key, extra, status)):
                grid.addWidget(w, i, col)
            self.combos.append(combo)
            self.keys.append(key)
            self.extras.append(extra)
            self.status.append(status)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)

        self.show_keys = QCheckBox("Показывать ключи")
        self.show_keys.toggled.connect(self.on_show_keys)
        self.conductor = QComboBox()
        self.translator = QComboBox()
        roles_row = QHBoxLayout()
        for text, box in (("Дирижёр (план задач):", self.conductor), ("Переводчик:", self.translator)):
            roles_row.addWidget(QLabel(text))
            roles_row.addWidget(box, 1)

        check, save, cancel = QPushButton("Проверить ключи"), QPushButton("Сохранить"), QPushButton("Отмена")
        save.setDefault(True)
        check.clicked.connect(lambda _=False: self.check_all())
        save.clicked.connect(lambda _=False: self.try_accept())
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.show_keys)
        buttons.addStretch()
        for b in (check, cancel, save):
            buttons.addWidget(b)
        lay = QVBoxLayout(self)
        for part in (info,):
            lay.addWidget(part)
        lay.addLayout(grid)
        lay.addLayout(roles_row)
        lay.addLayout(buttons)

        for i, c in enumerate(conns[:MAX_ROWS]):  # подставляем сохранённое
            idx = self.combos[i].findData(c.model)
            if idx >= 0:
                self.combos[i].setCurrentIndex(idx)
                self.keys[i].setText(c.key)
                if c.extra:  # пусто - остаётся подставленный по умолчанию адрес
                    self.extras[i].setText(c.extra)
        self.refresh_roles(roles)
        self.result_rows: list[Connection] = []
        self.result_roles = Roles()

    # --- строки ---
    def on_show_keys(self, show: bool):
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        for k in self.keys:
            k.setEchoMode(mode)

    def on_row_changed(self, n: int):
        spec = catalog.by_key(self.combos[n].currentData() or "")
        needs_extra = self.combos[n].currentData() == catalog.CUSTOM or (spec is not None and spec.service == "compatible")
        gateway = spec is not None and spec.service in ("anthropic", "openai")  # адрес шлюза - по желанию
        self.extras[n].setVisible(needs_extra or gateway)
        self.extras[n].setPlaceholderText(catalog.GATEWAY_HINT if gateway else catalog.EXTRA_HINT)
        if spec is not None and spec.default_extra:  # адрес по умолчанию подставляем, человек может поправить
            self.extras[n].setText(spec.default_extra)
        else:
            self.extras[n].setText("")
        self.status[n].setText("")
        self.refresh_roles()

    def rows(self) -> list[Connection]:
        return [Connection(self.combos[i].currentData(), self.keys[i].text(), self.extras[i].text())
                for i in range(MAX_ROWS) if self.combos[i].currentData()]

    def labels(self) -> list[tuple[str, str]]:
        """(имя исполнителя, подпись) для выбранных строк."""
        out = []
        for c in self.rows():
            try:
                out.append((catalog.worker_key(c.model, c.extra), catalog.spec_for(c.model, c.extra).label))
            except ValueError:
                continue  # своя модель с неполным описанием
        return out

    # --- роли ---
    def refresh_roles(self, roles: Roles | None = None):
        """Списки ролей строятся из выбранных строк. Выбор пользователя при пересборке сохраняется."""
        keep = roles or Roles(self.conductor.currentData() or AUTO, self.translator.currentData() or AUTO)
        items = self.labels()
        for box, auto_text, extra, current in (
                (self.conductor, "Авто (средняя по цене)", [], keep.conductor),
                (self.translator, "Авто (GPT-4o mini или GPT-4o, иначе выключен)", [("Выключен", OFF)], keep.translator)):
            box.blockSignals(True)
            box.clear()
            box.addItem(auto_text, AUTO)
            for label, key in extra:
                box.addItem(label, key)
            for key, label in items:
                box.addItem(label, key)
            idx = box.findData(current)
            box.setCurrentIndex(idx if idx >= 0 else 0)
            box.blockSignals(False)

    def roles(self) -> Roles:
        return Roles(self.conductor.currentData() or AUTO, self.translator.currentData() or AUTO)

    # --- проверка ---
    def check_all(self):
        items = [(i, Connection(self.combos[i].currentData(), self.keys[i].text(), self.extras[i].text()))
                 for i in range(MAX_ROWS) if self.combos[i].currentData()]
        for i, _ in items:
            self.status[i].setText("проверяю...")
        self.worker = PingWorker(items, self.ping_fn)
        self.worker.result.connect(self.on_ping)
        self.worker.start()

    def on_ping(self, row: int, ok: bool, text: str):
        self.status[row].setText(("OK: " if ok else "Ошибка: ") + text)
        self.status[row].setStyleSheet("color:#2a7a2a;" if ok else "color:#b42318;")

    # --- сохранение ---
    def confirm_problems(self, problems: list[str]) -> bool:
        answer = QMessageBox.question(self, "Не всё подключится",
                                      "Эти строки будут пропущены:\n\n" + "\n".join(problems) + "\n\nСохранить остальное?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def try_accept(self):
        rows, roles = self.rows(), self.roles()
        problems = connections.build(rows, roles).problems if rows else []
        if problems and not self.confirm_problems(problems):
            return
        self.result_rows, self.result_roles = rows, roles
        self.accept()
