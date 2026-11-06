# PyInstaller spec for the frozen Varsha.exe (one folder: Varsha.exe + _internal\).
# Built by tools\deploy.py; by hand:  .venv\Scripts\python -m PyInstaller tools\varsha.spec
# -*- mode: python -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is set by PyInstaller)
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

datas = [
    (str(SRC / "varsha" / "web" / "static"), "varsha/web/static"),
    # run by path under a 32-bit Python (the Access ODBC driver is 32-bit), so it must be a real .py file
    (str(SRC / "varsha" / "data" / "access_odbc.py"), "varsha/data"),
    # run by path under the MDB rescue tool's own Python
    (str(SRC / "varsha" / "data" / "rescue_runner.py"), "varsha/data"),
    # and the MySQL rescue tool's
    (str(SRC / "varsha" / "data" / "mysql_rescue_runner.py"), "varsha/data"),
    (str(REPO / "etc" / "templates"), "etc/templates"),
    (str(REPO / "config" / "services.json"), "config"),
    (str(REPO / "config" / "settings.json"), "config"),
]

a = Analysis(  # noqa: F821
    [str(SRC / "varsha" / "__main__.py")],
    pathex=[str(SRC)],
    datas=datas,
    # --run-module loads modules by name (DNS service, megha), so include every varsha module.
    hiddenimports=collect_submodules("varsha"),
    excludes=["tkinter", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
               "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Varsha",
    console=False,  # tray app: no console window
    icon=str(SRC / "varsha" / "web" / "static" / "varsha.ico"),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Varsha")  # noqa: F821
