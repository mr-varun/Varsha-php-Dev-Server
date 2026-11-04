"""Git status for the site cards: branch, changes, push/pull counts and the last commit.

Read-only by design: `--no-optional-locks` (and GIT_OPTIONAL_LOCKS=0) stops `git status` from
refreshing the index on disk, nothing is ever fetched (ahead/behind compare with what this PC last
knew about the upstream), and no command that changes a repository is run.

A site's project folder is its folder, or the folder above public\\ when the site points straight
at public\\ (a junction). Several sites can live in one repository (a monorepo): the branch and
push/pull state are the repository's, but the changed files and the last commit are only the ones
inside that project's folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from varsha.services import process as proc_utils

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
MAX_FILES = 40
_FIELD = "\x1f"


def find_git() -> Path | None:
    found = shutil.which("git")
    if found:
        return Path(found)
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        candidate = Path(base) / ("Programs" if base.endswith("Local") else "") / "Git" / "cmd" / "git.exe"
        if candidate.is_file():
            return candidate
    return None


def project_folder(site_folder: Path) -> Path:
    real = site_folder.resolve()
    return real.parent if real.name.lower() == "public" else real


def parse_status(text: str) -> dict[str, Any]:
    """`git status --porcelain=v2 --branch` -> branch info and changed paths."""
    info: dict[str, Any] = {"branch": "", "detached": False, "upstream": "", "ahead": 0, "behind": 0,
                            "changed": 0, "untracked": 0, "conflicts": 0, "files": []}
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            info["detached"] = head == "(detached)"
            info["branch"] = "" if info["detached"] else head
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            info["ahead"], info["behind"] = int(parts[2].lstrip("+")), int(parts[3].lstrip("-"))
        elif line[:2] in ("1 ", "2 ", "u ", "? "):
            kind = line[0]
            if kind == "?":
                path, code = line[2:], "??"
                info["untracked"] += 1
            else:
                fields = line.split(" ", 8 if kind == "1" else 9 if kind == "2" else 10)
                code = fields[1]
                path = fields[-1].split("\t")[0]  # a rename: "new\told"
                if kind == "u":
                    info["conflicts"] += 1
                else:
                    info["changed"] += 1
            if len(info["files"]) < MAX_FILES:
                info["files"].append({"path": path, "code": code})
    return info


class SiteGit:
    def __init__(self, git: Path | None = None) -> None:
        self.git = git if git is not None else find_git()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.git is not None

    def _run(self, folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        return subprocess.run([str(self.git), "-C", str(folder), "--no-optional-locks", *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                              env=env, stdin=subprocess.DEVNULL, creationflags=proc_utils.CREATE_NO_WINDOW)

    def info(self, site_folder: Path) -> dict[str, Any]:
        """Git details for one site (no cache)."""
        if self.git is None:
            return {"repo": False, "error": "Git isn't installed"}
        try:
            folder = project_folder(site_folder)
        except OSError as exc:
            return {"repo": False, "error": str(exc)}
        if not folder.is_dir():
            return {"repo": False, "error": "folder not found"}
        top = self._run(folder, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return {"repo": False}
        root = Path(top.stdout.strip())
        status = self._run(folder, "status", "--porcelain=v2", "--branch", "--untracked-files=normal", "--", ".")
        if status.returncode != 0:
            return {"repo": True, "root": str(root), "error": (status.stderr or status.stdout).strip()[-300:]}
        data = parse_status(status.stdout)
        log = self._run(folder, "log", "-1", f"--format=%h{_FIELD}%s{_FIELD}%ct{_FIELD}%an", "--", ".")
        last = None
        if log.returncode == 0 and log.stdout.strip():
            short, subject, when, author = (log.stdout.strip().split(_FIELD) + ["", "", "0", ""])[:4]
            last = {"hash": short, "subject": subject, "when": int(when or 0), "author": author}
        try:
            subdir = folder.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            subdir = ""
        return {"repo": True, "root": str(root), "repo_name": root.name, "subdir": "" if subdir == "." else subdir,
                "shared": subdir not in ("", "."), **data, "last": last}

    def statuses(self, sites: list[tuple[str, Path]], refresh: bool = False) -> dict[str, dict[str, Any]]:
        """{domain: info} for every site, checked side by side, cached for CACHE_SECONDS."""
        now = time.monotonic()
        with self._lock:
            todo = [(d, f) for d, f in sites if refresh or d not in self._cache or now - self._cache[d][0] > CACHE_SECONDS]
        if todo:
            with ThreadPoolExecutor(max_workers=min(8, len(todo)), thread_name_prefix="git") as pool:
                results = list(pool.map(lambda item: (item[0], self._safe_info(item[1])), todo))
            with self._lock:
                for domain, data in results:
                    self._cache[domain] = (time.monotonic(), data)
        with self._lock:
            return {d: self._cache[d][1] for d, _f in sites if d in self._cache}

    def _safe_info(self, folder: Path) -> dict[str, Any]:
        try:
            return self.info(folder)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git status of %s failed: %s", folder, exc)
            return {"repo": False, "error": str(exc)}
