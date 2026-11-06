# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Windows -> single Dawn.exe, macOS -> Dawn.app.

Build:
    python scripts/fetch_bypass.py
    pyinstaller dawn.spec --noconfirm
"""
import os
from pathlib import Path
import platform
import sys
from shutil import which

from PyInstaller.utils.hooks import collect_data_files

ONE_FILE = sys.platform != "darwin"

# Bundle deno (JS challenge runtime for yt-dlp) so users never install it.
_deno = which("deno")
_extra_bins = [(_deno, ".")] if _deno else []
_extra_data = []

# Managed engines are fetched and SHA256-verified at build time, never at runtime.
_root = Path(SPECPATH)
_arch = platform.machine().lower()
_arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(_arch, _arch)
if sys.platform == "win32" and _arch == "x86_64":
    _engine_files = [
        ("goodbyedpi/x86_64/goodbyedpi.exe", "goodbyedpi/x86_64", True),
        ("goodbyedpi/x86_64/WinDivert.dll", "goodbyedpi/x86_64", True),
        ("goodbyedpi/x86_64/WinDivert64.sys", "goodbyedpi/x86_64", False),
        ("goodbyedpi/licenses/LICENSE-getline.txt", "goodbyedpi/licenses", False),
        ("goodbyedpi/licenses/LICENSE-goodbyedpi.txt", "goodbyedpi/licenses", False),
        ("goodbyedpi/licenses/LICENSE-uthash.txt", "goodbyedpi/licenses", False),
        ("goodbyedpi/licenses/LICENSE-windivert.txt", "goodbyedpi/licenses", False),
    ]
elif sys.platform in ("darwin", "linux") and _arch in ("arm64", "x86_64"):
    _engine_files = [
        ("spoofdpi/spoofdpi", ".", True),
        ("spoofdpi/LICENSE.spoofdpi", ".", False),
    ]
else:
    raise SystemExit(
        f"No managed bypass engine is packaged for {sys.platform}/{_arch}. "
        "Build on Windows x64 or macOS/Linux arm64/x86_64."
    )

for _relative, _target, _binary in _engine_files:
    _source = _root / "vendor" / _relative
    if _source.is_symlink() or not _source.is_file():
        raise SystemExit(
            f"Required bypass engine file is missing or invalid: {_source}\n"
            "Run: python scripts/fetch_bypass.py\nThen rerun PyInstaller."
        )
    if sys.platform != "win32" and _binary and not os.access(_source, os.X_OK):
        raise SystemExit(
            f"Bypass engine is not executable: {_source}\n"
            "Run: python scripts/fetch_bypass.py"
        )
    (_extra_bins if _binary else _extra_data).append((str(_source), _target))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=_extra_bins,
    datas=collect_data_files("yt_dlp_ejs") + collect_data_files("imageio_ffmpeg") + [
        ("dawn/icon.png", "dawn"), ("dawn/mascot.png", "dawn"),
    ] + _extra_data,
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
            "CFBundleShortVersionString": "0.3.2",
        },
    )
