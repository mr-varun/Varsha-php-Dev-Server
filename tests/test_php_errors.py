"""PHP error watcher: parsing PHP's error log, grouping, site mapping, alerts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from varsha.php.errors import PhpErrorWatcher, locate, parse_lines

# Real lines from varsha's logs (an older app under PHP 5.4 and 8.3).
WARNING = ("[04-Nov-2026 10:46:03 UTC] PHP Warning:  mysqli_num_rows() expects parameter 1 to be mysqli_result, "
           "boolean given in E:\\Projects\\billing\\app\\index.php on line 14")
NOTICE = "[04-Nov-2026 10:46:03 UTC] PHP Notice:  Undefined variable: branchname in E:\\Projects\\billing\\app\\index.php on line 60"
FATAL = """[04-Nov-2026 10:55:43 UTC] PHP Fatal error:  Uncaught mysqli_sql_exception: Access denied for user 'root'@'localhost' (using password: NO) in E:\\Projects\\ledger\\app\\connection.php:9
Stack trace:
#0 E:\\Projects\\ledger\\app\\connection.php(9): mysqli_connect('localhost', 'root', Object(SensitiveParameterValue), 'appdb', 3308)
#1 E:\\Projects\\ledger\\app\\index.php(5): include('E:\\\\Projects\\\\le...')
#2 {main}
  thrown in E:\\Projects\\ledger\\app\\connection.php on line 9"""


def test_parses_levels_messages_locations_and_stack_traces() -> None:
    entries = parse_lines((WARNING + "\n" + NOTICE + "\n" + FATAL).splitlines())
    assert [e.level for e in entries] == ["Warning", "Notice", "Fatal error"]
    message, file, line = locate(entries[0])
    assert message.startswith("mysqli_num_rows() expects parameter 1") and line == 14
    assert file == "E:\\Projects\\billing\\app\\index.php"
    fatal = entries[2]
    assert len(fatal.lines) == 6  # the stack trace belongs to the fatal error
    message, file, line = locate(fatal)
    assert message.startswith("Uncaught mysqli_sql_exception: Access denied") and "connection.php:9" not in message
    assert file.endswith("connection.php") and line == 9


def test_apache_fcgid_stderr_lines() -> None:
    line = ("[Wed Nov 04 12:00:00.1 2026] [fcgid:warn] [pid 1:tid 2] [client 127.0.0.1:5000] mod_fcgid: stderr: "
            "PHP Parse error:  syntax error, unexpected '}' in D:\\site\\a.php on line 3, referer: https://shop.test/")
    (entry,) = parse_lines([line], fcgid=True)
    assert entry.level == "Parse error"
    assert locate(entry) == ("syntax error, unexpected '}'", "D:\\site\\a.php", 3)
    assert parse_lines(["[Wed Nov 04] [core:notice] AH00094: Command line: 'httpd.exe'"], fcgid=True) == []


class Harness:
    def __init__(self, tmp_path: Path, sites: list[tuple[str, Path]] | None = None) -> None:
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.events: list[dict[str, Any]] = []
        self.watcher = PhpErrorWatcher(self.logs, tmp_path / "config" / "php-errors.json",
                                       site_roots=lambda: sites or [], on_event=self.events.append)

    def write(self, text: str, name: str = "php-8.3.33.log") -> None:
        with open(self.logs / name, "a", encoding="utf-8", newline="\n") as out:
            out.write(text + "\n")


def test_old_log_content_is_history_and_new_errors_are_announced(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.write(FATAL)  # already in the log before varsha started
    h.watcher.poll(history=True)
    assert h.events == [] and h.watcher.summary()["unseen"] == 0  # history: listed, never announced
    assert h.watcher.groups()[0]["level"] == "Fatal error"
    h.write(FATAL.replace("10:55:43", "11:00:00"))  # the same bug again
    h.watcher.poll()
    assert h.watcher.groups()[0]["count"] == 2  # one group, counted
    assert [g["level"] for g in h.events[-1]["new"]] == ["Fatal error"]
    assert h.events[-1]["summary"]["unseen_errors"] == 1


def test_numbers_in_messages_dont_split_a_bug_and_repeats_stay_quiet(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.watcher.poll(history=True)
    for n in (1, 2, 3):
        h.write(f"[04-Nov-2026 11:0{n}:00 UTC] PHP Fatal error:  Allowed memory size of {n}34217728 bytes exhausted in D:\\a\\x.php on line 7")
        h.watcher.poll()
    groups = h.watcher.groups()
    assert len(groups) == 1 and groups[0]["count"] == 3
    assert sum(len(e["new"]) for e in h.events) == 1  # announced once, not three times


def test_notify_level_and_mute(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.watcher.poll(history=True)
    h.write(WARNING)
    h.watcher.poll()
    assert h.events[-1]["new"] == []  # default: only fatal errors raise an alert
    h.watcher.set_notify("warnings")
    h.write(NOTICE)
    h.write(WARNING.replace("line 14", "line 99"))
    h.watcher.poll()
    assert [g["level"] for g in h.events[-1]["new"]] == ["Warning"]  # notices never alert
    h.write(FATAL)
    h.watcher.poll()
    fatal_id = h.events[-1]["new"][0]["id"]
    h.watcher.set_muted(fatal_id, True)
    h.watcher.groups()[0]["announced"] = 0
    h.watcher.group(fatal_id).announced = 0
    h.write(FATAL.replace("10:55:43", "12:00:00"))
    h.watcher.poll()
    assert h.events[-1]["new"] == []  # muted
    again = PhpErrorWatcher(h.logs, tmp_path / "config" / "php-errors.json", site_roots=lambda: [], on_event=lambda e: None)
    assert again.settings == {"notify": "warnings", "muted": [fatal_id]}  # settings persist
    with pytest.raises(ValueError):
        h.watcher.set_notify("loud")


def test_site_is_found_through_a_junction_to_public(tmp_path: Path) -> None:
    project = tmp_path / "Websites" / "Admin"
    (project / "public").mkdir(parents=True)
    (project / "src").mkdir()
    h = Harness(tmp_path, sites=[("admin.test", project / "public"), ("other.test", tmp_path / "Websites" / "Other")])
    h.watcher.poll(history=True)
    h.write(f"[04-Nov-2026 11:00:00 UTC] PHP Warning:  oops in {project / 'src' / 'Db.php'} on line 4")
    h.write(f"[04-Nov-2026 11:00:01 UTC] PHP Warning:  elsewhere in {tmp_path / 'Unrelated' / 'x.php'} on line 1")
    h.watcher.poll()
    sites = {g["message"]: g["site"] for g in h.watcher.groups()}
    assert sites == {"oops": "admin.test", "elsewhere": ""}  # code above public\ still belongs to the site


def test_partial_lines_wait_and_rotated_logs_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.watcher.poll(history=True)
    with open(h.logs / "php-8.3.33.log", "a", encoding="utf-8") as out:
        out.write("[04-Nov-2026 11:00:00 UTC] PHP Warning:  half writ")  # PHP is mid-write
    h.watcher.poll()
    assert h.watcher.groups() == []
    with open(h.logs / "php-8.3.33.log", "a", encoding="utf-8") as out:
        out.write("ten in D:\\a.php on line 2\n")
    h.watcher.poll()
    assert h.watcher.groups()[0]["message"] == "half written"
    (h.logs / "php-8.3.33.log").write_text(NOTICE + "\n", encoding="utf-8")  # cleared and rewritten
    h.watcher.poll()
    assert any(g["level"] == "Notice" for g in h.watcher.groups())


def test_seen_clear_and_dismiss(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.watcher.poll(history=True)
    h.write(WARNING)
    h.write(NOTICE, name="php-5.4.16.log")  # every PHP version's log is followed
    h.watcher.poll()
    assert h.watcher.summary()["unseen"] == 2
    h.watcher.mark_seen()
    assert h.watcher.summary()["unseen"] == 0
    gid = h.watcher.groups(severity="notice")[0]["id"]
    h.watcher.dismiss(gid)
    assert [g["level"] for g in h.watcher.groups()] == ["Warning"]
    assert h.watcher.groups(query="NUM_ROWS")[0]["level"] == "Warning"
    h.watcher.clear()
    assert h.watcher.groups() == []
