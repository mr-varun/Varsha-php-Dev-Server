"""SiteManager: keep vhosts, certificates and the hosts file in step with www\\.

watchdog reports folder changes in www\\ (top level only; node_modules churn inside a site
doesn't matter). Changes are debounced, then sync() regenerates the vhosts and gracefully
reloads Apache if it's running. The hosts file needs admin rights, so sync() only reports
whether it's out of date; update_hosts() asks megha to fix it (one UAC prompt).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from varsha.config_engine.engine import ConfigEngine
from varsha.errors import ServiceError
from varsha.helper import elevate
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState
from varsha.sites import hosts
from varsha.sites.discovery import Site

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
Listener = Callable[[], None]


@dataclass(frozen=True)
class SiteInfo:
    name: str
    domain: str
    folder: str  # as shown to the user, relative to the varsha root
    docroot: str
    https: bool
    in_hosts: bool  # resolvable through the hosts file (varsha's block or another tool's)
    php: str | None  # the version asked for in sites.json, None = default
    php_label: str  # what it actually runs, e.g. "8.3.33 TS x64"


class SiteManager:
    def __init__(self, engine: ConfigEngine, services: ServiceManager, apache_id: str = "apache",
                 dns_id: str = "dns") -> None:
        self.engine = engine
        self.services = services
        self.apache_id = apache_id
        self.dns_id = dns_id
        self.paths = engine.paths
        self._listener: Listener | None = None
        self._observer: Observer | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_error = ""

    def set_listener(self, listener: Listener | None) -> None:
        self._listener = listener

    def _notify(self) -> None:
        if self._listener is not None:
            try:
                self._listener()
            except Exception:
                logger.exception("Site listener failed")

    # ------------------------------------------------------------------ queries

    @property
    def tld(self) -> str:
        return self.engine.settings.tld

    def sites(self) -> list[Site]:
        return self.engine.sites()

    def hosts_text(self) -> str:
        try:
            return hosts.read_text(hosts.hosts_path())
        except OSError:
            return ""

    def wanted_host_names(self, sites: list[Site] | None = None) -> list[str]:
        names: list[str] = []
        for site in sites if sites is not None else self.sites():
            names.extend(site.hosts_names)
        return names

    def hosts_in_sync(self) -> bool:
        return hosts.is_in_sync(self.hosts_text(), self.wanted_host_names())

    def site_infos(self) -> list[SiteInfo]:
        text = self.hosts_text()
        mapped = set(hosts.current_names(text)) | hosts.mapped_outside(text)
        ssl_dir = self.engine.ssl_dir
        default = self.engine.default_php()
        infos = []
        for site in self.sites():
            build = self.engine.find_php(site.php) if site.php else default
            infos.append(SiteInfo(
                name=site.name,
                domain=site.domain,
                folder=_relative(site.folder, self.paths.root),
                docroot=_relative(site.docroot, self.paths.root),
                https=(ssl_dir / f"{site.domain}.pem").is_file(),
                in_hosts=site.domain in mapped,
                php=site.php,
                php_label=build.label if build else f"PHP {site.php} missing",
            ))
        return infos

    # ------------------------------------------------------------------ actions

    def sync(self) -> bool:
        """Regenerate vhosts; reload Apache if they changed. Returns True if anything changed."""
        with self._lock:
            apache = self.services.definition(self.apache_id)
            try:
                changed = self.engine.generate_sites(apache)
                if changed and self.services.status(self.apache_id).state is ServiceState.RUNNING:
                    logger.info("Sites changed: %s", self.services.reload(self.apache_id))
                self.last_error = ""
            except ServiceError as exc:
                self.last_error = str(exc)
                logger.warning("Site sync failed: %s", exc)
                raise
            finally:
                self._notify()
            return changed

    def update_hosts(self) -> str:
        """Write all site names into varsha's hosts block via megha (UAC prompt)."""
        names = self.wanted_host_names()

        def verify() -> str | None:  # reading the hosts file needs no admin rights
            return f"hosts file updated: {len(names)} names" if hosts.is_in_sync(self.hosts_text(), names) else None

        message = elevate.run_elevated(self.paths, ["hosts-sync", "--tld", self.tld],
                                       request={"tld": self.tld, "names": names}, verify=verify)
        self._notify()
        return message

    def dns_route(self, enable: bool) -> str:
        args = ["dns-route-add", "--tld", self.tld] if enable else ["dns-route-remove"]

        def verify() -> str | None:
            if elevate.dns_route_active(self.tld) != enable:
                return None
            return f"*.{self.tld} now resolves through varsha's DNS (127.0.0.1)" if enable else "DNS route removed"

        message = elevate.run_elevated(self.paths, args, verify=verify)
        self._notify()
        return message

    def dns_route_active(self) -> bool:
        return elevate.dns_route_active(self.tld)

    # ------------------------------------------------------------------ watching

    def start_watching(self) -> None:
        self.paths.www.mkdir(parents=True, exist_ok=True)
        handler = _WwwHandler(self._schedule)
        observer = Observer()
        observer.schedule(handler, str(self.paths.www), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        logger.info("Watching %s for new sites", self.paths.www)

    def stop_watching(self) -> None:
        if self._timer:
            self._timer.cancel()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _schedule(self) -> None:
        # Creating a folder fires several events (create, then renames from "New folder");
        # wait until things settle, then sync once.
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(DEBOUNCE_SECONDS, self._sync_quietly)
        self._timer.daemon = True
        self._timer.start()

    def _sync_quietly(self) -> None:
        try:
            self.sync()
        except ServiceError:
            pass  # recorded in last_error and shown in the panel
        except Exception:
            logger.exception("Site sync crashed")


class _WwwHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Don't filter on event.is_directory: on Windows a *deleted* folder is reported as a
        # FileDeletedEvent (the path no longer exists, so watchdog can't tell what it was).
        # The watch is top-level only, so every create/delete/rename here matters.
        if event.event_type in ("created", "deleted", "moved"):
            self.callback()


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
