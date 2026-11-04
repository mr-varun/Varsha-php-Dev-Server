"""Build the frozen Varsha.exe on D: and put only the final app files into C:\\Varsha.

    tools\\deploy.cmd [--target C:\\Varsha] [--skip-build] [--skip-bin]

What it does:
  1. builds Varsha.exe + _internal\\ with PyInstaller (tools\\varsha.spec) into build\\dist\\Varsha
     (code, web panel, templates and default config are all inside the build)
  2. replaces <target>\\Varsha.exe and <target>\\_internal (swapped in at the end)
  3. syncs bin\\ (Apache, PHP, MySQL, ...) to <target>\\bin: new and changed files only,
     never deletes, skips phpMyAdmin's generated config files
  4. checks that the new exe starts and finds its root folder

Nothing else goes to the target: no venv, no source, no tests. varsha creates config\\, data\\,
www\\, logs\\ and etc\\ there by itself on first start, and deploy never touches them.
It refuses to run while anything runs from the target (varsha or one of its services).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path(r"C:\Varsha")
DIST = REPO / "build" / "dist" / "Varsha"
# Written by varsha's config engine at runtime; each folder keeps its own.
BIN_SKIP = {("phpmyadmin", "config.inc.php"), ("phpmyadmin", "config.footer.inc.php")}


def say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def fail(message: str) -> None:
    say(f"deploy: {message}")
    raise SystemExit(1)


def running_from(target: Path) -> list[str]:
    """Processes whose exe lives in the target folder (varsha itself, httpd, mysqld, ...)."""
    import psutil

    found = []
    for proc in psutil.process_iter(["exe", "name"]):
        exe = proc.info.get("exe")
        try:
            if exe and Path(exe).resolve().is_relative_to(target):
                found.append(f"{proc.info.get('name')} (PID {proc.pid})")
        except (OSError, ValueError):
            continue
    return found


def build() -> None:
    say("  build      PyInstaller (takes about a minute)...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", str(REPO / "build" / "dist"), "--workpath", str(REPO / "build" / "work"),
         str(REPO / "tools" / "varsha.spec")],
        cwd=str(REPO), capture_output=True, text=True)
    if result.returncode != 0 or not (DIST / "Varsha.exe").is_file():
        fail(f"PyInstaller failed:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")
    say(f"  build      {DIST}")


def install_app(target: Path) -> None:
    if not (DIST / "Varsha.exe").is_file():
        fail(f"{DIST / 'Varsha.exe'} not found: build first (drop --skip-build)")
    target.mkdir(parents=True, exist_ok=True)
    new, current, old = target / "_internal.new", target / "_internal", target / "_internal.old"
    for leftover in (new, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(DIST / "_internal", new)
    if current.exists():
        current.rename(old)
    new.rename(current)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copy2(DIST / "Varsha.exe", target / "Varsha.exe")
    files = sum(1 for p in current.rglob("*") if p.is_file())
    say(f"  app        Varsha.exe + _internal ({files} files) -> {target}")


def sync_bin(target: Path) -> None:
    source, dest = REPO / "bin", target / "bin"
    copied = same = 0
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if not path.is_file() or (len(rel.parts) == 2 and (rel.parts[0], rel.name) in BIN_SKIP):
            continue
        out = dest / rel
        st = path.stat()
        if out.is_file():
            ot = out.stat()
            if ot.st_size == st.st_size and int(ot.st_mtime) == int(st.st_mtime):
                same += 1
                continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        copied += 1
    extra = [p.name for p in dest.iterdir() if not (source / p.name).exists()] if dest.is_dir() else []
    say(f"  bin        {copied} copied, {same} unchanged" + (f"; only in target: {', '.join(extra)}" if extra else ""))


def verify(target: Path) -> None:
    env = dict(os.environ)
    env.pop("VARSHA_ROOT", None)
    probe = subprocess.run([str(target / "Varsha.exe"), "--version"], capture_output=True, text=True,
                           env=env, cwd=str(target), timeout=60)
    out = probe.stdout.strip()
    if probe.returncode != 0 or f"root {target}".lower() not in out.lower():
        fail(f"check failed (exit {probe.returncode}): {out} {probe.stderr.strip()}")
    say(f"  check      {out.splitlines()[0]}, root {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--skip-build", action="store_true", help="reuse build\\dist\\Varsha")
    parser.add_argument("--skip-bin", action="store_true", help="don't sync bin\\")
    args = parser.parse_args()
    target: Path = args.target.resolve()
    if target == REPO or target.is_relative_to(REPO):
        fail("the target must be outside the source folder")
    busy = running_from(target)
    if busy:
        fail(f"these run from {target}: {', '.join(busy)}. Quit varsha (tray > Quit, stop services), then deploy again.")

    say(f"deploying {REPO} -> {target}")
    if not args.skip_build:
        build()
    install_app(target)
    if not args.skip_bin:
        sync_bin(target)
    info = {"source": str(REPO), "deployed": time.strftime("%Y-%m-%d %H:%M:%S")}
    (target / "_internal" / "DEPLOYED.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    verify(target)
    say(f"done at {info['deployed']}. Start {target / 'Varsha.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
