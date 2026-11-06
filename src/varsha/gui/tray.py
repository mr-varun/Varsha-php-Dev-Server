"""The tray icon: varsha's only native UI. Everything else lives in the browser panel.

The menu is rebuilt each time it opens, so new sites in www\\ show up without a restart.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState
from varsha.sites.discovery import Site

PHPMYADMIN = "http://localhost/phpmyadmin/"


class Tray:
    def __init__(self, icon: Path, panel_url: Callable[[], str], manager: ServiceManager,
                 run: Callable[[str, Callable[[], object]], None], quit_app: Callable[[bool], None],
                 sites: Callable[[], list[Site]] | None = None) -> None:
        self.manager = manager
        self.panel_url = panel_url
        self.run = run
        self.quit_app = quit_app
        self.sites = sites or (lambda: [])
        self.tray = QSystemTrayIcon(QIcon(str(icon)))
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._rebuild)
        self._rebuild()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self._timer = QTimer()
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._update_tooltip)
        self._timer.start()
        self._update_tooltip()
        self.tray.show()

    # ------------------------------------------------------------------ menu

    def _add(self, menu: QMenu, text: str, slot: Callable[[], object], bold: bool = False,
             enabled: bool = True) -> QAction:
        action = QAction(text, menu)
        if bold:
            font = action.font()
            font.setBold(True)
            action.setFont(font)
        action.setEnabled(enabled)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _rebuild(self) -> None:
        menu = self.menu
        menu.clear()
        self._add(menu, "Open control panel", self.open_panel, bold=True)
        self._add(menu, "Import Banks Data", self.open_bank_window)
        menu.addSeparator()
        self._add(menu, "Start all", lambda: self.run("Start all", self.manager.start_all))
        self._add(menu, "Restart running services", lambda: self.run("Restart running services", self.manager.restart_running))
        self._add(menu, "Stop all", lambda: self.run("Stop all", self.manager.stop_all))
        menu.addSeparator()

        statuses = {s.id: s for s in self.manager.statuses()}
        apache_up = any(s.type == "apache" and s.state is ServiceState.RUNNING for s in statuses.values())
        sites_menu = menu.addMenu("Sites")
        sites = self.sites()
        for site in sites[:30]:
            url = f"http://{site.domain}/"
            self._add(sites_menu, site.domain, lambda url=url: webbrowser.open(url), enabled=apache_up)
        if not sites:
            self._add(sites_menu, "(no folders in www yet)", lambda: None, enabled=False)
        self._add(menu, "phpMyAdmin", lambda: webbrowser.open(PHPMYADMIN), enabled=apache_up)
        mail = next((s for s in statuses.values() if s.type == "mail"), None)
        if mail is not None and len(mail.ports) > 1:
            inbox = f"http://127.0.0.1:{mail.ports[1]}/"
            self._add(menu, "Mail inbox", lambda: webbrowser.open(inbox), enabled=mail.state is ServiceState.RUNNING)
        menu.addSeparator()
        self._add(menu, "Quit, leave services running", lambda: self.quit_app(False))
        self._add(menu, "Stop all and quit", lambda: self.quit_app(True))

    # ------------------------------------------------------------------ events

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_panel()

    def open_bank_window(self) -> None:
        """The small, always-on-top Import Banks Data window (the full page if no Edge/Chrome)."""
        from varsha.gui.mini_window import open_bank_window
        try:
            if open_bank_window(self.panel_url()):
                return
        except Exception:  # noqa: BLE001 - never lose the action over the window tricks
            pass
        self.open_panel("bankimport")

    def open_panel(self, page: str = "") -> None:
        """Open the control panel, on one of its pages (e.g. "bankimport") when given."""
        webbrowser.open(self.panel_url() + (f"#{page}" if page else ""))

    def _update_tooltip(self) -> None:
        statuses = self.manager.statuses()
        running = sum(s.state is ServiceState.RUNNING for s in statuses)
        self.tray.setToolTip(f"varsha: {running} service{'s' if running != 1 else ''} running\nClick to open the control panel")

    def notify(self, title: str, message: str, error: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title, message[:250], icon, 6000)

    def hide(self) -> None:
        self._timer.stop()
        self.tray.hide()
