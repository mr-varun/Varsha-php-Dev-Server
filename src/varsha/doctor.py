"""Doctor: one button that checks the whole setup and says what's wrong and how to fix it.

Each check returns findings: {"id", "group", "status" (ok | warn | fail | info), "title",
"detail", "fix"}. "fix" is a short action name the panel offers as a button; the panel maps the
generic ones (hosts, trust, goto:<view>, start:<service>) onto existing actions, and
Doctor.fix() handles the rest (renew a certificate, stop a leftover server process).

Everything here only reads, except fix(). A check that crashes is reported as its own finding
instead of hiding the others.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import ssl
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from varsha.errors import ServiceError
from varsha.services import ports as port_utils
from varsha.services import process as proc_utils
from varsha.services.models import ServiceState
from varsha.services.resolve import expand
from varsha.sites import hosts

logger = logging.getLogger(__name__)

SERVER_EXES = {"httpd.exe", "mysqld.exe", "mariadbd.exe", "mailpit.exe"}
CERT_WARN_DAYS = 30
DISK_WARN = 5 * 1024**3
DISK_FAIL = 1 * 1024**3


def finding(fid: str, group: str, status: str, title: str, detail: str = "", fix: str = "") -> dict[str, Any]:
    return {"id": fid, "group": group, "status": status, "title": title, "detail": detail, "fix": fix}


def cert_expiry(pem: Path) -> datetime | None:
    """notAfter of a PEM certificate (stdlib only)."""
    try:
        info = ssl._ssl._test_decode_cert(str(pem))  # type: ignore[attr-defined]  # stdlib's own PEM reader
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)
    except (OSError, ValueError, KeyError, ssl.SSLError):
        return None


class Doctor:
    def __init__(self, paths: Any, manager: Any, engine: Any, sites: Any, scheduler: Any, databases: Any,
                 dns_route: Callable[[], bool]) -> None:
        self.paths = paths
        self.manager = manager
        self.engine = engine
        self.sites = sites
        self.scheduler = scheduler
        self.databases = databases
        self.dns_route = dns_route

    # ------------------------------------------------------------------ running

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        findings: list[dict[str, Any]] = []
        checks = (self.check_services, self.check_ports, self.check_leftovers, self.check_apache_config,
                  self.check_sites, self.check_https, self.check_php, self.check_disk, self.check_backups,
                  self.check_config)

        def safe(check: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
            try:
                return check()
            except Exception as exc:  # one broken check must not hide the rest
                logger.exception("doctor check %s failed", check.__name__)
                return [finding(f"crash:{check.__name__}", "Doctor", "warn",
                                f"The {check.__name__.removeprefix('check_')} check couldn't run", str(exc))]

        # The checks only read and don't depend on each other: run them side by side.
        with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="doctor") as pool:
            for result in pool.map(safe, checks):
                findings.extend(result)
        order = {"fail": 0, "warn": 1, "info": 2, "ok": 3}
        findings.sort(key=lambda f: order[f["status"]])
        counts = {s: sum(f["status"] == s for f in findings) for s in order}
        return {"findings": findings, "counts": counts, "took": round(time.monotonic() - started, 1),
                "checked_at": time.time()}

    # ------------------------------------------------------------------ checks

    def check_services(self) -> list[dict[str, Any]]:
        out = []
        for status in self.manager.statuses():
            if status.state is ServiceState.DISABLED:
                continue
            if status.state is ServiceState.MISSING:
                out.append(finding(f"missing:{status.id}", "Services", "fail", f"{status.name} isn't installed",
                                   status.message or "Its program wasn't found in bin\\.", "goto:system"))
            elif status.state is ServiceState.ERROR:
                out.append(finding(f"error:{status.id}", "Services", "fail", f"{status.name} failed",
                                   status.message, f"log:{status.id}"))
        if not out:
            out.append(finding("services", "Services", "ok", "Every service is installed and none has failed"))
        return out

    def check_ports(self) -> list[dict[str, Any]]:
        out = []
        for status in self.manager.statuses():
            if status.state in (ServiceState.DISABLED, ServiceState.MISSING) or not status.ports:
                continue
            if status.state is ServiceState.RUNNING:
                continue  # its ports are in use by itself
            for owner in port_utils.check_ports(status.ports):
                out.append(finding(f"port:{owner.port}", "Ports", "fail",
                                   f"Port {owner.port} is taken, so {status.name} can't start",
                                   port_utils.describe_owner(owner)))
        if not out:
            out.append(finding("ports", "Ports", "ok", "Every stopped service's port is free"))
        return out

    def _tracked_pids(self) -> set[int]:
        pids: set[int] = set()
        for status in self.manager.statuses():
            if status.pid:
                try:
                    proc = psutil.Process(status.pid)
                    pids.add(proc.pid)
                    pids.update(c.pid for c in proc.children(recursive=True))
                except psutil.Error:
                    pass
        return pids

    def check_leftovers(self) -> list[dict[str, Any]]:
        """Server processes from varsha's bin\\ that varsha doesn't manage (e.g. left by a crash)."""
        out = []
        tracked = self._tracked_pids()
        bin_dir = os.path.normcase(str(self.paths.bin.resolve()))
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            exe = proc.info.get("exe") or ""
            if name not in SERVER_EXES or not exe or proc.pid in tracked:
                continue
            if not os.path.normcase(exe).startswith(bin_dir + os.sep):
                continue
            try:
                if proc.parent() is not None and proc.parent().pid in tracked:
                    continue
                ports = sorted({c.laddr.port for c in proc.net_connections(kind="inet") if c.status == psutil.CONN_LISTEN})
            except psutil.Error:
                ports = []
            out.append(finding(f"leftover:{proc.pid}", "Ports", "warn",
                               f"A leftover {proc.info['name']} (PID {proc.pid}) is running",
                               f"It's from varsha's bin\\ but varsha doesn't manage it{f'; it holds port {ports}' if ports else ''}. "
                               "It's usually left by a crash and blocks the real service.", f"stop-pid:{proc.pid}"))
        return out

    def check_apache_config(self) -> list[dict[str, Any]]:
        sid = "apache"
        if sid not in self.manager.service_ids:
            return []
        defn = self.manager.definition(sid)
        try:
            ctx = self.manager.context(sid)
        except ServiceError:
            return []  # missing: reported by check_services
        config = Path(str(ctx.get("config") or ""))
        if not defn.reload.test_command or not config.is_file():
            return [finding("apache-config", "Apache", "info", "Apache's config hasn't been generated yet",
                            "It's created the first time Apache starts.")]
        result = proc_utils.run_quiet(expand(defn.reload.test_command, ctx, sid), timeout=60)
        text = "\n".join(line for line in (result.stdout + result.stderr).splitlines()
                         if "AH01882" not in line and line.strip())  # the harmless OpenSSL notice
        if result.returncode != 0:
            return [finding("apache-config", "Apache", "fail", "Apache's configuration has an error", text[-1500:], "log:apache")]
        return [finding("apache-config", "Apache", "ok", "Apache's configuration is valid (httpd -t)")]

    def check_sites(self) -> list[dict[str, Any]]:
        out = []
        infos = self.sites.site_infos()
        missing = [i.domain for i in infos if not i.in_hosts]
        dns_on = self.dns_route()
        if missing and not dns_on:
            out.append(finding("hosts", "Sites", "warn", f"{len(missing)} site name(s) aren't in the hosts file",
                               ", ".join(missing[:8]) + ". The browser can't find them.", "hosts"))
        unresolved = []
        for info in infos:
            try:
                addrs = {a[4][0] for a in socket.getaddrinfo(info.domain, 80, proto=socket.IPPROTO_TCP)}
            except OSError:
                addrs = set()
            if not addrs or not all(a in ("127.0.0.1", "::1") for a in addrs):
                unresolved.append(f"{info.domain} → {', '.join(sorted(addrs)) or 'not found'}")
        if unresolved:
            out.append(finding("resolve", "Sites", "fail" if not dns_on else "warn",
                               f"{len(unresolved)} site name(s) don't point to this PC", "; ".join(unresolved[:6]),
                               "hosts"))
        for info in infos:
            folder = self.paths.root / info.folder if not Path(info.folder).is_absolute() else Path(info.folder)
            try:
                if not folder.exists():
                    out.append(finding(f"folder:{info.domain}", "Sites", "fail", f"{info.domain}: its folder is gone",
                                       f"{folder} (a junction whose target was moved or deleted?)", "goto:sites"))
            except OSError:
                pass
        if not out:
            out.append(finding("sites", "Sites", "ok", f"All {len(infos)} sites resolve to this PC and their folders exist"
                               if infos else "No sites in www\\ yet: nothing to check"))
        return out

    def check_https(self) -> list[dict[str, Any]]:
        out = []
        if not self.engine.mkcert_exe.is_file():
            return [finding("mkcert", "HTTPS", "warn", "mkcert is missing: sites are HTTP only",
                            "Put mkcert.exe in bin\\mkcert\\ to get HTTPS for every site.")]
        if not (self.engine.ssl_dir / "ca-trusted.flag").is_file():
            out.append(finding("ca", "HTTPS", "warn", "The local certificate authority isn't trusted yet",
                               "Browsers warn on https:// pages until you trust it once.", "trust"))
        now = datetime.now(tz=timezone.utc)
        soon, expired = [], []
        for pem in sorted(self.engine.ssl_dir.glob("*.pem")):
            if pem.name.endswith("-key.pem"):
                continue
            until = cert_expiry(pem)
            if until is None:
                continue
            days = (until - now).days
            if days < 0:
                expired.append(pem.stem)
            elif days < CERT_WARN_DAYS:
                soon.append(f"{pem.stem} ({days} days)")
        if expired:
            out.append(finding("cert-expired", "HTTPS", "fail", f"{len(expired)} certificate(s) have expired",
                               ", ".join(expired), "renew-certs"))
        if soon:
            out.append(finding("cert-soon", "HTTPS", "warn", f"{len(soon)} certificate(s) expire soon",
                               ", ".join(soon), "renew-certs"))
        if not out:
            out.append(finding("https", "HTTPS", "ok", "HTTPS is trusted and every certificate is valid"))
        return out

    def check_php(self) -> list[dict[str, Any]]:
        out = []
        builds = self.engine.php_builds()
        with ThreadPoolExecutor(max_workers=max(1, len(builds)), thread_name_prefix="doctor-php") as pool:
            reports = list(pool.map(lambda b: self.engine.check_php(b) if b.cgi_exe is not None else None, builds))
        for build, problems in zip(builds, reports):
            if build.cgi_exe is None:
                out.append(finding(f"php-cgi:{build.folder.name}", "PHP", "warn", f"PHP {build.label}: no php-cgi.exe",
                                   "Apache can't run sites with this build."))
                continue
            if problems:
                out.append(finding(f"php:{build.folder.name}", "PHP", "warn",
                                   f"PHP {build.label}: {len(problems)} startup warning(s)", "\n".join(problems[:6]),
                                   f"php:{build.folder.name}"))
        if not out:
            out.append(finding("php", "PHP", "ok", f"All {len(builds)} PHP builds start cleanly with their extensions"))
        return out

    def check_disk(self) -> list[dict[str, Any]]:
        out = []
        seen = set()
        for label, folder in (("varsha", self.paths.root), ("backups", self.databases.backup_root)):
            anchor = Path(folder).anchor or str(folder)
            if anchor in seen:
                continue
            seen.add(anchor)
            try:
                free = shutil.disk_usage(anchor).free
            except OSError:
                continue
            gb = free / 1024**3
            if free < DISK_FAIL:
                out.append(finding(f"disk:{anchor}", "Disk", "fail", f"Drive {anchor} is almost full ({gb:.1f} GB free)",
                                   f"Used by {label}: databases and backups can fail."))
            elif free < DISK_WARN:
                out.append(finding(f"disk:{anchor}", "Disk", "warn", f"Drive {anchor} is getting full ({gb:.1f} GB free)",
                                   f"Used by {label}."))
            else:
                out.append(finding(f"disk:{anchor}", "Disk", "ok", f"Drive {anchor}: {gb:.0f} GB free ({label})"))
        return out

    def check_backups(self) -> list[dict[str, Any]]:
        out = []
        for status in self.manager.statuses():
            if not status.is_database or status.state in (ServiceState.DISABLED, ServiceState.MISSING):
                continue
            plan = self.scheduler.backup_status(status.id)
            if plan["enabled"] and plan["last_ok"] is False:
                out.append(finding(f"backup:{status.id}", "Backups", "fail", f"The last automatic backup of {status.name} failed",
                                   plan["message"], "goto:databases"))
            elif not plan["enabled"]:
                out.append(finding(f"backup:{status.id}", "Backups", "info", f"{status.name} has no automatic backup",
                                   "Turn on a daily backup so a mistake can't cost you your data.", "goto:databases"))
        if not out:
            out.append(finding("backups", "Backups", "ok", "Automatic backups are on and the last ones worked"))
        return out

    def check_config(self) -> list[dict[str, Any]]:
        out = []
        if self.scheduler.load_error:
            out.append(finding("schedule.json", "Config", "fail", "config\\schedule.json can't be read", self.scheduler.load_error))
        if self.sites.last_error:
            out.append(finding("sites-error", "Config", "fail", "The last site update failed", self.sites.last_error, "goto:sites"))
        current = hosts.read_text(hosts.hosts_path()) if hosts.hosts_path().exists() else ""
        if current.count(hosts.START) > 1:
            out.append(finding("hosts-block", "Config", "warn", "The hosts file has more than one varsha block",
                               "Update the hosts file to merge them.", "hosts"))
        return out

    # ------------------------------------------------------------------ fixes

    def fix(self, action: str) -> str:
        if action == "renew-certs":
            return self._renew_certs()
        if action.startswith("stop-pid:"):
            return self._stop_leftover(int(action.split(":", 1)[1]))
        raise ValueError(f"unknown fix {action!r}")

    def _renew_certs(self) -> str:
        """Delete expired / soon-expiring site certificates; regenerating the vhosts makes new ones."""
        now = datetime.now(tz=timezone.utc)
        renewed = []
        for pem in self.engine.ssl_dir.glob("*.pem"):
            if pem.name.endswith("-key.pem"):
                continue
            until = cert_expiry(pem)
            if until is not None and (until - now).days < CERT_WARN_DAYS:
                for stale in (pem, pem.with_name(f"{pem.stem}-key.pem"), pem.with_suffix(".names")):
                    stale.unlink(missing_ok=True)
                renewed.append(pem.stem)
        if not renewed:
            return "No certificate needed renewing"
        self.sites.sync()
        if self.manager.status("apache").state is ServiceState.RUNNING:
            self.manager.reload("apache")
        return f"Renewed {len(renewed)} certificate(s): {', '.join(renewed)}"

    def _stop_leftover(self, pid: int) -> str:
        """Stop a leftover server process: only one of varsha's own bin\\ servers that varsha doesn't manage."""
        if pid in self._tracked_pids():
            raise ServiceError("That process is managed by varsha: stop it with its switch instead")
        try:
            proc = psutil.Process(pid)
            name, exe = proc.name(), proc.exe()
        except psutil.Error as exc:
            raise ServiceError(f"Process {pid} is gone") from exc
        bin_dir = os.path.normcase(str(self.paths.bin.resolve()))
        if name.lower() not in SERVER_EXES or not os.path.normcase(exe).startswith(bin_dir + os.sep):
            raise ServiceError(f"{name} (PID {pid}) isn't one of varsha's servers; varsha won't stop it")
        proc_utils.kill_tree(proc)
        return f"Stopped the leftover {name} (PID {pid})"
