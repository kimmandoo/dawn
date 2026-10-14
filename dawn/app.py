"""Dawn — 초간단 다운로더 UI. 링크를 붙여넣으면 바로 받아진다."""
from __future__ import annotations

import os
import queue
import re
import sys
import threading

from PySide6.QtCore import (
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .downloader import CookieConfig, DownloadJob, detect_local_proxy, run_job

APP_NAME = "Dawn"
BROWSERS = [
    ("사용 안 함", None),
    ("Chrome", "chrome"),
    ("Edge", "edge"),
    ("Firefox", "firefox"),
    ("Safari", "safari"),
    ("Brave", "brave"),
    ("Opera", "opera"),
    ("Vivaldi", "vivaldi"),
    ("Whale", "whale"),
]
VIDEO_QUALITIES = [
    ("최고 화질", "best"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
]
AUDIO_QUALITIES = [
    ("MP3 · 최고 음질 (VBR)", "mp3:0"),
    ("MP3 · 320 kbps", "mp3:320"),
    ("MP3 · 192 kbps", "mp3:192"),
    ("MP3 · 128 kbps", "mp3:128"),
    ("M4A/AAC · 최고 음질", "m4a:0"),
    ("M4A/AAC · 256 kbps", "m4a:256"),
    ("FLAC · 무손실", "flac:0"),
]
URL_RE = re.compile(r"https?://[^\s]*\.[^\s/][^\s]*", re.IGNORECASE)  # host must contain a dot — avoids partial-URL autostart
PLAYLIST_RE = re.compile(r"[?&]list=", re.IGNORECASE)

STATUS_TEXT = {
    "wait": "대기 중",
    "extract": "정보 가져오는 중…",
    "downloading": "다운로드 중",
    "postprocess": "변환/태그 중…",
    "done": "완료 ✓",
    "error": "실패",
    "cancelled": "취소됨",
}
STATUS_COLOR = {
    "done": "#4caf50",
    "error": "#e2554f",
    "cancelled": "#999",
}

def _res(name: str) -> str:
    """Asset path that works in dev (dawn/) and inside the PyInstaller bundle."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "dawn", name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def human_bytes(n) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "-"


def human_speed(s) -> str:
    return f"{human_bytes(s)}/s" if s else ""


def is_playlist_url(url: str) -> bool:
    return bool(PLAYLIST_RE.search(url))


class ElidedLabel(QLabel):
    """QLabel that right-elides long text instead of clipping mid-glyph."""

    def __init__(self, text="", parent=None):
        self._full = text
        super().__init__(text, parent)

    def setText(self, t) -> None:
        self._full = t
        super().setText(t)
        self._elide()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        w = self.width() - 4
        if w > 10 and self._full:
            super().setText(
                self.fontMetrics().elidedText(self._full, Qt.ElideRight, w)
            )



# ---------------------------------------------------------------------------
# Worker thread: FIFO queue of jobs, sequential download.
# ---------------------------------------------------------------------------

class Worker(QThread):
    progressed = Signal(int, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.q: "queue.Queue" = queue.Queue()
        self._cancels = {}
        self._lock = threading.Lock()
        self._last_emit = {}
        self.max_parallel = 2  # UI spinbox adjusts live
        self._running = 0
        self._cv = threading.Condition()
        from concurrent.futures import ThreadPoolExecutor

        self._pool = ThreadPoolExecutor(max_workers=8)

    def _emit(self, job_id: int, d: dict) -> None:
        """Throttle 'progress' to ~80ms/job; lifecycle events always pass."""
        import time

        if d.get("event") == "progress" and d.get("phase") != "postprocess":
            now = time.monotonic()
            if (d.get("fraction") or 0) < 1.0 and now - self._last_emit.get(job_id, 0) < 0.08:
                return
            self._last_emit[job_id] = now
        self.progressed.emit(job_id, d)

    def add(self, job_id: int, job: DownloadJob) -> None:
        with self._lock:
            self._cancels[job_id] = threading.Event()
        self.q.put((job_id, job))

    def cancel(self, job_id: int) -> None:
        with self._lock:
            ev = self._cancels.get(job_id)
        if ev:
            ev.set()

    def stop(self) -> None:
        with self._lock:
            for ev in self._cancels.values():
                ev.set()
        self.q.put(None)

    def run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                self._pool.shutdown(wait=True)
                return
            with self._cv:
                while self._running >= self.max_parallel:
                    self._cv.wait()
                self._running += 1
            job_id, job = item
            ev = self._cancels.get(job_id)
            if ev is None or ev.is_set():
                with self._cv:
                    self._running -= 1
                    self._cv.notify()
                if ev is not None:
                    self.progressed.emit(job_id, {"event": "cancelled"})
                continue
            self._pool.submit(self._run_one, job_id, job, ev)

    def _run_one(self, job_id: int, job: DownloadJob, ev: threading.Event) -> None:
        try:
            run_job(job, lambda d, jid=job_id: self._emit(jid, d), ev)
        finally:
            with self._cv:
                self._running -= 1
                self._cv.notify()
            with self._lock:
                self._cancels.pop(job_id, None)


# ---------------------------------------------------------------------------
# One row = one download job
# ---------------------------------------------------------------------------

class JobRow(QFrame):
    cancelRequested = Signal()
    retryRequested = Signal()

    def __init__(self, url: str, mode: str, parent=None):
        super().__init__(parent)
        self.setObjectName("jobRow")
        self.status = "wait"
        self.url = url
        self.outdir = ""

        self.icon = QLabel("🎬" if mode == "video" else "🎵")
        self.icon.setFixedSize(48, 30)
        self.icon.setAlignment(Qt.AlignCenter)
        self.title = ElidedLabel(url)
        self.title.setToolTip(url)
        self.title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.cancel_btn = QPushButton("✕")
        self.cancel_btn.setFixedSize(24, 24)
        self.cancel_btn.setToolTip("취소")
        self.cancel_btn.clicked.connect(self.cancelRequested)
        self.retry_btn = QPushButton("↻")
        self.retry_btn.setFixedSize(24, 24)
        self.retry_btn.setToolTip("다시 시도")
        self.retry_btn.clicked.connect(self.retryRequested)
        self.retry_btn.hide()
        self.folder_btn = QPushButton("폴더")
        self.folder_btn.setFixedSize(44, 24)
        self.folder_btn.setToolTip("저장 폴더 열기")
        self.folder_btn.hide()

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.icon)
        top.addWidget(self.title, 1)
        top.addWidget(self.retry_btn)
        top.addWidget(self.folder_btn)
        top.addWidget(self.cancel_btn)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)

        self.status_lbl = QLabel(STATUS_TEXT["wait"])
        self.status_lbl.setObjectName("statusLbl")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.status_lbl)

        self.folder_btn.clicked.connect(self._open_folder)

    def _open_folder(self) -> None:
        if self.outdir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.outdir))

    def set_state(self, status: str, detail: str = "") -> None:
        self.status = status
        text = STATUS_TEXT.get(status, status)
        if detail:
            text = f"{text} — {detail}"
        self.status_lbl.setText(text)
        color = STATUS_COLOR.get(status)
        self.status_lbl.setStyleSheet(f"color: {color};" if color else "")
        if status in ("extract", "postprocess"):
            self.bar.setRange(0, 0)  # indeterminate while preparing/converting
        elif status == "wait":
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        if status in ("done", "error", "cancelled"):
            self.cancel_btn.hide()
            self.retry_btn.setVisible(status == "error")
            self.folder_btn.setVisible(status == "done")
            if status == "done":
                self.bar.setRange(0, 100)
                self.bar.setValue(100)

    def reset_for_retry(self) -> None:
        self.status = "wait"
        self.retry_btn.hide()
        self.folder_btn.hide()
        self.cancel_btn.show()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status_lbl.setStyleSheet("")
        self.status_lbl.setText(STATUS_TEXT["wait"])

    def set_progress(self, fraction, speed=None, eta=None) -> None:
        bits = []
        if fraction is None:
            self.bar.setRange(0, 0)  # indeterminate
        else:
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 100)
            pct = int(fraction * 100)
            self.bar.setValue(pct)
            bits.append(f"{pct}%")
        if speed:
            bits.append(human_speed(speed))
        if eta:
            bits.append(f"남은 시간 {eta}초")
        self.status = "downloading"
        self.status_lbl.setStyleSheet("")
        self.status_lbl.setText(f"{STATUS_TEXT['downloading']}  {' · '.join(bits)}".rstrip())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(660, 780)
        self.setMinimumSize(560, 600)
        self.setAcceptDrops(True)
        self.settings = QSettings("Dawn", "Dawn")
        self._next_id = 1
        self._rows = {}  # id -> (QListWidgetItem, JobRow)
        self._mode = "video"
        self._last_clip = ""
        self._thumb_pool = []
        from PySide6.QtWidgets import QSystemTrayIcon

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(QIcon(_res("icon.png")))
        if self._tray.isSystemTrayAvailable():
            self._tray.show()
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(700)
        self._auto_timer.timeout.connect(self._auto_enqueue)

        self.worker = Worker(self)
        self.worker.progressed.connect(self._on_progress)
        self.worker.start()

        self._build_ui()

        # clipboard watcher (opt-in via adv checkbox)
        self._clip_timer = QTimer(self)
        self._clip_timer.setInterval(1200)
        self._clip_timer.timeout.connect(self._clipboard_tick)
        self.clip_chk.toggled.connect(
            lambda on: self._clip_timer.start() if on else self._clip_timer.stop()
        )
        if self.clip_chk.isChecked():
            self._clip_timer.start()


    # ----- UI -----

    def _build_ui(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(10)

        from PySide6.QtGui import QPixmap

        header = QHBoxLayout()
        chick = QLabel()
        icon_png = _res("icon.png")
        if os.path.exists(icon_png):
            pm = QPixmap(icon_png).scaledToHeight(
                52, Qt.SmoothTransformation
            )
            pm.setDevicePixelRatio(2)
            chick.setPixmap(pm)
        header.addWidget(chick)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title = QLabel("Dawn")
        title.setObjectName("appTitle")
        sub = QLabel("링크를 붙여넣으면 바로 다운로드 🐣")
        sub.setObjectName("appSub")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        header.addLayout(title_col)
        header.addStretch(1)
        lay.addLayout(header)


        # URL input (paste -> auto download)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("🔗 링크 붙여넣기 (자동 시작)")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.textChanged.connect(self._on_url_changed)
        self.url_edit.returnPressed.connect(self.enqueue)
        paste_btn = QPushButton("📋 붙여넣기")
        paste_btn.clicked.connect(self._paste)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste_btn)
        lay.addLayout(url_row)

        # Mode toggle
        mode_row = QHBoxLayout()
        self.btn_video = QPushButton("🎬  영상 + 음원 (MP4)")
        self.btn_audio = QPushButton("🎵  음원만 (MP3·M4A·FLAC)")
        for b in (self.btn_video, self.btn_audio):
            b.setCheckable(True)
            b.setMinimumHeight(44)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_video.clicked.connect(lambda: self._set_mode("video"))
        self.btn_audio.clicked.connect(lambda: self._set_mode("audio"))
        mode_row.addWidget(self.btn_video)
        mode_row.addWidget(self.btn_audio)
        lay.addLayout(mode_row)

        # Quality + folder
        opt_row = QHBoxLayout()
        self.quality_lbl = QLabel("화질")
        self.quality_combo = QComboBox()
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setToolTip("저장 폴더 — 클릭해서 변경")
        folder_btn = QPushButton("📁 폴더")
        folder_btn.clicked.connect(self._pick_folder)
        opt_row.addWidget(self.quality_lbl)
        opt_row.addWidget(self.quality_combo)
        opt_row.addWidget(self.folder_edit, 1)
        opt_row.addWidget(folder_btn)
        lay.addLayout(opt_row)

        # Advanced: cookies / playlist
        self.adv_btn = QPushButton("⚙ 고급 설정 (인증 · 프록시 · 자막 · 구간 · 병렬) ▾")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setObjectName("advBtn")
        self.adv_btn.toggled.connect(self._toggle_adv)
        lay.addWidget(self.adv_btn)

        self.adv = QFrame()
        self.adv.setObjectName("advBox")
        adv_lay = QVBoxLayout(self.adv)
        adv_lay.setContentsMargins(12, 10, 12, 10)
        adv_lay.setSpacing(8)

        cookie_lbl = QLabel(
            "성인 인증·로그인 필요 영상은 브라우저 쿠키가 필요합니다.\n"
            "해당 사이트에 로그인된 브라우저를 선택하세요 (다운로드 중 브라우저 종료 권장)."
        )
        cookie_lbl.setWordWrap(True)
        cookie_lbl.setObjectName("hint")
        adv_lay.addWidget(cookie_lbl)

        ck_row = QHBoxLayout()
        ck_row.addWidget(QLabel("쿠키 가져올 브라우저"))
        self.browser_combo = QComboBox()
        for label, b in BROWSERS:
            self.browser_combo.addItem(label, b)
        ck_row.addWidget(self.browser_combo, 1)
        adv_lay.addLayout(ck_row)

        cf_row = QHBoxLayout()
        cf_row.addWidget(QLabel("cookies.txt"))
        self.cookie_file_edit = QLineEdit()
        self.cookie_file_edit.setPlaceholderText("선택 안 함")
        cf_btn = QPushButton("찾기")
        cf_btn.clicked.connect(self._pick_cookie_file)
        cf_row.addWidget(self.cookie_file_edit, 1)
        cf_row.addWidget(cf_btn)
        adv_lay.addLayout(cf_row)

        self.playlist_chk = QCheckBox("재생목록 링크는 항상 전체 다운로드")
        adv_lay.addWidget(self.playlist_chk)

        bypass_lbl = QLabel(
            "국가·ISP 차단 우회 (warning.or.kr 등): 로컬/원격 프록시 경유\n"
            "예) socks5://127.0.0.1:1080  ·  http://127.0.0.1:8080"
        )
        bypass_lbl.setWordWrap(True)
        bypass_lbl.setObjectName("hint")
        adv_lay.addWidget(bypass_lbl)

        px_row = QHBoxLayout()
        px_row.addWidget(QLabel("프록시"))
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("비워두면 직접 연결")
        px_row.addWidget(self.proxy_edit, 1)
        adv_lay.addLayout(px_row)

        self.geo_chk = QCheckBox("지역 제한 우회 (yt-dlp geo-bypass)")
        adv_lay.addWidget(self.geo_chk)
        self.adv.hide()


        sec_lbl = QLabel("일부만 받기 (선택) — 시:분:초 또는 분:초")
        sec_lbl.setObjectName("hint")
        adv_lay.addWidget(sec_lbl)
        sec_row = QHBoxLayout()
        self.sec_start = QLineEdit()
        self.sec_start.setPlaceholderText("시작  예) 0:30")
        self.sec_end = QLineEdit()
        self.sec_end.setPlaceholderText("끝  예) 2:15")
        sec_row.addWidget(self.sec_start)
        sec_row.addWidget(QLabel("~"))
        sec_row.addWidget(self.sec_end)
        adv_lay.addLayout(sec_row)

        self.subs_chk = QCheckBox("자막도 저장 (한국어/영어 .srt)")
        adv_lay.addWidget(self.subs_chk)
        self.dedup_chk = QCheckBox("이미 받은 항목 건너뛰기 (다운로드 이력 기억)")
        self.dedup_chk.setChecked(True)
        adv_lay.addWidget(self.dedup_chk)
        self.clip_chk = QCheckBox("클립보드 감시 — 링크 복사하면 바로 다운로드")
        adv_lay.addWidget(self.clip_chk)
        self.autoproxy_chk = QCheckBox(
            "로컬 우회툴 자동 연결 (GoodbyeDPI·Psiphon·Clash 등 켜져 있으면 자동 감지)"
        )
        self.autoproxy_chk.setChecked(True)
        adv_lay.addWidget(self.autoproxy_chk)

        par_row = QHBoxLayout()
        par_row.addWidget(QLabel("동시 다운로드"))
        from PySide6.QtWidgets import QSpinBox

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(2)
        self.parallel_spin.valueChanged.connect(
            lambda v: setattr(self.worker, "max_parallel", v)
        )
        par_row.addWidget(self.parallel_spin)
        par_row.addStretch(1)
        adv_lay.addLayout(par_row)
        lay.addWidget(self.adv)

        # Queue
        q_row = QHBoxLayout()
        self.summary_lbl = QLabel("대기 중인 항목 없음")
        self.summary_lbl.setObjectName("appSub")
        clear_btn = QPushButton("완료 항목 정리")
        clear_btn.setObjectName("ghostBtn")
        clear_btn.clicked.connect(self._clear_finished)
        q_row.addWidget(self.summary_lbl, 1)
        q_row.addWidget(clear_btn)
        lay.addLayout(q_row)

        self.list = QListWidget()
        self.list.setSpacing(4)
        self.list.itemDoubleClicked.connect(self._open_row_folder)

        # 빈 상태 ↔ 목록: 스택으로 전환 (오버레이 좌표 버그 방지)
        from PySide6.QtWidgets import QStackedWidget

        self.stack = QStackedWidget()
        empty_page = QWidget()
        empty_page.setObjectName("emptyPage")
        ep = QVBoxLayout(empty_page)
        ep.addStretch(1)
        big = QLabel("⬇")
        big.setAlignment(Qt.AlignCenter)
        big.setStyleSheet("color: #C9B88A; font-size: 30px;")
        empty_txt = QLabel("여기로 링크를 드래그하거나\n위 칸에 붙여넣으세요")
        empty_txt.setAlignment(Qt.AlignCenter)
        empty_txt.setStyleSheet("color: #C9B88A; font-size: 15px;")
        ep.addWidget(big)
        ep.addWidget(empty_txt)
        ep.addStretch(1)
        self.stack.addWidget(empty_page)
        self.stack.addWidget(self.list)
        self.stack.setCurrentIndex(0)
        lay.addWidget(self.stack, 1)

        hint = QLabel("목록 항목 더블클릭 → 저장 폴더 열기")
        hint.setObjectName("appSub")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)

        self.setCentralWidget(root)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #FFF6E3; color: #3E2723; }
            #appTitle { font-size: 26px; font-weight: 800; color: #3E2723; }
            #appSub, #hint, #statusLbl { color: #8D6E63; font-size: 12px; }
            QLineEdit, QComboBox {
                background: #FFFBEF; border: 2px solid #DCCB9E; border-radius: 8px;
                padding: 7px 10px; selection-background-color: #FFD159;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #FF8F3D; }
            QComboBox::drop-down { border: none; width: 24px; }
            QPushButton {
                background: #FFFBEF; border: 2px solid #DCCB9E; border-radius: 8px;
                padding: 7px 14px; color: #3E2723;
            }
            QPushButton:hover { background: #FFF0C8; border-color: #FF8F3D; }
            QPushButton:pressed { background: #FFE1A8; }
            QPushButton[checkable="true"]:checked {
                background: #FFD159; border: 2px solid #3E2723; font-weight: 700;
            }
            #jobRow {
                background: #FFFBEF; border: 2px solid #DCCB9E; border-radius: 10px;
            }
            #advBox { background: #FFFBEF; border: 2px dashed #DCCB9E; border-radius: 10px; }
            #advBtn { text-align: left; border: none; color: #8D6E63; padding: 2px 4px; }
            #advBtn:hover { color: #3E2723; }
            #ghostBtn { border: none; color: #8D6E63; }
            #ghostBtn:hover { color: #3E2723; text-decoration: underline; }
            QListWidget, #emptyPage {
                background: #FFF9EA; border: 2px solid #DCCB9E; border-radius: 10px;
            }
            #emptyPage { border-style: dashed; }
            QListWidget::item { border: none; }
            QProgressBar {
                border: none; border-radius: 5px; background: #EFE3C3;
            }
            QProgressBar::chunk { border-radius: 5px; background: #FF8F3D; }
            QToolTip { background: #3E2723; color: #FFF6E3; border: none; padding: 6px; }
            #accentBtn { background: #FF8F3D; border-color: #E8721C; color: #fff; font-weight: 700; }
            #accentBtn:hover { background: #F57C00; }
            #accentBtn:disabled { background: #DCCB9E; border-color: #DCCB9E; }
            QSpinBox {
                background: #FFFBEF; border: 2px solid #DCCB9E; border-radius: 8px;
                padding: 4px 8px; min-width: 54px;
            }
            QSpinBox:focus { border-color: #FF8F3D; }
            """
        )

        self.setWindowIcon(QIcon(_res("icon.png")))
        self._set_mode(self.settings.value("mode", "video"))

    # ----- settings -----

    def _load_settings(self) -> None:
        default_dir = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.DownloadLocation), "Dawn"
        )
        self.folder_edit.setText(self.settings.value("outdir", default_dir))
        self._restore_combo("vquality", "best")
        self._restore_combo("aquality", "mp3:0")
        self.parallel_spin.setValue(int(self.settings.value("parallel", 2)))
        self.worker.max_parallel = self.parallel_spin.value()
        bi = self.settings.value("cookie_browser", "")
        for i in range(self.browser_combo.count()):
            if (self.browser_combo.itemData(i) or "") == bi:
                self.browser_combo.setCurrentIndex(i)
        self.cookie_file_edit.setText(self.settings.value("cookie_file", ""))
        self.playlist_chk.setChecked(self.settings.value("playlist", False, type=bool))
        self.proxy_edit.setText(self.settings.value("proxy", ""))
        self.geo_chk.setChecked(self.settings.value("geo_bypass", False, type=bool))
        self.autoproxy_chk.setChecked(
            self.settings.value("autoproxy", True, type=bool)
        )
        if self.settings.value("adv_open", False, type=bool):
            self.adv_btn.setChecked(True)

    def _restore_combo(self, key: str, default: str) -> None:
        want = self.settings.value(key, default)
        for i in range(self.quality_combo.count()):
            if self.quality_combo.itemData(i) == want:
                self.quality_combo.setCurrentIndex(i)
                return

    def _save_settings(self) -> None:
        self.settings.setValue("outdir", self.folder_edit.text())
        self.settings.setValue("mode", self._mode)
        key = "vquality" if self._mode == "video" else "aquality"
        self.settings.setValue(key, self.quality_combo.currentData())
        self.settings.setValue("cookie_browser", self.browser_combo.currentData() or "")
        self.settings.setValue("cookie_file", self.cookie_file_edit.text())
        self.settings.setValue("playlist", self.playlist_chk.isChecked())
        self.settings.setValue("adv_open", self.adv_btn.isChecked())
        self.settings.setValue("proxy", self.proxy_edit.text())
        self.settings.setValue("geo_bypass", self.geo_chk.isChecked())
        self.settings.setValue("autoproxy", self.autoproxy_chk.isChecked())
        self.settings.setValue("sec_start", self.sec_start.text())
        self.settings.setValue("sec_end", self.sec_end.text())
        self.settings.setValue("subtitles", self.subs_chk.isChecked())
        self.settings.setValue("dedup", self.dedup_chk.isChecked())
        self.settings.setValue("clip_watch", self.clip_chk.isChecked())
        self.settings.setValue("parallel", self.parallel_spin.value())

    # ----- actions -----

    def _set_mode(self, mode: str) -> None:
        # persist current combo selection under the old mode first
        if hasattr(self, "quality_combo"):
            old_key = "vquality" if self._mode == "video" else "aquality"
            if self.quality_combo.count():
                self.settings.setValue(old_key, self.quality_combo.currentData())
        self._mode = mode
        self.btn_video.setChecked(mode == "video")
        self.btn_audio.setChecked(mode == "audio")
        self.quality_lbl.setText("화질" if mode == "video" else "음질")
        items = VIDEO_QUALITIES if mode == "video" else AUDIO_QUALITIES
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for label, q in items:
            self.quality_combo.addItem(label, q)
        self.quality_combo.blockSignals(False)
        self._restore_combo("vquality" if mode == "video" else "aquality",
                            "best" if mode == "video" else "mp3:0")

    def _toggle_adv(self, on: bool) -> None:
        self.adv.setVisible(on)
        self.adv_btn.setText("⚙ 고급 설정 (인증 · 프록시 · 자막 · 구간 · 병렬) " + ("▴" if on else "▾"))

    def _paste(self) -> None:
        self.url_edit.setText(QApplication.clipboard().text().strip())

    def _on_url_changed(self, text: str) -> None:
        # 붙여넣기/드롭 = 큰 덩어리 입력일 때만 자동 시작; 타이핑은 건드리지 않음
        prev = getattr(self, "_prev_url_len", 0)
        self._prev_url_len = len(text or "")
        grew_by_paste = len(text or "") - prev >= 10
        if grew_by_paste and URL_RE.search(text or ""):
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _auto_enqueue(self) -> None:
        if self.url_edit.text().strip():
            self.enqueue()

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", self.folder_edit.text())
        if d:
            self.folder_edit.setText(d)

    def _pick_cookie_file(self) -> None:
        f, _ = QFileDialog.getOpenFileName(
            self, "cookies.txt 선택", "", "Cookie 파일 (*.txt);;모든 파일 (*)"
        )
        if f:
            self.cookie_file_edit.setText(f)

    def enqueue(self) -> None:
        self._auto_timer.stop()
        text = self.url_edit.text().strip()
        urls = URL_RE.findall(text)
        if not urls:
            self.url_edit.setPlaceholderText("🔗 링크를 붙여넣으세요! (https://…)")
            return
        playlist_cache = {}  # don't re-ask the same playlist choice per batch
        for url in urls:
            self._enqueue_url(url, playlist_cache)
        self.url_edit.clear()

    def _enqueue_url(self, url: str, playlist_cache: dict | None = None) -> None:
        """Enqueue one URL (shared by paste, Enter, drag&drop)."""
        if self._url_active(url):
            return
        outdir = self.folder_edit.text().strip()
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            outdir = os.path.expanduser("~/Downloads/Dawn")
            self.folder_edit.setText(outdir)
            os.makedirs(outdir, exist_ok=True)
        codec_q = self._job_quality()
        job = DownloadJob(
            url=url,
            mode=self._mode,
            outdir=outdir,
            quality=codec_q[0],
            audio_codec=codec_q[1],
            cookies=CookieConfig(
                browser=self.browser_combo.currentData(),
                cookie_file=self.cookie_file_edit.text().strip() or None,
            ),
            playlist=self._resolve_playlist(url, playlist_cache),
            proxy=self._effective_proxy(),
            geo_bypass=self.geo_chk.isChecked(),
            section=self._parse_section(),
            subtitles=self.subs_chk.isChecked(),
            dedup=self.dedup_chk.isChecked(),
        )
        self._add_row(job)

    def _job_quality(self):
        """Combo data 'codec:q' for audio → (q, codec); video → (q, 'mp3')."""
        data = str(self.quality_combo.currentData() or "0")
        if self._mode == "audio" and ":" in data:
            codec, q = data.split(":", 1)
            return q, codec
        if self._mode == "audio":
            return data, "mp3"  # legacy setting without codec prefix
        return data, "mp3"

    # ----- helpers for new features -----

    def _parse_section(self) -> "tuple | None":
        """Parse 'H:MM:SS'/'MM:SS'/'SS' fields into (start, end) seconds."""
        def to_sec(s: str):
            s = s.strip()
            if not s:
                return None
            parts = s.split(":")
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                return None
            if len(nums) == 1:
                return nums[0]
            if len(nums) == 2:
                return nums[0] * 60 + nums[1]
            if len(nums) == 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]
            return None

        start = to_sec(self.sec_start.text())
        end = to_sec(self.sec_end.text())
        if start is None and end is None:
            return None
        return (start or 0, end)  # end None = 영상 끝까지 (yt-dlp는 None 지원)

    def _clipboard_tick(self) -> None:
        if not self.clip_chk.isChecked():
            return
        text = QApplication.clipboard().text().strip()
        if text == self._last_clip or not text:
            return
        self._last_clip = text
        if URL_RE.fullmatch(text):
            self._enqueue_url(text)

    def _notify_done(self, title: str) -> None:
        """OS toast when a job finishes; falls back to taskbar flash."""
        from PySide6.QtWidgets import QSystemTrayIcon

        if self._tray.isSystemTrayAvailable():
            self._tray.showMessage(
                "Dawn — 다운로드 완료", title, QSystemTrayIcon.Information, 4000
            )
        else:
            QApplication.alert(self, 0)
        self._update_summary()

    def _effective_proxy(self) -> "str | None":
        """Explicit proxy wins; otherwise auto-detect a running local
        circumvention tool when enabled."""
        manual = self.proxy_edit.text().strip()
        if manual:
            return manual
        if not self.autoproxy_chk.isChecked():
            return None
        found = detect_local_proxy()
        if found:
            self.proxy_edit.setPlaceholderText(f"자동 감지됨: {found}")
        return found

    def _resolve_playlist(self, url: str, cache: dict | None = None) -> bool:
        if not is_playlist_url(url):
            return False
        if self.playlist_chk.isChecked():
            return True
        if cache is not None and url in cache:
            return cache[url]
        box = QMessageBox(self)
        box.setWindowTitle("재생목록 감지")
        box.setText("이 링크는 재생목록을 포함하고 있습니다.")
        box.setInformativeText("재생목록 전체를 받을까요, 이 영상만 받을까요?")
        all_btn = box.addButton("재생목록 전체", QMessageBox.AcceptRole)
        box.addButton("이 영상만", QMessageBox.RejectRole)
        box.setDefaultButton(all_btn)
        box.exec()
        result = box.clickedButton() is all_btn
        if cache is not None:
            cache[url] = result
        return result

    def _url_active(self, url: str) -> bool:
        for _item, row in self._rows.values():
            if row.url == url and row.status in ("wait", "extract", "downloading", "postprocess"):
                return True
        return False

    def _clear_finished(self) -> None:
        for job_id, (item, row) in list(self._rows.items()):
            if row.status in ("done", "error", "cancelled"):
                self.list.takeItem(self.list.row(item))
                del self._rows[job_id]
        self._update_summary()

    def _update_summary(self) -> None:
        n_run = sum(
            1
            for _i, r in self._rows.values()
            if r.status in ("wait", "extract", "downloading", "postprocess")
        )
        n_done = sum(1 for _i, r in self._rows.values() if r.status == "done")
        n_fail = sum(1 for _i, r in self._rows.values() if r.status == "error")
        if not self._rows:
            self.summary_lbl.setText("대기 중인 항목 없음")
        else:
            bits = [f"진행 {n_run}", f"완료 {n_done}"]
            if n_fail:
                bits.append(f"실패 {n_fail}")
            self.summary_lbl.setText(" · ".join(bits))
        self.stack.setCurrentIndex(0 if not self._rows else 1)

    def _add_row(self, job: DownloadJob) -> None:
        job_id = self._next_id
        self._next_id += 1
        row = JobRow(job.url, job.mode)
        row.outdir = job.outdir
        row.job = job
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        self.list.scrollToBottom()
        row.cancelRequested.connect(lambda jid=job_id: self._cancel(jid))
        row.retryRequested.connect(lambda jid=job_id: self._retry(jid))
        self._rows[job_id] = (item, row)
        self.worker.add(job_id, job)

    def _retry(self, job_id: int) -> None:
        pair = self._rows.get(job_id)
        if not pair:
            return
        _item, row = pair
        row.reset_for_retry()
        self.worker.add(job_id, row.job)
        self._update_summary()

    def _cancel(self, job_id: int) -> None:
        self.worker.cancel(job_id)
        if job_id in self._rows:
            self._rows[job_id][1].set_state("cancelled")

    def _open_row_folder(self, item: QListWidgetItem) -> None:
        row = self.list.itemWidget(item)
        if isinstance(row, JobRow) and row.outdir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(row.outdir))

    # ----- drag & drop -----

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        text = e.mimeData().text()
        urls = URL_RE.findall(text) if text else [
            u.toString() for u in e.mimeData().urls()
        ]
        for url in urls:
            self._enqueue_url(url)
        e.acceptProposedAction()

    @Slot(int, dict)
    def _on_progress(self, job_id: int, d: dict) -> None:
        pair = self._rows.get(job_id)
        if not pair:
            return
        _, row = pair
        ev = d.get("event")
        if ev == "info":
            row.set_state("extract")
        elif ev == "progress":
            if d.get("title"):
                row.title.setText(d["title"])
            if d.get("phase") == "postprocess":
                row.set_state("postprocess")
            else:
                row.set_progress(d.get("fraction"), d.get("speed"), d.get("eta"))
        elif ev == "title":
            row.title.setText(d.get("title") or row.title.text())
            thumb = d.get("thumb")
            if thumb:
                self._load_thumb(row, thumb)
        elif ev == "pp":
            row.set_state("postprocess")
        elif ev == "done":
            row.set_state("done")
            self._notify_done(row.title.text())
            self._update_summary()
        elif ev == "cancelled":
            row.set_state("cancelled")
            self._update_summary()
        elif ev == "error":
            row.set_state("error", (d.get("message") or "")[:160])
            row.setToolTip(d.get("message") or "")
            self._update_summary()
        elif ev == "warn":
            row.setToolTip(d.get("message") or "")

    def _load_thumb(self, row: "JobRow", url: str) -> None:
        """Fetch video thumbnail off-thread and set it as the row icon."""
        from PySide6.QtGui import QPixmap

        def work():
            import urllib.request

            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}
                )
                data = urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                return
            pm = QPixmap()
            pm.loadFromData(data)
            if pm.isNull():
                return
            pm = pm.scaled(48, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            # marshal to GUI thread
            from PySide6.QtCore import QMetaObject, Q_ARG

            QMetaObject.invokeMethod(
                row.icon,
                "setPixmap",
                Qt.QueuedConnection,
                Q_ARG("QPixmap", pm),
            )

        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._thumb_pool.append(t)


    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # empty/list 전환은 QStackedWidget이 처리 — 수동 reposition 불필요

    # ----- lifecycle -----

    def closeEvent(self, e) -> None:
        self._save_settings()
        self.worker.stop()
        if not self.worker.wait(3000):
            self.worker.terminate()
            self.worker.wait(1000)
        super().closeEvent(e)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Dawn")
    from PySide6.QtGui import QFont

    # Platform-native UI font + emoji-capable fallbacks (offscreen/minimal
    # systems render emoji as boxes without these)
    families = {
        "darwin": ["SF Pro Text", "Apple SD Gothic Neo", "Helvetica Neue",
                   "Apple Color Emoji", "Arial"],
        "win32": ["Malgun Gothic", "Segoe UI", "Microsoft YaHei",
                  "Segoe UI Emoji", "Arial"],
    }.get(sys.platform, ["Noto Sans", "DejaVu Sans", "Noto Color Emoji"])
    f = QFont()
    f.setFamilies(families)
    f.setPointSize(10)
    app.setFont(f)
    icon_path = _res("icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
