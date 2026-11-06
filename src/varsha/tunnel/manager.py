"""Run cloudflared quick tunnels and pick the public trycloudflare.com link out of its log.

Like services/manager.py this has no Qt: `start` blocks until the link appears (up to
`start_timeout`), so call it from a worker thread.

Safety rules:
- Tunnels never outlive varsha. `stop_all` runs on exit, and `kill_orphans` at startup ends
  any cloudflared from bin\\cloudflared\\ that a crashed session left exposing a site.
- Only local addresses can be shared (see target.py), and only if something is listening.
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import psutil

from varsha.errors import ConfigError, ServiceError
from varsha.paths import AppPaths
from varsha.services import ports as portcheck
from varsha.services import process as proc_utils
from varsha.tunnel.target import TunnelTarget, parse_target

logger = logging.getLogger(__name__)

CLOUDFLARED = Path("bin") / "cloudflared" / "cloudflared.exe"
PUBLIC_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared prints the link about a second BEFORE the tunnel is registered. Opening it in
# that gap gives NXDOMAIN, and Windows then caches the "not found" answer for minutes, so the
# link looks broken. Only report the link once a connection is registered.
REGISTERED_RE = re.compile(r"Registered tunnel connection")
# cloudflared prints a failure reason as e.g. 'ERR ... error="..."'.
ERROR_RE = re.compile(r"\bERR\b|failed|error=", re.IGNORECASE)

Listener = Callable[[], None]


class TunnelError(ServiceError):
    """A tunnel could not be started (shown in the GUI like a service error)."""


class TunnelState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


@dataclass(frozen=True)
class TunnelStatus:
    id: str
    target: TunnelTarget
    state: TunnelState
    public_url: str | None
    message: str
    pid: int | None


@dataclass(eq=False)
class _Tunnel:
    id: str
    target: TunnelTarget
    popen: subprocess.Popen[bytes]
    state: TunnelState = TunnelState.STARTING
    public_url: str | None = None  # set only once the tunnel is usable
    found_url: str | None = None  # printed by cloudflared, maybe not registered yet
    registered: bool = False
    message: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    tail: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=8))
    stopping: bool = False


DOH_URL = "https://cloudflare-dns.com/dns-query?name={host}&type=A"


def public_dns_resolves(host: str) -> bool:
    """Ask Cloudflare's DNS-over-HTTPS whether `host` resolves yet.

    A new trycloudflare.com name takes about 10-15 s to appear in DNS. Opening it before then
    makes Windows cache the "not found" answer for minutes, so the user sees a dead link.
    DoH is used instead of the system resolver so that our own check can't poison that cache.
    """
    request = urllib.request.Request(DOH_URL.format(host=host),
                                     headers={"accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            answer = json.load(response)
    except (OSError, ValueError):
        return False
    return answer.get("Status") == 0 and bool(answer.get("Answer"))


class TunnelManager:
    def __init__(self, paths: AppPaths, command: Sequence[str] | None = None,
                 start_timeout: float = 45.0, dns_check: Callable[[str], bool] | None = None,
                 dns_timeout: float = 45.0) -> None:
        self.paths = paths
        self.tld = "test"  # the sites' TLD; the GUI sets it from settings.json (and Settings changes it)
        self._command = list(command) if command else [str(paths.root / CLOUDFLARED)]
        self.start_timeout = start_timeout
        self._dns_check = dns_check or public_dns_resolves
        self.dns_timeout = dns_timeout
        # Ports that must never be put online (the control panel's own port).
        self.blocked_ports: set[int] = set()
        self._tunnels: dict[str, _Tunnel] = {}
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._listener: Listener | None = None
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------ queries

    @property
    def installed(self) -> bool:
        return Path(self._command[0]).is_file()

    @property
    def log_path(self) -> Path:
        return self.paths.logs / "tunnel.log"

    def set_listener(self, listener: Listener | None) -> None:
        self._listener = listener

    def statuses(self) -> list[TunnelStatus]:
        with self._lock:
            return [self._snapshot(t) for t in self._tunnels.values()]

    def any_running(self) -> bool:
        with self._lock:
            return any(t.popen.poll() is None for t in self._tunnels.values())

    # ------------------------------------------------------------------ actions

    def start(self, text: str) -> TunnelStatus:
        try:
            target = parse_target(text, self.tld)
        except ConfigError as exc:
            raise TunnelError(str(exc)) from exc
        if not self.installed:
            raise TunnelError(f"cloudflared is not installed. Put it at {CLOUDFLARED}.")
        if target.port in self.blocked_ports:
            raise TunnelError("That's varsha's own control panel; it can't be shared online.")

        with self._lock:
            for tunnel in self._tunnels.values():
                if tunnel.target == target and tunnel.popen.poll() is None:
                    return self._snapshot(tunnel)  # already shared

        if not portcheck.listening([target.port]):
            raise TunnelError(
                f"Nothing is listening on port {target.port}, so there is nothing to share yet. "
                f"Start the service that serves {target.display} first."
            )

        tunnel_id = f"t{next(self._ids)}"
        cmd = [*self._command, "tunnel", "--no-autoupdate",
               # An empty config file: a ~/.cloudflared/config.yml left from a named tunnel
               # would otherwise be picked up and break the quick tunnel.
               "--config", str(self._empty_config()),
               "--url", target.origin, "--http-host-header", target.host_header]
        if target.scheme == "https":
            cmd.append("--no-tls-verify")  # local sites use mkcert certificates Cloudflare can't verify
        self._log(tunnel_id, f"starting: {subprocess.list2cmdline(cmd)}")
        try:
            popen = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=proc_utils.CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as exc:
            raise TunnelError(f"Could not start cloudflared: {exc}") from exc

        tunnel = _Tunnel(id=tunnel_id, target=target, popen=popen)
        with self._lock:
            self._tunnels[tunnel_id] = tunnel
        threading.Thread(target=self._read_output, args=(tunnel,), name=f"tunnel-{tunnel_id}",
                         daemon=True).start()
        self._notify()

        if not tunnel.ready.wait(self.start_timeout):
            self.stop(tunnel_id)
            raise TunnelError(
                f"cloudflared gave no public link within {self.start_timeout:.0f}s. Check your "
                "internet connection, or open the tunnel log."
            )
        if tunnel.state is TunnelState.ERROR or tunnel.found_url is None:
            message = tunnel.message or "cloudflared stopped before giving a public link"
            self._forget(tunnel_id)
            raise TunnelError(message)

        # Registered with Cloudflare; now wait until the new name is in public DNS.
        host = tunnel.found_url.split("//", 1)[1]
        deadline = time.monotonic() + self.dns_timeout
        resolved = self._dns_check(host)
        while not resolved and time.monotonic() < deadline:
            if tunnel.state is TunnelState.ERROR or tunnel.stopping:
                message = tunnel.message or "the tunnel was stopped"
                self._forget(tunnel_id)
                raise TunnelError(message)
            time.sleep(1.5)
            resolved = self._dns_check(host)
        if not resolved:
            logger.warning("%s not visible in public DNS after %.0fs", host, self.dns_timeout)
            tunnel.message = ("Cloudflare hasn't published this link in DNS yet. If it doesn't "
                              "open, wait a minute and run: ipconfig /flushdns")
        self._log(tunnel_id, f"public link ready: {tunnel.found_url} (dns confirmed: {resolved})")
        tunnel.public_url = tunnel.found_url
        tunnel.state = TunnelState.RUNNING
        self._notify()
        return self._snapshot(tunnel)

    def stop(self, tunnel_id: str) -> None:
        with self._lock:
            tunnel = self._tunnels.get(tunnel_id)
        if tunnel is None:
            return
        tunnel.stopping = True
        if tunnel.popen.poll() is None:
            try:
                # cloudflared has no graceful stop on Windows; Cloudflare drops the link at once.
                proc_utils.kill_tree(psutil.Process(tunnel.popen.pid))
            except psutil.NoSuchProcess:
                pass
        try:
            tunnel.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("cloudflared %s did not exit", tunnel_id)
        self._log(tunnel_id, "stopped")
        self._forget(tunnel_id)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._tunnels)
        for tunnel_id in ids:
            self.stop(tunnel_id)

    def kill_orphans(self) -> int:
        """Kill cloudflared processes from our bin\\ that no live varsha owns."""
        exe = self._command[0]
        own = {t.popen.pid for t in self._tunnels.values()}
        victims = []
        for proc in psutil.process_iter(["exe"]):
            path = proc.info.get("exe")
            if path and proc.pid not in own and proc_utils.same_path(path, exe):
                victims.append(proc)
        if victims:
            logger.warning("Closing %d tunnel(s) left open by an earlier session", len(victims))
            proc_utils.kill_processes(victims)
        return len(victims)

    # ------------------------------------------------------------------ internals

    def _empty_config(self) -> Path:
        path = self.paths.etc / "cloudflared" / "quick-tunnel.yml"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Not literally empty: cloudflared logs an ERR line for an empty config file.
            path.write_text("# varsha quick tunnels: everything else is passed on the command line.\n"
                            "no-autoupdate: true\n", encoding="utf-8")
        return path

    def _read_output(self, tunnel: _Tunnel) -> None:
        # Must keep draining the pipe: if nobody reads it, cloudflared blocks once the
        # OS pipe buffer (about 4 KB on Windows) is full.
        assert tunnel.popen.stdout is not None
        for raw in tunnel.popen.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self._log(tunnel.id, line)
            tunnel.tail.append(line)
            if tunnel.state is not TunnelState.STARTING:
                continue
            if tunnel.found_url is None:
                match = PUBLIC_URL_RE.search(line)
                if match:
                    tunnel.found_url = match.group(0)
            if REGISTERED_RE.search(line):
                tunnel.registered = True
            if tunnel.found_url and tunnel.registered:
                tunnel.ready.set()  # start() now waits for DNS, then publishes the link

        code = tunnel.popen.wait()
        if tunnel.stopping:
            tunnel.ready.set()
            return
        errors = [line for line in tunnel.tail if ERROR_RE.search(line)] or list(tunnel.tail)
        detail = "\n".join(errors[-3:])
        if tunnel.public_url is None:
            tunnel.message = f"cloudflared exited ({proc_utils.describe_exit_code(code)}) before " \
                             f"giving a public link.\n{detail}".rstrip()
        else:
            tunnel.message = f"cloudflared stopped unexpectedly ({proc_utils.describe_exit_code(code)}); " \
                             "the public link no longer works"
        tunnel.state = TunnelState.ERROR
        tunnel.ready.set()
        self._notify()

    def _forget(self, tunnel_id: str) -> None:
        with self._lock:
            self._tunnels.pop(tunnel_id, None)
        self._notify()

    def _snapshot(self, tunnel: _Tunnel) -> TunnelStatus:
        return TunnelStatus(tunnel.id, tunnel.target, tunnel.state, tunnel.public_url,
                            tunnel.message, tunnel.popen.pid if tunnel.popen.poll() is None else None)

    def _notify(self) -> None:
        if self._listener is not None:
            try:
                self._listener()
            except Exception:
                logger.exception("Tunnel listener failed")

    def _log(self, tunnel_id: str, line: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._log_lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"{stamp} [{tunnel_id}] {line}\n")
            except OSError as exc:
                logger.warning("Could not write tunnel log: %s", exc)
