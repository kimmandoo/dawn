#!/usr/bin/env python3
"""Exercise the packaged downloader and native DPI engine using local media."""
import argparse
import functools
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit
import wave


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dpi-bypass", action="store_true")
    parser.add_argument("--source", action="store_true", help="Run main.py instead of a built executable")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        executable = root / "dist" / "Dawn.exe"
    elif sys.platform == "darwin":
        executable = root / "dist" / "Dawn.app" / "Contents" / "MacOS" / "Dawn"
    else:
        executable = root / "dist" / "Dawn"

    class MediaHandler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            # Proxied HTTP may use absolute-form request targets (RFC 9112).
            return super().translate_path(urlsplit(path).path)

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="dawn-package-smoke-") as directory:
        media = Path(directory) / "sample.wav"
        with wave.open(str(media), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(44100)
            audio.writeframes(b"\0\0" * 88200)
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(MediaHandler, directory=directory)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [sys.executable, str(root / "main.py")] if args.source else [str(executable)]
            command += ["--selftest", f"http://127.0.0.1:{server.server_port}/sample.wav"]
            if args.dpi_bypass:
                command.append("--dpi-bypass")
            result = subprocess.run(command, timeout=120, stdout=sys.stdout, stderr=sys.stderr)
            if result.returncode:
                print(f"Download failed with exit code {result.returncode}", file=sys.stderr)
                return 1
            print(("Source" if args.source else "Packaged") + " audio download/conversion passed" + (" with DPI bypass" if args.dpi_bypass else ""))
            return 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
