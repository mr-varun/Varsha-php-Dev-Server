"""Developer tools behind the panel: terminal, Quick Create, deleting a site, PHP settings.

Kept out of the HTTP layer so each action is a plain function that can be tested.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from varsha.config_engine.engine import ConfigEngine
from varsha.errors import ConfigError, ServiceError
from varsha.paths import AppPaths
from varsha.php.versions import PhpBuild
from varsha.secrets import client_option_file
from varsha.services import process as proc_utils
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState
from varsha.settings import Settings
from varsha.sites.discovery import Site, save_override, slugify

logger = logging.getLogger(__name__)

WORDPRESS_ZIP = "https://wordpress.org/latest.zip"
CREATE_TIMEOUT = 1800
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
KINDS = ("blank", "link", "wordpress")


# ---------------------------------------------------------------------- settings.json

def update_settings_file(path: Path, **changes: Any) -> None:
    """Change keys in settings.json without touching the others (or the user's comments keys)."""
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class DevTools:
    def __init__(self, paths: AppPaths, engine: ConfigEngine, manager: ServiceManager) -> None:
        self.paths = paths
        self.engine = engine
        self.manager = manager

    # ------------------------------------------------------------------ lookups

    def site(self, domain: str) -> Site:
        for site in self.engine.sites():
            if site.domain == domain:
                return site
        raise ServiceError(f"No site {domain!r} in www\\")

    def php_for(self, site: Site | None) -> PhpBuild:
        build = self.engine.find_php(site.php) if site and site.php else self.engine.default_php()
        if build is None:
            raise ServiceError("No PHP build with php-cgi.exe in bin\\php")
        return build

    def mysql_bin(self) -> Path | None:
        """The bin folder of the newest MySQL-compatible client (so `mysql` works in the terminal)."""
        for status in sorted((s for s in self.manager.statuses() if s.is_database and s.version),
                             key=lambda s: s.type == "mysql", reverse=True):
            try:
                return self.manager.expand_tool(status.id, "client").parent
            except ServiceError:
                continue
        return None

    # ------------------------------------------------------------------ PHP settings

    def set_default_php(self, folder: str) -> str:
        build = self.engine.php_build_by_folder(folder)
        if build.cgi_exe is None:
            raise ServiceError(f"PHP {build.version} has no php-cgi.exe, so Apache can't use it")
        update_settings_file(self.paths.settings_file, default_php=build.version)
        self.engine.settings = replace(self.engine.settings, default_php=build.version)
        message = self.manager.reload("apache")
        return f"Default PHP is now {build.label} (Apache {message})"

    def save_php_config(self, folder: str, extensions: dict[str, bool], ini: dict[str, str]) -> str:
        from varsha.php.config import PhpOverrides, validate_extensions, validate_ini

        build = self.engine.php_build_by_folder(folder)
        try:
            overrides = PhpOverrides(validate_extensions(extensions), validate_ini(ini))
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc
        available = {e["name"] for e in self.engine.php_extensions(build)}
        unknown = set(overrides.extensions) - available
        if unknown:
            raise ServiceError(f"PHP {build.version} has no extension {', '.join(sorted(unknown))}")
        # Keep only real changes from the defaults, so future default changes still apply.
        defaults = {e["name"]: e["default"] for e in self.engine.php_extensions(build)}
        overrides = PhpOverrides({k: v for k, v in overrides.extensions.items() if defaults.get(k) != v}, overrides.ini)
        self.engine.php_config.save(folder, overrides)
        warnings = self.engine.check_php(build)
        reload = self.manager.reload("apache")
        if warnings:
            raise ServiceError(f"Saved, but PHP {build.version} reports:\n" + "\n".join(warnings[:6]))
        return f"PHP {build.version} settings saved; Apache {reload}"

    # ------------------------------------------------------------------ sites

    def set_site_php(self, domain: str, version: str | None) -> str:
        site = self.site(domain)
        if version:
            build = self.engine.find_php(version)
            if build is None:
                raise ServiceError(f"No PHP {version} with php-cgi.exe in bin\\php")
        try:
            save_override(self.paths.config / "sites.json", site.folder.name, php=version or None)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc
        return f"{domain} now uses PHP {version or 'default'}"

    def delete_site(self, domain: str) -> str:
        """Move the site's folder to the Windows Recycle Bin (recoverable). Databases stay."""
        site = self.site(domain)
        link = self.paths.www / site.folder.name
        if is_link(link):
            # A junction (Quick Create "Link a folder", or mklink /J): remove the link only.
            # The project folder it points to is never touched, so nothing goes to the Recycle Bin.
            target = link_target(link)
            os.rmdir(link)  # on a junction or symlink this deletes the link, not the target
            save_override(self.paths.config / "sites.json", site.folder.name, php=None, docroot=None)
            return f"{site.folder.name} unlinked. The project folder {target} was not touched."
        folder = site.folder.resolve()
        if folder.parent != self.paths.www.resolve():
            raise ServiceError("Only folders directly inside www\\ can be deleted")
        if not send_to_recycle_bin(folder):
            raise ServiceError(f"Windows couldn't move {folder.name} to the Recycle Bin (a file may be open)")
        save_override(self.paths.config / "sites.json", site.folder.name, php=None, docroot=None)
        return f"{site.folder.name} moved to the Recycle Bin. Its database (if any) was kept."

    # ------------------------------------------------------------------ terminal

    def write_shims(self, build: PhpBuild) -> Path:
        """etc/shims: `composer` for the chosen PHP, plus the script that prepares a terminal."""
        shims = self.paths.etc / "shims"
        shims.mkdir(parents=True, exist_ok=True)
        phar = self.paths.bin / "composer" / "composer.phar"
        (shims / "composer.cmd").write_text(f'@"{build.php_exe}" "{phar}" %*\r\n', encoding="utf-8")
        return shims

    def terminal_script(self, build: PhpBuild, title: str) -> Path:
        shims = self.write_shims(build)
        parts = [str(build.folder), str(shims)]
        mysql_bin = self.mysql_bin()
        if mysql_bin:
            parts.append(str(mysql_bin))
        script = shims / "varsha-env.cmd"
        script.write_text(
            "@echo off\r\n"
            f'set "PATH={";".join(parts)};%PATH%"\r\n'
            f'set "COMPOSER_HOME={self.paths.data / "composer"}"\r\n'
            f"title varsha - {title}\r\n"
            f"echo varsha terminal: PHP {build.version}, composer{', mysql' if mysql_bin else ''} on PATH\r\n",
            encoding="utf-8")
        return script

    def open_terminal(self, domain: str | None = None) -> str:
        site = self.site(domain) if domain else None
        build = self.php_for(site)
        cwd = site.folder if site else self.paths.www
        script = self.terminal_script(build, site.domain if site else "www")
        # The PATH is set by the script inside the shell, not inherited: a running Windows
        # Terminal opens new tabs with *its* environment, not the caller's.
        wt = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
        if wt.exists():
            subprocess.Popen([str(wt), "-w", "new", "-d", str(cwd), "cmd", "/k", str(script)], close_fds=True)
        else:
            subprocess.Popen(["cmd.exe", "/k", str(script)], cwd=str(cwd), creationflags=CREATE_NEW_CONSOLE,
                             close_fds=True)
        return f"Terminal opened in {cwd.name} with PHP {build.version}"

    # ------------------------------------------------------------------ Quick Create

    def create(self, kind: str, name: str, db_service: str | None = None, php: str | None = None,
               log: Path | None = None, source: str | None = None) -> str:
        if kind not in KINDS:
            raise ServiceError(f"Unknown site type {kind!r}")
        slug = slugify(name)
        if not slug or slug != name.lower():
            raise ServiceError(f'Use a simple folder name like "shop" or "client-site" (got "{name}")')
        folder = self.paths.www / slug
        source_dir = self.link_source(source) if kind == "link" else None
        if folder.exists() or is_link(folder):
            raise ServiceError(f"www\\{slug} already exists")
        build = self.engine.find_php(php) if php else self.engine.default_php()
        if build is None:
            raise ServiceError("No usable PHP build in bin\\php")
        log = log or self.paths.logs / "create.log"
        db_name = re.sub(r"[^a-z0-9_]", "_", slug)
        db = self._db_details(db_service, db_name) if db_service else None
        self._log(log, f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} creating {kind} site {slug}.{self.engine.settings.tld}")
        try:
            if kind == "blank":
                self._create_blank(folder, slug, build, db)
            elif kind == "link":
                assert source_dir is not None
                self.paths.www.mkdir(parents=True, exist_ok=True)
                make_junction(source_dir, folder)
                self._log(log, f"linked www\\{slug} -> {source_dir}")
            else:
                self._create_wordpress(folder, db, log)
        except Exception:
            self._log(log, "failed; partial files were left in place for inspection" if folder.exists() else "failed")
            raise
        if php:
            save_override(self.paths.config / "sites.json", slug, php=php)
        if db:
            self._create_database(db)
        self._log(log, "done")
        extra = f", database {db['name']} on {db['service_name']}" if db else ""
        what = f"linked to {source_dir}" if source_dir else kind
        return f"{slug}.{self.engine.settings.tld} created ({what}{extra})"

    def link_source(self, source: str | None) -> Path:
        """The folder a linked site points to: an existing folder outside varsha's own root."""
        text = (source or "").strip().strip('"')
        if not text:
            raise ServiceError("Choose the project folder to link")
        path = Path(text)
        if not path.is_absolute():
            raise ServiceError(f"Give the full path of the folder, like D:\\Projects\\shop (got {text})")
        if not path.is_dir():
            raise ServiceError(f"{text} isn't a folder")
        resolved = path.resolve()
        if resolved.parent == resolved:
            raise ServiceError("Link a project folder, not a whole drive")
        root = self.paths.root.resolve()
        if resolved == root or root in resolved.parents or resolved in root.parents:
            raise ServiceError("That folder is part of varsha itself; link a project folder from elsewhere")
        return resolved

    def _log(self, log: Path, line: str) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _db_details(self, service_id: str, name: str) -> dict[str, Any]:
        defn = self.manager.definition(service_id)
        if not defn.is_database:
            raise ServiceError(f"{defn.name} is not a database")
        if self.manager.status(service_id).state is not ServiceState.RUNNING:
            raise ServiceError(f"Start {defn.name} first, so the site's database can be created")
        return {"service": service_id, "service_name": defn.name, "name": name, "host": "127.0.0.1",
                "port": defn.ports[0], "user": "root", "password": self.manager.credentials.get(service_id)}

    def _create_database(self, db: dict[str, Any]) -> None:
        client = self.manager.expand_tool(db["service"], "client")
        with client_option_file(db["port"], db["password"]) as cnf:
            result = proc_utils.run_quiet(
                [str(client), f"--defaults-extra-file={cnf.as_posix()}", "-e",
                 f"CREATE DATABASE IF NOT EXISTS `{db['name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"],
                timeout=60)
        if result.returncode != 0:
            raise ServiceError(f"Creating database {db['name']} failed: {(result.stderr or result.stdout).strip()}")

    def _create_blank(self, folder: Path, slug: str, build: PhpBuild, db: dict[str, Any] | None) -> None:
        public = folder / "public"
        public.mkdir(parents=True)
        db_line = (f"$db = new mysqli('127.0.0.1', 'root', getenv('DB_PASSWORD') ?: '', '{db['name']}', {db['port']});\n"
                   if db else "")
        (public / "index.php").write_text(BLANK_INDEX.replace("{{slug}}", slug).replace("{{db}}", db_line), encoding="utf-8")

    def _create_wordpress(self, folder: Path, db: dict[str, Any] | None, log: Path) -> None:
        self._log(log, f"downloading {WORDPRESS_ZIP}")
        request = urllib.request.Request(WORDPRESS_ZIP, headers={"User-Agent": "varsha-quick-create"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        extract_wordpress(data, folder)
        self._log(log, f"extracted {len(data) // 1024} KB")
        if db:
            write_wp_config(folder, db)


def extract_wordpress(data: bytes, folder: Path) -> None:
    """Unpack wordpress/ from the official zip into `folder`, refusing paths that escape it."""
    folder.mkdir(parents=True)
    root = folder.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            name = member.filename
            if not name.startswith("wordpress/") or name == "wordpress/":
                continue
            target = (folder / name[len("wordpress/"):]).resolve()
            if not target.is_relative_to(root):
                raise ServiceError(f"Unsafe path in the WordPress zip: {name}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)


def write_wp_config(folder: Path, db: dict[str, Any]) -> None:
    """wp-config.php from WordPress's own sample, with the DB settings and fresh salts
    (generated here rather than fetched from api.wordpress.org)."""
    sample = (folder / "wp-config-sample.php").read_text(encoding="utf-8")

    def php(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    for key, value in (("DB_NAME", db["name"]), ("DB_USER", db["user"]), ("DB_PASSWORD", db["password"]),
                       ("DB_HOST", f"{db['host']}:{db['port']}")):
        sample = re.sub(rf"define\(\s*'{key}',\s*'[^']*'\s*\);", f"define( '{key}', {php(value)} );", sample)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()-_[]{}<>~+=,.;:/?|"
    sample = re.sub(r"'put your unique phrase here'",
                    lambda _m: php("".join(secrets.choice(alphabet) for _ in range(64))), sample)
    (folder / "wp-config.php").write_text(sample, encoding="utf-8")


def is_link(path: Path) -> bool:
    """A junction (mklink /J) or a symbolic link, even one whose target is gone."""
    try:
        return path.is_junction() or path.is_symlink()
    except OSError:
        return False


def link_target(path: Path) -> str:
    try:
        return os.readlink(path).removeprefix("\\\\?\\")
    except OSError:
        return "(unknown)"


def make_junction(source: Path, link: Path) -> None:
    """mklink /J without a console: junctions need no admin rights and work across drives."""
    import _winapi  # Windows only; part of the standard library

    try:
        _winapi.CreateJunction(str(source), str(link))
    except OSError as exc:
        raise ServiceError(f"Couldn't create the link {link.name} -> {source}: {exc}") from exc


def send_to_recycle_bin(path: Path) -> bool:
    """SHFileOperation with FOF_ALLOWUNDO: the folder lands in the Recycle Bin, restorable."""

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    fo_delete, allow_undo, no_confirm, silent, no_error_ui = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path) + "\0", None,
                         allow_undo | no_confirm | silent | no_error_ui, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted and not path.exists()


BLANK_INDEX = """<?php
// {{slug}} - created by varsha Quick Create.
{{db}}?><!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{slug}}</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f4f0e6; color: #121212;
           font: 16px/1.5 "Bahnschrift", "Segoe UI", sans-serif; }
    main { padding: 32px 40px; background: #fff; border: 3px solid #121212; border-radius: 10px; box-shadow: 8px 8px 0 #121212; }
    h1 { margin: 0 0 8px; font-size: 40px; letter-spacing: -.02em; }
    code { background: #ffd23f; padding: 1px 6px; border: 2px solid #121212; border-radius: 5px; }
  </style>
</head>
<body>
  <main>
    <h1>{{slug}}</h1>
    <p>Running on PHP <code><?= PHP_VERSION ?></code>. Edit <code>public/index.php</code> to start.</p>
  </main>
</body>
</html>
"""
