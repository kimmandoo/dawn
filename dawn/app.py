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
    ("최고 음질 (VBR)", "0"),
    ("320 kbps", "320"),
    ("192 kbps", "192"),
    ("128 kbps", "128"),
]
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
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
                return
            job_id, job = item
            ev = self._cancels.get(job_id)
            if ev is None:
                continue
            if ev.is_set():
                self.progressed.emit(job_id, {"event": "cancelled"})
                continue
            run_job(job, lambda d, jid=job_id: self.progressed.emit(jid, d), ev)
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

        icon = QLabel("🎬" if mode == "video" else "🎵")
        icon.setFixedWidth(24)
        self.title = QLabel(url)
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

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(icon)
        top.addWidget(self.title, 1)
        top.addWidget(self.retry_btn)
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

    def set_state(self, status: str, detail: str = "") -> None:
        self.status = status
        text = STATUS_TEXT.get(status, status)
        if detail:
            text = f"{text} — {detail}"
        self.status_lbl.setText(text)
        color = STATUS_COLOR.get(status)
        self.status_lbl.setStyleSheet(f"color: {color};" if color else "")
        if status in ("done", "error", "cancelled"):
            self.cancel_btn.hide()
            self.retry_btn.setVisible(status == "error")
            if status == "done":
                self.bar.setRange(0, 100)
                self.bar.setValue(100)

    def reset_for_retry(self) -> None:
        self.status = "wait"
        self.retry_btn.hide()
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
        self.resize(660, 700)
        self.setAcceptDrops(True)
        self.settings = QSettings("Dawn", "Dawn")
        self._next_id = 1
        self._rows = {}  # id -> (QListWidgetItem, JobRow)
        self._mode = "video"
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(700)
        self._auto_timer.timeout.connect(self._auto_enqueue)

        self.worker = Worker(self)
        self.worker.progressed.connect(self._on_progress)
        self.worker.start()

        self._build_ui()
        self._load_settings()

    # ----- UI -----

    def _build_ui(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(10)

        title = QLabel("⬇ Dawn")
        title.setObjectName("appTitle")
        sub = QLabel("링크를 붙여넣으면 바로 다운로드됩니다")
        sub.setObjectName("appSub")
        lay.addWidget(title)
        lay.addWidget(sub)

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
        self.btn_audio = QPushButton("🎵  음원만 (MP3 · 앨범아트)")
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
        self.adv_btn = QPushButton("⚙ 성인 인증 · 쿠키 · 재생목록 ▾")
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
        self.adv.hide()
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
        lay.addWidget(self.list, 1)

        hint = QLabel("목록 항목 더블클릭 → 저장 폴더 열기")
        hint.setObjectName("appSub")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)

        self.setCentralWidget(root)
        self.setStyleSheet(
            """
            #appTitle { font-size: 24px; font-weight: 700; }
            #appSub, #hint, #statusLbl { color: #888; font-size: 12px; }
            QPushButton[checkable="true"]:checked {
                border: 2px solid #e2554f; font-weight: 600; }
            #jobRow { border: 1px solid rgba(128,128,128,0.35); border-radius: 8px; }
            #advBox { border: 1px dashed rgba(128,128,128,0.5); border-radius: 8px; }
            #advBtn { text-align: left; border: none; color: #888; }
            QProgressBar { border: none; border-radius: 3px;
                           background: rgba(128,128,128,0.25); }
            QProgressBar::chunk { border-radius: 3px; background: #e2554f; }
            """
        )
        self._set_mode(self.settings.value("mode", "video"))

    # ----- settings -----

    def _load_settings(self) -> None:
        default_dir = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.DownloadLocation), "Dawn"
        )
        self.folder_edit.setText(self.settings.value("outdir", default_dir))
        self._restore_combo("vquality", "best")
        self._restore_combo("aquality", "0")
        bi = self.settings.value("cookie_browser", "")
        for i in range(self.browser_combo.count()):
            if (self.browser_combo.itemData(i) or "") == bi:
                self.browser_combo.setCurrentIndex(i)
        self.cookie_file_edit.setText(self.settings.value("cookie_file", ""))
        self.playlist_chk.setChecked(self.settings.value("playlist", False, type=bool))
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
                            "best" if mode == "video" else "0")

    def _toggle_adv(self, on: bool) -> None:
        self.adv.setVisible(on)
        self.adv_btn.setText("⚙ 성인 인증 · 쿠키 · 재생목록 " + ("▴" if on else "▾"))

    def _paste(self) -> None:
        self.url_edit.setText(QApplication.clipboard().text().strip())

    def _on_url_changed(self, text: str) -> None:
        # 링크가 들어오면(붙여넣기) 잠깐 후 자동 시작
        if URL_RE.search(text or ""):
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
        for url in urls:
            self._enqueue_url(url)
        self.url_edit.clear()

    def _enqueue_url(self, url: str) -> None:
        """Enqueue one URL (shared by paste, Enter, drag&drop)."""
        if self._url_active(url):
            return
        outdir = self.folder_edit.text().strip()
        os.makedirs(outdir, exist_ok=True)
        job = DownloadJob(
            url=url,
            mode=self._mode,
            outdir=outdir,
            quality=self.quality_combo.currentData(),
            cookies=CookieConfig(
                browser=self.browser_combo.currentData(),
                cookie_file=self.cookie_file_edit.text().strip() or None,
            ),
            playlist=self._resolve_playlist(url),
        )
        self._add_row(job)
        self._update_summary()

    def _resolve_playlist(self, url: str) -> bool:
        if not is_playlist_url(url):
            return False
        if self.playlist_chk.isChecked():
            return True
        box = QMessageBox(self)
        box.setWindowTitle("재생목록 감지")
        box.setText("이 링크는 재생목록을 포함하고 있습니다.")
        box.setInformativeText("재생목록 전체를 받을까요, 이 영상만 받을까요?")
        all_btn = box.addButton("재생목록 전체", QMessageBox.AcceptRole)
        one_btn = box.addButton("이 영상만", QMessageBox.RejectRole)
        box.setDefaultButton(all_btn)
        box.exec()
        return box.clickedButton() is all_btn

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
        elif ev == "pp":
            row.set_state("postprocess")
        elif ev == "done":
            row.set_state("done")
            self._update_summary()
        elif ev == "cancelled":
            row.set_state("cancelled")
            self._update_summary()
        elif ev == "error":
            row.set_state("error", (d.get("message") or "")[:160])
            row.setToolTip(d.get("message") or "")
            self._update_summary()
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
    icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
