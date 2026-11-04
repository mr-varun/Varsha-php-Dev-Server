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
