import os
import sys


def _selftest(url: str) -> int:
    """Headless download verification for CI / packaged builds."""
    import tempfile
    import threading

    from dawn.downloader import DownloadJob, run_job

    outdir = tempfile.mkdtemp(prefix="dawn-selftest-")
    job = DownloadJob(url=url, mode="audio", quality="192", outdir=outdir)
    last = {}

    def cb(d):
        last.update(d)
        if d.get("event") != "progress":
            print("[selftest]", d)

    run_job(job, cb, threading.Event())
    ok = last.get("event") == "done"
    files = os.listdir(outdir)
    mp3s = [f for f in files if f.endswith(".mp3")]
    print("[selftest] result:", "OK" if ok else "FAIL", "| mp3:", mp3s)
    return 0 if ok and mp3s else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest(sys.argv[2]))

    from dawn.app import main

    sys.exit(main())
