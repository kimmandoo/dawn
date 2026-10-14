# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Windows -> single Dawn.exe, macOS -> Dawn.app.

Build:
    pyinstaller dawn.spec --noconfirm
"""
import sys

from PyInstaller.utils.hooks import collect_data_files

ONE_FILE = sys.platform != "darwin"

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=collect_data_files("yt_dlp_ejs") + collect_data_files("imageio_ffmpeg"),
    hiddenimports=["imageio_ffmpeg", "yt_dlp_ejs"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

if ONE_FILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Dawn",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon="dawn/icon.ico",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Dawn",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="Dawn",
    )
    app = BUNDLE(
        coll,
        name="Dawn.app",
        icon="dawn/icon.icns",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.1.0",
        },
    )
