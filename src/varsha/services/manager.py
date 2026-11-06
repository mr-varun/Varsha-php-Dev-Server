"""ServiceManager: start, stop, monitor and auto-restart the services in services.json.

Thread model: every public method may block (waiting for ports, graceful shutdowns) and is
meant to run on a worker thread, never on the GUI thread. Each service has its own lock, so
actions on different services run in parallel. A monitor thread polls processes, detects
crashes and schedules restarts. State changes are reported through a listener callback with
the service id; the GUI turns that into a Qt signal.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import sys
import tempfile
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from varsha import __version__
from varsha.errors import ConfigError, ServiceError
from varsha.paths import AppPaths
from varsha.secrets import CredentialStore, client_option_file
from varsha.services import ports as portcheck
from varsha.services import process as proc_utils
from varsha.services.models import (
    Installation,
    ServiceDef,
    ServiceState,
    ServiceStatus,
    StopMethod,
)
from varsha.services.resolve import build_context, expand, find_installation, install_hint
from varsha.settings import Settings

logger = logging.getLogger(__name__)

Locator = Callable[[ServiceDef], Installation | None]
Preparer = Callable[[ServiceDef, Installation], None]
Listener = Callable[[str], None]

_INIT_TIMEOUT = 600.0  # MySQL 5.6 loading its help tables takes a while on slow disks

_NO_PORT_GRACE = 1.0  # a service without ports counts as started after staying alive this long
_POLL = 0.2


@dataclass(eq=False)
class _Runtime:
    defn: ServiceDef
    lock: threading.RLock = field(default_factory=threading.RLock)
    state: ServiceState = ServiceState.STOPPED
    message: str = ""
    installation: Installation | None = None
    proc: psutil.Process | None = None
    popen: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    restarts: int = 0
    restart_pending: bool = False


class ServiceManager:
    def __init__(
        self,
        paths: AppPaths,
        services: Sequence[ServiceDef],
        settings: Settings,
        locator: Locator | None = None,
        prepare: Preparer | None = None,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self._locate: Locator = locator or (lambda defn: default_locator(paths.root, defn))
        # Generates configs right before a start (ConfigEngine.prepare); kept as a hook so the
        # manager stays independent of templates.
        self._prepare = prepare
        self.credentials = credentials or CredentialStore(paths.data / "credentials.json")
        self._runtimes: dict[str, _Runtime] = {d.id: _Runtime(d) for d in services}
        self._listener: Listener | None = None
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self.refresh()

    # ------------------------------------------------------------------ queries

    def set_listener(self, listener: Listener | None) -> None:
        self._listener = listener

    @property
    def service_ids(self) -> list[str]:
        return list(self._runtimes)

    def status(self, service_id: str) -> ServiceStatus:
        return self._snapshot(self._get(service_id))

    def statuses(self) -> list[ServiceStatus]:
        return [self._snapshot(rt) for rt in self._runtimes.values()]

    def any_running(self) -> bool:
        return any(proc_utils.is_alive(rt.proc) for rt in self._runtimes.values())

    def definition(self, service_id: str) -> ServiceDef:
        return self._get(service_id).defn

    def installation(self, service_id: str) -> Installation | None:
        """The binary in use (or that would be started); None when it isn't installed."""
        rt = self._get(service_id)
        return rt.installation or self._locate(rt.defn)

    def context(self, service_id: str) -> dict[str, object]:
        """Placeholder values for a service (raises ServiceError if it isn't installed)."""
        rt = self._get(service_id)
        inst = rt.installation or self._locate(rt.defn)
        if inst is None:
            raise ServiceError(f"{rt.defn.name} is not installed", service_id)
        return build_context(self.paths, rt.defn, inst)

    def expand_tool(self, service_id: str, tool: str) -> Path:
        defn = self.definition(service_id)
        template = defn.tools.get(tool)
        if not template:
            raise ServiceError(f"{defn.name} has no '{tool}' tool in services.json", service_id)
        try:
            (path,) = expand([template], self.context(service_id), service_id)
        except ConfigError as exc:
            raise ServiceError(str(exc), service_id) from exc
        if not Path(path).is_file():
            raise ServiceError(f"{defn.name}: {path} not found", service_id)
        return Path(path)

    # ------------------------------------------------------------------ actions

    def refresh(self) -> None:
        """Rescan bin\\ for binaries of every service that isn't currently active."""
        for rt in self._runtimes.values():
            with rt.lock:
                if rt.state in (ServiceState.RUNNING, ServiceState.STARTING, ServiceState.STOPPING):
                    continue
                self._rescan(rt)
            self._notify(rt)

    def start(self, service_id: str) -> None:
        rt = self._get(service_id)
        with rt.lock:
            if rt.state is ServiceState.RUNNING and proc_utils.is_alive(rt.proc):
                return
            rt.restarts = 0
            rt.restart_pending = False
            self._start_locked(rt)

    def stop(self, service_id: str) -> None:
        rt = self._get(service_id)
        rt.restart_pending = False  # cancel a pending auto-restart before waiting for the lock
        with rt.lock:
            rt.restart_pending = False
            self._stop_locked(rt)

    def restart(self, service_id: str) -> None:
        rt = self._get(service_id)
        rt.restart_pending = False
        with rt.lock:
            rt.restart_pending = False
            self._stop_locked(rt)
            rt.restarts = 0
            self._start_locked(rt)

    def reload(self, service_id: str) -> str:
        """Apply new configs. Apache: regenerate, test with `httpd -t`, graceful restart (no
        downtime, no dropped requests). Others: a normal restart. A stopped service stays stopped."""
        rt = self._get(service_id)
        defn = rt.defn
        with rt.lock:
            if not (rt.state is ServiceState.RUNNING and proc_utils.is_alive(rt.proc)):
                return "not running; new config applies on next start"
            if defn.reload.method != "apache_signal" or rt.installation is None:
                self._stop_locked(rt)
                self._start_locked(rt)
                return "restarted"
            if self._prepare is not None:
                try:
                    self._prepare(defn, rt.installation)
                except ConfigError as exc:
                    raise ServiceError(f"{defn.name}: could not generate its config: {exc}", defn.id) from exc
            if defn.reload.test_command:
                cmd = expand(defn.reload.test_command, self.context(service_id), service_id)
                result = proc_utils.run_quiet(cmd, timeout=60)
                if result.returncode != 0:
                    # Never hand Apache a broken config: a graceful restart with a bad file
                    # would take the whole server down.
                    detail = (result.stderr or result.stdout).strip()
                    raise ServiceError(f"{defn.name}: new config rejected, still running the old "
                                       f"one:\n{detail}", defn.id)
            assert rt.proc is not None
            if not proc_utils.signal_apache_restart(rt.proc.pid):
                self._stop_locked(rt)
                self._start_locked(rt)
                return "restarted"
            logger.info("%s reloaded gracefully", service_id)
            self._set(rt, ServiceState.RUNNING, "")
            return "reloaded"

    def reset_root_password(self, service_id: str, new_password: str) -> None:
        """Set root's password without knowing the old one.

        MySQL's documented recovery method: restart once with --init-file pointing at a
        temporary SQL file (in the user's private %TEMP%) that sets the password, check the
        new password works, then delete the file. Leaves the server running if it was.
        """
        rt = self._get(service_id)
        defn = rt.defn
        if not defn.password_reset_sql:
            raise ServiceError(f"{defn.name} has no password_reset_sql in services.json", service_id)
        rt.restart_pending = False
        with rt.lock:
            rt.restart_pending = False
            was_running = proc_utils.is_alive(rt.proc)
            ctx = dict(self.context(service_id))
            ctx["password"] = sql_string_body(new_password)
            try:
                statements = expand(defn.password_reset_sql, ctx, service_id)
            except ConfigError as exc:
                raise ServiceError(str(exc), service_id) from exc
            # --init-file executes one statement per line: never join them on one line.
            sql = "\n".join(statements)

            self._stop_locked(rt)
            handle, name = tempfile.mkstemp(prefix="varsha-reset-", suffix=".sql")
            init_file = Path(name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(sql + "\n")
                self._start_locked(rt, extra_args=[f"--init-file={init_file.as_posix()}"])
                if not self._login_works(service_id, new_password):
                    raise ServiceError(
                        f"{defn.name} restarted, but logging in with the new password failed. "
                        "Check the log.", service_id)
            finally:
                init_file.unlink(missing_ok=True)
            self.credentials.set(service_id, new_password)
            self._set(rt, ServiceState.RUNNING, "root password changed")
            logger.info("Root password of %s changed", service_id)
            if not was_running:
                self._stop_locked(rt)

    def _login_works(self, service_id: str, password: str, attempts: int = 10) -> bool:
        client = self.expand_tool(service_id, "client")
        port = self.definition(service_id).ports[0]
        for _attempt in range(attempts):
            with client_option_file(port, password) as cnf:
                result = proc_utils.run_quiet(
                    [str(client), f"--defaults-extra-file={cnf.as_posix()}", "-e", "SELECT 1"], timeout=15)
            if result.returncode == 0:
                return True
            time.sleep(0.5)
        logger.warning("Login check for %s failed: %s", service_id, (result.stderr or result.stdout).strip())
        return False

    def start_all(self) -> None:
        ids = [rt.defn.id for rt in self._runtimes.values()
               if rt.state in (ServiceState.STOPPED, ServiceState.ERROR)]
        self._run_parallel(ids, self.start, "start")

    def set_autostart(self, service_id: str, enabled: bool) -> None:
        """Change a service's autostart flag for this session too (the Settings page also writes it
        into services.json)."""
        rt = self._get(service_id)
        rt.defn = dataclasses.replace(rt.defn, autostart=enabled)

    def start_autostart(self) -> None:
        ids = [rt.defn.id for rt in self._runtimes.values()
               if rt.defn.autostart and rt.state in (ServiceState.STOPPED, ServiceState.ERROR)]
        self._run_parallel(ids, self.start, "start")

    def stop_all(self) -> None:
        for rt in self._runtimes.values():
            rt.restart_pending = False
        ids = [rt.defn.id for rt in self._runtimes.values()
               if proc_utils.is_alive(rt.proc) or rt.state is ServiceState.STARTING]
        self._run_parallel(ids, self.stop, "stop")

    def adopt_running(self) -> None:
        """Take over services still running from an earlier varsha session.

        Only processes whose exe lives inside the varsha folder are considered, so a MySQL
        from Laragon or WAMP is never mistaken for ours. Their output still lands in
        logs\\<id>.log because they inherited that file handle when they were started.
        """
        targets: dict[str, _Runtime] = {}
        # Built-in services run as "python -m <module>", so the exe alone (python.exe) would
        # match unrelated Python processes, varsha itself included: match the command line.
        module_targets: dict[str, _Runtime] = {}
        for rt in self._runtimes.values():
            inst = rt.installation
            if inst is None or rt.state not in (ServiceState.STOPPED, ServiceState.ERROR):
                continue
            if rt.defn.is_builtin:
                if rt.defn.module:
                    module_targets[rt.defn.module] = rt
                continue
            if not inst.exe.is_relative_to(self.paths.root):
                continue
            targets[os.path.normcase(os.path.abspath(inst.exe))] = rt
        if not targets and not module_targets:
            return

        own = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
        found: dict[str, list[psutil.Process]] = {}
        for proc in psutil.process_iter(["exe", "ppid", "cmdline"]):
            exe = proc.info.get("exe")
            if not exe or proc.pid in own:
                continue
            rt = targets.get(os.path.normcase(os.path.abspath(exe)))
            if rt is None and module_targets:
                cmdline = proc.info.get("cmdline") or []
                for module, candidate in module_targets.items():
                    if ("-m" in cmdline or "--run-module" in cmdline) and module in cmdline:
                        rt = candidate
                        break
            if rt is not None:
                found.setdefault(rt.defn.id, []).append(proc)

        for sid, procs in found.items():
            pids = {p.pid for p in procs}
            # The tree root is the process whose parent isn't the same binary
            # (Apache: parent httpd.exe -> child httpd.exe).
            roots = [p for p in procs if p.info.get("ppid") not in pids] or procs
            root = min(roots, key=lambda p: p.create_time())
            rt = self._runtimes[sid]
            with rt.lock:
                rt.proc = root
                rt.popen = None
                rt.started_at = time.monotonic()
                self._set(rt, ServiceState.RUNNING, f"adopted from an earlier session (PID {root.pid})")
            logger.info("Adopted running %s (PID %s)", sid, root.pid)

    # ------------------------------------------------------------------ monitor

    def start_monitor(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._stop_event.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, name="varsha-monitor", daemon=True)
        self._monitor.start()

    def shutdown(self) -> None:
        """Stop monitoring (services themselves keep running unless stop_all was called)."""
        self._stop_event.set()
        for rt in self._runtimes.values():
            rt.restart_pending = False
        if self._monitor:
            self._monitor.join(timeout=5)
            self._monitor = None

    LOG_TRIM_EVERY = 600.0  # seconds between log size checks (also once when monitoring starts)

    def _monitor_loop(self) -> None:
        last_trim = 0.0
        while not self._stop_event.wait(self.settings.monitor_interval):
            if time.monotonic() - last_trim >= self.LOG_TRIM_EVERY:
                last_trim = time.monotonic()
                try:
                    proc_utils.trim_logs(self.paths.logs, self.settings.log_max_bytes)
                except Exception:
                    logger.exception("Trimming the logs failed")
            for rt in list(self._runtimes.values()):
                # Skip services that are busy starting or stopping; never block the loop.
                if not rt.lock.acquire(blocking=False):
                    continue
                try:
                    self._check(rt)
                except Exception:
                    logger.exception("Monitor check failed for %s", rt.defn.id)
                finally:
                    rt.lock.release()

    def _check(self, rt: _Runtime) -> None:
        if rt.state is not ServiceState.RUNNING or rt.proc is None:
            return
        if proc_utils.is_alive(rt.proc):
            if rt.restarts and time.monotonic() - rt.started_at > self.settings.stable_after:
                rt.restarts = 0
            return

        exit_text = proc_utils.describe_exit_code(rt.popen.poll() if rt.popen else None)
        logger.warning("%s exited unexpectedly (%s)", rt.defn.id, exit_text)
        self._cleanup(rt)
        if rt.restarts >= self.settings.max_auto_restarts:
            self._set(rt, ServiceState.ERROR,
                      f"crashed ({exit_text}) after {rt.restarts} automatic restarts; not "
                      "restarting again. Check the log")
            return
        rt.restarts += 1
        rt.restart_pending = True
        self._set(rt, ServiceState.STARTING,
                  f"crashed ({exit_text}), restarting ({rt.restarts}/{self.settings.max_auto_restarts})")
        threading.Thread(target=self._auto_restart, args=(rt,), name=f"restart-{rt.defn.id}",
                         daemon=True).start()

    def _auto_restart(self, rt: _Runtime) -> None:
        while True:
            if self._stop_event.wait(self.settings.restart_delay):
                return
            with rt.lock:
                if not rt.restart_pending:
                    return
                try:
                    self._start_locked(rt, keep_message=True)
                    rt.restart_pending = False
                    return
                except ServiceError as exc:
                    if rt.restarts >= self.settings.max_auto_restarts:
                        rt.restart_pending = False
                        self._set(rt, ServiceState.ERROR,
                                  f"gave up after {rt.restarts} restart attempts: {exc}")
                        return
                    rt.restarts += 1
                    self._set(rt, ServiceState.STARTING,
                              f"restart failed, retrying ({rt.restarts}/{self.settings.max_auto_restarts}): {exc}")

    # ------------------------------------------------------------------ internals

    def _get(self, service_id: str) -> _Runtime:
        try:
            return self._runtimes[service_id]
        except KeyError:
            raise ServiceError(f"Unknown service '{service_id}'") from None

    def _snapshot(self, rt: _Runtime) -> ServiceStatus:
        proc = rt.proc
        return ServiceStatus(
            id=rt.defn.id,
            name=rt.defn.name,
            type=rt.defn.type,
            state=rt.state,
            version=rt.installation.version if rt.installation else None,
            ports=rt.defn.ports,
            pid=proc.pid if proc is not None else None,
            message=rt.message,
            log_path=(self.paths.root / rt.defn.log_file) if rt.defn.log_file
            else self.paths.service_log(rt.defn.id),
            autostart=rt.defn.autostart,
            is_database=rt.defn.is_database,
        )

    def _notify(self, rt: _Runtime) -> None:
        if self._listener is None:
            return
        try:
            self._listener(rt.defn.id)
        except Exception:
            logger.exception("Service listener failed")

    def _set(self, rt: _Runtime, state: ServiceState, message: str = "") -> None:
        rt.state = state
        rt.message = message
        self._notify(rt)

    def _rescan(self, rt: _Runtime) -> None:
        defn = rt.defn
        if not defn.enabled:
            rt.installation = None
            self._set(rt, ServiceState.DISABLED, 'set "enabled": true in config\\services.json to use it')
            return
        if defn.is_builtin and not defn.module:
            rt.installation = None
            self._set(rt, ServiceState.MISSING, 'built-in service without a "module" in services.json')
            return
        rt.installation = self._locate(defn)
        if rt.installation is None:
            self._set(rt, ServiceState.MISSING, f"put the binary at {install_hint(defn.exe or '')}")
        elif rt.state in (ServiceState.MISSING, ServiceState.DISABLED):
            self._set(rt, ServiceState.STOPPED)

    def _preflight(self, rt: _Runtime) -> tuple[list[str], Path]:
        defn = rt.defn
        if not defn.enabled:
            raise ServiceError(f"{defn.name} is disabled in services.json", defn.id)
        if defn.is_builtin and not defn.module:
            raise ServiceError(f'{defn.name} is a built-in service without a "module"', defn.id)
        rt.installation = self._locate(defn)
        if rt.installation is None:
            message = f"put the binary at {install_hint(defn.exe or '')}"
            self._set(rt, ServiceState.MISSING, message)
            raise ServiceError(f"{defn.name} is not installed: {message}", defn.id)
        inst = rt.installation

        if self._prepare is not None:
            try:
                self._prepare(defn, inst)
            except ConfigError as exc:
                raise ServiceError(f"{defn.name}: could not generate its config: {exc}", defn.id) from exc
        if defn.config_file and not (self.paths.root / defn.config_file).is_file():
            raise ServiceError(f"{defn.name}: config file {defn.config_file} not found.", defn.id)

        busy = portcheck.check_ports(defn.ports)
        if busy:
            reasons = "; ".join(portcheck.describe_owner(owner) for owner in busy)
            raise ServiceError(f"Can't start {defn.name}: {reasons}.", defn.id)

        ctx = build_context(self.paths, defn, inst)
        if defn.data_dir:
            data_dir = self.paths.root / defn.data_dir
            needs_init = bool(defn.init_command) and (not data_dir.is_dir() or not any(data_dir.iterdir()))
            data_dir.mkdir(parents=True, exist_ok=True)
            if needs_init:
                self._initialize(rt, inst, ctx, data_dir)

        try:
            args = expand(defn.args, ctx, defn.id)
        except ConfigError as exc:
            raise ServiceError(str(exc), defn.id) from exc
        if defn.is_builtin and defn.module:
            # Built-in services are varsha's own Python modules, run with this interpreter
            # (python -m) or, in the frozen build, with Varsha.exe --run-module.
            flag = "--run-module" if getattr(sys, "frozen", False) else "-m"
            return [str(inst.exe), flag, defn.module, *args], self.paths.root
        return [str(inst.exe), *args], inst.exe.parent

    def _initialize(self, rt: _Runtime, inst: Installation, ctx: dict[str, object], data_dir: Path) -> None:
        """First start: create the data directory (mysqld --initialize, mariadb-install-db, ...)."""
        defn = rt.defn
        self._set(rt, ServiceState.STARTING, "first start: creating the data directory…")
        try:
            cmd = expand(defn.init_command, ctx, defn.id)
            stdin = self._init_stdin(defn, ctx)
        except (ConfigError, OSError) as exc:
            raise ServiceError(f"{defn.name}: can't prepare first-time setup: {exc}", defn.id) from exc

        log_path = self.paths.service_log(defn.id)
        logger.info("Initialising %s: %s", defn.id, subprocess.list2cmdline(cmd))
        with open(log_path, "ab") as log:
            log.write(f"\n===== varsha first-time setup: {subprocess.list2cmdline(cmd)}\n".encode())
            log.flush()
            try:
                result = subprocess.run(cmd, cwd=str(inst.exe.parent), input=stdin,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=proc_utils.CREATE_NO_WINDOW,
                                        timeout=_INIT_TIMEOUT)
                code: int | None = result.returncode
            except subprocess.TimeoutExpired:
                code = None
            except OSError as exc:
                raise ServiceError(f"{defn.name}: could not run {cmd[0]}: {exc}", defn.id) from exc

        if code == 0 and any(data_dir.iterdir()):
            logger.info("%s data directory created", defn.id)
            return
        # Keep whatever was produced for inspection, but out of the way, so the next start
        # retries from scratch instead of booting a half-built data directory.
        failed = data_dir.with_name(f"{data_dir.name}.failed-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            data_dir.rename(failed)
        except OSError:
            logger.warning("Could not move aside %s", data_dir)
        reason = "timed out" if code is None else proc_utils.describe_exit_code(code)
        tail = _log_tail(log_path)
        raise ServiceError(f"{defn.name}: first-time setup failed ({reason}). Last log lines:\n{tail}", defn.id)

    def _init_stdin(self, defn: ServiceDef, ctx: dict[str, object]) -> bytes | None:
        if not defn.init_stdin:
            return None
        chunks: list[bytes] = []
        for entry in defn.init_stdin:
            if entry.startswith("sql:"):
                chunks.append(entry[4:].encode("utf-8"))
            else:
                (path,) = expand([entry], ctx, defn.id)
                chunks.append(Path(path).read_bytes())
        return b"\n".join(chunks) + b"\n"

    def _start_locked(self, rt: _Runtime, *, keep_message: bool = False,
                      extra_args: Sequence[str] = ()) -> None:
        defn = rt.defn
        try:
            cmd, cwd = self._preflight(rt)
            cmd = [*cmd, *extra_args]
        except ServiceError as exc:
            if rt.state is not ServiceState.MISSING:
                self._set(rt, ServiceState.ERROR, str(exc))
            raise

        log_path = self.paths.service_log(defn.id)
        proc_utils.rotate_log(log_path, self.settings.log_max_bytes)
        self._set(rt, ServiceState.STARTING, rt.message if keep_message else "")
        logger.info("Starting %s: %s", defn.id, subprocess.list2cmdline(cmd))
        try:
            popen = proc_utils.spawn(cmd, cwd, log_path)
        except OSError as exc:
            self._set(rt, ServiceState.ERROR, f"could not launch {cmd[0]}: {exc}")
            raise ServiceError(f"Could not launch {defn.name}: {exc}", defn.id) from exc

        rt.popen = popen
        try:
            rt.proc = psutil.Process(popen.pid)
        except psutil.NoSuchProcess:
            rt.proc = None

        deadline = time.monotonic() + self.settings.start_timeout
        grace_until = time.monotonic() + _NO_PORT_GRACE
        while True:
            code = popen.poll()
            if code is not None:
                self._cleanup(rt)
                tail = _log_tail(log_path)
                if defn.log_file:  # e.g. Apache writes most startup errors to its ErrorLog
                    own = _log_tail(self.paths.root / defn.log_file, lines=4, since_marker=False)
                    tail = "\n".join(part for part in (tail, own) if part)
                message = f"exited during startup ({proc_utils.describe_exit_code(code)})"
                if tail:
                    message += f". Last log lines:\n{tail}"
                self._set(rt, ServiceState.ERROR, message)
                raise ServiceError(f"{defn.name} {message}", defn.id)
            now = time.monotonic()
            if defn.ports and portcheck.listening(defn.ports):
                self._mark_running(rt, "")
                return
            if not defn.ports and now >= grace_until:
                self._mark_running(rt, "")
                return
            if now >= deadline:
                ports = ", ".join(map(str, defn.ports))
                self._mark_running(
                    rt, f"process is up but port {ports} isn't open after "
                        f"{self.settings.start_timeout:.0f}s; check the log")
                return
            time.sleep(_POLL)

    def _mark_running(self, rt: _Runtime, message: str) -> None:
        rt.started_at = time.monotonic()
        self._set(rt, ServiceState.RUNNING, message)
        logger.info("%s running (PID %s)", rt.defn.id, rt.proc.pid if rt.proc else "?")

    def _stop_locked(self, rt: _Runtime) -> None:
        proc = rt.proc
        if not proc_utils.is_alive(proc):
            self._cleanup(rt)
            if rt.state not in (ServiceState.MISSING, ServiceState.DISABLED):
                self._set(rt, ServiceState.STOPPED)
            return
        assert proc is not None
        self._set(rt, ServiceState.STOPPING)
        logger.info("Stopping %s (PID %s) via %s", rt.defn.id, proc.pid, rt.defn.stop.method)
        try:
            tree = proc.children(recursive=True)
        except psutil.Error:
            tree = []

        if self._request_graceful_stop(rt, proc):
            if rt.defn.stop.also_kill:
                doomed = [p for p in tree if _proc_name(p) in rt.defn.stop.also_kill]
                if doomed:
                    proc_utils.kill_processes(doomed, timeout=2)
            try:
                proc.wait(timeout=self.settings.stop_timeout)
            except psutil.TimeoutExpired:
                logger.warning("%s did not stop within %.0fs, killing it", rt.defn.id,
                               self.settings.stop_timeout)
                proc_utils.kill_tree(proc)
            except psutil.NoSuchProcess:
                pass
        else:
            proc_utils.kill_tree(proc)

        leftovers = [child for child in tree if proc_utils.is_alive(child)]
        if leftovers:
            names = sorted({_proc_name(child) or str(child.pid) for child in leftovers})
            # conhost.exe belongs to a console child (php-cgi.exe) that was just killed and
            # exits on its own a moment later: expected, not worth a warning.
            level = logging.INFO if set(names) <= {"conhost.exe"} else logging.WARNING
            logger.log(level, "Killing %d leftover child process(es) of %s: %s",
                       len(leftovers), rt.defn.id, ", ".join(names))
            proc_utils.kill_processes(leftovers)
        self._cleanup(rt)
        self._set(rt, ServiceState.STOPPED)

    def _request_graceful_stop(self, rt: _Runtime, proc: psutil.Process) -> bool:
        """Ask the service to stop. False means it can't be asked, so kill it right away."""
        spec = rt.defn.stop
        if spec.method is StopMethod.APACHE_SIGNAL:
            return proc_utils.signal_apache_shutdown(proc.pid)
        if spec.method is StopMethod.TERMINATE:
            try:
                proc.terminate()  # TerminateProcess on Windows: there is no gentle SIGTERM
            except psutil.Error:
                pass
            return True

        if rt.installation is None:
            return False
        ctx = build_context(self.paths, rt.defn, rt.installation)
        needs_cnf = any("{client_cnf}" in part for part in spec.command)
        port = rt.defn.ports[0] if rt.defn.ports else 3306
        with client_option_file(port, self.credentials.get(rt.defn.id)) if needs_cnf \
                else contextlib.nullcontext(None) as cnf:
            if cnf is not None:
                ctx["client_cnf"] = cnf.as_posix()
            try:
                cmd = expand(spec.command, ctx, rt.defn.id)
            except ConfigError as exc:
                logger.warning("%s", exc)
                return False
            if not Path(cmd[0]).is_file():
                logger.warning("Stop command %s not found for %s; killing instead", cmd[0], rt.defn.id)
                return False
            try:
                result = proc_utils.run_quiet(cmd, timeout=self.settings.stop_timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Stop command for %s timed out", rt.defn.id)
                return True
            except OSError as exc:
                logger.warning("Stop command for %s failed to run: %s", rt.defn.id, exc)
                return False
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            logger.warning("Stop command for %s failed (exit %s): %s", rt.defn.id, result.returncode, output)
            return False
        return True

    def _cleanup(self, rt: _Runtime) -> None:
        if rt.popen is not None:
            try:
                rt.popen.wait(timeout=1)  # reap it and release the process handle
            except subprocess.TimeoutExpired:
                pass
        rt.popen = None
        rt.proc = None

    def _run_parallel(self, ids: Iterable[str], action: Callable[[str], None], verb: str) -> None:
        ids = list(ids)
        if not ids:
            return
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(ids), thread_name_prefix=f"{verb}-all") as pool:
            futures = {sid: pool.submit(action, sid) for sid in ids}
            for sid, future in futures.items():
                try:
                    future.result()
                except ServiceError as exc:
                    errors.append(f"• {self._runtimes[sid].defn.name}: {exc}")
                except Exception as exc:
                    logger.exception("Unexpected error trying to %s %s", verb, sid)
                    errors.append(f"• {self._runtimes[sid].defn.name}: {exc}")
        if errors:
            noun = "service" if len(errors) == 1 else "services"
            raise ServiceError(f"{len(errors)} {noun} failed to {verb}:\n" + "\n".join(errors))


def default_locator(root: Path, defn: ServiceDef) -> Installation | None:
    if defn.is_builtin:
        return Installation(Path(sys.executable), __version__) if defn.module else None
    return find_installation(root, defn)


def _proc_name(proc: psutil.Process) -> str:
    try:
        return proc.name().lower()
    except psutil.Error:
        return ""


def sql_string_body(value: str) -> str:
    """Escape text for use inside a single-quoted MySQL string literal."""
    # Newlines are escaped too: --init-file splits statements at line ends.
    return (value.replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0")
            .replace("\n", "\\n").replace("\r", "\\r"))


def _log_tail(path: Path, lines: int = 6, max_bytes: int = 4096, since_marker: bool = True) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    # Only lines from the latest run (after varsha's start marker).
    marker = text.rfind("===== ") if since_marker else -1
    if marker != -1:
        text = text[marker:].split("\n", 1)[-1] if "\n" in text[marker:] else ""
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])
