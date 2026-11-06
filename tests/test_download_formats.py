"""Real local downloads: output formats must coexist and keep separate history."""
import collections
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import functools
import hashlib
import http.server
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from PIL import Image
from mutagen.mp3 import MP3

from dawn.downloader import DownloadJob, _FFMPEG_EXE, run_job


class DownloadFormatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_tmp = tempfile.TemporaryDirectory(prefix="dawn-format-source-")
        source = Path(cls.source_tmp.name)
        subprocess.run([
            _FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
            str(source / "sample.mp4"),
        ], check=True)
        Image.new("RGB", (320, 180), "#EAC674").save(source / "cover.jpg")
        (source / "watch.html").write_text(
            '<html><head><title>Sample</title></head><body>'
            '<video src="sample.mp4" poster="cover.jpg"></video></body></html>',
            encoding="utf-8",
        )
        cls.requests = collections.Counter()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                cls.requests[self.path] += 1
                super().do_GET()
            def log_message(self, *args):
                pass
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Handler, directory=str(source))
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.source_tmp.cleanup()

    def setUp(self):
        self.output_tmp = tempfile.TemporaryDirectory(prefix="dawn-format-output-")
        self.addCleanup(self.output_tmp.cleanup)
        self.output = Path(self.output_tmp.name)
        self.job = DownloadJob(
            url=self.base_url + "/sample.mp4", outdir=str(self.output), proxy=""
        )
        self.requests.clear()

    def download(self, job, terminal="done"):
        events = []
        run_job(job, events.append, threading.Event())
        self.assertEqual(events[-1]["event"], terminal, events[-1])
        return events

    def assert_video(self, path):
        subprocess.run([
            _FFMPEG_EXE, "-v", "error", "-i", str(path), "-map", "0:v:0",
            "-frames:v", "1", "-f", "null", "-",
        ], check=True, capture_output=True)

    def test_audio_then_video_ignores_ambiguous_legacy_history(self):
        (self.output / ".dawn-archive.txt").write_text("generic sample\n")
        self.download(replace(self.job, mode="audio", quality="0"))
        audio = self.output / "sample.mp3"
        before = hashlib.sha256(audio.read_bytes()).digest()
        self.download(self.job)
        self.assert_video(self.output / "sample.mp4")
        self.assertEqual(hashlib.sha256(audio.read_bytes()).digest(), before)

    def test_audio_conversion_preserves_previously_saved_video(self):
        self.download(self.job)
        video = self.output / "sample.mp4"
        before = hashlib.sha256(video.read_bytes()).digest()
        for codec in ("mp3", "m4a", "flac"):
            with self.subTest(codec=codec):
                self.download(replace(self.job, mode="audio", audio_codec=codec, quality="0"))
                self.assertTrue((self.output / f"sample.{codec}").is_file())
                self.assertEqual(hashlib.sha256(video.read_bytes()).digest(), before)

    def test_audio_and_video_can_download_concurrently(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.download, job) for job in (
                self.job, replace(self.job, mode="audio", quality="0"),
            )]
            for future in futures:
                future.result()
        self.assert_video(self.output / "sample.mp4")
        self.assertGreater(MP3(self.output / "sample.mp3").info.length, 0.5)
        self.assertFalse(any(path.is_dir() for path in self.output.iterdir()))

    def test_same_format_reports_skip_and_explicit_redownload_fetches_media(self):
        audio = replace(self.job, mode="audio", quality="0")
        self.download(audio)
        self.download(audio, terminal="skipped")
        before = self.requests["/sample.mp4"]
        self.download(replace(audio, dedup=False))
        self.assertGreater(self.requests["/sample.mp4"], before)
        self.assertGreater(MP3(self.output / "sample.mp3").info.length, 0.5)

    def test_downloaded_cover_is_reused_for_preview_and_audio_tags(self):
        self.download(replace(self.job, url=self.base_url + "/watch.html", mode="audio", quality="0"))
        self.assertEqual(self.requests["/cover.jpg"], 1)
        audio = MP3(next(self.output.glob("*.mp3")))
        self.assertEqual(audio.tags.getall("APIC")[0].mime, "image/jpeg")

    def test_cli_download_succeeds_with_legacy_console_encoding(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"),
             "--selftest", self.job.url],
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
