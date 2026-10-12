# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller glowtray.spec
# Paths are relative to this file, so the build works from any checkout location.
import os

ROOT = os.path.abspath(SPECPATH)
APP = os.path.join(ROOT, "app")

a = Analysis(
    [os.path.join(APP, "main.py")],
    pathex=[APP],
    binaries=[],
    datas=[(os.path.join(APP, "ui"), "ui")],
    hiddenimports=["pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Glowtray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(APP, "icon.ico")],
)
