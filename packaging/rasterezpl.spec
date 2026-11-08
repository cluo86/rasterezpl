# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: a ONE-FOLDER build of the rasterezpl command line for a machine without Python — the site
laptop. Carries the package's data (the four bundled faces, the web page, the font licences) so a label composed
on the laptop renders dot for dot as on any other machine; pywin32's win32print is included on Windows for the
``win:<queue>`` transport. Build with ``pyinstaller packaging/rasterezpl.spec`` from the repository root; the
result is ``dist/rasterezpl/rasterezpl[.exe]`` beside its ``_internal`` folder — zip the folder.

A one-FILE build is deliberately not used: it unpacks itself into a temp folder at every start (slow, and
antivirus on a site laptop dislikes it); one folder starts at once and its fonts are plain files."""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("rasterezpl")  # fonts/*.ttf, fonts/*.txt, fonts/LICENSES.md, web/index.html
hidden = ["segno", "yaml", "PIL.ImageFont", "PIL.ImageDraw", "PIL.PngImagePlugin", "PIL.JpegImagePlugin"]
if sys.platform == "win32":
    hidden += ["win32print", "win32api", "pywintypes"]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],  # SPECPATH: this file's folder, wherever pyinstaller is run from
    pathex=[os.path.join(SPECPATH, "..", "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rasterezpl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # a command-line tool: the summaries and the plan print to the console
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rasterezpl")
