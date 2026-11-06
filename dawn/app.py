"""Dawn — 링크를 확인하고 간편하게 저장하는 다운로더."""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
from dataclasses import replace

from PySide6.QtCore import (
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
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
    QScrollArea,
    QStyle,
    QStyleOptionButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .bypass import find_bypass_engine, is_admin, shutdown_bypass
from .downloader import CookieConfig, DownloadJob, run_job

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
    "done": "저장 완료",
    "error": "실패",
    "cancelled": "취소됨",
    "skipped": "건너뜀",
}
STATUS_COLOR = {
    "done": "#28734D",
    "error": "#B63832",
    "cancelled": "#726D66",
    "skipped": "#726D66",
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


class Dropdown(QComboBox):
    """Draw a visible disclosure arrow independently of the native theme."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#736F66"), 1.5))
        x, y = self.width() - 16, self.height() // 2
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)


class OptionCheckBox(QCheckBox):
    """Keep native checkbox behavior with a consistent, high-contrast indicator."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        checked = self.isChecked()
        painter.setPen(QPen(QColor("#B84E23" if checked or self.hasFocus() else "#C8C2B7"), 1.5))
        painter.setBrush(QColor("#B84E23" if checked else "#FFFFFF"))
        painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 5, 5)
        if checked:
            painter.setPen(QPen(QColor("#FFFFFF"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            x, y = rect.x(), rect.y()
            painter.drawLine(x + 5, y + 10, x + 8, y + 13)
            painter.drawLine(x + 8, y + 13, x + 14, y + 6)


class ElidedLabel(QLabel):
    """QLabel that right-elides long text instead of clipping mid-glyph."""

    def __init__(self, text="", parent=None):
        self._full = text
        super().__init__(text, parent)
        self.setTextFormat(Qt.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

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
        self.output_format = "MP4" if mode == "video" else "음원"

        self.icon = QLabel("MP4" if mode == "video" else "음원")
        self.icon.setObjectName("mediaBadge")
        self.icon.setFixedSize(48, 36)
        self.icon.setAlignment(Qt.AlignCenter)
        self.title = ElidedLabel(url)
        self.title.setToolTip(url)
        self.title.setObjectName("jobTitle")
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.setMinimumHeight(32)
        self.cancel_btn.setToolTip("취소")
        self.cancel_btn.clicked.connect(self.cancelRequested)
        self.retry_btn = QPushButton("다시 시도")
        self.retry_btn.setMinimumHeight(32)
        self.retry_btn.setToolTip("같은 설정으로 다시 다운로드")
        self.retry_btn.clicked.connect(self.retryRequested)
        self.retry_btn.hide()
        self.folder_btn = QPushButton("폴더 열기")
        self.folder_btn.setMinimumHeight(32)
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

        self.status_lbl = ElidedLabel(STATUS_TEXT["wait"])
        self.status_lbl.setObjectName("statusLbl")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.status_lbl)

        self.folder_btn.clicked.connect(self._open_folder)

    def _open_folder(self) -> None:
        if self.outdir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.outdir))

    def set_state(self, status: str, detail: str = "") -> None:
        self.status = status
        text = f"{self.output_format} · {STATUS_TEXT.get(status, status)}"
        if detail:
            text = f"{text} — {detail}"
        self.status_lbl.setText(text)
        self.status_lbl.setToolTip(text)
        color = STATUS_COLOR.get(status)
        self.status_lbl.setStyleSheet(f"color: {color};" if color else "")
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}" if color else "")
        if status in ("extract", "postprocess"):
            self.bar.setRange(0, 0)  # indeterminate while preparing/converting
        elif status == "wait":
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        if status in ("done", "error", "cancelled", "skipped"):
            self.cancel_btn.hide()
            self.retry_btn.setText("다시 받기" if status == "skipped" else "다시 시도")
            self.retry_btn.setVisible(status in ("error", "cancelled", "skipped"))
            self.folder_btn.setVisible(status in ("done", "skipped"))
            self.bar.setRange(0, 100)
            self.bar.setValue(100 if status == "done" else 0)

    def reset_for_retry(self) -> None:
        self.status = "wait"
        self.retry_btn.hide()
        self.folder_btn.hide()
        self.cancel_btn.show()
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("취소")
        self.setToolTip("")
        self.status_lbl.setToolTip("")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status_lbl.setStyleSheet("")
        self.bar.setStyleSheet("")
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
        self.status_lbl.setText(f"{self.output_format} · {STATUS_TEXT['downloading']}  {' · '.join(bits)}".rstrip())


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(760, 800)
        self.setMinimumSize(620, 700)
        self.setAcceptDrops(True)
        self.settings = QSettings("Dawn", "Dawn")
        self._next_id = 1
        self._rows = {}  # id -> (QListWidgetItem, JobRow)
        self._mode = "video"
        self._last_clip = ""
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
        self._load_settings()
        self._update_summary()
        self._update_options_hint()
        self.url_edit.setFocus()

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
        root.setObjectName("window")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        brand = QLabel()
        brand.setObjectName("brandMark")
        brand.setFixedSize(56, 56)
        brand.setAlignment(Qt.AlignCenter)
        logo = QPixmap(_res("mascot.png")).scaled(112, 112, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo.setDevicePixelRatio(2)
        brand.setPixmap(logo)
        header.addWidget(brand)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Dawn")
        title.setObjectName("appTitle")
        sub = QLabel("좋아하는 콘텐츠를, 간편하게.")
        sub.setObjectName("appSub")
        title_col.addWidget(title)
        title_col.addWidget(sub)
        header.addLayout(title_col)
        header.addStretch()
        self.adv_btn = QPushButton("설정")
        self.adv_btn.setToolTip("다운로드 옵션 · 로그인 · 네트워크")
        self.adv_btn.clicked.connect(self._show_settings)
        header.addWidget(self.adv_btn)
        lay.addLayout(header)

        card = QFrame()
        card.setObjectName("downloadCard")
        composer = QVBoxLayout(card)
        composer.setContentsMargins(20, 18, 20, 18)
        composer.setSpacing(12)
        heading = QLabel("무엇을 저장할까요?")
        heading.setObjectName("sectionTitle")
        composer.addWidget(heading)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlInput")
        self.url_edit.setPlaceholderText("https:// 링크를 붙여넣으세요")
        self.url_edit.setAccessibleName("다운로드할 링크")
        self.url_edit.setMinimumHeight(46)
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setAcceptDrops(False)
        self.url_edit.textChanged.connect(self._on_url_changed)
        self.url_edit.returnPressed.connect(self.enqueue)
        paste_btn = QPushButton("붙여넣기")
        paste_btn.setObjectName("pasteBtn")
        paste_btn.setMinimumHeight(46)
        paste_btn.clicked.connect(self._paste)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(paste_btn)
        composer.addLayout(url_row)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        segments = QFrame()
        segments.setObjectName("modeSwitch")
        segments.setMinimumHeight(48)
        segment_layout = QHBoxLayout(segments)
        segment_layout.setContentsMargins(4, 4, 4, 4)
        segment_layout.setSpacing(4)
        self.btn_video = QPushButton("영상 · MP4")
        self.btn_audio = QPushButton("음원")
        self.btn_video.setToolTip("소리가 포함된 MP4 영상")
        self.btn_audio.setToolTip("MP3 · M4A · FLAC 음원")
        for button in (self.btn_video, self.btn_audio):
            button.setObjectName("modeBtn")
            button.setCheckable(True)
            button.setMinimumSize(76, 40)
            segment_layout.addWidget(button)
        self.btn_video.clicked.connect(lambda: self._set_mode("video"))
        self.btn_audio.clicked.connect(lambda: self._set_mode("audio"))
        mode_row.addWidget(segments)
        mode_row.addStretch()
        self.quality_lbl = QLabel("화질")
        self.quality_lbl.setObjectName("hint")
        self.quality_combo = Dropdown()
        self.quality_combo.setAccessibleName("저장 화질 또는 음질")
        self.quality_combo.setMinimumWidth(200)
        self.quality_combo.setMinimumHeight(44)
        self.quality_lbl.setBuddy(self.quality_combo)
        mode_row.addWidget(self.quality_lbl)
        mode_row.addWidget(self.quality_combo)
        composer.addLayout(mode_row)

        self.start_btn = QPushButton("다운로드")
        self.start_btn.setObjectName("accentBtn")
        self.start_btn.setMinimumHeight(46)
        self.start_btn.clicked.connect(self.enqueue)
        self.start_btn.setEnabled(False)
        composer.addWidget(self.start_btn)
        self.feedback_lbl = QLabel("여러 링크도 한 번에 붙여넣을 수 있어요.")
        self.feedback_lbl.setObjectName("hint")
        self.feedback_lbl.setWordWrap(True)
        composer.addWidget(self.feedback_lbl)
        lay.addWidget(card)

        destination = QVBoxLayout()
        destination.setSpacing(6)
        folder_row = QHBoxLayout()
        folder_lbl = QLabel("저장 위치")
        folder_lbl.setObjectName("hint")
        self.folder_edit = QLineEdit()
        self.folder_edit.setObjectName("folderPath")
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setAccessibleName("저장 폴더 경로")
        self.folder_edit.textChanged.connect(self.folder_edit.setToolTip)
        folder_btn = QPushButton("변경")
        folder_btn.clicked.connect(self._pick_folder)
        folder_row.addWidget(folder_lbl)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(folder_btn)
        destination.addLayout(folder_row)
        self.options_hint = ElidedLabel()
        self.options_hint.setObjectName("hint")
        destination.addWidget(self.options_hint)
        lay.addLayout(destination)

        q_row = QHBoxLayout()
        queue_title = QLabel("다운로드 목록")
        queue_title.setObjectName("sectionTitle")
        self.summary_lbl = ElidedLabel("아직 다운로드가 없어요")
        self.summary_lbl.setObjectName("appSub")
        self.clear_btn = QPushButton("완료 항목 정리")
        self.clear_btn.setObjectName("ghostBtn")
        self.clear_btn.setToolTip("완료·건너뜀·취소 항목만 목록에서 제거합니다. 파일은 삭제하지 않습니다.")
        self.clear_btn.clicked.connect(self._clear_finished)
        q_row.addWidget(queue_title)
        q_row.addWidget(self.summary_lbl, 1)
        q_row.addWidget(self.clear_btn)
        lay.addLayout(q_row)

        self.list = QListWidget()
        self.list.setSpacing(6)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.itemDoubleClicked.connect(self._open_row_folder)
        self.stack = QStackedWidget()
        empty_page = QFrame()
        empty_page.setObjectName("emptyPage")
        ep = QVBoxLayout(empty_page)
        ep.setContentsMargins(16, 16, 16, 16)
        ep.setSpacing(8)
        ep.addStretch()
        empty_icon = QLabel()
        empty_icon.setObjectName("emptyMascot")
        empty_icon.setFixedSize(80, 80)
        empty_icon.setAlignment(Qt.AlignCenter)
        mascot = QPixmap(_res("mascot.png")).scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        mascot.setDevicePixelRatio(2)
        empty_icon.setPixmap(mascot)
        ep.addWidget(empty_icon, 0, Qt.AlignHCenter)
        empty_title = QLabel("좋아하는 순간을 담아보세요")
        empty_title.setObjectName("emptyTitle")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_txt = QLabel("링크를 붙여넣거나 여기로 끌어오세요.")
        empty_txt.setObjectName("hint")
        empty_txt.setAlignment(Qt.AlignCenter)
        empty_txt.setWordWrap(True)
        ep.addWidget(empty_title)
        ep.addWidget(empty_txt)
        ep.addStretch()
        self.stack.addWidget(empty_page)
        self.stack.addWidget(self.list)
        lay.addWidget(self.stack, 1)

        key = "⌘" if sys.platform == "darwin" else "Ctrl"
        hint = QLabel(f"{key}+V 붙여넣기  ·  Enter 다운로드  ·  {key}+L 링크 입력")
        hint.setObjectName("appSub")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        focus_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        focus_shortcut.activated.connect(self.url_edit.setFocus)
        focus_shortcut.activated.connect(self.url_edit.selectAll)
        paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        paste_shortcut.activated.connect(self._paste)
        self.setCentralWidget(root)
        self._build_settings()
        self.setStyleSheet(
            """
            QWidget { color: #352E27; font-size: 13px; }
            QMainWindow, #window, QDialog { background: #FBF8F4; }
            QLabel { background: transparent; }
            #appTitle { color: #48372B; font-size: 28px; font-weight: 700; letter-spacing: -1px; }
            #brandMark { background: #F5EBD8; border-radius: 20px; }
            #sectionTitle, #emptyTitle { font-size: 16px; font-weight: 600; }
            #appSub, #hint, #statusLbl { color: #736F66; font-size: 12px; }
            #downloadCard, #jobRow {
                background: #FFFFFF; border: 1px solid #E9E2D9; border-radius: 18px;
            }
            QLineEdit, QComboBox {
                background: #FFFFFF; border: 1px solid #DDD5CA; border-radius: 11px;
                padding: 9px 10px; selection-background-color: #F6D7C0;
                selection-color: #282620;
            }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #B84E23; }
            #urlInput { background: #FCFAF7; font-size: 14px; }
            #folderPath { background: transparent; border: none; color: #736F66; padding: 6px; }
            QComboBox { padding-right: 26px; }
            QComboBox::drop-down { border: none; width: 24px; }
            QComboBox::down-arrow { image: none; }
            QComboBox QAbstractItemView {
                background: #FFFFFF; color: #282620;
                selection-background-color: #F6E9DD; selection-color: #282620;
            }
            QPushButton {
                background: #FFFFFF; border: 1px solid #DDD5CA; border-radius: 11px;
                padding: 8px 14px; font-weight: 500;
            }
            QPushButton:hover { background: #F1F0EB; border-color: #AAA59B; }
            QPushButton:pressed { background: #E8E5DE; }
            QPushButton:focus { border: 1px solid #B84E23; }
            QPushButton:disabled { color: #938E84; border-color: #E4E2DC; background: #F1F0EC; }
            #pasteBtn { color: #805638; background: #F7EFE5; border-color: #EADCCD; }
            #pasteBtn:hover { background: #F0E2D1; border-color: #D6BFA6; }
            #pasteBtn:focus { border-color: #B84E23; }
            #modeSwitch { background: #F5F0E9; border-radius: 13px; }
            #modeBtn { background: transparent; border-color: transparent; color: #736F66; }
            #modeBtn:checked { color: #983F1C; background: #FFFFFF; border-color: #E2D1C1; font-weight: 600; }
            #modeBtn:focus { border-color: #B84E23; }
            #accentBtn { background: #B84E23; color: #FFFFFF; border-color: #B84E23; font-weight: 600; }
            #accentBtn:hover { background: #9E401C; border-color: #9E401C; }
            #accentBtn:pressed { background: #833316; }
            #accentBtn:focus { border: 2px solid #713015; }
            #accentBtn:disabled { background: #F4E7DD; border-color: #F4E7DD; color: #9B735B; }
            #ghostBtn { background: transparent; border-color: transparent; color: #736F66; }
            #ghostBtn:hover, #ghostBtn:focus { background: #EBE9E3; border-color: #D8D5CE; }
            #ghostBtn:disabled { color: #A9A499; }
            QListWidget { background: transparent; border: none; outline: none; }
            QListWidget::item { border: none; }
            QListWidget::item:selected { background: #F6E9DD; border-radius: 14px; }
            #emptyPage { background: #F5EFE5; border: 1px solid #EBE1D2; border-radius: 20px; }
            #emptyTitle { color: #64513F; }
            #jobTitle { font-weight: 600; }
            #mediaBadge { color: #8D6546; background: #F6EDE2; border-radius: 8px; font-size: 11px; font-weight: 600; }
            QProgressBar { border: none; border-radius: 3px; background: #EEECE6; }
            QProgressBar::chunk { border-radius: 3px; background: #B84E23; }
            QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 1px; }
            QScrollBar::handle:vertical { background: #C9C3B9; border-radius: 4px; min-height: 32px; }
            QScrollBar::handle:vertical:hover { background: #A69C8E; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            QTabWidget::pane { background: #FFFFFF; border: 1px solid #E4E2DC; border-radius: 10px; }
            QTabBar::tab {
                background: transparent; border: none; border-bottom: 2px solid transparent;
                padding: 12px 18px; color: #736F66;
            }
            QTabBar::tab:selected { color: #983F1C; border-bottom-color: #B84E23; font-weight: 600; }
            QTabBar::tab:hover { background: #F6EDE2; }
            QCheckBox { spacing: 12px; padding: 6px 0; }
            QCheckBox::indicator { width: 20px; height: 20px; border: none; background: transparent; image: none; }
            QToolTip { background: #282620; color: #FFFFFF; border: none; padding: 8px; }
            """
        )
        self.setWindowIcon(QIcon(_res("icon.png")))
        self._set_mode(self.settings.value("mode", "video"))

    def _build_settings(self) -> None:
        self.adv = QDialog(self)
        self.adv.setWindowTitle("Dawn 설정")
        self.adv.resize(560, 520)
        self.adv.setMinimumSize(500, 440)
        layout = QVBoxLayout(self.adv)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        settings_title = QLabel("내게 맞게 설정")
        settings_title.setObjectName("sectionTitle")
        layout.addWidget(settings_title)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        def page(title):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            column = QVBoxLayout(inner)
            column.setContentsMargins(20, 20, 20, 20)
            column.setSpacing(12)
            scroll.setWidget(inner)
            tabs.addTab(scroll, title)
            return column

        download = page("다운로드")
        self.auto_chk = OptionCheckBox("붙여넣은 링크 자동 다운로드")
        self.auto_chk.setToolTip("켜면 링크를 붙여넣고 0.7초 후 현재 설정으로 시작합니다.")
        self.auto_chk.toggled.connect(lambda _: self._auto_timer.stop())
        self.playlist_chk = OptionCheckBox("재생목록 링크는 항상 전체 다운로드")
        self.subs_chk = OptionCheckBox("자막도 저장 (한국어 / 영어 .srt)")
        self.dedup_chk = OptionCheckBox("같은 형식으로 이미 받은 항목 건너뛰기")
        self.clip_chk = OptionCheckBox("클립보드 감시 · 링크 복사 시 바로 다운로드")
        for control in (self.auto_chk, self.playlist_chk, self.subs_chk, self.dedup_chk, self.clip_chk):
            download.addWidget(control)
        section_lbl = QLabel("일부만 받기 · 비워두면 전체 저장")
        section_lbl.setObjectName("hint")
        download.addWidget(section_lbl)
        section_row = QHBoxLayout()
        self.sec_start = QLineEdit()
        self.sec_start.setPlaceholderText("시작  0:30")
        self.sec_start.setAccessibleName("구간 시작 시간")
        self.sec_end = QLineEdit()
        self.sec_end.setPlaceholderText("끝  2:15")
        self.sec_end.setAccessibleName("구간 끝 시간")
        section_row.addWidget(self.sec_start)
        section_row.addWidget(QLabel("~"))
        section_row.addWidget(self.sec_end)
        download.addLayout(section_row)
        parallel_row = QHBoxLayout()
        parallel_row.addWidget(QLabel("동시 다운로드"))
        self.parallel_combo = Dropdown()
        for count in range(1, 5):
            self.parallel_combo.addItem(f"{count}개", count)
        self.parallel_combo.setMinimumWidth(100)
        self.parallel_combo.setAccessibleName("동시 다운로드 수")
        self.parallel_combo.currentIndexChanged.connect(
            lambda _: setattr(self.worker, "max_parallel", self.parallel_combo.currentData())
        )
        parallel_row.addWidget(self.parallel_combo)
        parallel_row.addStretch()
        download.addLayout(parallel_row)
        download.addStretch()

        login = page("로그인")
        cookie_lbl = QLabel("로그인·성인 인증이 필요한 콘텐츠에만 설정하세요.\n로그인한 브라우저를 선택하고, 다운로드 중에는 종료해 주세요.")
        cookie_lbl.setWordWrap(True)
        cookie_lbl.setObjectName("hint")
        login.addWidget(cookie_lbl)
        login.addWidget(QLabel("쿠키를 가져올 브라우저"))
        self.browser_combo = Dropdown()
        self.browser_combo.setAccessibleName("로그인된 브라우저")
        for label, browser in BROWSERS:
            self.browser_combo.addItem(label, browser)
        login.addWidget(self.browser_combo)
        login.addWidget(QLabel("또는 cookies.txt 파일"))
        cookie_row = QHBoxLayout()
        self.cookie_file_edit = QLineEdit()
        self.cookie_file_edit.setPlaceholderText("선택 안 함")
        self.cookie_file_edit.setAccessibleName("쿠키 파일 경로")
        self.cookie_file_edit.setClearButtonEnabled(True)
        cookie_btn = QPushButton("찾기")
        cookie_btn.clicked.connect(self._pick_cookie_file)
        cookie_row.addWidget(self.cookie_file_edit, 1)
        cookie_row.addWidget(cookie_btn)
        login.addLayout(cookie_row)
        login.addStretch()

        network = page("네트워크")
        self.dpi_chk = OptionCheckBox("DPI 검열 우회 사용")
        network.addWidget(self.dpi_chk)
        if sys.platform == "win32":
            explanation = (
                "GoodbyeDPI로 패킷을 분할하고 DNS 요청을 1.1.1.1로 보냅니다.\n"
                "관리자 권한 필요 · 실행 중 다른 앱의 네트워크에도 영향을 줄 수 있어요."
            )
        else:
            explanation = (
                "SpoofDPI로 TLS 패킷 분할 + 암호화 DNS(1.1.1.1)를 사용합니다.\n"
                "Dawn의 다운로드에만 적용되며 시스템 프록시 설정은 바꾸지 않습니다."
            )
        dpi_description = QLabel(explanation + "\nIP 차단·로그인 제한은 해결하지 못할 수 있습니다.")
        dpi_description.setObjectName("hint")
        dpi_description.setWordWrap(True)
        network.addWidget(dpi_description)
        self.bypass_status = QLabel()
        self.bypass_status.setObjectName("hint")
        self.bypass_status.setWordWrap(True)
        network.addWidget(self.bypass_status)
        self.admin_btn = QPushButton("관리자 권한으로 다시 열기")
        self.admin_btn.clicked.connect(self._restart_admin)
        network.addWidget(self.admin_btn)
        proxy_lbl = QLabel("우회를 끄면 아래 프록시 설정을 사용합니다.\n직접 입력한 프록시가 자동 감지보다 우선합니다.")
        proxy_lbl.setWordWrap(True)
        proxy_lbl.setObjectName("hint")
        network.addWidget(proxy_lbl)
        network.addWidget(QLabel("프록시 주소"))
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("예: socks5://127.0.0.1:1080")
        self.proxy_edit.setAccessibleName("프록시 주소")
        self.proxy_edit.setClearButtonEnabled(True)
        network.addWidget(self.proxy_edit)
        self.geo_chk = OptionCheckBox("지역 제한 우회 (geo-bypass)")
        self.autoproxy_chk = OptionCheckBox("실행 중인 HTTP 프록시 자동 감지")
        network.addWidget(self.geo_chk)
        network.addWidget(self.autoproxy_chk)
        self.dpi_chk.toggled.connect(self._update_bypass_controls)
        network.addStretch()
        footer = QHBoxLayout()
        note = QLabel("변경한 설정은 다음 다운로드부터 적용됩니다.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        close_btn = QPushButton("확인")
        close_btn.setObjectName("accentBtn")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.adv.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        self.adv.finished.connect(self._settings_finished)

    # ----- settings -----

    def _load_settings(self) -> None:
        default_dir = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.DownloadLocation), "Dawn"
        )
        self.folder_edit.setText(self.settings.value("outdir", "") or default_dir)
        self.parallel_combo.setCurrentIndex(max(0, min(3, int(self.settings.value("parallel", 2)) - 1)))
        self.worker.max_parallel = self.parallel_combo.currentData()
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
        self.auto_chk.setChecked(self.settings.value("auto_start", False, type=bool))
        self.sec_start.setText(self.settings.value("sec_start", ""))
        self.sec_end.setText(self.settings.value("sec_end", ""))
        self.subs_chk.setChecked(self.settings.value("subtitles", False, type=bool))
        self.dedup_chk.setChecked(self.settings.value("dedup", True, type=bool))
        self.clip_chk.setChecked(self.settings.value("clip_watch", False, type=bool))
        self.dpi_chk.setChecked(self.settings.value("dpi_bypass", False, type=bool))
        self._update_bypass_controls()

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
        self.settings.setValue("auto_start", self.auto_chk.isChecked())
        self.settings.setValue("proxy", self.proxy_edit.text())
        self.settings.setValue("geo_bypass", self.geo_chk.isChecked())
        self.settings.setValue("autoproxy", self.autoproxy_chk.isChecked())
        self.settings.setValue("dpi_bypass", self.dpi_chk.isChecked())
        self.settings.setValue("sec_start", self.sec_start.text())
        self.settings.setValue("sec_end", self.sec_end.text())
        self.settings.setValue("subtitles", self.subs_chk.isChecked())
        self.settings.setValue("dedup", self.dedup_chk.isChecked())
        self.settings.setValue("clip_watch", self.clip_chk.isChecked())
        self.settings.setValue("parallel", self.parallel_combo.currentData())

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

    def _show_settings(self) -> None:
        self._auto_timer.stop()
        self._update_bypass_controls()
        self.adv.open()

    def _settings_finished(self) -> None:
        self._save_settings()
        self._update_options_hint()
        self._on_url_changed(self.url_edit.text())
        self.url_edit.setFocus()

    def _update_bypass_controls(self) -> None:
        enabled = self.dpi_chk.isChecked()
        self.proxy_edit.setEnabled(not enabled)
        self.autoproxy_chk.setEnabled(not enabled)
        self.admin_btn.setVisible(sys.platform == "win32" and not is_admin())
        engine = find_bypass_engine()
        if not engine:
            text = "우회 엔진 없음 · 앱을 다시 설치하거나 개발 환경에서 scripts/fetch_bypass.py를 실행하세요."
        elif sys.platform == "win32" and not is_admin():
            text = "엔진 준비됨 · 우회 사용 전 관리자 권한으로 다시 열어 주세요."
        else:
            text = "엔진 준비됨 · 다운로드할 때 시작하고 작업이 끝나면 종료합니다."
        self.bypass_status.setText(text)

    def _restart_admin(self) -> None:
        if sys.platform != "win32" or is_admin():
            return
        if any(row.status in ("wait", "extract", "downloading", "postprocess") for _, row in self._rows.values()):
            QMessageBox.information(self, "작업 진행 중", "다운로드가 끝나거나 취소된 뒤 다시 열어 주세요.")
            return
        import ctypes
        import subprocess
        from ctypes import wintypes

        self._save_settings()
        self.settings.sync()
        args = [] if getattr(sys, "frozen", False) else [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        ]
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell_execute.restype = ctypes.c_void_p
        # A restarted onefile app must unpack its own resources before this
        # instance exits and removes its temporary bundle (PyInstaller >= 6.9).
        previous_reset = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            result = shell_execute(None, "runas", sys.executable, subprocess.list2cmdline(args), None, 1)
        finally:
            if previous_reset is None:
                os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            else:
                os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = previous_reset
        if not result or result <= 32:
            QMessageBox.warning(self, "관리자 권한 요청", "권한 요청이 취소되었거나 실행하지 못했습니다. 현재 창은 유지합니다.")
            return
        self.adv.accept()
        self.close()

    def _update_options_hint(self) -> None:
        options = []
        if self.auto_chk.isChecked():
            options.append("붙여넣기 자동 시작")
        if self.clip_chk.isChecked():
            options.append("클립보드 감시 켜짐")
        if self.playlist_chk.isChecked():
            options.append("재생목록 전체")
        if self.subs_chk.isChecked():
            options.append("자막 저장")
        if self.sec_start.text() or self.sec_end.text():
            options.append(f"구간 {self.sec_start.text() or '0:00'} ~ {self.sec_end.text() or '끝'}")
        if self.browser_combo.currentData() or self.cookie_file_edit.text():
            options.append("로그인 쿠키 사용")
        if self.dpi_chk.isChecked():
            options.append("DPI 우회 · " + ("GoodbyeDPI" if sys.platform == "win32" else "SpoofDPI"))
        elif self.proxy_edit.text():
            options.append("프록시 사용")
        self.options_hint.setText(" · ".join(options))
        self.options_hint.setToolTip(self.options_hint._full)
        self.options_hint.setVisible(bool(options))

    def _paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        if not text:
            self.feedback_lbl.setText("클립보드가 비어 있어요. 먼저 링크를 복사해 주세요.")
            return
        self.url_edit.setText(text)
        self.url_edit.setFocus()

    def _on_url_changed(self, text: str) -> None:
        urls = URL_RE.findall(text)
        self.start_btn.setEnabled(bool(urls))
        self.start_btn.setText(f"{len(urls)}개 다운로드" if len(urls) > 1 else "다운로드")
        if urls:
            message = f"링크 {len(urls)}개 준비됨 · 형식과 화질을 확인한 뒤 시작하세요."
        elif text.strip():
            message = "http:// 또는 https://로 시작하는 링크를 입력해 주세요."
        else:
            message = "여러 링크도 한 번에 붙여넣을 수 있어요."
        prev = getattr(self, "_prev_url_len", 0)
        self._prev_url_len = len(text)
        self._auto_timer.stop()
        if self.auto_chk.isChecked() and len(text) - prev >= 10 and urls:
            self._auto_timer.start()
            message = "자동 다운로드를 시작합니다…"
        self.feedback_lbl.setText(message)

    def _auto_enqueue(self) -> None:
        if self.auto_chk.isChecked() and self.url_edit.text().strip():
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
        urls = URL_RE.findall(self.url_edit.text().strip())
        if not urls:
            self.feedback_lbl.setText("다운로드할 링크를 먼저 입력해 주세요.")
            self.url_edit.setFocus()
            return
        playlist_cache = {}
        added = 0
        duplicates = 0
        remaining = []
        for url in urls:
            if self._url_active(url):
                duplicates += 1
            elif self._enqueue_url(url, playlist_cache):
                added += 1
            else:
                remaining.append(url)
        self.url_edit.setText(" ".join(remaining or (urls if duplicates and not added else [])))
        self._auto_timer.stop()
        if remaining:
            self.feedback_lbl.setText("저장 폴더를 만들 수 없어요. 저장 위치를 변경한 뒤 다시 시작해 주세요.")
        else:
            parts = [f"{added}개를 다운로드 목록에 추가했어요."] if added else []
            if duplicates:
                parts.append(f"같은 링크·형식의 작업 {duplicates}개가 이미 진행 중이에요. 해당 항목으로 이동했어요.")
            self.feedback_lbl.setText(" ".join(parts))
        self._save_settings()
        self.url_edit.setFocus()

    def _enqueue_url(self, url: str, playlist_cache: dict | None = None) -> bool:
        """Queue a URL using the currently selected download options."""
        if self._url_active(url):
            self.feedback_lbl.setText("같은 링크·형식으로 이미 다운로드 중이에요. 다른 형식은 바로 받을 수 있어요.")
            return False
        outdir = self.folder_edit.text().strip()
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            self.feedback_lbl.setText("저장 폴더를 만들 수 없어요. 저장 위치를 변경해 주세요.")
            return False
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
            proxy=None if self.dpi_chk.isChecked() else self.proxy_edit.text().strip() or None,
            dpi_bypass=self.dpi_chk.isChecked(),
            auto_proxy=self.autoproxy_chk.isChecked() and not self.dpi_chk.isChecked(),
            geo_bypass=self.geo_chk.isChecked(),
            section=self._parse_section(),
            subtitles=self.subs_chk.isChecked(),
            dedup=self.dedup_chk.isChecked(),
        )
        self._add_row(job)
        return True

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

    def _url_active(self, url: str, job: DownloadJob | None = None) -> bool:
        mode = job.mode if job else self._mode
        codec = job.audio_codec if job else self._job_quality()[1]
        outdir = os.path.normcase(os.path.realpath(job.outdir if job else self.folder_edit.text().strip()))
        for item, row in self._rows.values():
            if (
                row.url == url
                and row.status in ("wait", "extract", "downloading", "postprocess")
                and row.job.mode == mode
                and (mode == "video" or row.job.audio_codec == codec)
                and os.path.normcase(os.path.realpath(row.job.outdir)) == outdir
            ):
                self.list.setCurrentItem(item)
                self.list.scrollToItem(item)
                return True
        return False

    def _clear_finished(self) -> None:
        for job_id, (item, row) in list(self._rows.items()):
            if row.status in ("done", "cancelled", "skipped"):
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
            self.summary_lbl.setText("아직 다운로드가 없어요")
        else:
            bits = [f"진행 {n_run}", f"완료 {n_done}"]
            if n_fail:
                bits.append(f"실패 {n_fail}")
            n_cancelled = sum(1 for _i, r in self._rows.values() if r.status == "cancelled")
            if n_cancelled:
                bits.append(f"취소 {n_cancelled}")
            n_skipped = sum(1 for _i, r in self._rows.values() if r.status == "skipped")
            if n_skipped:
                bits.append(f"건너뜀 {n_skipped}")
            self.summary_lbl.setText(" · ".join(bits))
        self.summary_lbl.setToolTip(self.summary_lbl._full)
        self.stack.setCurrentIndex(0 if not self._rows else 1)
        self.clear_btn.setEnabled(any(r.status in ("done", "cancelled", "skipped") for _i, r in self._rows.values()))

    def _add_row(self, job: DownloadJob) -> None:
        job_id = self._next_id
        self._next_id += 1
        row = JobRow(job.url, job.mode)
        row.outdir = job.outdir
        row.job = job
        row.output_format = job.output_format.upper()
        row.icon.setText(row.output_format)
        row.set_state("wait")
        item = QListWidgetItem()
        self.list.addItem(item)
        self.list.setItemWidget(item, row)
        row.ensurePolished()
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        self.list.scrollToBottom()
        row.cancelRequested.connect(lambda jid=job_id: self._cancel(jid))
        row.retryRequested.connect(lambda jid=job_id: self._retry(jid))
        self._rows[job_id] = (item, row)
        self.worker.add(job_id, job)
        self._update_summary()

    def _retry(self, job_id: int) -> None:
        pair = self._rows.get(job_id)
        if not pair:
            return
        _item, row = pair
        if self._url_active(row.job.url, row.job):
            self.feedback_lbl.setText("같은 링크·형식의 작업이 이미 진행 중이에요.")
            return
        if row.status == "skipped":
            row.job = replace(row.job, dedup=False)
        row.reset_for_retry()
        self.worker.add(job_id, row.job)
        self._update_summary()

    def _cancel(self, job_id: int) -> None:
        self.worker.cancel(job_id)
        if job_id in self._rows:
            row = self._rows[job_id][1]
            row.cancel_btn.setEnabled(False)
            row.cancel_btn.setText("취소 중")

    def _open_row_folder(self, item: QListWidgetItem) -> None:
        row = self.list.itemWidget(item)
        if isinstance(row, JobRow) and row.outdir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(row.outdir))

    # ----- drag & drop -----

    def dragEnterEvent(self, e) -> None:
        text = e.mimeData().text() or " ".join(u.toString() for u in e.mimeData().urls())
        if URL_RE.search(text):
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        text = e.mimeData().text() or " ".join(u.toString() for u in e.mimeData().urls())
        urls = URL_RE.findall(text)
        if urls:
            existing = self.url_edit.text().strip()
            self.url_edit.setText(" ".join(filter(None, [existing, *urls])))
            self.url_edit.setFocus()
            e.acceptProposedAction()

    @Slot(int, dict)
    def _on_progress(self, job_id: int, d: dict) -> None:
        pair = self._rows.get(job_id)
        if not pair:
            return
        _, row = pair
        ev = d.get("event")
        if ev == "info":
            row.set_state("extract", d.get("message") or "")
        elif ev == "progress":
            if d.get("title"):
                row.title.setText(d["title"])
            if d.get("phase") == "postprocess":
                row.set_state("postprocess")
            else:
                row.set_progress(d.get("fraction"), d.get("speed"), d.get("eta"))
        elif ev == "title":
            row.title.setText(d.get("title") or row.title.text())
            thumb = d.get("thumb_data")
            if thumb:
                self._set_thumb(row, thumb)
        elif ev == "pp":
            row.set_state("postprocess")
        elif ev == "done":
            row.set_state("done")
            self._notify_done(row.title.text())
            self._update_summary()
        elif ev == "skipped":
            row.set_state("skipped", d.get("message") or "")
            self._update_summary()
        elif ev == "cancelled":
            row.set_state("cancelled")
            self._update_summary()
        elif ev == "error":
            row.set_state("error", d.get("message") or "다운로드하지 못했어요. 다시 시도해 주세요.")
            row.setToolTip(d.get("message") or "")
            self._update_summary()
        elif ev == "warn":
            row.setToolTip(d.get("message") or "")

    def _set_thumb(self, row: JobRow, data: bytes) -> None:
        """Decode artwork already fetched through the job's network route."""
        pm = QPixmap()
        pm.loadFromData(data)
        if pm.isNull():
            return
        side = min(pm.width(), pm.height())
        pm = pm.copy((pm.width() - side) // 2, (pm.height() - side) // 2, side, side)
        pm = pm.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(2)
        row.icon.setPixmap(pm)



    # ----- lifecycle -----

    def closeEvent(self, e) -> None:
        self._save_settings()
        self.worker.stop()
        shutdown_bypass()
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
