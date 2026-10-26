"""Демонстрация изоляции: может ли ИИ из своего проекта добраться до чужого.

Запуск:   python demo/escape_demo.py      (из папки проекта, с установленным PySide6 или PyQt6)

Создаёт на диске два соседних проекта, как два сайта на одном дешёвом сервере:
    <tmp>/mine/     - проект, в котором работает ИИ
    <tmp>/victim/   - чужой проект с секретом (CANARY_SECRET_...)
Потом пробует добраться до секрета всеми способами, какие есть у ИИ: его инструменты (чтение, поиск, создание,
удаление, переименование) и консоль в песочнице. Секрет не должен появиться ни в одном ответе, а чужие файлы не
должны измениться. Код выхода 0 - ничего не вышло наружу, 1 - есть утечка или изменение.
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiedit import sandbox  # noqa: E402
from aiedit.workspace import Workspace  # noqa: E402

CANARY = "CANARY_SECRET_9f3b2c7a"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<нет файла>"


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="aiedit-demo-")).resolve()
    mine, victim = base / "mine", base / "victim"
    mine.mkdir()
    victim.mkdir()
    (mine / "main.py").write_text("print('мой проект')\n")
    secret = victim / "secret.txt"
    secret.write_text(f"ключ чужого сайта: {CANARY}\n")
    (victim / "db.sqlite").write_text(f"пароль базы: {CANARY}\n")
    (mine / "link_to_secret").symlink_to(secret)           # ссылки из моего проекта на чужое
    (mine / "link_to_victim").symlink_to(victim)
    (mine / "link_to_home").symlink_to(Path.home())
    os.environ["AIEDIT_DEMO_API_KEY"] = f"sk-{CANARY}"      # секрет в окружении самой программы
    before = {p: digest(p) for p in (secret, victim / "db.sqlite")}
    victims = sorted(p.name for p in victim.iterdir())

    ws = Workspace(mine, model="demo", log_enabled=False)
    results: list[tuple[str, bool, str]] = []

    def attempt(name: str, output: str):
        leaked = CANARY in output or "TOKEN" in output
        results.append((name, not leaked, output.strip().split("\n")[0][:70]))

    print(f"Два соседних проекта:\n  мой    {mine}\n  чужой  {victim}  (в нём секрет {CANARY})\n")

    # --- инструменты ИИ ---
    for label, path in (("../victim/secret.txt", "../victim/secret.txt"),
                        ("обход через a/../..", "a/../../victim/secret.txt"),
                        ("абсолютный путь", str(secret)),
                        ("~ (домашняя папка)", "~/.bashrc"),
                        ("ссылка на файл", "link_to_secret"),
                        ("файл через ссылку на папку", "link_to_victim/secret.txt"),
                        ("пустой байт в пути", "main.py\x00../victim/secret.txt"),
                        ("/etc/shadow", "/etc/shadow")):
        attempt(f"read_file: {label}", ws.read_file(path))
    attempt("list_files('../victim')", ws.list_files("../victim"))
    attempt("list_files('/')", ws.list_files("/"))
    attempt("list_files('.') не показывает чужое", ws.list_files(".") + "\n" + "\n".join(
        n for n in victims if n in ws.list_files(".")))
    attempt("search по секрету", ws.search("CANARY"))
    for label, call in (("create_file вне проекта", lambda: ws.create_file("../victim/new.txt", "x")),
                        ("create_file абсолютный", lambda: ws.create_file(str(base / "pwn.txt"), "x")),
                        ("delete_file чужого", lambda: ws.delete_file("../victim/secret.txt")),
                        ("rename_file наружу", lambda: ws.rename_file("main.py", "../victim/main.py")),
                        ("edit_file чужого", lambda: ws.edit_file("../victim/secret.txt", "ключ", "хак"))):
        out = call()
        results.append((f"{label}: отказ", out.startswith("ERROR"), out[:70]))

    # --- консоль ИИ ---
    if sandbox.available():
        print("песочница недоступна:", sandbox.available(), "- консоль не проверяю\n")
    else:
        term = sandbox.Terminal(ws, lambda command: "run")
        for label, command in (
                ("прочитать чужой секрет по пути", f"cat {secret} 2>&1; ls {victim} 2>&1"),
                ("поиск секрета по всей системе", f"grep -r {CANARY} / 2>/dev/null | head -3; find / -name 'secret.txt' 2>/dev/null"),
                ("через ссылки проекта", "cat link_to_secret link_to_victim/secret.txt link_to_home/.bashrc 2>&1"),
                ("домашние папки и /root", "ls /home /root /home/* 2>&1"),
                ("чужие процессы и их окружение", "ps aux 2>&1 | head -5; cat /proc/*/environ 2>/dev/null | tr '\\0' '\\n' | grep -c API_KEY"),
                ("ключ из окружения программы", "env; cat /proc/1/environ 2>&1 | tr '\\0' '\\n'"),
                ("выход через ../", "cat ../victim/secret.txt ../../victim/secret.txt 2>&1"),
                ("сеть: отправить наружу", "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),3)\" 2>&1")):
            attempt(f"консоль: {label}", term(command))
        write_out = term(f"echo hack > {victim}/hacked.txt 2>&1; echo hack > /usr/hacked 2>&1; cd / ; touch /outside 2>&1")
        results.append(("консоль: запись вне проекта", not (victim / "hacked.txt").exists() and not os.path.exists("/usr/hacked"),
                        write_out.strip().split("\n")[-1][:70]))
        term.close()

    # --- итог ---
    after = {p: digest(p) for p in before}
    results.append(("чужие файлы не изменены", after == before, ""))
    results.append(("чужая папка без новых файлов", sorted(p.name for p in victim.iterdir()) == victims, ""))
    results.append(("рядом с проектами ничего не появилось", sorted(p.name for p in base.iterdir()) == ["mine", "victim"], ""))
    results.append(("мой проект не повреждён", (mine / "main.py").read_text() == "print('мой проект')\n", ""))

    width = max(len(name) for name, _, _ in results)
    for name, ok, shown in results:
        print(f"  {'ЗАЩИТА СРАБОТАЛА' if ok else '!!! УТЕЧКА !!!   '}  {name.ljust(width)}  {shown}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\nПопыток: {len(results)}, прошли защиту: {len(failed)}.", "Ни одной утечки." if not failed else f"Утечки: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
