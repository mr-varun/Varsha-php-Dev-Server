"""Entry point: python -m varsha (later: varsha.exe).

varsha has no window: it runs in the tray and serves its control panel to the browser at
http://127.0.0.1:<panel_port>/. Starting it a second time just reopens that panel.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from varsha.config_engine.engine import ConfigEngine
from varsha.errors import ConfigError
from varsha.gui.tray import Tray
from varsha.logging_setup import setup_logging
from varsha.paths import AppPaths
from varsha.secrets import CredentialStore
from varsha.services.config import load_services
from varsha.services.manager import ServiceManager
from varsha.services.remote import RemoteDbStore
from varsha.settings import load_settings
from varsha.sites.manager import SiteManager
from varsha.tunnel.manager import TunnelManager
from varsha.web.panel import PanelApp
from varsha.web.server import STATIC, bind_panel

logger = logging.getLogger(__name__)

QUIT_DEADLINE = int(os.environ.get("VARSHA_QUIT_DEADLINE", "150"))  # seconds from "quit" to a forced exit
_hang_file: Any = None  # kept open for faulthandler's C-level timer


def arm_quit_watchdog(paths: AppPaths) -> None:
    """varsha must never hang on quit. faulthandler's timer runs in C, so it works even when a
    thread is stuck in native code holding the GIL: after QUIT_DEADLINE seconds it writes every
    thread's stack to logs/varsha-hang.log (so the cause can be fixed) and ends the process.
    Stopping services takes at most stop_timeout per service, well inside the deadline."""
    global _hang_file
    _hang_file = open(paths.logs / "varsha-hang.log", "a", encoding="utf-8")  # noqa: SIM115 - must stay open
    _hang_file.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} quit requested; "
                     f"if this is followed by stacks, varsha hung for {QUIT_DEADLINE} s and was ended\n")
    _hang_file.flush()
    faulthandler.dump_traceback_later(QUIT_DEADLINE, exit=True, file=_hang_file)


def launch_file(paths: AppPaths) -> Path:
    """Where the running instance leaves its panel login URL, for a second launch to reopen it.
    It's in %LOCALAPPDATA% (private to this Windows user), never in the shared varsha folder."""
    digest = hashlib.sha256(str(paths.root).lower().encode()).hexdigest()[:12]
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "varsha"
    return base / f"panel-{digest}.json"


class _MainThread(QObject):
    """Requests from worker/HTTP threads that must run on the Qt thread."""

    quit_requested = Signal(bool)
    notify = Signal(str, str, bool)
    # Ends the Qt event loop. Worker threads must emit this instead of calling app.quit():
    # app.quit() from a worker holds the GIL while it waits on the Qt thread, which can deadlock
    # with a Qt thread that is waiting on the GIL (seen when quitting with no services to stop).
    exit_loop = Signal()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="varsha")
    parser.add_argument("--no-browser", action="store_true", help="don't open the control panel on start")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    paths = AppPaths.discover()
    paths.ensure_dirs()
    paths.seed_config()
    setup_logging(paths.app_log)
    logger.info("varsha starting, root=%s", paths.root)
    try:
        logger.info("start page: %s", paths.seed_start_page())
    except OSError as exc:  # never block startup over the start page
        logger.warning("start page not written: %s", exc)

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("varsha")
    app.setQuitOnLastWindowClosed(False)  # there are no windows: the tray keeps us alive

    lock = QLockFile(str(paths.data / "varsha.lock"))
    if not lock.tryLock(200):
        try:
            url = json.loads(launch_file(paths).read_text(encoding="utf-8"))["url"]
            webbrowser.open(url)
        except (OSError, ValueError, KeyError):
            QMessageBox.information(None, "varsha", "varsha is already running (see the tray icon).")
        return 0

    try:
        settings = load_settings(paths.settings_file)
        services = load_services(paths.services_file)
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        QMessageBox.critical(None, "varsha: configuration error", str(exc))
        return 1

    credentials = CredentialStore(paths.data / "credentials.json")
    remote = RemoteDbStore(paths.config / "remote-databases.json", credentials)
    engine = ConfigEngine(paths, settings, services, remote=remote)
    manager = ServiceManager(paths, services, settings, prepare=engine.prepare, credentials=credentials)
    sites = SiteManager(engine, manager)
    tunnels = TunnelManager(paths)
    tunnels.kill_orphans()  # a crashed session must not leave a site public

    main_thread = _MainThread()
    panel = PanelApp(paths, settings, manager, engine, sites, tunnels, remote=remote,
                     on_quit=main_thread.quit_requested.emit)
    try:
        server = bind_panel(panel, settings.panel_port)
    except OSError as exc:
        QMessageBox.critical(None, "varsha", f"The control panel could not start: {exc}")
        return 1
    tunnels.blocked_ports = {server.port}
    server.start()
    logger.info("Control panel on %s", server.url)

    info = launch_file(paths)
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps({"url": server.login_url, "pid": os.getpid()}), encoding="utf-8")

    def run(title: str, fn: object) -> None:
        panel.run_task(title, fn)  # type: ignore[arg-type]

    def safe_sites() -> list:  # the tray must never fail because sites.json has a typo
        try:
            return engine.sites()
        except Exception:
            return []

    tray = Tray(STATIC / "varsha.ico", lambda: server.login_url, manager, run,
                main_thread.quit_requested.emit, sites=safe_sites)

    def on_event_toast() -> None:
        # Mirror failures as tray balloons, so they're seen even with the panel closed.
        last = panel.events.last
        while not panel.events.closed:
            for seq, kind, data in panel.events.wait(last, timeout=30):
                last = seq
                if kind == "toast" and isinstance(data, dict) and not data.get("ok"):
                    main_thread.notify.emit(str(data.get("title", "varsha")), str(data.get("message", "")), True)
                elif kind == "php_error" and isinstance(data, dict) and data.get("new"):
                    new = data["new"]
                    first = new[0]
                    where = first["site"] or (first["file_name"] or "PHP")
                    place = f"{first['file_name']}:{first['line']}" if first["file_name"] else ""
                    if len(new) == 1:
                        main_thread.notify.emit(f"PHP {first['level']} in {where}",
                                                f"{first['message'][:160]}\n{place}".strip(), first["severity"] == "error")
                    else:
                        main_thread.notify.emit(f"{len(new)} new PHP errors",
                                                f"Latest in {where}: {first['message'][:120]}", True)
                elif kind == "mail" and isinstance(data, dict) and data.get("new"):
                    new = data["new"]
                    sender = new[0]["from"]["name"] or new[0]["from"]["address"]
                    if len(new) == 1:
                        main_thread.notify.emit(f"New mail: {new[0]['subject'] or '(no subject)'}", f"From {sender}", False)
                    else:
                        main_thread.notify.emit(f"{len(new)} new emails", f"Latest from {sender}. Open the Mail page to read them.", False)

    main_thread.notify.connect(tray.notify)
    threading.Thread(target=on_event_toast, name="tray-toasts", daemon=True).start()

    quitting = {"done": False}
    def shutdown(stop_services: bool) -> None:
        if quitting["done"]:
            return
        quitting["done"] = True
        arm_quit_watchdog(paths)
        logger.info("Quitting (%s)", "stopping services" if stop_services else "services keep running")

        def work() -> None:
            tunnels.stop_all()  # shared links never outlive varsha, even when services stay up
            sites.lan_stop_all()  # nor do LAN shares (Apache reloads without them if it stays up)
            if stop_services:
                try:
                    manager.stop_all()
                except Exception:
                    logger.exception("Stopping services on quit failed")
            logger.info("Services handled; closing")
            main_thread.exit_loop.emit()  # queued to the Qt thread; never call app.quit() from here

        threading.Thread(target=work, name="quit", daemon=True).start()

    main_thread.quit_requested.connect(shutdown)
    main_thread.exit_loop.connect(app.quit)

    manager.adopt_running()
    sites.lan_stop_all()  # a crashed session must not leave a site on the LAN
    manager.start_monitor()
    panel.mail_watch.start()
    panel.scheduler.start()
    panel.php_errors.start()
    sites.start_watching()
    panel.run_task("Autostart", manager.start_autostart)
    if not args.no_browser:
        webbrowser.open(server.login_url)

    try:
        return app.exec()
    finally:
        tray.hide()
        tunnels.stop_all()
        sites.stop_watching()
        manager.shutdown()
        panel.shutdown()
        server.shutdown()
        info.unlink(missing_ok=True)
        lock.unlock()
        logger.info("varsha exited")


if __name__ == "__main__":
    sys.exit(main())
