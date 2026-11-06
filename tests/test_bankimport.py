"""Import Banks Data: the file checks that run before anything is dropped."""

from __future__ import annotations

from pathlib import Path

import pytest

from varsha.bankimport import Stop, check_sql, file_name_ok

OWN = b"""-- Backup of bankdb
-- Generated at 2026-10-23_10-17-30

-- Structure for `branchmaster`
CREATE TABLE `branchmaster` (
  `branameng` varchar(250) NOT NULL,
  `idno` int(11) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`idno`)
) ENGINE=InnoDB DEFAULT CHARSET=latin1;

INSERT INTO `branchmaster` VALUES ('PACS SAVALAGI', '1');
INSERT INTO `branchmaster` VALUES ('it\\'s ok; really', '2');
INSERT INTO `branchmaster` VALUES ('two
lines', '3');
"""

DUMP = b"""-- MySQL dump 10.13  Distrib 5.6.12, for Win32 (x86)
/*!40101 SET NAMES utf8 */;
DROP TABLE IF EXISTS `items`;
CREATE TABLE `items` (`id` int(11) NOT NULL, `name` varchar(20) DEFAULT 'x;y') ENGINE=InnoDB;
LOCK TABLES `items` WRITE;
INSERT INTO `items` VALUES (1,'a'),(2,'b;c');
UNLOCK TABLES;
DELIMITER ;;
/*!50003 CREATE*/ /*!50003 TRIGGER `t` BEFORE INSERT ON `items` FOR EACH ROW BEGIN
  SET NEW.name = 'z';
END */;;
DELIMITER ;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-10-27 16:54:57
"""


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_only_the_two_exact_names_are_accepted() -> None:
    assert file_name_ok("bankdb.sql") == "bankdb" and file_name_ok(r"C:\x\INVENTORY1.SQL") == "inventory1"
    for name in ("bankdb (1).sql", "bank.sql", "bankdb.sql.zip", "inventory.sql", "bankdb"):
        assert file_name_ok(name) is None


def test_good_files_pass(tmp_path: Path) -> None:
    seen = []
    own = check_sql(write(tmp_path, "bankdb.sql", OWN), "bankdb", lambda d, t: seen.append((d, t)))
    assert own == {"tables": 1, "makes": False, "chooses": False, "own": True}
    assert seen[-1][0] == seen[-1][1]  # the progress reaches 100%
    dump = check_sql(write(tmp_path, "inventory1.sql", DUMP), "inventory1")
    assert dump["tables"] == 1 and not dump["own"]


@pytest.mark.parametrize("data, database, message", [
    (b"", "bankdb", "empty"),
    (OWN + b"INSERT INTO `branchmaster` VALUES ('cut off", "bankdb", "never ends"),        # a quote never closed
    (OWN + b"INSERT INTO `branchmaster` VALUES ('x', '4')", "bankdb", "never ends"),        # no closing ;
    (OWN.replace(b"-- Backup of bankdb", b"-- Backup of otherdb"), "bankdb", "another database"),
    (DUMP.split(b"UNLOCK TABLES;")[0], "inventory1", "not whole"),  # cut off after a complete INSERT
    (DUMP.replace(b"-- Dump completed on 2026-10-27 16:54:57", b""), "inventory1", "not whole"),  # ends on SQL_NOTES
    (DUMP.replace(b"UNLOCK TABLES;", b"UNLOCK TABLES;\nGARBAGE here;"), "inventory1", "not an SQL statement"),
    (DUMP.replace(b"/*!40101 SET NAMES utf8 */;", b"/*!40101 SET NAMES utf8 */;\nUSE `bankdb`;"), "inventory1", "another database"),
    (b"-- Backup of bankdb\nSET x=1;\n", "bankdb", "no tables"),
])
def test_broken_files_are_refused_before_anything_changes(tmp_path: Path, data: bytes, database: str, message: str) -> None:
    with pytest.raises(Stop, match=message):
        check_sql(write(tmp_path, f"{database}.sql", data), database)


# ---------------------------------------------------------------------- the whole run, on a throwaway server

import socket  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from test_mysql_rescue import mariadb_home  # noqa: E402

from varsha.bankimport import SERVICE, BankImport  # noqa: E402
from varsha.paths import AppPaths  # noqa: E402

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeManager:
    """Stands in for varsha's ServiceManager around one throwaway MariaDB."""

    def __init__(self, home: Path, port: int) -> None:
        self.home, self.port = home, port
        self.credentials = SimpleNamespace(get=lambda _sid: "")

    def definition(self, _sid: str) -> SimpleNamespace:
        return SimpleNamespace(name="MySQL 5.6 (test)", ports=[self.port])

    def status(self, _sid: str) -> SimpleNamespace:
        return SimpleNamespace(state=SimpleNamespace(value="running"))

    def expand_tool(self, _sid: str, tool: str) -> Path:
        return self.home / "bin" / {"client": "mariadb.exe", "dump": "mariadb-dump.exe"}[tool]


class FakeDatabases:
    def __init__(self, manager: FakeManager, folder: Path) -> None:
        self.manager, self.folder = manager, folder

    def backup_dir(self, _sid: str) -> Path:
        return self.folder

    def backup(self, sid: str, databases: list[str], filename: str | None = None) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / (filename or f"{sid}.sql")
        subprocess.run([str(self.manager.expand_tool(sid, "dump")), "--no-defaults", "--protocol=TCP", "-h127.0.0.1",
                        f"-P{self.manager.port}", "-uroot", f"--result-file={target}", "--databases", *databases],
                       check=True, capture_output=True, creationflags=FLAGS)
        return target


@pytest.fixture
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    home = mariadb_home()
    if home is None:
        pytest.skip("needs MariaDB in bin")
    datadir = tmp_path / "server-data"
    subprocess.run([str(home / "bin" / "mariadb-install-db.exe"), f"--datadir={datadir}"], check=True,
                   capture_output=True, timeout=180, creationflags=FLAGS)
    port = free_port()
    proc = subprocess.Popen([str(home / "bin" / "mariadbd.exe"), "--no-defaults", f"--datadir={datadir.as_posix()}",
                             f"--port={port}", "--bind-address=127.0.0.1", "--skip-grant-tables"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=FLAGS)
    client = [str(home / "bin" / "mariadb.exe"), "--no-defaults", "--protocol=TCP", "-h127.0.0.1", f"-P{port}", "-uroot"]
    for _ in range(120):
        if subprocess.run([*client, "-e", "SELECT 1"], capture_output=True, creationflags=FLAGS).returncode == 0:
            break
        _time.sleep(0.5)
    yield home, port, client
    proc.kill()
    proc.wait(30)


def wait_done(bank: BankImport) -> dict:
    deadline = _time.monotonic() + 300
    while bank.busy() and _time.monotonic() < deadline:
        _time.sleep(0.1)
    return bank.status()


def test_drop_import_backup_and_restore_on_a_real_server(server, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    home, port, client = server
    old = ("CREATE DATABASE bankdb; CREATE TABLE bankdb.branchmaster (branameng varchar(250), idno int primary key);"
           "INSERT INTO bankdb.branchmaster VALUES ('PACS SAVALAGI', 1); CREATE TABLE bankdb.old_only (x int);"
           "CREATE DATABASE inventory1; CREATE TABLE inventory1.keep (x int);")
    subprocess.run(client, input=old.encode(), check=True, capture_output=True, creationflags=FLAGS)
    paths = AppPaths(tmp_path / "varsha")
    paths.ensure_dirs()
    manager = FakeManager(home, port)
    bank = BankImport(paths, manager, FakeDatabases(manager, tmp_path / "backups"))
    run = bank.new_run()
    data = OWN.replace(b"('PACS SAVALAGI', '1')", b"('PACS SAVALAGI', '1')") + "INSERT INTO `branchmaster` VALUES ('ಸಾವಳಗಿ', '9');\n".encode()
    import io
    assert bank.save_upload(run, "bankdb.sql", io.BytesIO(data), len(data))["database"] == "bankdb"
    with pytest.raises(ValueError, match="isn't bankdb.sql"):
        bank.save_upload(run, "other.sql", io.BytesIO(b"x"), 1)
    bank.start(run)  # only bankdb was dropped: inventory1 must stay as it is
    state = wait_done(bank)
    assert state["status"] == "done", state["log"][-6:]
    backup = state["backups"]["bankdb"]
    assert backup.startswith("PACS_SAVALAGI_bankdb_") and backup.endswith(".sql")  # named after the bank, with the time
    tables = subprocess.run([*client, "-N", "-e", "SHOW TABLES FROM bankdb; SHOW TABLES FROM inventory1"],
                            capture_output=True, creationflags=FLAGS).stdout.decode().split()
    assert tables == ["branchmaster", "keep"]  # bankdb replaced (old_only gone), inventory1 untouched
    raw = subprocess.run([*client, "-N", "-e", "SELECT HEX(branameng) FROM bankdb.branchmaster WHERE idno = 9"],
                         capture_output=True, creationflags=FLAGS).stdout.decode().strip()
    assert bytes.fromhex(raw).decode("utf-8") == "ಸಾವಳಗಿ"  # the bank backup's raw bytes went in unchanged (latin1)
    assert "bankdb imported" in "\n".join(state["log"])
    bank.restore("bankdb")
    tables = subprocess.run([*client, "-N", "-e", "SHOW TABLES FROM bankdb"], capture_output=True,
                            creationflags=FLAGS).stdout.decode().split()
    assert tables == ["branchmaster", "old_only"]  # the data from before the import is back


def test_old_backups_are_pruned_after_a_successful_import(tmp_path: Path) -> None:
    import os
    paths = AppPaths(tmp_path / "varsha")
    paths.ensure_dirs()
    folder = tmp_path / "backups"
    folder.mkdir()
    bank = BankImport(paths, SimpleNamespace(), SimpleNamespace(backup_dir=lambda _sid: folder))
    for day in range(1, 13):  # 12 older backups of bankdb, oldest first
        f = folder / f"PACS_SAVALAGI_bankdb_202611{day:02d}-120000.sql"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (day * 1000, day * 1000))
    other = folder / "mysql-5.6_20261101-120000.sql"  # a normal varsha backup: never touched
    other.write_text("x", encoding="utf-8")
    keep_inventory = folder / "PKPS_TAKKOD_inventory1_20261101-120000.sql"  # inventory1 wasn't imported
    keep_inventory.write_text("x", encoding="utf-8")
    upload = tmp_path / "bankdb.sql"
    upload.write_text("uploaded", encoding="utf-8")
    bank._tidy({"bankdb": upload})  # noqa: SLF001
    left = sorted(p.name for p in folder.glob("*_bankdb_*.sql"))
    assert len(left) == 10 and left[0].endswith("20261103-120000.sql")  # the two oldest went
    assert other.exists() and keep_inventory.exists() and not upload.exists()
