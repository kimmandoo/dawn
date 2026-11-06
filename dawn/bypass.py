"""Job-scoped SpoofDPI proxies and a shared Windows GoodbyeDPI process."""
from __future__ import annotations

import atexit
import ctypes
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator


class BypassError(RuntimeError):
    pass


_LOG_LIMIT = 8192
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_registry_lock = threading.Lock()
_children: set[_Child] = set()
_shutting_down = False
_windows_lock = threading.Lock()
_windows_child: _Child | None = None
_windows_users = 0
_port_lock = threading.Lock()
_ports: set[int] = set()


def is_admin() -> bool:
    """Return whether this Windows process is elevated; never request elevation."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def find_bypass_engine() -> str | None:
    """Find an installed engine without downloading or modifying anything."""
    if sys.platform == "win32":
        if platform.machine().lower() not in {"amd64", "x86_64"}:
            return None
        relative = Path("goodbyedpi/x86_64/goodbyedpi.exe")
        executable = "goodbyedpi.exe"
    elif sys.platform in {"darwin", "linux"}:
        relative = Path("spoofdpi")
        executable = "spoofdpi"
    else:
        return None

    candidates: list[Path] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.append(Path(bundled) / relative)
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    candidates.append(vendor / relative if sys.platform == "win32"
                      else vendor / "spoofdpi" / executable)
    on_path = shutil.which(executable)
    if on_path:
        candidates.append(Path(on_path))
    if sys.platform != "win32":
        candidates.extend(Path(base) / executable
                          for base in ("/opt/homebrew/bin", "/usr/local/bin"))
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if sys.platform == "win32" and not all(
            (candidate.parent / name).is_file()
            for name in ("WinDivert.dll", "WinDivert64.sys")
        ):
            continue
        return str(candidate.resolve())
    return None


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise BypassError("DPI 우회 연결 준비가 취소되었습니다.")
    if _shutting_down:
        raise BypassError("Dawn이 종료 중이므로 DPI 우회를 시작할 수 없습니다.")


class _Child:
    """Drain output continuously, retaining only a bounded diagnostic tail."""

    def __init__(self, command: list[str], name: str):
        self.name = name
        self._log = bytearray()
        self._log_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopped = False
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {
            "start_new_session": True,
        }
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=str(Path(command[0]).parent),
            **options,
        )
        # Daemon status allows atexit to run; stop() still joins this owned thread.
        try:
            self._reader = threading.Thread(
                target=self._drain, name=f"Dawn-{name}-{self.process.pid}", daemon=True,
            )
            self._reader.start()
        except BaseException:
            self._terminate()
            self.process.stdout.close()
            raise

    def _drain(self) -> None:
        try:
            while chunk := self.process.stdout.read(4096):
                with self._log_lock:
                    self._log.extend(chunk)
                    del self._log[:-_LOG_LIMIT]
        except OSError as exc:
            with self._log_lock:
                self._log.extend(f"\n로그 읽기 실패: {exc}".encode("utf-8"))
                del self._log[:-_LOG_LIMIT]

    def diagnostics(self) -> str:
        with self._log_lock:
            text = self._log.decode("utf-8", errors="replace")
        text = _ANSI.sub("", text)
        return "".join(char for char in text if char.isprintable() or char in "\n\t").strip()

    def error(self, message: str) -> BypassError:
        detail = self.diagnostics()
        return BypassError(f"{self.name}: {message}" + (f"\n{detail}" if detail else ""))

    def _terminate(self) -> None:
        if self.process.poll() is None:
            try:
                if sys.platform == "win32":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform == "win32":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()
        # SpoofDPI can invoke short-lived route-discovery helpers. Do not leave
        # descendants holding the output pipe open after their parent exits.
        if sys.platform != "win32":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._terminate()
            self._reader.join()
            self.process.stdout.close()
            self._stopped = True
            with _registry_lock:
                _children.discard(self)


def _spawn(command: list[str], name: str, cancel: threading.Event) -> _Child:
    with _registry_lock:
        _check_cancel(cancel)
        try:
            child = _Child(command, name)
        except (OSError, RuntimeError) as exc:
            raise BypassError(
                f"{name} 실행 실패: {exc}\n"
                "운영체제/CPU에 맞는 배포 파일과 실행 권한을 확인하고 Dawn을 다시 설치해 주세요."
            ) from exc
        _children.add(child)
    try:
        _check_cancel(cancel)
    except BaseException:
        child.stop()
        raise
    return child


def _wait_ready(child: _Child, cancel: threading.Event, port: int | None = None) -> None:
    started = time.monotonic()
    windows = port is None
    deadline = started + (22 if windows else 15)
    marker = ("Filter activated, GoodbyeDPI is now running!" if windows
              else f"server started on 127.0.0.1:{port};")
    while True:
        _check_cancel(cancel)
        if child.process.poll() is not None:
            # Reap and finish draining before rendering the final stderr tail.
            child.stop()
            raise child.error("시작 중 종료되었습니다. 배포 파일과 네트워크/보안 설정을 확인해 주세요.")
        if time.monotonic() >= deadline:
            raise child.error("연결 준비 시간이 초과되었습니다. 인터넷 연결과 방화벽을 확인해 주세요.")
        log = child.diagnostics()
        if windows and "Error opening filter:" in log:
            raise child.error("WinDivert 필터를 열 수 없습니다. 관리자 권한과 보안 프로그램의 차단 여부를 확인해 주세요.")
        if not windows:
            version = re.search(r"\bversion=\"?(v?[^\s\";]+)", log)
            if version and version.group(1).removeprefix("v") != "1.5.5":
                raise child.error("SpoofDPI v1.5.5가 필요합니다. 동봉된 엔진을 다시 설치해 주세요.")
        if marker in log:
            if not windows:
                if not version:
                    raise child.error("엔진 버전을 확인할 수 없습니다. SpoofDPI v1.5.5를 다시 설치해 주세요.")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                except OSError:
                    cancel.wait(0.05)
                    continue
            _check_cancel(cancel)
            if child.process.poll() is None:
                return
        # GoodbyeDPI 0.2.2 may buffer printf when stdout is redirected. Its
        # filter-init failure path sleeps 20 seconds before exit. Waiting past
        # that delay is a conservative liveness check, not proof of filtering.
        if windows and time.monotonic() - started >= 21:
            _check_cancel(cancel)
            if child.process.poll() is None:
                return
        cancel.wait(0.05)


@contextmanager
def _reserved_port() -> Iterator[int]:
    # v1.5.5 reports the configured address, not the actual bound port, so :0
    # cannot be used directly. Keep in-flight Dawn jobs from choosing one port.
    try:
        with _port_lock, ExitStack() as sockets:
            while True:
                reservation = sockets.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
                if port not in _ports:
                    _ports.add(port)
                    break
    except OSError as exc:
        raise BypassError(f"SpoofDPI 로컬 포트를 준비하지 못했습니다: {exc}") from exc
    try:
        yield port
    finally:
        with _port_lock:
            _ports.discard(port)


def _check_spoofdpi_state() -> None:
    # Upstream resets hardcoded state files even with --clean and auto-config
    # disabled. Never touch another instance's state. This preflight cannot
    # protect against an external instance creating state during startup.
    names = ("spoofdpi.http.darwin.state", "spoofdpi.socks5.darwin.state",
             "spoofdpi.darwin.tun.state") if sys.platform == "darwin" else (
                 "spoofdpi.linux.tun.state",
             )
    for name in names:
        path = Path("/tmp") / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BypassError(f"SpoofDPI 상태 파일을 확인할 수 없습니다: {path}\n{exc}") from exc
        raise BypassError(
            f"외부 SpoofDPI의 네트워크 상태 파일이 있습니다: {path}\n"
            "시스템 설정을 건드리지 않도록 시작을 중단했습니다. "
            "별도로 실행한 SpoofDPI를 정상 종료한 뒤 다시 시도해 주세요."
        )


@contextmanager
def _spoofdpi(executable: str, cancel: threading.Event) -> Iterator[str]:
    _check_spoofdpi_state()
    with _reserved_port() as port:
        command = [
            executable, "--clean", "--no-tui", "--app-mode", "http",
            "--listen-addr", f"127.0.0.1:{port}",
            "--dns-mode", "https", "--dns-https-url", "https://1.1.1.1/dns-query",
            "--https-split-mode", "sni", "--log-level", "info",
            "--auto-configure-network=false",
        ]
        child = _spawn(command, "SpoofDPI", cancel)
        try:
            _wait_ready(child, cancel, port)
            yield f"http://127.0.0.1:{port}"
            _check_cancel(cancel)
            if child.process.poll() is not None:
                raise child.error("다운로드 중 엔진이 종료되었습니다. 네트워크/보안 설정을 확인해 주세요.")
        finally:
            child.stop()


@contextmanager
def _goodbyedpi(executable: str, cancel: threading.Event) -> Iterator[str]:
    global _windows_child, _windows_users
    if not is_admin():
        raise BypassError(
            "Windows DPI 우회는 관리자 권한이 필요합니다. "
            "Dawn의 '관리자 권한으로 다시 열기'를 선택한 뒤 다운로드를 다시 시도해 주세요."
        )
    while not _windows_lock.acquire(timeout=0.1):
        _check_cancel(cancel)
    try:
        _check_cancel(cancel)
        if _windows_child is None:
            child = _spawn([
                executable, "-p", "-r", "-s", "-e", "2",
                "--native-frag", "--max-payload",
                "--dns-addr", "1.1.1.1", "--dns-port", "53",
            ], "GoodbyeDPI", cancel)
            try:
                _wait_ready(child, cancel)
                _check_cancel(cancel)
            except BaseException:
                child.stop()
                raise
            _windows_child = child
        child = _windows_child
        if child.process.poll() is not None:
            raise child.error("공유 우회 엔진이 종료되었습니다. 진행 중인 작업을 중지한 뒤 다시 시도해 주세요.")
        _windows_users += 1
    finally:
        _windows_lock.release()
    try:
        _check_cancel(cancel)
        # An empty proxy explicitly disables inherited proxies in yt-dlp.
        # GoodbyeDPI filters Windows traffic, rather than serving a proxy.
        yield ""
        _check_cancel(cancel)
        if child.process.poll() is not None:
            raise child.error("다운로드 중 우회 엔진이 종료되었습니다. WinDivert 차단 여부를 확인해 주세요.")
    finally:
        with _windows_lock:
            _windows_users -= 1
            if _windows_users == 0:
                _windows_child = None
                child.stop()


@contextmanager
def bypass_connection(cancel: threading.Event) -> Iterator[str]:
    """Yield a job proxy URL, or '' while Windows packet filtering is active."""
    _check_cancel(cancel)
    if sys.platform not in {"darwin", "linux", "win32"}:
        raise BypassError("이 운영체제는 DPI 우회를 지원하지 않습니다. macOS, Linux 또는 Windows x64를 사용해 주세요.")
    if sys.platform == "win32" and platform.machine().lower() not in {"amd64", "x86_64"}:
        raise BypassError("동봉된 WinDivert 드라이버는 Windows x64 전용입니다. Windows x64에서 실행해 주세요.")
    executable = find_bypass_engine()
    if executable is None:
        raise BypassError(
            "DPI 우회 엔진을 찾을 수 없습니다. Dawn 배포 파일을 다시 설치해 주세요. "
            "개발 환경에서는 python scripts/fetch_bypass.py를 실행해 주세요. "
            "Windows는 goodbyedpi.exe, WinDivert.dll, WinDivert64.sys가 함께 있어야 합니다."
        )
    manager = _goodbyedpi if sys.platform == "win32" else _spoofdpi
    with manager(executable, cancel) as proxy:
        yield proxy


def shutdown_bypass() -> None:
    """Permanently prevent new launches and stop only Dawn-owned processes."""
    global _shutting_down
    with _registry_lock:
        _shutting_down = True
        children = tuple(_children)
    for child in children:
        child.stop()


atexit.register(shutdown_bypass)
