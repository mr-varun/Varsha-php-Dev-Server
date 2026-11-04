"""Build the installer (setup\\) on D: and install the final app into C:\\Varsha with it.

    tools\\deploy.cmd [--target C:\\Varsha] [--skip-build] [--no-install]

What it does:
  1. builds Varsha.exe + _internal\\ with PyInstaller (tools\\varsha.spec) into build\\dist\\Varsha
     (code, web panel, templates and default config are all inside the build)
  2. refreshes setup\\payload: the build, plus bin\\ as hard links to this repo's bin\\
     (no second 1.3 GB copy on D:; phpMyAdmin's generated config files are left out)
  3. builds setup\\Setup.exe (tools\\installer, C# compiler that ships with Windows)
  4. runs  Setup.exe /quiet /noshortcuts  to install into the target, then checks the new exe

setup\\ is self-contained: after formatting the PC, run setup\\Setup.exe and it sets
up C:\\Varsha again with nothing else installed. Setup never touches config\\, data\\ or www\\.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path(r"C:\Varsha")
DIST = REPO / "build" / "dist" / "Varsha"
SETUP = REPO / "setup"
PAYLOAD = SETUP / "payload"
# Written by varsha's config engine at runtime; each install keeps its own.
BIN_SKIP = {("phpmyadmin", "config.inc.php"), ("phpmyadmin", "config.footer.inc.php")}

README = """varsha setup
============

Run Setup.exe. It installs varsha into C:\\Varsha (you can pick another folder) and creates
Desktop and Start menu shortcuts. Nothing else needs to be installed on the PC: Python, the
control panel and the Visual C++ runtimes are all inside payload\\.

Running it again updates the app and bin\\. It never touches config\\, data\\ (databases) or
www\\ (sites) in the install folder.

After a fresh install, in the control panel:
  - Trust HTTPS      (installs the local certificate authority for https://*.test)
  - Update hosts / DNS route   (one administrator prompt) so *.test names resolve
  - restore your database backups

Built {built} from {source}
"""


def say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def fail(message: str) -> None:
    say(f"deploy: {message}")
    raise SystemExit(1)


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


def make_payload() -> None:
    if not (DIST / "Varsha.exe").is_file():
        fail(f"{DIST / 'Varsha.exe'} not found: build first (drop --skip-build)")
    PAYLOAD.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(PAYLOAD / "_internal", ignore_errors=True)
    shutil.copytree(DIST / "_internal", PAYLOAD / "_internal")
    shutil.copy2(DIST / "Varsha.exe", PAYLOAD / "Varsha.exe")
    # Data files are plain files in the build, so refresh them from the repo even with --skip-build
    # (only Python code changes need a new PyInstaller build).
    internal = PAYLOAD / "_internal"
    shutil.copytree(REPO / "etc" / "templates", internal / "etc" / "templates", dirs_exist_ok=True)
    shutil.copytree(REPO / "src" / "varsha" / "web" / "static", internal / "varsha" / "web" / "static", dirs_exist_ok=True)
    for name in ("services.json", "settings.json"):
        shutil.copy2(REPO / "config" / name, internal / "config" / name)
    # bin: hard links (same drive), so the payload costs no space and is always current.
    # Removing the old links never touches the real files in REPO\bin.
    shutil.rmtree(PAYLOAD / "bin", ignore_errors=True)
    source = REPO / "bin"
    linked = copied = 0
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if not path.is_file() or (len(rel.parts) == 2 and (rel.parts[0], rel.name) in BIN_SKIP):
            continue
        out = PAYLOAD / "bin" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, out)
            linked += 1
        except OSError:
            shutil.copy2(path, out)
            copied += 1
    (SETUP / "README.txt").write_text(
        README.format(built=time.strftime("%Y-%m-%d %H:%M"), source=REPO), encoding="utf-8")
    say(f"  payload    {PAYLOAD} (bin: {linked} hard links" + (f", {copied} copies" if copied else "") + ")")


def build_setup() -> None:
    result = subprocess.run(["cmd.exe", "/c", str(REPO / "tools" / "installer" / "build.cmd"), str(SETUP)],
                            capture_output=True, text=True)
    if result.returncode != 0 or not (SETUP / "Setup.exe").is_file():
        fail(f"building Setup.exe failed:\n{result.stdout}{result.stderr}")
    say(f"  setup      {SETUP / 'Setup.exe'}")


def install(target: Path) -> None:
    log = Path(tempfile.gettempdir()) / "varsha-setup.log"
    log.unlink(missing_ok=True)
    code = subprocess.run([str(SETUP / "Setup.exe"), "/quiet", "/noshortcuts", "/target", str(target),
                           "/log", str(log)]).returncode
    text = log.read_text(encoding="utf-8-sig") if log.is_file() else "(no log)"
    for line in text.strip().splitlines():
        say(f"  install    {line}")
    if code != 0:
        fail(f"Setup.exe failed (exit {code})")


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
    parser.add_argument("--no-install", action="store_true", help="only refresh setup")
    args = parser.parse_args()
    target: Path = args.target.resolve()
    if target == REPO or target.is_relative_to(REPO):
        fail("the target must be outside the source folder")

    say(f"deploying {REPO} -> {target}")
    if not args.skip_build:
        build()
    make_payload()
    info = {"source": str(REPO), "built": time.strftime("%Y-%m-%d %H:%M:%S")}
    (PAYLOAD / "_internal" / "DEPLOYED.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    build_setup()
    if not args.no_install:
        install(target)
        verify(target)
    say(f"done. Installer: {SETUP / 'Setup.exe'}" + ("" if args.no_install else f"  App: {target / 'Varsha.exe'}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
