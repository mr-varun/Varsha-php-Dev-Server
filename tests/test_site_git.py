"""Git status on site cards: parsing, monorepo scoping, junction sites, and read-only use."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from varsha.sites.git import SiteGit, find_git, parse_status, project_folder

GIT = find_git()
needs_git = pytest.mark.skipif(GIT is None, reason="needs git")


def test_parse_status_porcelain_v2() -> None:
    text = "\n".join([
        "# branch.oid 1234",
        "# branch.head feature/reports",
        "# branch.upstream origin/feature/reports",
        "# branch.ab +2 -1",
        "1 .M N... 100644 100644 100644 aaa bbb Admin/src/Db.php",
        "2 R. N... 100644 100644 100644 aaa bbb R100 Admin/new name.php\tAdmin/old.php",
        "u UU N... 100644 100644 100644 100644 aaa bbb ccc Admin/conflict.php",
        "? Admin/notes.txt",
    ])
    info = parse_status(text)
    assert (info["branch"], info["upstream"], info["ahead"], info["behind"]) == ("feature/reports", "origin/feature/reports", 2, 1)
    assert (info["changed"], info["untracked"], info["conflicts"]) == (2, 1, 1)
    assert [f["path"] for f in info["files"]] == ["Admin/src/Db.php", "Admin/new name.php", "Admin/conflict.php", "Admin/notes.txt"]
    detached = parse_status("# branch.oid 1234\n# branch.head (detached)")
    assert detached["detached"] and detached["branch"] == ""


def git(cwd: Path, *args: str) -> None:
    subprocess.run([str(GIT), "-C", str(cwd), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@x"))


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """A monorepo: one repository, one folder per project, each with public\\."""
    repo = tmp_path / "Websites"
    for project in ("Admin", "Banking"):
        (repo / project / "public").mkdir(parents=True)
        (repo / project / "public" / "index.php").write_text("<?php echo 1;")
        (repo / project / "src.php").write_text("<?php // code")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "first")
    (repo / "Banking" / "src.php").write_text("<?php // banking change")
    git(repo, "commit", "-q", "-am", "banking: interest fix")
    return repo


@needs_git
def test_monorepo_projects_see_only_their_own_changes(monorepo: Path) -> None:
    (monorepo / "Admin" / "src.php").write_text("<?php // edited")
    (monorepo / "Admin" / "new.php").write_text("<?php")
    (monorepo / "Banking" / "public" / "index.php").write_text("<?php echo 2;")
    sg = SiteGit(GIT)
    admin = sg.info(monorepo / "Admin" / "public")  # the site points at public\ (a junction)
    assert admin["repo"] and admin["branch"] == "main" and admin["shared"] and admin["subdir"] == "Admin"
    assert (admin["changed"], admin["untracked"]) == (1, 1)
    assert sorted(f["path"] for f in admin["files"]) == ["new.php", "src.php"]  # relative to the project
    assert admin["last"]["subject"] == "first"  # the banking commit didn't touch Admin\
    banking = sg.info(monorepo / "Banking" / "public")
    assert banking["changed"] == 1 and banking["last"]["subject"] == "banking: interest fix"
    assert admin["upstream"] == "" and admin["ahead"] == 0


@needs_git
def test_reading_status_never_writes_to_the_repository(monorepo: Path) -> None:
    index = monorepo / ".git" / "index"
    os.utime(index, (time.time() - 100, time.time() - 100))
    (monorepo / "Admin" / "src.php").write_text("<?php // touched")  # a stale index makes plain `git status` rewrite it
    before = (index.stat().st_mtime_ns, index.read_bytes())
    SiteGit(GIT).info(monorepo / "Admin" / "public")
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before
    assert not (monorepo / ".git" / "index.lock").exists()


@needs_git
def test_not_a_repository_and_cache(tmp_path: Path, monorepo: Path) -> None:
    plain = tmp_path / "plain" / "public"
    plain.mkdir(parents=True)
    sg = SiteGit(GIT)
    assert sg.info(plain) == {"repo": False}
    first = sg.statuses([("admin.test", monorepo / "Admin" / "public"), ("plain.test", plain)])
    assert first["admin.test"]["repo"] and first["plain.test"] == {"repo": False}
    (monorepo / "Admin" / "src.php").write_text("<?php // later")
    assert sg.statuses([("admin.test", monorepo / "Admin" / "public")])["admin.test"]["changed"] == 0  # cached
    assert sg.statuses([("admin.test", monorepo / "Admin" / "public")], refresh=True)["admin.test"]["changed"] == 1


def test_without_git_and_project_folder(tmp_path: Path) -> None:
    assert SiteGit(git=None).available is False or SiteGit(git=None).git is not None  # find_git may still find one
    no_git = SiteGit(git=None)
    no_git.git = None
    assert no_git.info(tmp_path) == {"repo": False, "error": "Git isn't installed"}
    (tmp_path / "Shop" / "public").mkdir(parents=True)
    assert project_folder(tmp_path / "Shop" / "public") == (tmp_path / "Shop").resolve()
    assert project_folder(tmp_path / "Shop") == (tmp_path / "Shop").resolve()
