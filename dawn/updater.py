"""앱 자동 업데이트: GitHub 릴리즈 확인 → 다운로드 → 자기 교체 후 재시작.

PyInstaller frozen 빌드에서만 자기 교체를 수행한다. 개발 모드(scripts 실행)나
실패 시에는 다운로드 폴더/릴리즈 페이지를 여는 것으로 폴백한다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Optional, Tuple

REPO = "kimmandoo/dawn"
_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_UA = {"User-Agent": "Dawn-Updater", "Accept": "application/vnd.github+json"}


def _version_tuple(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _asset_name() -> str:
    if sys.platform == "darwin":
        return "Dawn-macos-arm64.zip"
    if os.name == "nt":
        return "Dawn-windows-x64.zip"
    return ""


def check_latest(timeout: int = 8) -> Optional[Tuple[str, str]]:
    """-> (tag, asset_download_url). None on failure or wrong platform asset."""
    want = _asset_name()
    if not want:
        return None
    try:
        req = urllib.request.Request(_RELEASE_API, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    tag = data.get("tag_name") or ""
    for a in data.get("assets") or []:
        if a.get("name") == want:
            return tag, a.get("browser_download_url") or ""
    return tag, ""


def download(url: str, cb=None, timeout: int = 30) -> str:
    """Download asset to a temp zip, calling cb(fraction). Returns zip path."""
    fd, zpath = tempfile.mkstemp(prefix="dawn-update-", suffix=".zip")
    req = urllib.request.Request(url, headers={"User-Agent": "Dawn-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, os.fdopen(fd, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if cb and total:
                cb(min(got / total, 1.0))
    return zpath


def _app_root() -> Tuple[str, str]:
    """(path-to-replace, relaunch-command-target).

    macOS .app bundle → the bundle dir. Windows single-file exe → the exe path.
    """
    exe = sys.executable
    if sys.platform == "darwin":
        # …/Dawn.app/Contents/MacOS/Dawn → …/Dawn.app
        bundle = exe
        for _ in range(3):
            bundle = os.path.dirname(bundle)
        return bundle, bundle
    return exe, exe


def apply_and_restart(zip_path: str) -> None:
    """Extract the update over the running install and relaunch.

    Spawns a detached script that waits for this process to exit, swaps the
    files, then relaunches. Raises on failure — caller shows fallback.
    """
    extract_dir = tempfile.mkdtemp(prefix="dawn-update-x-")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    if sys.platform == "darwin":
        # find the .app inside the archive (may be nested one level)
        app = None
        for root, dirs, _files in os.walk(extract_dir):
            for d in dirs:
                if d.endswith(".app"):
                    app = os.path.join(root, d)
                    break
            if app:
                break
        if not app:
            raise RuntimeError("update archive has no .app")
        _apply_macos(app)
    else:
        # find the new Dawn.exe
        new_exe = None
        for root, _dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower() == "dawn.exe":
                    new_exe = os.path.join(root, f)
                    break
            if new_exe:
                break
        if not new_exe:
            raise RuntimeError("update archive has no Dawn.exe")
        _apply_windows(os.path.dirname(new_exe))


def _apply_macos(new_app: str) -> None:
    target, _ = _app_root()
    parent = os.path.dirname(target)  # e.g. /Applications or ~/Downloads
    if not os.access(parent, os.W_OK):
        raise PermissionError(f"cannot write {parent}")
    script = tempfile.NamedTemporaryFile(
        "w", prefix="dawn-upd-", suffix=".sh", delete=False
    )
    script.write(
        "#!/bin/bash\n"
        f"while pgrep -x '{os.path.basename(sys.executable)}' >/dev/null; do sleep 0.5; done\n"
        f"rm -rf '{target}'\n"
        f"mv '{new_app}' '{target}'\n"
        f"xattr -dr com.apple.quarantine '{target}' 2>/dev/null\n"
        f"open '{target}'\n"
    )
    script.close()
    os.chmod(script.name, stat.S_IRWXU)
    subprocess.Popen(
        ["bash", script.name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _apply_windows(new_dir: str) -> None:
    target_exe, _ = _app_root()
    install_dir = os.path.dirname(target_exe)
    if not os.access(install_dir, os.W_OK):
        raise PermissionError(f"cannot write {install_dir}")
    bat = tempfile.NamedTemporaryFile(
        "w", prefix="dawn-upd-", suffix=".bat", delete=False
    )
    bat.write(
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {os.getpid()}" | find "{os.getpid()}" >nul\r\n'
        "if %errorlevel%==0 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        f'xcopy /E /Y /I "{new_dir}" "{install_dir}" >nul\r\n'
        f'start "" "{target_exe}"\r\n'
        'del "%~f0"\r\n'
    )
    bat.close()
    subprocess.Popen(
        ["cmd", "/c", bat.name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
