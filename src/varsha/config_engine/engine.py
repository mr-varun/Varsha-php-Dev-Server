"""Render etc/templates/*.j2 into real config files with absolute paths, at runtime.

Absolute paths are computed when a service starts, so moving the varsha folder just means the
next start writes configs for the new location. Generated files are only rewritten when their
content changes, and the previous version is kept as <file>.bak.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import subprocess

import jinja2

from varsha.errors import ConfigError
from varsha.paths import AppPaths
from varsha.php.config import PhpConfigStore
from varsha.php.versions import PhpBuild, find_php_builds
from varsha.services.process import CREATE_NO_WINDOW
from varsha.services.remote import RemoteDbStore
from varsha.sites.discovery import Site, discover_sites, load_overrides
from varsha.services.models import Installation, ServiceDef
from varsha.services.resolve import build_context, find_installation, version_key
from varsha.settings import Settings

logger = logging.getLogger(__name__)

# Apache modules varsha loads, in order; ones missing from the build are skipped.
APACHE_MODULES: tuple[tuple[str, str], ...] = (
    ("authn_core_module", "mod_authn_core.so"),
    ("authn_file_module", "mod_authn_file.so"),
    ("auth_basic_module", "mod_auth_basic.so"),
    ("authz_core_module", "mod_authz_core.so"),
    ("authz_host_module", "mod_authz_host.so"),
    ("authz_user_module", "mod_authz_user.so"),
    ("authz_groupfile_module", "mod_authz_groupfile.so"),
    ("access_compat_module", "mod_access_compat.so"),  # old "Order allow,deny" .htaccess files
    ("alias_module", "mod_alias.so"),
    ("autoindex_module", "mod_autoindex.so"),
    ("deflate_module", "mod_deflate.so"),
    ("dir_module", "mod_dir.so"),
    ("env_module", "mod_env.so"),
    ("expires_module", "mod_expires.so"),
    ("filter_module", "mod_filter.so"),
    ("headers_module", "mod_headers.so"),
    ("log_config_module", "mod_log_config.so"),
    ("mime_module", "mod_mime.so"),
    ("negotiation_module", "mod_negotiation.so"),
    ("rewrite_module", "mod_rewrite.so"),
    ("setenvif_module", "mod_setenvif.so"),
    ("socache_shmcb_module", "mod_socache_shmcb.so"),
    ("ssl_module", "mod_ssl.so"),
    ("substitute_module", "mod_substitute.so"),  # LAN sharing rewrites the site's own links
    ("fcgid_module", "mod_fcgid.so"),
)

GENERATED_MARK = "# varsha-generated"

# PHP extensions enabled when the DLL exists. mbstring must load before exif on PHP 5.
PHP_EXTENSIONS: tuple[str, ...] = (
    "mbstring", "openssl", "curl", "mysqli", "pdo_mysql", "pdo_sqlite", "sqlite3", "fileinfo",
    "gd", "gd2", "intl", "zip", "sodium", "exif", "gettext", "soap", "sockets", "xsl",
    "mysql",  # PHP 5 only: the old mysql_* functions legacy apps (like the banking one) need
)
ZEND_EXTENSIONS = frozenset({"opcache", "xdebug"})
HIDDEN_EXTENSIONS = frozenset({"dl_test", "zend_test"})  # PHP's own test modules

DB_LABELS = {"mariadb": "MariaDB", "mysql": "MySQL"}


@dataclass(frozen=True)
class PhpContext:
    label: str
    version: str
    major: int
    minor: int
    folder: str
    cgi: str
    ini_dir: str
    extension_dir: str


@dataclass(frozen=True)
class CertPair:
    cert: str
    key: str


@dataclass(frozen=True)
class DbServer:
    index: int
    name: str
    label: str
    host: str
    port: int
    service_id: str | None  # None for extra servers that varsha doesn't run
    remote_id: str | None = None  # set for config/remote-databases.json entries
    user: str = ""
    ssl: bool = False
    ssl_verify: bool = False
    ssl_ca: str | None = None
    auto_login: bool = False  # auth_type "config" with the saved password

    @property
    def is_remote(self) -> bool:
        return self.remote_id is not None


def php_str(value: object) -> str:
    """A PHP single-quoted string literal."""
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


class ConfigEngine:
    def __init__(self, paths: AppPaths, settings: Settings, services: Sequence[ServiceDef],
                 remote: RemoteDbStore | None = None, php_config: PhpConfigStore | None = None) -> None:
        self.paths = paths
        self.settings = settings
        self.services = list(services)
        self.remote = remote
        self.php_config = php_config or PhpConfigStore(paths.config / "php.json")
        self.templates = paths.templates
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.templates)),
            undefined=jinja2.StrictUndefined,  # a typo in a template fails loudly
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )
        self._env.filters["php_str"] = php_str

    # ------------------------------------------------------------------ hook for ServiceManager

    def prepare(self, defn: ServiceDef, inst: Installation) -> None:
        """Called by ServiceManager right before a service starts."""
        for folder in (self.paths.data / "tmp" / "sessions", self.paths.data / "apache",
                       self.paths.etc / "apache" / "sites-enabled"):
            folder.mkdir(parents=True, exist_ok=True)
        if defn.type == "apache":
            self.generate_php_inis()
            self.generate_phpmyadmin()
            self.generate_sites(defn)
        if defn.config_template and defn.config_file:
            context = self.service_context(defn, inst)
            self.render(defn.config_template, context, self.paths.root / defn.config_file)

    # ------------------------------------------------------------------ rendering

    def render(self, template: str, context: dict[str, Any], output: Path) -> bool:
        """Render `template` to `output`. Returns True if the file changed."""
        try:
            text = self._env.get_template(template).render(**context)
        except jinja2.TemplateNotFound as exc:
            raise ConfigError(f"Template not found: {self.templates / template}") from exc
        except jinja2.TemplateError as exc:
            raise ConfigError(f"Template {template} failed: {exc}") from exc
        return write_if_changed(output, text)

    def service_context(self, defn: ServiceDef, inst: Installation) -> dict[str, Any]:
        context = build_context(self.paths, defn, inst)
        major, minor = (version_key(inst.version) + (0, 0))[:2]
        context.update(
            name=defn.name,
            type=defn.type,
            major=major,
            minor=minor,
            is_mariadb=defn.type == "mariadb",
            system_root=os.environ.get("SystemRoot", "C:/Windows").replace("\\", "/"),
        )
        if defn.type == "apache":
            build = self.default_php()
            context["php"] = self.php_context(build) if build and build.cgi_exe else None
            pma = self.paths.bin / "phpmyadmin"
            context["phpmyadmin"] = pma.as_posix() if (pma / "index.php").is_file() else None
            modules_dir = Path(str(context["home"])) / "modules"
            context["modules"] = [
                {"name": name, "path": (modules_dir / filename).as_posix()}
                for name, filename in APACHE_MODULES if (modules_dir / filename).is_file()
            ]
            context["tld"] = self.settings.tld
            context["https_port"] = defn.ports[1] if len(defn.ports) > 1 else None
            has_ssl_module = (modules_dir / "mod_ssl.so").is_file()
            localhost = self.ensure_cert("localhost", ["localhost", "127.0.0.1", "::1"]) if has_ssl_module else None
            context["localhost_cert"] = localhost
            context["ssl_enabled"] = localhost is not None
        return context

    # ------------------------------------------------------------------ sites + HTTPS

    @property
    def mkcert_exe(self) -> Path:
        return self.paths.bin / "mkcert" / "mkcert.exe"

    @property
    def ssl_dir(self) -> Path:
        return self.paths.data / "ssl"

    def sites(self) -> list[Site]:
        overrides = load_overrides(self.paths.config / "sites.json")
        return discover_sites(self.paths.www, self.settings.tld, overrides)

    def ensure_cert(self, stem: str, names: list[str]) -> CertPair | None:
        """A certificate for `names` signed by the local mkcert CA, reused while names match.

        mkcert keeps its CA in the user's own %LOCALAPPDATA%\\mkcert (CAROOT), not in the
        varsha folder: the CA key can sign certificates for any site, so it stays private to
        this Windows account. Returns None when mkcert isn't installed (HTTP only).
        """
        if not self.mkcert_exe.is_file():
            return None
        self.ssl_dir.mkdir(parents=True, exist_ok=True)
        cert, key = self.ssl_dir / f"{stem}.pem", self.ssl_dir / f"{stem}-key.pem"
        meta = self.ssl_dir / f"{stem}.names"
        wanted = "\n".join(names)
        try:
            if cert.is_file() and key.is_file() and meta.read_text(encoding="utf-8") == wanted:
                return CertPair(cert.as_posix(), key.as_posix())
        except OSError:
            pass
        result = subprocess.run(
            [str(self.mkcert_exe), "-cert-file", str(cert), "-key-file", str(key), *names],
            capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        if result.returncode != 0 or not cert.is_file():
            logger.warning("mkcert failed for %s: %s", stem, (result.stderr or result.stdout).strip())
            return None
        meta.write_text(wanted, encoding="utf-8")
        logger.info("Created certificate for %s", ", ".join(names))
        return CertPair(cert.as_posix(), key.as_posix())

    def generate_sites(self, apache: ServiceDef) -> bool:
        """Write one vhost file per site and delete varsha's files for removed folders.
        Returns True if anything changed (so a running Apache needs a reload)."""
        folder = self.paths.etc / "apache" / "sites-enabled"
        folder.mkdir(parents=True, exist_ok=True)
        http_port = apache.ports[0] if apache.ports else 80
        https_port = apache.ports[1] if len(apache.ports) > 1 else None
        has_ssl = https_port is not None and self.mkcert_exe.is_file()
        changed = False
        expected: set[str] = set()
        default = self.default_php()
        for site in self.sites():
            pair = self.ensure_cert(site.domain, [site.domain, *site.aliases]) if has_ssl else None
            build = self.find_php(site.php) if site.php else None
            if site.php and build is None:
                logger.warning("%s wants PHP %s, which isn't in bin\\php; using the default", site.domain, site.php)
            site_php = self.php_context(build) if build and (default is None or build.folder != default.folder) else None
            context = {"site": site, "docroot": site.docroot.as_posix(), "http_port": http_port,
                       "https_port": https_port, "cert": pair.cert if pair else None,
                       "key": pair.key if pair else None, "php": site_php,
                       "system_root": os.environ.get("SystemRoot", "C:/Windows").replace("\\", "/"),
                       "data": self.paths.data.as_posix()}
            target = folder / f"{site.domain}.conf"
            expected.add(target.name)
            changed |= self.render("vhost.conf.j2", context, target)
        for old in folder.glob("*.conf"):
            if old.name in expected:
                continue
            try:
                if old.read_text(encoding="utf-8").startswith(GENERATED_MARK):
                    old.unlink()  # only files varsha wrote; hand-made vhosts stay
                    old.with_name(old.name + ".bak").unlink(missing_ok=True)
                    changed = True
                    logger.info("Removed vhost %s (folder gone)", old.name)
            except OSError as exc:
                logger.warning("Could not check %s: %s", old, exc)
        return changed

    @property
    def lan_dir(self) -> Path:
        return self.paths.etc / "apache" / "lan-enabled"

    def generate_lan(self, shares: dict[str, int], address: str | None, cert_names: Sequence[str]) -> bool:
        """Write one LAN vhost per shared site ({domain: port}) and delete every other LAN file.
        An empty `shares` (or no address) removes them all. Returns True if anything changed."""
        folder = self.lan_dir
        expected: set[str] = set()
        changed = False
        if shares and address:
            folder.mkdir(parents=True, exist_ok=True)
            pair = self.ensure_cert("lan", list(cert_names))
            if pair is None:
                raise ConfigError("Sharing on the LAN needs HTTPS: put mkcert at bin\\mkcert\\mkcert.exe")
            sites = {site.domain: site for site in self.sites()}
            default = self.default_php()
            for domain, port in sorted(shares.items()):
                site = sites.get(domain)
                if site is None:
                    continue
                build = self.find_php(site.php) if site.php else None
                site_php = self.php_context(build) if build and (default is None or build.folder != default.folder) else None
                context = {"site": site, "docroot": site.docroot.as_posix(), "port": port, "address": address,
                           "domain_re": re.escape(site.domain), "cert": pair.cert, "key": pair.key, "php": site_php,
                           "system_root": os.environ.get("SystemRoot", "C:/Windows").replace("\\", "/"),
                           "data": self.paths.data.as_posix()}
                target = folder / f"{domain}.conf"
                expected.add(target.name)
                changed |= self.render("lan.conf.j2", context, target)
        if folder.is_dir():
            for old in folder.glob("*.conf*"):
                if old.name in expected:
                    continue
                try:
                    if old.suffix == ".bak" or old.read_text(encoding="utf-8").startswith(GENERATED_MARK):
                        old.unlink()
                        changed |= old.suffix == ".conf"
                except OSError as exc:
                    logger.warning("Could not remove %s: %s", old, exc)
        return changed

    # ------------------------------------------------------------------ PHP

    def php_builds(self) -> list[PhpBuild]:
        return find_php_builds(self.paths.bin / "php", self.paths.root)

    def default_php(self) -> PhpBuild | None:
        """Newest build matching settings.default_php ("8.3" matches 8.3.x), preferring x64
        builds with php-cgi.exe; otherwise the newest usable build."""
        builds = [b for b in self.php_builds() if b.cgi_exe]
        wanted = self.settings.default_php
        matching = [b for b in builds if b.version == wanted or b.version.startswith(wanted + ".")]
        for pool in (matching, builds):
            if pool:
                return sorted(pool, key=lambda b: (b.arch == "x64", version_key(b.version)))[-1]
        return None

    def php_ini_dir(self, build: PhpBuild) -> Path:
        return self.paths.etc / "php" / build.folder.name

    def php_context(self, build: PhpBuild) -> PhpContext:
        major, minor = (version_key(build.version) + (0, 0))[:2]
        ext_dir = build.folder / "ext"
        # PHP Desktop builds (like the banking one) keep their extensions next to php.exe.
        extension_dir = ext_dir if ext_dir.is_dir() else build.folder
        return PhpContext(
            label=build.label,
            version=build.version,
            major=major,
            minor=minor,
            folder=build.folder.as_posix(),
            cgi=(build.cgi_exe or build.php_exe).as_posix(),
            ini_dir=self.php_ini_dir(build).as_posix(),
            extension_dir=extension_dir.as_posix(),
        )

    def find_php(self, wanted: str | None) -> PhpBuild | None:
        """The build for a version prefix ("8.3" -> newest 8.3.x, x64 preferred); None = default."""
        if not wanted:
            return self.default_php()
        builds = [b for b in self.php_builds() if b.cgi_exe and (b.version == wanted or b.version.startswith(wanted + "."))]
        if not builds:
            return None
        return sorted(builds, key=lambda b: (b.arch == "x64", version_key(b.version)))[-1]

    def php_build_by_folder(self, folder: str) -> PhpBuild:
        for build in self.php_builds():
            if build.folder.name == folder:
                return build
        raise ConfigError(f"No PHP build in bin\\php\\{folder}")

    def php_extensions(self, build: PhpBuild) -> list[dict[str, Any]]:
        """Every extension DLL the build ships, with its on/off state (default or overridden)."""
        ext_dir = Path(self.php_context(build).extension_dir)
        names = sorted({p.stem[4:].lower() for p in ext_dir.glob("php_*.dll")} - HIDDEN_EXTENSIONS)
        overrides = self.php_config.get(build.folder.name).extensions
        defaults = set(PHP_EXTENSIONS)
        if "gd" in names:
            defaults.discard("gd2")  # one GD is enough; PHP 8 calls it gd, PHP 5 gd2
        # mbstring first: on PHP 5 exif refuses to load unless mbstring is already loaded.
        names.sort(key=lambda n: (n != "mbstring", n))
        return [{"name": n, "zend": n in ZEND_EXTENSIONS, "default": n in defaults,
                 "enabled": overrides.get(n, n in defaults)} for n in names]

    def generate_php_inis(self) -> None:
        """One php.ini per PHP build, in etc/php/<build folder>/php.ini."""
        for build in self.php_builds():
            self.generate_php_ini(build)

    def generate_php_ini(self, build: PhpBuild) -> Path:
        php = self.php_context(build)
        exts = self.php_extensions(build)
        context = build_context_for_globals(self.paths)
        context.update(
            php=php,
            extensions=[f"php_{e['name']}.dll" for e in exts if e["enabled"] and not e["zend"]],
            zend_extensions=[f"php_{e['name']}.dll" for e in exts if e["enabled"] and e["zend"]],
            overrides=self.php_config.get(build.folder.name).ini,
            timezone=self.settings.php_timezone, smtp_port=self._mailpit_smtp_port(),
            cacert=(self.paths.bin / "ca" / "cacert.pem").as_posix())
        target = self.php_ini_dir(build) / "php.ini"
        self.render("php.ini.j2", context, target)
        return target

    def check_php(self, build: PhpBuild) -> list[str]:
        """Start php.exe once with the generated php.ini and return any startup warnings
        (e.g. an extension DLL that fails to load)."""
        ini = self.generate_php_ini(build)
        result = subprocess.run([str(build.php_exe), "-c", str(ini.parent), "-r", "echo 'ok';"],
                                capture_output=True, text=True, errors="replace", timeout=30,
                                creationflags=CREATE_NO_WINDOW)
        lines = (result.stdout + "\n" + result.stderr).splitlines()
        return [line.strip() for line in lines if "Warning" in line or "Startup" in line or "Fatal" in line]

    def _mailpit_smtp_port(self) -> int:
        for defn in self.services:
            if defn.type == "mail" and defn.ports:
                return defn.ports[0]
        return 1025

    # ------------------------------------------------------------------ phpMyAdmin

    def db_servers(self) -> list[DbServer]:
        """Servers phpMyAdmin lists: varsha's databases (by port), then the extra servers."""
        servers: list[DbServer] = []
        databases = sorted((d for d in self.services if d.enabled and d.is_database and d.ports),
                           key=lambda d: d.ports[0])
        for defn in databases:
            inst = find_installation(self.paths.root, defn) if defn.exe else None
            if inst is None:
                continue
            kind = DB_LABELS.get(defn.type, defn.type)
            # "MariaDB 11.8.9", "MySQL 5.6.12 (32-bit)": the exact build, not the services.json name.
            name = f"{kind} {inst.version}" if inst.version else defn.name
            if "32-bit" in defn.name:
                name += " (32-bit)"
            label = f"{name} · port {defn.ports[0]}"
            servers.append(DbServer(len(servers) + 1, name, label, "127.0.0.1", defn.ports[0], defn.id))
        taken = {s.port for s in servers}
        for extra in self.settings.phpmyadmin_extra_servers:
            if extra.port in taken and extra.host == "127.0.0.1":
                continue
            servers.append(DbServer(len(servers) + 1, extra.name,
                                    f"{extra.name} · port {extra.port}", extra.host, extra.port, None))
        for remote in (self.remote.list() if self.remote else []):
            servers.append(DbServer(
                len(servers) + 1, remote.name, f"{remote.name} · {remote.host}:{remote.port} (remote)",
                remote.host, remote.port, None, remote_id=remote.id, user=remote.user, ssl=remote.ssl,
                ssl_verify=remote.ssl_verify, ssl_ca=Path(remote.ssl_ca).as_posix() if remote.ssl_ca else None,
                auto_login=remote.remember and self.remote.has_password(remote)))
        return servers

    def phpmyadmin_remote_index(self, remote_id: str) -> int | None:
        for server in self.db_servers():
            if server.remote_id == remote_id:
                return server.index
        return None

    def phpmyadmin_server_index(self, service_id: str) -> int | None:
        for server in self.db_servers():
            if server.service_id == service_id:
                return server.index
        return None

    def generate_phpmyadmin(self) -> None:
        pma = self.paths.bin / "phpmyadmin"
        if not (pma / "index.php").is_file():
            return
        temp_dir = self.paths.data / "phpmyadmin" / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        servers = self.db_servers()
        secrets_file = self.paths.data / "phpmyadmin" / "remote-secrets.php"
        self._write_remote_secrets(secrets_file, [s for s in servers if s.auto_login])
        context = {
            "servers": servers,
            "blowfish_secret": self._blowfish_secret(),
            "temp_dir": temp_dir.as_posix() + "/",
            "secrets_file": secrets_file.as_posix(),
            "servers_php": _php_array(
                [{"index": s.index, "name": s.name, "host": s.host, "port": s.port,
                  "remote": 1 if s.is_remote else 0, "auto": 1 if s.auto_login else 0} for s in servers]),
        }
        self.render("phpmyadmin.config.inc.php.j2", context, pma / "config.inc.php")
        self.render("phpmyadmin.footer.inc.php.j2", context, pma / "config.footer.inc.php")

    def _write_remote_secrets(self, path: Path, servers: list[DbServer]) -> None:
        """Saved remote passwords for phpMyAdmin's auto-login, in a PHP file only this Windows
        user can read. Removed when no remote server uses auto-login."""
        if not servers or self.remote is None:
            path.unlink(missing_ok=True)
            return
        entries = {}
        for server in servers:
            remote = self.remote.get(server.remote_id or "")
            entries[server.remote_id] = self.remote.password(remote)
        body = "<?php\n// Generated by varsha. Readable only by this Windows user. Do not share.\nreturn [\n"
        body += "".join(f"    {php_str(key)} => {php_str(value)},\n" for key, value in entries.items())
        body += "];\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.write_text(body, encoding="utf-8")
        restrict_to_current_user(path)

    def _blowfish_secret(self) -> str:
        """32 random characters, kept in data/ so logins survive regeneration."""
        path = self.paths.data / "phpmyadmin" / "blowfish_secret.txt"
        try:
            secret = path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[A-Za-z0-9]{32}", secret):
                return secret
        except (OSError, UnicodeDecodeError):
            pass
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        secret = "".join(secrets.choice(alphabet) for _ in range(32))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="ascii")
        return secret


def restrict_to_current_user(path: Path) -> None:
    """Drop inherited permissions so only the current user (and SYSTEM) can open the file.
    C:\\Varsha is usually readable by every local account; this file holds passwords."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    account = f"{domain}\\{user}" if domain else user
    if not user:
        return
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F", "/grant:r", "*S-1-5-18:F"],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        logger.warning("Could not restrict %s: %s", path, (result.stderr or result.stdout).strip())


def build_context_for_globals(paths: AppPaths) -> dict[str, Any]:
    return {
        "root": paths.root.as_posix(),
        "bin": paths.bin.as_posix(),
        "etc": paths.etc.as_posix(),
        "data": paths.data.as_posix(),
        "www": paths.www.as_posix(),
        "logs": paths.logs.as_posix(),
    }


def _php_array(rows: list[dict[str, object]]) -> str:
    def literal(value: object) -> str:
        return str(value) if isinstance(value, int) else php_str(value)

    items = ["[" + ", ".join(f"{php_str(k)} => {literal(v)}" for k, v in row.items()) + "]" for row in rows]
    return "[\n    " + ",\n    ".join(items) + "\n]" if items else "[]"


def write_if_changed(path: Path, text: str) -> bool:
    """Write `text` to `path` unless it's identical; back up a differing old file to .bak."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # newline="\n": Apache and MySQL don't mind, and it keeps diffs against the templates clean.
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)
    logger.info("Wrote %s", path)
    return True


__all__ = ["ConfigEngine", "DbServer", "PhpContext", "write_if_changed"]
