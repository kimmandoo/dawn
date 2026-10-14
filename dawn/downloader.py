"""yt-dlp download engine: jobs, progress callbacks, cookie support,
audio extraction with square album-art embedding."""
from __future__ import annotations

import os
import re
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

import yt_dlp

def _find_ffmpeg() -> str:
    """Bundled imageio-ffmpeg binary; under PyInstaller it lands in
    sys._MEIPASS. Falls back to PATH."""
    import sys

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(exe):
            return exe
    except Exception:
        pass
    for base in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.argv[0])):
        if not base:
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.startswith("ffmpeg"):
                    return os.path.join(root, f)
    return "ffmpeg"


_FFMPEG_EXE = _find_ffmpeg()



class Cancelled(Exception):
    pass


@dataclass
class CookieConfig:
    """성인 인증/로그인 필요 영상용 쿠키.

    browser: 'chrome'|'edge'|'firefox'|'safari'|'brave'|'opera'|'vivaldi'|'whale'
    cookie_file: cookies.txt (Netscape 형식) 경로. browser 우선.
    """

    browser: Optional[str] = None
    cookie_file: Optional[str] = None

    def ydl_opts(self) -> dict:
        if self.browser:
            return {"cookiesfrombrowser": (self.browser,)}
        if self.cookie_file:
            return {"cookiefile": self.cookie_file}
        return {}


@dataclass
class DownloadJob:
    url: str
    mode: str = "video"  # 'video' | 'audio'
    outdir: str = ""
    quality: str = "best"  # video: 'best'|'1080'|'720'|'480'; audio: '0'|'320'|'192'|'128'
    cookies: CookieConfig = field(default_factory=CookieConfig)
    playlist: bool = False


def _format_selector(quality: str) -> str:
    if quality == "1080":
        return "bv*[height<=1080]+ba/b[height<=1080]/b"
    if quality == "720":
        return "bv*[height<=720]+ba/b[height<=720]/b"
    if quality == "480":
        return "bv*[height<=480]+ba/b[height<=480]/b"
    return "bv*+ba/b"


class _NullLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _js_runtimes() -> dict:
    """Locate deno for yt-dlp's JS challenge solving (n-sig / bot checks).
    Bundled copy (PyInstaller) -> PATH -> common install dirs. When nothing
    is found, return {'deno': {}} so yt-dlp still tries its own discovery —
    {} would DISABLE the default runtime entirely."""
    import sys
    from shutil import which

    exe = "deno.exe" if sys.platform == "win32" else "deno"
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", "") or "", exe),
        os.path.join(os.path.dirname(sys.argv[0]), exe),
        which("deno") or "",
        os.path.expanduser("~/.deno/bin/deno"),
        "/opt/homebrew/bin/deno",
        "/usr/local/bin/deno",
        os.path.join(os.environ.get("USERPROFILE", ""), ".deno", "bin", "deno.exe"),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
        ),  # winget layouts vary; covered below by glob
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return {"deno": {"path": cand}}
    # winget Links dir
    links = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "deno.exe"
    )
    if os.path.isfile(links):
        return {"deno": {"path": links}}
    return {"deno": {}}


def _ydl_opts(job: DownloadJob) -> dict:
    opts = {
        "outtmpl": os.path.join(
            job.outdir,
            "%(playlist_title|)s",
            "%(playlist_index&{:>03} - |)s%(title).150B [%(id)s].%(ext)s",
        ),
        "ffmpeg_location": _FFMPEG_EXE,
        "noplaylist": not job.playlist,
        "ignoreerrors": job.playlist,  # dead/private entries shouldn't kill a playlist batch
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "retries": 3,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        "logger": _NullLogger(),
        "js_runtimes": _js_runtimes(),
        "remote_components": ["ejs:github"],
    }
    opts.update(job.cookies.ydl_opts())
    if job.mode == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": job.quality,  # <=10 -> VBR scale, >10 -> -b:a Nk CBR
            }
        ]
    else:
        opts["format"] = _format_selector(job.quality)
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegMetadata"}]
    return opts


def _best_thumbnail_url(info: dict) -> Optional[str]:
    """Pick the highest-resolution thumbnail. On YouTube the declared list
    often omits maxresdefault.jpg, so probe it first."""
    vid = info.get("id")
    extractor = (info.get("extractor_key") or "").lower()
    if vid and "youtube" in extractor:
        yield_url = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
        try:
            req = urllib.request.Request(
                yield_url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return yield_url
        except Exception:
            pass
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        return info.get("thumbnail")
    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    if (best.get("width") or 0) < 1:  # resolution unknown -> trust order: last is best
        best = thumbs[-1]
    return best.get("url") or info.get("thumbnail")


def _fetch_thumbnail(info: dict, timeout: int = 20) -> Optional[bytes]:
    url = _best_thumbnail_url(info)
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None

_PLAYER_CLIENTS = [["default"], ["android"], ["ios"], ["tv"]]

_RETRYABLE = re.compile(
    r"needs to be reloaded|Video unavailable|Sign in to confirm|"
    r"HTTP Error 403|This video is not available",
    re.IGNORECASE,
)


def _try_download(
    job: DownloadJob,
    progress: ProgressCb,
    progress_hooks: list,
    pp_hooks: list,
    cancel: threading.Event,
) -> Optional[dict]:
    """Download, retrying with alternate YouTube player clients when the
    current IP is bot-gated on the default (web) client."""
    last_exc: Optional[Exception] = None
    for clients in _PLAYER_CLIENTS:
        if cancel.is_set():
            raise Cancelled()
        opts = _ydl_opts(job)
        opts["progress_hooks"] = progress_hooks
        opts["postprocessor_hooks"] = pp_hooks
        if clients != ["default"]:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                progress({"event": "info", "phase": "extract"})
                return ydl.extract_info(job.url, download=True)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if cancel.is_set():
                raise Cancelled()
            last_exc = e
            if not _RETRYABLE.search(str(e)):
                raise
    assert last_exc is not None
    raise last_exc


def _final_files(info: dict):
    """Yield (info_dict, final_filepath) for every downloaded file,
    recursing into playlist entries."""
    if not isinstance(info, dict):
        return
    for rd in info.get("requested_downloads") or []:
        path = rd.get("filepath")
        if path:
            yield info, os.path.normpath(path)
    for entry in info.get("entries") or []:
        yield from _final_files(entry)


def _tag_audio_files(info: dict, progress: ProgressCb, cancel: threading.Event) -> None:
    for ent, path in _final_files(info):
        if cancel.is_set():
            raise Cancelled()
        if not path.lower().endswith(".mp3") or not os.path.exists(path):
            continue
        try:
            raw = _fetch_thumbnail(ent)
            jpeg = _square_jpeg(raw) if raw else None
            embed_audio_metadata(path, ent, jpeg)
            progress({"event": "pp", "name": "tag+art", "path": path})
        except Cancelled:
            raise
        except Exception:
            pass  # tagging must never fail the download

# ---------------------------------------------------------------------------
# Album art: fetch thumbnail -> center-crop to square -> embed via mutagen.
# ---------------------------------------------------------------------------


def _square_jpeg(img_bytes: bytes, size: int = 1280) -> Optional[bytes]:
    """Center-crop to a square and re-encode as JPEG."""
    import io

    from PIL import Image

    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        if side > size:
            img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return None


def embed_audio_metadata(mp3_path: str, info: dict, cover_jpeg: Optional[bytes]) -> None:
    """Write ID3 title/artist/album + square cover art into an mp3."""
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1
    from mutagen.id3 import ID3TimeStamp
    from mutagen.mp3 import MP3

    try:
        audio = MP3(mp3_path)
    except Exception:
        return
    try:
        tags = audio.tags
        if tags is None:
            raise ID3NoHeaderError()
    except ID3NoHeaderError:
        audio.add_tags()
        tags = audio.tags
    title = info.get("track") or info.get("title")
    artist = (
        info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or info.get("channel")
    )
    album = info.get("album") or info.get("playlist") or info.get("playlist_title")
    if title:
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=[str(title)]))
    if artist:
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=[str(artist)]))
    if album:
        tags.delall("TALB")
        tags.add(TALB(encoding=3, text=[str(album)]))
    upload_date = str(info.get("upload_date") or "")  # 'YYYYMMDD'
    if len(upload_date) == 8:
        tags.delall("TDRC")
        tags.add(
            TDRC(
                encoding=3,
                text=[ID3TimeStamp(f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}")],
            )
        )
    if cover_jpeg:
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # cover (front)
                desc="Cover",
                data=cover_jpeg,
            )
        )
    try:
        tags.save(mp3_path, v2_version=3)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

ProgressCb = Callable[[dict], None]


def run_job(
    job: DownloadJob,
    progress: ProgressCb,
    cancel: threading.Event,
) -> None:
    """Run one job to completion. Calls progress(dict) with keys:
    event: 'info'|'progress'|'pp'|'done'|'error'|'cancelled'
    """

    def progress_hook(d: dict) -> None:
        if cancel.is_set():
            raise Cancelled()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            progress(
                {
                    "event": "progress",
                    "fraction": (downloaded / total) if total else None,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "title": (d.get("info_dict") or {}).get("title") or "",
                }
            )
        elif status == "finished":
            progress({"event": "progress", "fraction": 1.0, "phase": "postprocess"})

    def pp_hook(d: dict) -> None:
        if cancel.is_set():
            raise Cancelled()
        if d.get("status") == "started":
            progress({"event": "pp", "name": d.get("postprocessor")})

    try:
        info = _try_download(job, progress, [progress_hook], [pp_hook], cancel)
        if info:
            title = info.get("title") or info.get("id") or ""
            if title:
                progress({"event": "title", "title": title})
        if job.mode == "audio" and info:
            _tag_audio_files(info, progress, cancel)
        progress({"event": "done"})
    except Cancelled:
        progress({"event": "cancelled"})
    except Exception as e:  # noqa: BLE001 - surface message to UI
        if cancel.is_set():
            progress({"event": "cancelled"})
        else:
            progress({"event": "error", "message": str(e)})
