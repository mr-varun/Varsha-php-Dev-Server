"""PanelApp: everything the control panel can see and do, without any HTTP details.

The HTTP layer (server.py) maps requests onto these methods. Long actions run as tasks on
a thread pool; their outcome is published on the EventBus as a "toast", and every state
change as "state", which the page answers by re-reading /api/state.
"""

from __future__ import annotations

import collections
import itertools
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from varsha import __version__
from varsha.config_engine.engine import ConfigEngine
from varsha.devtools import DevTools
from varsha.doctor import Doctor
from varsha.errors import ConfigError, ServiceError, VarshaError
from varsha.mail.mailpit import MailpitClient, MailWatcher
from varsha.paths import AppPaths
from varsha.php.errors import PhpErrorWatcher
from varsha.schedule import Scheduler
from varsha.services import process as proc_utils
from varsha.services.database import DatabaseTools
from varsha.services.importer import Importer
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState
from varsha.services.remote import RemoteDbStore, test_connection, validate
from varsha.services.resolve import expand
from varsha.settings import Settings
from varsha.sites import hosts
from varsha.sites.manager import SiteManager
from varsha.tunnel.manager import TunnelManager

logger = logging.getLogger(__name__)

LOG_CHUNK = 256 * 1024


class EventBus:
    """Fan-out of small events to every open panel (Server-Sent Events)."""

    def __init__(self, keep: int = 200) -> None:
        self._cond = threading.Condition()
        self._events: collections.deque[tuple[int, str, Any]] = collections.deque(maxlen=keep)
        self._seq = 0
        self._closed = False

    def publish(self, kind: str, data: Any = None) -> None:
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, kind, data))
            self._cond.notify_all()

    @property
    def last(self) -> int:
        return self._seq

    def wait(self, after: int, timeout: float) -> list[tuple[int, str, Any]]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq > after or self._closed, timeout=timeout)
            return [event for event in self._events if event[0] > after]

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed


class PanelApp:
    def __init__(self, paths: AppPaths, settings: Settings, manager: ServiceManager,
                 engine: ConfigEngine, sites: SiteManager, tunnels: TunnelManager,
                 remote: RemoteDbStore | None = None, on_quit: Callable[[bool], None] | None = None) -> None:
        self.paths = paths
        self.settings = settings
        self.manager = manager
        self.engine = engine
        self.sites = sites
        self.tunnels = tunnels
        self.databases = DatabaseTools(manager)
        self.remote = remote
        self.tools = DevTools(paths, engine, manager)
        self.events = EventBus()
        self.mail = MailpitClient(manager)
        # Counter changes and newly arrived mail go out as "mail" events (badge, list, tray balloon).
        self.mail_watch = MailWatcher(self.mail, lambda data: self.events.publish("mail", data))
        self.php_errors = PhpErrorWatcher(paths.logs, paths.config / "php-errors.json",
                                          site_roots=self._site_roots,
                                          on_event=lambda data: self.events.publish("php_error", data))
        self.importer = Importer(manager, self.databases, paths.data / "import",
                                 notify=lambda ok, title, message: self._job_done(ok, title, message, ""))
        self.scheduler = Scheduler(manager, self.databases, paths.config / "schedule.json",
                                   paths.data / "schedule-state.json", notify=self._job_done,
                                   prepare_task=self._prepare_task, logs_dir=paths.logs, background=True)
        self.doctor = Doctor(paths, manager, engine, sites, self.scheduler, self.databases,
                             dns_route=self.dns_route_active)
        self.on_quit = on_quit
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="panel-task")
        self._task_ids = itertools.count(1)
        self._busy: dict[int, str] = {}
        self._busy_lock = threading.Lock()
        self._dns_route_cache: tuple[float, bool] = (0.0, False)
        manager.set_listener(lambda _sid: self.events.publish("state"))
        sites.set_listener(lambda: self.events.publish("state"))
        tunnels.set_listener(lambda: self.events.publish("state"))

    # ------------------------------------------------------------------ tasks

    def run_task(self, title: str, fn: Callable[..., Any], *args: Any,
                 success: Callable[[Any], str] | None = None, service_id: str = "") -> int:
        task_id = next(self._task_ids)
        with self._busy_lock:
            self._busy[task_id] = title
        self.events.publish("state")

        def work() -> None:
            try:
                result = fn(*args)
                message = success(result) if success else ""
                self.events.publish("toast", {"ok": True, "title": title, "message": message})
            except VarshaError as exc:
                sid = getattr(exc, "service_id", None) or service_id
                self.events.publish("toast", {"ok": False, "title": title, "message": str(exc), "service": sid})
            except Exception as exc:
                logger.exception("%s failed", title)
                self.events.publish("toast", {"ok": False, "title": title, "message": f"Unexpected error: {exc}"})
            finally:
                with self._busy_lock:
                    self._busy.pop(task_id, None)
                self.events.publish("state")

        self._pool.submit(work)
        return task_id

    def _job_done(self, ok: bool, title: str, message: str, service_id: str) -> None:
        """A scheduled job finished: a toast in the panel (failures also become tray balloons)."""
        self.events.publish("toast", {"ok": ok, "title": title, "message": message, "service": "" if ok else service_id})
        self.events.publish("state")

    def _prepare_task(self, plan: Any) -> tuple[Path, dict[str, str], str]:
        """Folder, environment and PHP for a scheduled task: the same PATH as varsha's terminal."""
        site = self.tools.site(plan.site) if plan.site else None
        if plan.php:
            build = self.engine.find_php(plan.php)
            if build is None:
                raise ServiceError(f"No PHP {plan.php} in bin\\php")
        else:
            build = self.tools.php_for(site)
        if plan.where == "custom":
            cwd = Path(plan.cwd)
        else:
            assert site is not None  # parse_task guarantees a site unless the folder is custom
            cwd = site.folder
            real = site.folder.resolve()
            # Sites often point straight at a public\ folder (a junction): artisan lives one level up.
            if plan.where == "project" and real.name.lower() == "public":
                cwd = real.parent
        if not cwd.is_dir():
            raise ServiceError(f"The folder {cwd} doesn't exist")
        self.tools.write_shims(build)
        env = dict(os.environ)
        parts = [str(build.folder), str(self.paths.etc / "shims")]
        mysql = self.tools.mysql_bin()
        if mysql:
            parts.append(str(mysql))
        env["PATH"] = ";".join(parts + [env.get("PATH", "")])
        env["PHPRC"] = str(self.engine.php_ini_dir(build))
        env["COMPOSER_HOME"] = str(self.paths.data / "composer")
        env["VARSHA_TASK"] = plan.id
        return cwd, env, f"PHP {build.version}"

    def _site_roots(self) -> list[tuple[str, Path]]:
        try:
            return [(s.domain, s.folder) for s in self.sites.sites()]
        except Exception:  # a broken sites.json must not stop the error watcher
            return []

    def open_php_error(self, group_id: str) -> str:
        """Open the file of an error at its line: VS Code if installed, else Explorer on the file."""
        group = self.php_errors.group(group_id)
        target = Path(group.file)
        if not group.file or not target.is_file():
            raise ServiceError(f"The file {group.file or '(unknown)'} no longer exists")
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [local / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft VS Code" / "bin" / "code.cmd"]
        code = next((c for c in candidates if c.is_file()), None)
        if code is not None:
            subprocess.Popen(["cmd.exe", "/c", str(code), "-g", f"{target}:{max(1, group.line)}"],
                             creationflags=proc_utils.CREATE_NO_WINDOW, close_fds=True)
            return f"Opened {target.name}:{group.line} in VS Code"
        subprocess.Popen(["explorer.exe", f"/select,{target}"], close_fds=True)
        return f"Showing {target.name} in Explorer (VS Code not found)"

    def import_inspect(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("kind") == "folder":
            session = self.importer.inspect_folder(str(body.get("path") or ""))
        elif body.get("kind") == "server":
            session = self.importer.inspect_server(str(body.get("host") or ""), int(body.get("port") or 3306),
                                                   str(body.get("user") or ""), str(body.get("password") or ""))
        else:
            raise ValueError('kind must be "folder" or "server"')
        return session.public()

    def import_run(self, session_id: str, body: dict[str, Any]) -> None:
        names = body.get("databases") or []
        if not isinstance(names, list):
            raise ValueError("databases must be a list")
        session = self.importer.session(session_id)
        if session.state == "running":
            raise ServiceError("This import is already running")
        session.state = "running"  # visible at once; run() does the real checks
        self.events.publish("state")
        self._pool.submit(self._import_job, session_id, [str(n) for n in names],
                          str(body.get("target") or ""), str(body.get("mode") or "skip"))

    def _import_job(self, session_id: str, names: list[str], target: str, mode: str) -> None:
        session = self.importer.session(session_id)
        session.state = "ready"
        try:
            self.importer.run(session_id, names, target, mode)
        except Exception as exc:  # validation errors before the import starts
            session.state, session.error = "failed", str(exc)
            self._job_done(False, "Import", str(exc), "")

    def run_backup_now(self, service_id: str) -> None:
        self.manager.definition(service_id)  # unknown id -> KeyError -> 400
        self.events.publish("state")
        self._pool.submit(self.scheduler.run_now, service_id)

    def shutdown(self) -> None:
        self.php_errors.stop()
        self.scheduler.stop()
        self.mail_watch.stop()
        self.events.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        services = [self._service_json(status) for status in self.manager.statuses()]
        hosts_text = self.sites.hosts_text()
        wanted = self.sites.wanted_host_names()
        missing = [n for n in hosts.entries_for(wanted, hosts_text) if n not in hosts.current_names(hosts_text)]
        default_php = self.engine.default_php()
        with self._busy_lock:
            busy = list(self._busy.values())
        return {
            "version": __version__,
            "root": str(self.paths.root),
            "tld": self.settings.tld,
            "services": services,
            "sites": [self._site_json(info) for info in self.sites.site_infos()],
            "site_error": self.sites.last_error,
            "tunnels": [self._tunnel_json(t) for t in self.tunnels.statuses()],
            "remote_dbs": self._remote_json(),
            "php": [{"label": b.label, "version": b.version, "folder": b.folder.name, "arch": b.arch,
                     "cgi": b.cgi_exe is not None,
                     "default": default_php is not None and b.folder == default_php.folder}
                    for b in self.engine.php_builds()],
            "composer": (self.paths.bin / "composer" / "composer.phar").is_file(),
            "system": {
                "hosts_in_sync": not missing and hosts.is_in_sync(hosts_text, wanted),
                "hosts_missing": missing,
                "mkcert": self.engine.mkcert_exe.is_file(),
                "ca_trusted": (self.engine.ssl_dir / "ca-trusted.flag").is_file(),
                "cloudflared": self.tunnels.installed,
                "heidisql": self._heidisql.is_file(),
                "dns_route": self.dns_route_active(),
            },
            "busy": busy,
            "schedule_error": self.scheduler.load_error,
            "php_errors": self.php_errors.summary(),
            "tasks": self.scheduler.task_statuses(),
            "backup_root": str(self.databases.backup_root),
            "mail": {"available": self.mail.available, "service": self.mail.service_id,
                     "smtp_port": self.mail.smtp_port, "ui": self.mail.base_url, **self.mail_watch.snapshot},
        }

    def _service_json(self, status: Any) -> dict[str, Any]:
        defn = self.manager.definition(status.id)
        links = []
        for link in defn.links:
            try:
                (url,) = expand([link.url], self.manager.context(status.id), status.id)
                links.append({"label": link.label, "url": url})
            except (ConfigError, ServiceError):
                pass
        return {
            "id": status.id, "name": status.name, "type": status.type, "state": status.state.value,
            "label": status.state.label, "version": status.version, "ports": list(status.ports),
            "pid": status.pid, "message": status.message, "is_database": status.is_database,
            "links": links, "pma_server": self.engine.phpmyadmin_server_index(status.id) if status.is_database else None,
            "can_reset_password": bool(defn.password_reset_sql),
            "has_password": bool(self.manager.credentials.get(status.id)) if status.is_database else False,
            "autobackup": self.scheduler.backup_status(status.id) if status.is_database else None,
        }

    def _site_json(self, info: Any) -> dict[str, Any]:
        data = asdict(info)
        data["url"] = f"{'https' if info.https else 'http'}://{info.domain}/"
        return data

    def _remote_json(self) -> list[dict[str, Any]]:
        if self.remote is None:
            return []
        try:
            servers = self.remote.list()
        except ConfigError as exc:
            logger.warning("%s", exc)
            return []
        return [{**asdict(s), "has_password": self.remote.has_password(s),
                 "pma_server": self.engine.phpmyadmin_remote_index(s.id)} for s in servers]

    # ------------------------------------------------------------------ remote databases

    def _remote_store(self) -> RemoteDbStore:
        if self.remote is None:
            raise ServiceError("Remote databases are not available")
        return self.remote

    def remote_save(self, data: dict[str, Any], db_id: str | None) -> str:
        store = self._remote_store()
        try:
            server = store.save(data, db_id)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc
        self.engine.generate_phpmyadmin()  # phpMyAdmin reads its config per request: no restart
        self.events.publish("state")
        return server.id

    def remote_delete(self, db_id: str) -> None:
        self._remote_store().delete(db_id)
        self.engine.generate_phpmyadmin()
        self.events.publish("state")

    def remote_test(self, data: dict[str, Any], db_id: str | None) -> str:
        store = self._remote_store()
        try:
            server = validate(data, existing_id=db_id or "test")
        except ConfigError as exc:
            raise ServiceError(str(exc)) from exc
        password = data.get("password") or ""
        if not password and db_id:
            password = store.password(store.get(db_id))
        client, is_mysql = self._remote_client()
        return test_connection(client, server, password, mysql_client=is_mysql)

    def _remote_client(self) -> tuple[Path, bool]:
        """A command-line client to reach remote servers: MySQL 8's, else MariaDB's."""
        ranked = sorted((s for s in self.manager.statuses() if s.is_database and s.version),
                        key=lambda s: (s.type == "mysql" and s.version.split(".")[0] >= "8", s.version), reverse=True)
        for status in ranked:
            try:
                return self.manager.expand_tool(status.id, "client"), status.type == "mysql"
            except ServiceError:
                continue
        raise ServiceError("No MySQL or MariaDB client found in bin\\ to test the connection")

    @staticmethod
    def _tunnel_json(status: Any) -> dict[str, Any]:
        return {"id": status.id, "target": status.target.display, "state": status.state.value,
                "public_url": status.public_url, "message": status.message,
                "path": status.target.path}

    def dns_route_active(self, refresh: bool = False) -> bool:
        stamp, value = self._dns_route_cache
        if refresh or time.monotonic() - stamp > 60:
            value = self.sites.dns_route_active()  # PowerShell, ~0.5 s: cached
            self._dns_route_cache = (time.monotonic(), value)
        return value

    # ------------------------------------------------------------------ logs

    def log_path(self, service_id: str) -> Path:
        if service_id == "varsha":
            return self.paths.app_log
        if service_id == "tunnel":
            return self.tunnels.log_path
        if service_id == "create":
            return self.paths.logs / "create.log"
        if service_id.startswith("task-"):
            return self.scheduler.task_log(service_id[5:])
        return self.manager.status(service_id).log_path

    def read_log(self, service_id: str, offset: int) -> dict[str, Any]:
        """Bytes from `offset` (or the last 256 KB when offset < 0) of a service's log."""
        path = self.log_path(service_id)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return {"offset": 0, "text": "", "missing": True, "path": str(path)}
        if offset < 0 or offset > size:  # first read, or the log was rotated
            offset = max(0, size - LOG_CHUNK)
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(min(size - offset, LOG_CHUNK))
        return {"offset": offset + len(data), "text": data.decode("utf-8", "replace").replace("\r", ""),
                "missing": False, "path": str(path)}

    # ------------------------------------------------------------------ desktop actions

    @property
    def _heidisql(self) -> Path:
        return self.paths.bin / "heidisql" / "heidisql.exe"

    def open_folder(self, what: str, ref: str = "") -> None:
        """Open one of varsha's own folders in Explorer. `what` is a keyword, never a path."""
        folders = {"root": self.paths.root, "www": self.paths.www, "logs": self.paths.logs,
                   "bin": self.paths.bin, "config": self.paths.config}
        if what in folders:
            target = folders[what]
        elif what == "backups":
            target = self.databases.backup_dir(self.manager.definition(ref).id) if ref else self.databases.backup_root
        elif what == "data":
            defn = self.manager.definition(ref)
            if not defn.data_dir:
                raise ServiceError(f"{defn.name} has no data folder")
            target = self.paths.root / defn.data_dir
        elif what == "site":
            site = next((s for s in self.sites.sites() if s.domain == ref), None)
            if site is None:
                raise ServiceError(f"Unknown site {ref}")
            target = site.folder
        else:
            raise ServiceError(f"Unknown folder {what!r}")
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)  # noqa: S606 - a fixed, varsha-owned folder

    def open_heidisql(self, service_id: str) -> None:
        if not self._heidisql.is_file():
            raise ServiceError("HeidiSQL is not installed (bin\\heidisql\\heidisql.exe)")
        if self.manager.status(service_id).state is not ServiceState.RUNNING:
            raise ServiceError(f"Start {self.manager.definition(service_id).name} first")
        proc_utils.launch_tool(self._heidisql, self.databases.heidisql_args(service_id))

    def trust_https(self) -> str:
        """mkcert -install: Windows shows its own "install this certificate?" confirmation."""
        if not self.engine.mkcert_exe.is_file():
            raise ServiceError("mkcert is not installed (bin\\mkcert\\mkcert.exe)")
        result = subprocess.run([str(self.engine.mkcert_exe), "-install"], capture_output=True, text=True,
                                errors="replace", timeout=300, creationflags=proc_utils.CREATE_NO_WINDOW)
        output = (result.stdout + "\n" + result.stderr).strip()
        # mkcert also tries Java's and Firefox's stores. If a JDK is installed, its cacerts file
        # needs admin rights, so mkcert exits 1 even though the Windows store (what Chrome and
        # Edge use) succeeded. Judge by the Windows step, and mention the rest.
        windows_ok = "installed in the system trust store" in output or "already installed in the system trust store" in output
        if result.returncode != 0 and not windows_ok:
            raise ServiceError(f"mkcert -install failed: {output}")
        self.engine.ssl_dir.mkdir(parents=True, exist_ok=True)
        (self.engine.ssl_dir / "ca-trusted.flag").write_text("trusted by mkcert -install (Windows store)\n", encoding="utf-8")
        self.events.publish("state")
        note = " (Java's own certificate store was skipped: it needs admin rights.)" if "keytool" in output else ""
        return "Local HTTPS certificates are now trusted by Windows (Chrome, Edge)." + note

    def backups(self, service_id: str) -> list[dict[str, Any]]:
        folder = self.databases.backup_dir(self.manager.definition(service_id).id)
        if not folder.is_dir():
            return []
        files = sorted(folder.glob("*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime} for f in files]

    def backup_file(self, service_id: str, name: str) -> Path:
        """A backup file by name, refusing anything that isn't directly in the backups folder."""
        folder = self.databases.backup_dir(self.manager.definition(service_id).id)
        candidate = (folder / name).resolve()
        if candidate.parent != folder.resolve() or candidate.suffix.lower() != ".sql" or not candidate.is_file():
            raise ServiceError(f"No backup named {name!r}")
        return candidate

    def php_details(self, folder: str) -> dict[str, Any]:
        build = self.engine.php_build_by_folder(folder)
        default = self.engine.default_php()
        return {"folder": folder, "label": build.label, "version": build.version,
                "default": default is not None and default.folder == build.folder,
                "extensions": self.engine.php_extensions(build),
                "ini": self.engine.php_config.get(folder).ini,
                "ini_path": str(self.engine.php_ini_dir(build) / "php.ini")}

    def quit(self, stop_services: bool) -> None:
        if self.on_quit:
            self.on_quit(stop_services)
