#!/usr/bin/env python3
"""Install pinned native censorship-bypass engines for a Dawn build.

Run: python scripts/fetch_bypass.py
Windows x64 uses GoodbyeDPI; macOS/Linux use SpoofDPI. Only allowlisted engine
files and licenses are installed. Importing this module has no side effects.
"""

import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile


VERSION = "1.5.5"
RELEASE_URL = f"https://github.com/xvzc/spoofdpi/releases/download/v{VERSION}"
# Pinned from the official checksums.txt and independently matched against each
# asset's `digest` in https://api.github.com/repos/xvzc/spoofdpi/releases/tags/v1.5.5
# https://github.com/xvzc/spoofdpi/releases/download/v1.5.5/checksums.txt
ARCHIVE_SHA256 = {
    ("darwin", "arm64"): "e9948c59e52f6b983846c960278bbd1b07d4f63204ff434a9e2ce08da5a1d707",
    ("darwin", "x86_64"): "f3a9023fb4be6a2c17f10fd541b91e0c4d9cb636bdb46055c84f0afb4fffaf36",
    ("linux", "arm64"): "b8b389314c5906247a6ab209a81599c27a4766c400ba30af7de29fd1becd5c24",
    ("linux", "x86_64"): "654f54219f2ec099d4bd0ebbc99d907450efe7f08b45c51d8bd079671c8585fa",
}

GOODBYEDPI_VERSION = "0.2.2"
GOODBYEDPI_URL = (
    "https://github.com/ValdikSS/GoodbyeDPI/releases/download/0.2.2/goodbyedpi-0.2.2.zip"
)
# SHA256 of the official release archive retrieved over HTTPS. Unlike SpoofDPI,
# this old release has no publisher checksum and its GitHub asset digest is null:
# https://api.github.com/repos/ValdikSS/GoodbyeDPI/releases/tags/0.2.2
# Pinning here prevents trusting a checksum supplied by a future download.
GOODBYEDPI_SHA256 = "00a2f8b99cd817f8c7fc4c449033015f039d18af213de78cb66bf202277c0628"
GOODBYEDPI_FILES = (
    "licenses/LICENSE-getline.txt",
    "licenses/LICENSE-goodbyedpi.txt",
    "licenses/LICENSE-uthash.txt",
    "licenses/LICENSE-windivert.txt",
    "x86_64/WinDivert.dll",
    "x86_64/WinDivert64.sys",
    "x86_64/goodbyedpi.exe",
)


def release_asset() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    key = (sys.platform, arch)
    if key not in ARCHIVE_SHA256:
        raise RuntimeError(
            f"SpoofDPI v{VERSION} is not packaged for {sys.platform}/{machine}; "
            "use a native macOS or Linux arm64/x86_64 build environment."
        )
    return f"spoofdpi_{VERSION}_{sys.platform}_{arch}.tar.gz", ARCHIVE_SHA256[key]


def download(url: str, destination: Path, expected_hash: str) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Dawn-build"})
    print(f"Downloading {url}")
    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("xb") as archive_file:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                archive_file.write(chunk)
    actual_hash = digest.hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"SHA256 mismatch: expected {expected_hash}, received {actual_hash}. "
            "Nothing was installed."
        )


def install_spoofdpi() -> Path:
    asset, expected_hash = release_asset()
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    destination = vendor / "spoofdpi"
    for directory in (vendor, destination):
        if directory.is_symlink():
            raise RuntimeError(f"Refusing to install into symlink: {directory}")
        directory.mkdir(exist_ok=True)

    # Always obtain and authenticate the archive, rather than trusting an old
    # executable or a mutable local checksum receipt from a previous build.
    with tempfile.TemporaryDirectory(prefix=".spoofdpi-", dir=vendor) as temporary:
        staging = Path(temporary)
        archive_path = staging / asset
        download(f"{RELEASE_URL}/{asset}", archive_path, expected_hash)

        # Never use extract()/extractall(): archive names and link targets must
        # not choose output paths. Only these two root-level regular files count.
        wanted = {"spoofdpi": "spoofdpi", "LICENSE": "LICENSE.spoofdpi"}
        found = set()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                if member.name in found or not member.isfile() or member.issparse():
                    raise RuntimeError(f"Invalid or duplicate archive member: {member.name}")
                found.add(member.name)
                source = archive.extractfile(member)
                if source is None or member.size == 0:
                    raise RuntimeError(f"Empty archive member: {member.name}")
                output = staging / wanted[member.name]
                with source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                if output.stat().st_size != member.size:
                    raise RuntimeError(f"Incomplete archive member: {member.name}")
        if found != wanted.keys():
            raise RuntimeError(f"Archive is missing required files: {sorted(wanted.keys() - found)}")

        (staging / "spoofdpi").chmod(0o755)
        (staging / "LICENSE.spoofdpi").chmod(0o644)
        # Replacing files does not follow existing destination symlinks. Publish
        # the executable last, after both authenticated members are ready.
        for name in ("LICENSE.spoofdpi", "spoofdpi"):
            os.replace(staging / name, destination / name)
    print(f"Installed SpoofDPI v{VERSION} and license in {destination}")
    print(f"Verified archive SHA256: {expected_hash}")
    return destination / "spoofdpi"


def install_goodbyedpi() -> Path:
    machine = platform.machine().lower()
    if machine not in ("amd64", "x86_64"):
        raise RuntimeError(
            f"GoodbyeDPI is packaged only for Windows x64, not {machine}; "
            "build on an x64 Windows machine."
        )
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    destination = vendor / "goodbyedpi"
    for directory in (vendor, destination, destination / "licenses", destination / "x86_64"):
        if directory.is_symlink():
            raise RuntimeError(f"Refusing to install into symlink: {directory}")
        directory.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".goodbyedpi-", dir=vendor) as temporary:
        staging = Path(temporary)
        archive_path = staging / "goodbyedpi.zip"
        download(GOODBYEDPI_URL, archive_path, GOODBYEDPI_SHA256)
        prefix = f"goodbyedpi-{GOODBYEDPI_VERSION}/"
        wanted = {prefix + name: name for name in GOODBYEDPI_FILES}
        found = set()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.filename not in wanted:
                    continue
                mode = member.external_attr >> 16
                if member.filename in found or not stat.S_ISREG(mode) or member.is_dir():
                    raise RuntimeError(f"Invalid or duplicate archive member: {member.filename}")
                if not member.file_size:
                    raise RuntimeError(f"Empty archive member: {member.filename}")
                found.add(member.filename)
                output = staging / wanted[member.filename]
                output.parent.mkdir(exist_ok=True)
                with archive.open(member) as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                if output.stat().st_size != member.file_size:
                    raise RuntimeError(f"Incomplete archive member: {member.filename}")
        if found != wanted.keys():
            raise RuntimeError(f"Archive is missing required files: {sorted(wanted.keys() - found)}")

        # The fixed allowlist publishes the executable last; scripts and service
        # installers from the archive are intentionally never extracted or run.
        for name in GOODBYEDPI_FILES:
            os.replace(staging / name, destination / name)
    print(f"Installed GoodbyeDPI v{GOODBYEDPI_VERSION}, WinDivert and licenses in {destination}")
    print(f"Verified archive SHA256: {GOODBYEDPI_SHA256}")
    return destination / "x86_64" / "goodbyedpi.exe"


def main() -> int:
    try:
        if sys.platform == "win32":
            install_goodbyedpi()
        else:
            install_spoofdpi()
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError) as error:
        print(f"Bypass engine installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
