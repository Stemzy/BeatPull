

import json
import math
import os
import random
import shutil
import sys

from yt_dlp import YoutubeDL
from PySide6.QtCore import (
    Qt, QThread, Signal, QUrl, QSize, QTimer, QVariantAnimation, QPoint, QEvent,
)
from PySide6.QtGui import (
    QFont, QPixmap, QIcon, QColor, QPainter, QLinearGradient, QRadialGradient,
    QPen, QBrush, QAction, QImage, QKeySequence, QShortcut, QDesktopServices,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QGridLayout,
    QStackedLayout,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

vol1, vol2 = .25, 25

# library index 

DATA_DIR = os.path.join(os.path.expanduser("~"), ".beatpull")
LIBRARY_JSON = os.path.join(DATA_DIR, "library.json")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
os.makedirs(DATA_DIR, exist_ok=True)

# BPM/key analysis needs librosa. Only check if it's importable here (fast);
# the heavy import happens inside the worker thread when we actually analyze.
import importlib.util
ANALYSIS_AVAILABLE = importlib.util.find_spec("librosa") is not None

# Version shown in the app. Because the auto-updater replaces this whole file
# when it pulls an update, this constant is always the true running version —
# whether launched from the exe or run directly with `python main.py`.
# Bump it together with version.json on every release.
APP_VERSION = "1.0.13"


def app_version():
    return APP_VERSION


def load_store():
    """Return {"categories": [...], "tracks": [...]}. Handles the old
    list-only format so existing libraries still load."""
    if os.path.exists(LIBRARY_JSON):
        try:
            with open(LIBRARY_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = None
        if isinstance(data, list):
            return {"categories": [], "tracks": data}
        if isinstance(data, dict):
            return {
                "categories": list(data.get("categories", [])),
                "tracks": list(data.get("tracks", [])),
            }
    return {"categories": [], "tracks": []}


def save_store(store):
    with open(LIBRARY_JSON, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


# App settings
SETTINGS_JSON = os.path.join(DATA_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "out_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "format": "mp3",            
    "quality_mp3": "192",
    "quality_mp4": "1080",
    "volume": 80,
    "win_w": 980,
    "win_h": 700,
    "reduce_motion": False,
    "star_density": 230,
    "sort_mode": "added",      
    "last_file": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_JSON):
        try:
            with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


SETTINGS = load_settings()


def fmt_time(ms):
    if ms <= 0:
        return "0:00"
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


# Worker: downloads on a background thread and also grabs a thumbnail + info
class DownloadWorker(QThread):
    progress = Signal(float)
    log = Signal(str)
    finished_ok = Signal(dict)   
    failed = Signal(str)
    all_done = Signal(int)       

    def __init__(self, urls, fmt, quality, out_dir, allow_playlist=False):
        super().__init__()
        self.urls = urls         
        self.fmt = fmt
        self.quality = quality
        self.out_dir = out_dir
        self.allow_playlist = allow_playlist

    def _hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                self.progress.emit(d.get("downloaded_bytes", 0) / total * 100)
        elif d["status"] == "finished":
            self.progress.emit(100.0)
            self.log.emit("Converting with ffmpeg…")

    def _options(self):
        opts = {
            "outtmpl": os.path.join(self.out_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [self._hook],
            "noplaylist": not self.allow_playlist,
            "quiet": True,
            "no_warnings": True,
            "writethumbnail": True,
            "ignoreerrors": True,
            "postprocessors": [],
        }
        if self.fmt == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": self.quality,
            })
        elif self.fmt == "wav":
            # lossless - no quality setting applies
            opts["format"] = "bestaudio/best"
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
            })
        else:
            h = self.quality
            opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={h}]/best"
            )
            opts["merge_output_format"] = "mp4"
        # jpg thumbnail
        opts["postprocessors"].append(
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
        # WAV files can't hold embedded cover art, so skip embedding for them
        if self.fmt != "wav":
            opts["postprocessors"].append(
                {"key": "EmbedThumbnail", "already_have_thumbnail": True})
        opts["postprocessors"].append(
            {"key": "FFmpegMetadata", "add_metadata": True})
        return opts

    def _finalize(self, info, playlist=None):
        """Turn one downloaded video's info into a library entry, moving its
        thumbnail into the private thumbs folder and clearing stray images.
        If it came from a playlist, tag it with the playlist name."""
        media_path = None
        reqs = info.get("requested_downloads")
        if reqs and reqs[0].get("filepath"):
            media_path = reqs[0]["filepath"]
        if not media_path:
            base = os.path.join(self.out_dir, info.get("title", "track"))
            media_path = f"{base}.{self.fmt}"

        # For extracted audio (mp3/wav), yt-dlp sometimes reports the
        # pre-conversion path (e.g. ".webm") which gets deleted after ffmpeg
        # converts it. Point at the real converted file instead - otherwise
        # the entry references a ghost file and BPM/key analysis skips it.
        if self.fmt in ("mp3", "wav"):
            want = os.path.splitext(media_path)[0] + "." + self.fmt
            if os.path.exists(want):
                media_path = want

        print(f"[download] final file: {media_path} "
              f"(exists: {os.path.exists(media_path)})", file=sys.stderr)

        stem = os.path.splitext(media_path)[0]
        thumb_path = ""
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            cand = stem + ext
            if not os.path.exists(cand):
                continue
            if not thumb_path:
                os.makedirs(THUMBS_DIR, exist_ok=True)
                dest = os.path.join(THUMBS_DIR, os.path.basename(stem) + ext)
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                    shutil.move(cand, dest)
                    thumb_path = dest
                except Exception:
                    try:
                        os.remove(cand)
                    except Exception:
                        pass
            else:
                try:
                    os.remove(cand)
                except Exception:
                    pass

        # Catch-all sweep: videos sometimes leave thumbnails with extra bits in
        # the name (e.g. "Title.temp.jpg"). Delete any leftover image whose
        # name starts with this song's stem - keeps the folder media-only.
        try:
            base = os.path.basename(stem)
            for f in os.listdir(self.out_dir):
                if not f.startswith(base):
                    continue
                if os.path.splitext(f)[1].lower() in (
                        ".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    try:
                        os.remove(os.path.join(self.out_dir, f))
                    except Exception:
                        pass
        except Exception:
            pass

        return {
            "id": info.get("id"),
            "url": info.get("webpage_url"),
            "title": info.get("track") or info.get("title") or "Unknown",
            "artist": info.get("artist") or info.get("uploader") or "Unknown",
            "file": media_path,
            "thumb": thumb_path,
            "format": self.fmt,
            "duration": info.get("duration", 0),
            "playlist": playlist,
        }

    def run(self):
        added = 0
        for idx, url in enumerate(self.urls):
            try:
                self.log.emit(f"[{idx + 1}/{len(self.urls)}] Fetching…")
                self.progress.emit(0.0)
                with YoutubeDL(self._options()) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info is None:
                    self.failed.emit(f"{url}: nothing downloaded")
                    continue
                entries = info.get("entries")
                if entries is not None:
                    playlist_name = info.get("title") or None
                    items = list(entries)
                else:
                    playlist_name = None
                    items = [info]
                for it in items:
                    if not it:
                        continue
                    try:
                        entry = self._finalize(it, playlist_name)
                        self.finished_ok.emit(entry)
                        added += 1
                    except Exception as e:  # noqa: BLE001
                        self.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.failed.emit(f"{url}: {e}")
        self.all_done.emit(added)


# Preview worker
class PreviewWorker(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            opts = {"quiet": True, "no_warnings": True,
                    "skip_download": True, "noplaylist": True}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            thumb_url = info.get("thumbnail")
            thumb_bytes = None
            if thumb_url:
                try:
                    import urllib.request
                    thumb_bytes = urllib.request.urlopen(
                        thumb_url, timeout=10).read()
                except Exception:
                    thumb_bytes = None
            self.done.emit({
                "title": info.get("title") or "Unknown",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "duration": info.get("duration") or 0,
                "thumb_bytes": thumb_bytes,
            })
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# Analysis worker: estimates BPM (tempo) and musical key for an audio file
class AnalyzeWorker(QThread):
    done = Signal(str, float, str)   # file path, bpm, key ("" if it failed)

    KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

    def __init__(self, path):
        super().__init__()
        self.path = path

    def _load_audio(self, librosa):
        """Load audio for analysis. librosa often can't read video containers
        (mp4/webm) directly, so on failure fall back to extracting the audio
        with ffmpeg into a temp wav and analyzing that."""
        try:
            total = 0
            try:
                total = librosa.get_duration(path=self.path)
            except Exception:
                pass
            offset = 30.0 if total and total > 150 else 0.0
            y, sr = librosa.load(self.path, mono=True, sr=22050,
                                 offset=offset, duration=120)
            if y is not None and len(y) > sr * 3:
                return y, sr
        except Exception:
            pass
        # ffmpeg fallback (works for any container ffmpeg understands)
        import subprocess
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "30", "-i", self.path, "-t", "120",
                 "-vn", "-ac", "1", "-ar", "22050", tmp.name],
                capture_output=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            y, sr = librosa.load(tmp.name, mono=True, sr=22050)
            if y is None or len(y) < sr * 3:
                # song may be shorter than the 30s skip - try from the start
                subprocess.run(
                    ["ffmpeg", "-y", "-i", self.path, "-t", "120",
                     "-vn", "-ac", "1", "-ar", "22050", tmp.name],
                    capture_output=True, timeout=120,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                y, sr = librosa.load(tmp.name, mono=True, sr=22050)
            return y, sr
        finally:
            try:
                os.remove(tmp.name)
            except Exception:
                pass

    def run(self):
        try:
            import time
            import librosa
            import numpy as np
            t0 = time.time()
            y, sr = self._load_audio(librosa)
            # 60s from the body of the song is enough for tempo + key and
            # keeps analysis fast
            if len(y) > sr * 60:
                y = y[: sr * 60]

            # --- BPM: median onset strength is robust to noise, and fold
            # half/double-time results into the sensible 70-180 range ---
            onset = librosa.onset.onset_strength(y=y, sr=sr, aggregate=np.median)
            tempo, _ = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
            bpm = float(np.atleast_1d(tempo)[0])
            while 0 < bpm < 70:
                bpm *= 2
            while bpm > 180:
                bpm /= 2
            bpm = float(round(bpm))

            # --- Key: analyze only the harmonic (melodic) part so drums don't
            # smear the note profile; median over time beats mean for noise.
            # (hpss on the stft is much faster than librosa.effects.harmonic) ---
            stft = librosa.stft(y, n_fft=2048, hop_length=1024)
            harm, _ = librosa.decompose.hpss(stft)
            y_harm = librosa.istft(harm, hop_length=1024)
            chroma = librosa.feature.chroma_stft(y=y_harm, sr=sr,
                                                 hop_length=1024)
            chroma = np.median(chroma, axis=1)
            maj = np.array(self.MAJ)
            minor = np.array(self.MIN)
            best_corr, best = -2.0, ""
            for i in range(12):
                cm = np.corrcoef(chroma, np.roll(maj, i))[0, 1]
                ci = np.corrcoef(chroma, np.roll(minor, i))[0, 1]
                if cm > best_corr:
                    best_corr, best = cm, f"{self.KEYS[i]} major"
                if ci > best_corr:
                    best_corr, best = ci, f"{self.KEYS[i]} minor"
            print(f"[analyze] done in {time.time() - t0:.1f}s: "
                  f"{bpm:.0f} BPM, {best} - {os.path.basename(self.path)}",
                  file=sys.stderr)
            self.done.emit(self.path, bpm, best)
        except Exception:
            # visible in the terminal when run from source / console build
            import traceback
            print(f"[analyze] failed for {self.path}:", file=sys.stderr)
            traceback.print_exc()
            self.done.emit(self.path, 0.0, "")


# Download tab
class DownloadTab(QWidget):
    track_added = Signal(dict)

    def __init__(self):
        super().__init__()
        self.worker = None
        self.pworker = None
        self.out_dir = SETTINGS.get("out_dir") or os.path.join(
            os.path.expanduser("~"), "Downloads")
        self._build()

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("field")
        return lbl

    def _build(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("scroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(40, 34, 40, 34)
        outer.setSpacing(0)

        center = QHBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.addStretch(1)

        panel = QWidget()
        panel.setMinimumWidth(420)
        panel.setMaximumWidth(660)
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        title = QLabel("Download")
        title.setObjectName("title")
        subtitle = QLabel("Paste a link. Pick a format. Pull it down.")
        subtitle.setObjectName("subtitle")
        col.addWidget(title)
        col.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(24, 24, 24, 24)
        cl.setSpacing(16)

        cl.addWidget(self._label("Link"))
        link_row = QHBoxLayout()
        link_row.setSpacing(10)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "Paste a link, or more by separating with spaces")
        self.url_input.returnPressed.connect(self._preview)
        link_row.addWidget(self.url_input, 1)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setObjectName("ghost")
        self.preview_btn.clicked.connect(self._preview)
        link_row.addWidget(self.preview_btn, 0)
        cl.addLayout(link_row)

        self.playlist_chk = QCheckBox("Download full playlist (for playlist links)")
        self.playlist_chk.setObjectName("chk")
        cl.addWidget(self.playlist_chk)

        self.preview_box = QFrame()
        self.preview_box.setObjectName("previewBox")
        self.preview_box.hide()
        pv = QHBoxLayout(self.preview_box)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(14)
        self.preview_thumb = QLabel()
        self.preview_thumb.setObjectName("previewThumb")
        self.preview_thumb.setFixedSize(160, 90)
        self.preview_thumb.setAlignment(Qt.AlignCenter)
        pv.addWidget(self.preview_thumb, 0)
        pv_info = QVBoxLayout()
        pv_info.setSpacing(4)
        self.preview_title = QLabel("")
        self.preview_title.setObjectName("rowTitle")
        self.preview_title.setWordWrap(True)
        self.preview_meta = QLabel("")
        self.preview_meta.setObjectName("rowArtist")
        pv_info.addStretch()
        pv_info.addWidget(self.preview_title)
        pv_info.addWidget(self.preview_meta)
        pv_info.addStretch()
        pv.addLayout(pv_info, 1)
        cl.addWidget(self.preview_box)

        row = QHBoxLayout()
        row.setSpacing(14)
        fbox = QVBoxLayout()
        fbox.setSpacing(6)
        fbox.addWidget(self._label("Format"))
        self.fmt_select = QComboBox()
        self.fmt_select.addItems(["MP3 (audio)", "MP4 (video)", "WAV (audio)"])
        self.fmt_select.setMinimumWidth(150)
        saved_fmt = SETTINGS.get("format", "mp3")
        self.fmt_select.setCurrentIndex(
            {"mp3": 0, "mp4": 1, "wav": 2}.get(saved_fmt, 0))
        self.fmt_select.currentIndexChanged.connect(self._on_format_change)
        fbox.addWidget(self.fmt_select)
        row.addLayout(fbox, 1)
        qbox = QVBoxLayout()
        qbox.setSpacing(6)
        qbox.addWidget(self._label("Quality"))
        self.qual_select = QComboBox()
        self.qual_select.setMinimumWidth(150)
        qbox.addWidget(self.qual_select)
        row.addLayout(qbox, 1)
        cl.addLayout(row)
        self._on_format_change(self.fmt_select.currentIndex())
        if saved_fmt in ("mp3", "mp4"):
            q = SETTINGS.get("quality_mp4" if saved_fmt == "mp4"
                             else "quality_mp3")
            if q:
                self.qual_select.setCurrentText(q)

        cl.addWidget(self._label("Save to"))
        frow = QHBoxLayout()
        frow.setSpacing(12)
        self.folder_label = QLineEdit(self.out_dir)
        self.folder_label.setReadOnly(True)
        frow.addWidget(self.folder_label, 1)
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._choose_folder)
        frow.addWidget(browse, 0)
        open_btn = QPushButton("Open")
        open_btn.setObjectName("ghost")
        open_btn.setToolTip("Open this folder")
        open_btn.clicked.connect(self._open_folder)
        frow.addWidget(open_btn, 0)
        cl.addLayout(frow)
        col.addWidget(card)

        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self._start)
        col.addWidget(self.download_btn)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        col.addWidget(self.progress)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("log")
        self.log_view.setFixedHeight(120)
        col.addWidget(self.log_view)

        center.addWidget(panel, 0)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        scroll.setWidget(body)
        page.addWidget(scroll)

    def _log(self, m):
        self.log_view.append(m)

    # -- preview before download --
    def _preview(self):
        url = self.url_input.text().strip()
        if not url:
            self._log("Paste a link to preview it.")
            return
        self.preview_btn.setEnabled(False)
        self.preview_btn.setText("Fetching…")
        self.pworker = PreviewWorker(url)
        self.pworker.done.connect(self._preview_done)
        self.pworker.failed.connect(self._preview_fail)
        self.pworker.start()

    def _preview_done(self, info):
        dur = info.get("duration", 0)
        mins, secs = divmod(int(dur), 60)
        meta = info.get("uploader", "")
        if dur:
            meta = f"{meta}  ·  {mins}:{secs:02d}" if meta else f"{mins}:{secs:02d}"
        self.preview_title.setText(info.get("title", "Unknown"))
        self.preview_meta.setText(meta)
        data = info.get("thumb_bytes")
        if data:
            pix = QPixmap()
            if pix.loadFromData(data):
                self.preview_thumb.setPixmap(pix.scaled(
                    160, 90, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            else:
                self.preview_thumb.setText("♪")
        else:
            self.preview_thumb.setText("♪")
        self.preview_box.show()
        self.preview_btn.setEnabled(True)
        self.preview_btn.setText("Preview")

    def _preview_fail(self, msg):
        self._log(f"Couldn't preview: {msg}")
        self.preview_btn.setEnabled(True)
        self.preview_btn.setText("Preview")

    def _on_format_change(self, idx):
        self.qual_select.clear()
        if idx == 0:      # mp3
            self.qual_select.addItems(["320", "256", "192", "128"])
            self.qual_select.setCurrentText("192")
            self.qual_select.setEnabled(True)
        elif idx == 1:    # mp4
            self.qual_select.addItems(["2160", "1440", "1080", "720", "480"])
            self.qual_select.setCurrentText("1080")
            self.qual_select.setEnabled(True)
        else:             # wav - lossless, no quality choice
            self.qual_select.addItems(["Lossless"])
            self.qual_select.setEnabled(False)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder", self.out_dir)
        if path:
            self.out_dir = path
            self.folder_label.setText(path)
            SETTINGS["out_dir"] = path
            save_settings(SETTINGS)

    def _open_folder(self):
        if os.path.isdir(self.out_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir))

    def _start(self):
        raw = self.url_input.text().strip()
        if not raw:
            self._log("Please paste a link first.")
            return
        urls = [u for u in raw.replace(",", " ").split() if u]
        fmt = {0: "mp3", 1: "mp4", 2: "wav"}[self.fmt_select.currentIndex()]
        quality = self.qual_select.currentText()
        # remember choices for next time
        SETTINGS["format"] = fmt
        if fmt in ("mp3", "mp4"):
            SETTINGS["quality_mp3" if fmt == "mp3" else "quality_mp4"] = quality
        SETTINGS["out_dir"] = self.out_dir
        save_settings(SETTINGS)

        self.download_btn.setEnabled(False)
        self.download_btn.setText("Working…")
        self.progress.setValue(0)
        n = len(urls)
        self._log(f"Starting {fmt.upper()} @ {quality} — {n} link{'s' if n != 1 else ''}…")
        allow_pl = self.playlist_chk.isChecked()
        self.worker = DownloadWorker(urls, fmt, quality, self.out_dir, allow_pl)
        self.worker.progress.connect(lambda p: self.progress.setValue(int(p)))
        self.worker.log.connect(self._log)
        self.worker.finished_ok.connect(self._item_done)
        self.worker.failed.connect(self._error)
        self.worker.all_done.connect(self._all_done)
        self.worker.start()

    def _item_done(self, entry):
        self._log(f"Saved: {entry['title']}")
        self.track_added.emit(entry)

    def _all_done(self, count):
        self.progress.setValue(100 if count else 0)
        self._log(f"Done — {count} added to Library."
                  if count else "Done — nothing was added.")
        self._reset()

    def _error(self, msg):
        self._log(f"Error: {msg}")

    def _reset(self):
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Download")


# A single row in the library list
class TrackRow(QFrame):
    def __init__(self, entry, owner):
        super().__init__()
        self.entry = entry
        self.owner = owner
        self.setObjectName("trackRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(12)

        thumb = QLabel()
        thumb.setFixedSize(54, 54)
        thumb.setObjectName("rowThumb")
        thumb.setAlignment(Qt.AlignCenter)
        if entry.get("thumb") and os.path.exists(entry["thumb"]):
            pix = QPixmap(entry["thumb"]).scaled(
                54, 54, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            thumb.setPixmap(pix)
        else:
            thumb.setText("♪")
        lay.addWidget(thumb)

        info = QVBoxLayout()
        info.setSpacing(2)
        t = QLabel(entry["title"])
        t.setObjectName("rowTitle")
        self.sub_label = QLabel(self._subtitle(entry))
        self.sub_label.setObjectName("rowArtist")
        info.addWidget(t)
        info.addWidget(self.sub_label)
        lay.addLayout(info)
        lay.addStretch()

        self.fav_btn = QPushButton("★" if entry.get("favorite") else "☆")
        self.fav_btn.setObjectName("starBtn")
        self.fav_btn.setFixedWidth(34)
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.clicked.connect(lambda: owner.toggle_favorite(entry))
        lay.addWidget(self.fav_btn)

        play = QPushButton("Play")
        play.setObjectName("ghost")
        play.clicked.connect(lambda: owner._play_entry(entry))
        lay.addWidget(play)

        delete = QPushButton()
        delete.setObjectName("ghost")
        delete.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        delete.setToolTip("Remove from library")
        delete.setFixedWidth(40)
        delete.clicked.connect(lambda: owner.delete_track(entry))
        lay.addWidget(delete)

    @staticmethod
    def _subtitle(entry):
        parts = [entry.get("artist", ""), entry.get("format", "").upper()]
        if entry.get("bpm"):
            parts.append(f"{int(entry['bpm'])} BPM")
        if entry.get("key"):
            parts.append(entry["key"])
        cats = entry.get("categories") or []
        if cats:
            parts.append(", ".join(cats))
        return "  ·  ".join(p for p in parts if p)

    def update_meta(self):
        self.sub_label.setText(self._subtitle(self.entry))

    def set_playing(self, playing):
        self.setProperty("playing", "true" if playing else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, event):
        self.owner._play_entry(self.entry)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        addmenu = menu.addMenu("Add to category")
        cats = self.owner.store["categories"]
        cur = self.entry.get("categories") or []
        if cats:
            for c in cats:
                act = QAction(c, self)
                act.setCheckable(True)
                act.setChecked(c in cur)
                act.triggered.connect(
                    lambda checked=False, c=c: self.owner.toggle_category(self.entry, c))
                addmenu.addAction(act)
        else:
            none_act = QAction("(no categories yet)", self)
            none_act.setEnabled(False)
            addmenu.addAction(none_act)
        addmenu.addSeparator()
        new_act = QAction("New category…", self)
        new_act.triggered.connect(
            lambda: self.owner.new_category_and_add(self.entry))
        addmenu.addAction(new_act)

        fav = QAction("Unfavorite" if self.entry.get("favorite") else "Favorite", self)
        fav.triggered.connect(lambda: self.owner.toggle_favorite(self.entry))
        menu.addAction(fav)

        ren = QAction("Rename…", self)
        ren.triggered.connect(lambda: self.owner.rename_track(self.entry))
        menu.addAction(ren)

        analyze = QAction("Re-analyze BPM & key", self)
        analyze.triggered.connect(lambda: self.owner.analyze_entry(self.entry))
        menu.addAction(analyze)

        editbk = QAction("Edit BPM & key…", self)
        editbk.triggered.connect(lambda: self.owner.edit_bpm_key(self.entry))
        menu.addAction(editbk)

        menu.addSeparator()
        dele = QAction("Remove from library", self)
        dele.triggered.connect(lambda: self.owner.delete_track(self.entry))
        menu.addAction(dele)
        menu.exec(event.globalPos())


# Sidebar 
class Sidebar(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.art_opacity = 0.35   # 0-1; lower = more stars show through
        # blurred artwork background (behind everything)
        self.bg = QLabel(self)
        self.bg.setScaledContents(True)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(95)
        self.bg.setGraphicsEffect(blur)
        self.bg.hide()
        # dark scrim so foreground text stays readable
        self.scrim = QFrame(self)
        self.scrim.setObjectName("sideScrim")
        self.scrim.hide()

    def resizeEvent(self, e):
        self.bg.setGeometry(0, 0, self.width(), self.height())
        self.scrim.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(e)

    def set_art(self, pixmap):
        if pixmap and not pixmap.isNull():
            # pre-fade the artwork so the stars stay visible behind it
            faded = QPixmap(pixmap.size())
            faded.fill(Qt.transparent)
            p = QPainter(faded)
            p.setOpacity(self.art_opacity)
            p.drawPixmap(0, 0, pixmap)
            p.end()
            self.bg.setPixmap(faded)
            self.bg.show()
            self.scrim.show()
            self.bg.lower()
            self.scrim.raise_()
            self.scrim.stackUnder(self._first_content())
        else:
            self.bg.hide()
            self.scrim.hide()

    def _first_content(self):
        for c in self.children():
            if isinstance(c, QWidget) and c not in (self.bg, self.scrim):
                return c
        return self.scrim


# Now-playing video surface. Uses a stacked layout so a small round button
# reliably draws ON TOP of the video (a plain child of QVideoWidget gets hidden
# by the video surface). The whole panel goes fullscreen, so the button rides
# along and toggles back (same ⛶ symbol both ways). Esc also exits.
class VideoPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self._home = None   # (layout, index) to return to after fullscreen

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        lay.addWidget(self.video)

        # The button is its own always-on-top tool window, so it floats above
        # the native video surface no matter what. A timer keeps it pinned to
        # the video's bottom-right corner (and the screen corner in fullscreen).
        self.fs_btn = QPushButton("⛶", self)   # ⛶
        self.fs_btn.setObjectName("fsBtn")
        self.fs_btn.setFixedSize(34, 34)
        self.fs_btn.setCursor(Qt.PointingHandCursor)
        self.fs_btn.setToolTip("Fullscreen (Esc to exit)")
        self.fs_btn.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.fs_btn.setAttribute(Qt.WA_TranslucentBackground, True)
        self.fs_btn.clicked.connect(self.toggle_fullscreen)
        self.fs_btn.hide()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._reposition)
        self._filtered_window = None

    def set_home(self, layout, index):
        self._home = (layout, index)

    def show_button(self):
        # follow the top-level window's move/resize events for lag-free tracking
        win = self.window()
        if win is not self._filtered_window:
            if self._filtered_window is not None:
                self._filtered_window.removeEventFilter(self)
            if win is not None:
                win.installEventFilter(self)
            self._filtered_window = win
        if not self._timer.isActive():
            self._timer.start(200)   # slow fallback; moves are event-driven
        self._reposition()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Move, QEvent.Resize, QEvent.WindowStateChange):
            self._reposition()
        return False

    def hide_button(self):
        self._timer.stop()
        self.fs_btn.hide()

    def _reposition(self):
        # Only float the button while Beatpull is the active app, so it doesn't
        # hover over other windows when you tab away.
        app_active = QApplication.applicationState() == Qt.ApplicationActive
        if self.isFullScreen():
            visible = app_active
            if visible:
                geo = self.screen().geometry()
                x = geo.x() + geo.width() - self.fs_btn.width() - 24
                y = geo.y() + geo.height() - self.fs_btn.height() - 24
        else:
            win = self.window()
            visible = (app_active and self.isVisible() and self.video.isVisible()
                       and not (win is not None and win.isMinimized()))
            if visible:
                br = self.video.mapToGlobal(
                    QPoint(self.video.width(), self.video.height()))
                x = br.x() - self.fs_btn.width() - 10
                y = br.y() - self.fs_btn.height() - 10
        if not visible:
            if self.fs_btn.isVisible():
                self.fs_btn.hide()
            return
        if not self.fs_btn.isVisible():
            self.fs_btn.show()
            self.fs_btn.raise_()
        self.fs_btn.move(x, y)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            if self._home:
                layout, index = self._home
                layout.insertWidget(index, self)
            self.setFixedSize(250, 250)
            self.show()
        else:
            self.setParent(None)   # detach from the sidebar layout
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.showFullScreen()
            self.setFocus()
        self.show_button()

    def showEvent(self, event):
        super().showEvent(event)
        self.show_button()

    def hideEvent(self, event):
        self.hide_button()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(event)


# Library tab: now-playing sidebar + track list
class LibraryTab(QWidget):
    def __init__(self):
        super().__init__()
        self.store = load_store()
        self._prune_missing()            
        self._migrate()                 
        self.current_filter = None       
        self.search_text = ""
        self.sort_mode = SETTINGS.get("sort_mode", "added")
        self.now_file = None            
        self.rows = []                  
        self.play_order = []             
        self.play_index = -1
        self.shuffle = False
        self.repeat = 0
        self._accent = QColor("#453aa8")
        self._analyze_workers = []       # keep refs so threads aren't GC'd
        self._analyze_queue = []         # entries waiting for BPM/key analysis
        self._analyzing = False
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(SETTINGS.get("volume", 80) / 100)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self._seeking = False
        self._build()
        self._refresh_filters()
        self._refresh_list()
        self._refresh_categories_page()
        self._resume_last()
        # analyze any tracks missing BPM/key (one at a time, in the background)
        if ANALYSIS_AVAILABLE:
            for t in self.store["tracks"]:
                if not t.get("bpm"):
                    self._enqueue_analysis(t)

    def _resume_last(self):
        last = SETTINGS.get("last_file")
        if not last or not os.path.exists(last):
            return
        entry = next((t for t in self.store["tracks"] if t.get("file") == last), None)
        if not entry:
            return
        self.player.setSource(QUrl.fromLocalFile(last))
        self.now_title.setText(entry["title"])
        self.now_artist.setText(entry["artist"])
        self._set_art(entry.get("thumb"))
        self._apply_theme(self._dominant_color(entry.get("thumb")), entry.get("thumb"))
        self._show_media(entry)
        self.now_file = last
        self.play_order = self._visible_tracks()
        self.play_index = next(
            (i for i, t in enumerate(self.play_order) if t.get("file") == last), -1)
        self._highlight_now()

    def add_local_files(self, paths):
        exts = (".mp3", ".mp4", ".m4a", ".wav", ".flac", ".webm", ".mkv",
                ".ogg", ".aac")
        added = 0
        for p in paths:
            if os.path.splitext(p)[1].lower() not in exts:
                continue
            entry = {
                "id": None, "url": None,
                "title": os.path.splitext(os.path.basename(p))[0],
                "artist": "Local file", "file": p, "thumb": "",
                "format": os.path.splitext(p)[1].lstrip(".").lower(),
                "duration": 0, "categories": [], "favorite": False,
            }
            before = len(self.store["tracks"])
            self.add_track(entry)
            if len(self.store["tracks"]) != before:
                added += 1
        return added

    def _migrate(self):
        changed = False
        for t in self.store["tracks"]:
            if "categories" not in t:
                c = t.get("category")
                t["categories"] = [c] if c else []
                changed = True
            if "favorite" not in t:
                t["favorite"] = False
                changed = True
        if changed:
            save_store(self.store)

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- left: now playing ----
        side = Sidebar()
        self.side = side
        side.setFixedWidth(320)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(28, 28, 28, 28)
        sl.setSpacing(14)

        self.art = QLabel()
        self.art.setObjectName("art")
        self.art.setFixedSize(250, 250)
        self.art.setAlignment(Qt.AlignCenter)
        self._set_art(None)
        sl.addWidget(self.art)

        # video surface (shown instead of the art when playing a video file);
        # the fullscreen button overlaps its bottom-right corner.
        self.video = VideoPanel()
        self.video.setFixedSize(250, 250)
        self.video.hide()
        self.player.setVideoOutput(self.video.video)
        sl.addWidget(self.video)
        self.video.set_home(sl, sl.indexOf(self.video))

        self.now_title = QLabel("No song loaded")
        self.now_title.setObjectName("nowTitle")
        self.now_title.setWordWrap(True)
        self.now_artist = QLabel("Pick a track from your library")
        self.now_artist.setObjectName("nowArtist")
        sl.addWidget(self.now_title)
        sl.addWidget(self.now_artist)

        # seek bar + times
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek.sliderReleased.connect(self._seek_release)
        sl.addWidget(self.seek)
        trow = QHBoxLayout()
        self.t_cur = QLabel("0:00")
        self.t_cur.setObjectName("timeLbl")
        self.t_end = QLabel("0:00")
        self.t_end.setObjectName("timeLbl")
        trow.addWidget(self.t_cur)
        trow.addStretch()
        trow.addWidget(self.t_end)
        sl.addLayout(trow)

        # controls: prev / play / next
        ctl = QHBoxLayout()
        ctl.setSpacing(8)
        self.prev_btn = QPushButton("<")
        self.prev_btn.setObjectName("ghost")
        self.prev_btn.setFixedWidth(48)
        self.prev_btn.clicked.connect(self._play_prev)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("primary")
        self.play_btn.clicked.connect(self._toggle)
        self.next_btn = QPushButton(">")
        self.next_btn.setObjectName("ghost")
        self.next_btn.setFixedWidth(48)
        self.next_btn.clicked.connect(self._play_next)
        ctl.addWidget(self.prev_btn)
        ctl.addWidget(self.play_btn, 1)
        ctl.addWidget(self.next_btn)
        sl.addLayout(ctl)

        # shuffle + repeat toggles
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.shuffle_btn = QPushButton("Shuffle")
        self.shuffle_btn.setObjectName("chip")
        self.shuffle_btn.setCheckable(True)
        self.shuffle_btn.toggled.connect(self._set_shuffle)
        self.repeat_btn = QPushButton("Repeat: Off")
        self.repeat_btn.setObjectName("chip")
        self.repeat_btn.setCheckable(True)
        self.repeat_btn.clicked.connect(self._cycle_repeat)
        mode_row.addWidget(self.shuffle_btn)
        mode_row.addWidget(self.repeat_btn)
        mode_row.addStretch()
        sl.addLayout(mode_row)

        # volume
        vol_row = QHBoxLayout()
        vlbl = QLabel("Vol")
        vlbl.setObjectName("timeLbl")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(SETTINGS.get("volume", 80))
        self.vol.valueChanged.connect(self._on_volume)
        vol_row.addWidget(vlbl)
        vol_row.addWidget(self.vol)
        sl.addLayout(vol_row)

        sl.addStretch()
        root.addWidget(side)

        # ---- right: sub-tabs (Library / Categories) over a stacked view ----
        right = QVBoxLayout()
        right.setContentsMargins(28, 24, 28, 24)
        right.setSpacing(12)

        tabrow = QHBoxLayout()
        tabrow.setSpacing(6)
        self.tab_lib = QPushButton("Your Library")
        self.tab_lib.setObjectName("subtab")
        self.tab_lib.setCheckable(True)
        self.tab_lib.setChecked(True)
        self.tab_lib.clicked.connect(lambda: self._show_page(0))
        self.tab_cat = QPushButton("Categories")
        self.tab_cat.setObjectName("subtab")
        self.tab_cat.setCheckable(True)
        self.tab_cat.clicked.connect(lambda: self._show_page(1))
        tabrow.addWidget(self.tab_lib)
        tabrow.addWidget(self.tab_cat)
        tabrow.addStretch()
        right.addLayout(tabrow)

        self.stack = QStackedWidget()

        # --- page 0: library ---
        libpage = QWidget()
        lp = QVBoxLayout(libpage)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your library…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        top_row.addWidget(self.search, 1)
        self.sort_select = QComboBox()
        self.sort_select.addItems(
            ["Recently added", "Title A–Z", "Artist A–Z", "Duration"])
        self.sort_select.setCurrentIndex(
            {"added": 0, "title": 1, "artist": 2, "duration": 3}.get(self.sort_mode, 0))
        self.sort_select.currentIndexChanged.connect(self._on_sort)
        top_row.addWidget(self.sort_select, 0)
        lp.addLayout(top_row)

        self.filter_row = QHBoxLayout()
        self.filter_row.setSpacing(6)
        filt_holder = QWidget()
        filt_holder.setLayout(self.filter_row)
        lp.addWidget(filt_holder)

        self.empty = QLabel("Nothing here yet. Download something and it'll show up.")
        self.empty.setObjectName("subtitle")
        lp.addWidget(self.empty)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("scroll")
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self.list_layout = QVBoxLayout(holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        scroll.setWidget(holder)
        lp.addWidget(scroll)
        self.stack.addWidget(libpage)

        # --- page 1 ---
        catpage = QWidget()
        cp = QVBoxLayout(catpage)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(12)

        addrow = QHBoxLayout()
        addrow.setSpacing(10)
        self.cat_input = QLineEdit()
        self.cat_input.setPlaceholderText("New category name")
        self.cat_input.returnPressed.connect(self._add_from_input)
        addbtn = QPushButton("Add category")
        addbtn.setObjectName("ghost")
        addbtn.clicked.connect(self._add_from_input)
        addrow.addWidget(self.cat_input, 1)
        addrow.addWidget(addbtn, 0)
        cp.addLayout(addrow)

        cat_scroll = QScrollArea()
        cat_scroll.setWidgetResizable(True)
        cat_scroll.setObjectName("scroll")
        cat_scroll.setFrameShape(QFrame.NoFrame)
        cat_holder = QWidget()
        self.cat_list_layout = QVBoxLayout(cat_holder)
        self.cat_list_layout.setContentsMargins(0, 0, 0, 0)
        self.cat_list_layout.setSpacing(8)
        self.cat_list_layout.addStretch()
        cat_scroll.setWidget(cat_holder)
        cp.addWidget(cat_scroll)
        self.stack.addWidget(catpage)

        right.addWidget(self.stack, 1)

        rw = QWidget()
        rw.setLayout(right)
        root.addWidget(rw, 1)

    # -- sub-tab switching --
    def _show_page(self, i):
        self.tab_lib.setChecked(i == 0)
        self.tab_cat.setChecked(i == 1)
        self.stack.setCurrentIndex(i)
        if i == 0:
            self._prune_missing()        
            self._refresh_filters()
            self._refresh_list()
        else:
            self._prune_missing()
            self._refresh_categories_page()

    # -- category filter chips --
    def _refresh_filters(self):
        while self.filter_row.count():
            item = self.filter_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        def add_chip(label, value):
            b = QPushButton(label)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setChecked(self.current_filter == value)
            b.clicked.connect(lambda checked=False, v=value: self._set_filter(v))
            self.filter_row.addWidget(b)

        add_chip("All", None)
        add_chip("★ Favorites", "__fav__")
        for c in self.store["categories"]:
            add_chip(c, c)
        self.filter_row.addStretch()

    def _set_filter(self, value):
        self.current_filter = value
        self._refresh_filters()
        self._refresh_list()

    def _on_sort(self, idx):
        self.sort_mode = ["added", "title", "artist", "duration"][idx]
        SETTINGS["sort_mode"] = self.sort_mode
        save_settings(SETTINGS)
        self._refresh_list()

    # -- track list --
    def _visible_tracks(self):
        q = self.search_text.lower().strip()
        out = []
        for t in self.store["tracks"]:
            if self.current_filter == "__fav__":
                if not t.get("favorite"):
                    continue
            elif self.current_filter is not None:
                if self.current_filter not in (t.get("categories") or []):
                    continue
            if q and q not in t.get("title", "").lower() and q not in t.get("artist", "").lower():
                continue
            out.append(t)
        if self.sort_mode == "title":
            out.sort(key=lambda t: t.get("title", "").lower())
        elif self.sort_mode == "artist":
            out.sort(key=lambda t: t.get("artist", "").lower())
        elif self.sort_mode == "duration":
            out.sort(key=lambda t: t.get("duration", 0) or 0)
        else:  # "added" -> newest first
            out = list(reversed(out))
        return out

    def _on_search(self, text):
        self.search_text = text
        self._refresh_list()

    def _refresh_list(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.rows = []
        tracks = self._visible_tracks()
        self.empty.setVisible(len(tracks) == 0)
        for entry in tracks:
            row = TrackRow(entry, self)
            row.set_playing(entry.get("file") == self.now_file)
            self.rows.append(row)
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)

    def _highlight_now(self):
        for row in self.rows:
            row.set_playing(row.entry.get("file") == self.now_file)

    # -- categories management page --
    def _refresh_categories_page(self):
        while self.cat_list_layout.count() > 1:
            item = self.cat_list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self.store["categories"]:
            lbl = QLabel("No categories yet. Add one above.")
            lbl.setObjectName("subtitle")
            self.cat_list_layout.insertWidget(0, lbl)
            return
        for c in self.store["categories"]:
            count = sum(1 for t in self.store["tracks"]
                        if c in (t.get("categories") or []))
            rowf = QFrame()
            rowf.setObjectName("catRow")
            rl = QHBoxLayout(rowf)
            rl.setContentsMargins(14, 10, 14, 10)
            name = QLabel(c)
            name.setObjectName("rowTitle")
            cnt = QLabel(f"{count} song{'s' if count != 1 else ''}")
            cnt.setObjectName("rowArtist")
            rl.addWidget(name)
            rl.addStretch()
            rl.addWidget(cnt)
            dbtn = QPushButton()
            dbtn.setObjectName("ghost")
            dbtn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            dbtn.setToolTip("Delete category")
            dbtn.setFixedWidth(40)
            dbtn.clicked.connect(lambda checked=False, c=c: self.delete_category(c))
            rl.addWidget(dbtn)
            self.cat_list_layout.insertWidget(self.cat_list_layout.count() - 1, rowf)

    def _add_from_input(self):
        name = self.cat_input.text().strip()
        if name:
            self.add_category(name)
            self.cat_input.clear()

    # -- data operations --
    @staticmethod
    def _same_track(a, b):
        """Match by video id when available, else by file path."""
        if a.get("id") and b.get("id"):
            return a["id"] == b["id"]
        return a.get("file") == b.get("file")

    def _prune_missing(self):
        """Remove library entries whose file no longer exists on disk."""
        keep = [t for t in self.store["tracks"]
                if t.get("file") and os.path.exists(t["file"])]
        if len(keep) != len(self.store["tracks"]):
            self.store["tracks"] = keep
            save_store(self.store)

    def add_track(self, entry):
        entry.setdefault("categories", [])
        entry.setdefault("favorite", False)
        # Playlist downloads are auto-filed into a category named after the
        # playlist (created if it doesn't exist yet).
        pl = entry.pop("playlist", None)
        new_cat = False
        if pl and pl not in self.store["categories"]:
            self.store["categories"].append(pl)
            new_cat = True

        existing = next(
            (t for t in self.store["tracks"] if self._same_track(t, entry)), None)
        if existing:
            # already have it - just make sure it's tagged with the playlist
            if pl and pl not in (existing.get("categories") or []):
                existing.setdefault("categories", []).append(pl)
                save_store(self.store)
            if new_cat:
                self._refresh_filters()
            self._refresh_list()
            self._refresh_categories_page()
            return

        if pl and pl not in entry["categories"]:
            entry["categories"].append(pl)
        self.store["tracks"].append(entry)
        save_store(self.store)
        if new_cat:
            self._refresh_filters()
        self._refresh_list()
        self._refresh_categories_page()
        self._enqueue_analysis(entry)

    # -- BPM / key analysis (one file at a time, in the background) --
    def _enqueue_analysis(self, entry):
        if not ANALYSIS_AVAILABLE:
            print("[analyze] skipped: librosa is not installed", file=sys.stderr)
            return
        if not entry.get("file") or not os.path.exists(entry["file"]):
            print(f"[analyze] skipped: file missing: {entry.get('file')}",
                  file=sys.stderr)
            return
        if entry["file"] in [e.get("file") for e in self._analyze_queue]:
            return
        print(f"[analyze] queued: {entry['file']}", file=sys.stderr)
        self._analyze_queue.append(entry)
        self._process_analysis()

    def _process_analysis(self):
        if self._analyzing or not self._analyze_queue:
            return
        entry = self._analyze_queue.pop(0)
        self._analyzing = True
        worker = AnalyzeWorker(entry["file"])
        self._analyze_workers.append(worker)
        worker.done.connect(self._on_analyzed)
        worker.finished.connect(lambda w=worker: self._cleanup_analyzer(w))
        worker.start()

    def _cleanup_analyzer(self, worker):
        if worker in self._analyze_workers:
            self._analyze_workers.remove(worker)
        worker.deleteLater()

    def _on_analyzed(self, path, bpm, key):
        if bpm:
            hits = 0
            for t in self.store["tracks"]:
                if t.get("file") == path:
                    t["bpm"] = bpm
                    t["key"] = key
                    hits += 1
            save_store(self.store)
            row_hits = 0
            for row in self.rows:
                if row.entry.get("file") == path:
                    row.entry["bpm"] = bpm
                    row.entry["key"] = key
                    row.update_meta()
                    row_hits += 1
            print(f"[analyze] saved: store matches={hits}, "
                  f"visible rows updated={row_hits}", file=sys.stderr)
        self._analyzing = False
        self._process_analysis()

    def analyze_entry(self, entry):
        if not ANALYSIS_AVAILABLE:
            QMessageBox.information(
                self, "Analysis unavailable",
                "Install the 'librosa' package to detect BPM and key.")
            return
        self._enqueue_analysis(entry)

    def edit_bpm_key(self, entry):
        """Manually set BPM and key for a track (overrides detection)."""
        bpm, ok = QInputDialog.getInt(
            self, "Edit BPM", "BPM:", int(entry.get("bpm") or 120), 20, 300)
        if not ok:
            return
        keys = ["(none)"] + [
            f"{n} {m}" for m in ("major", "minor")
            for n in ["C", "C#", "D", "D#", "E", "F",
                      "F#", "G", "G#", "A", "A#", "B"]]
        current = entry.get("key") or "(none)"
        idx = keys.index(current) if current in keys else 0
        key, ok2 = QInputDialog.getItem(
            self, "Edit key", "Key:", keys, idx, False)
        if not ok2:
            return
        key = "" if key == "(none)" else key
        for t in self.store["tracks"]:
            if self._same_track(t, entry):
                t["bpm"] = float(bpm)
                t["key"] = key
                break
        entry["bpm"] = float(bpm)
        entry["key"] = key
        save_store(self.store)
        for row in self.rows:
            if row.entry.get("file") == entry.get("file"):
                row.entry["bpm"] = float(bpm)
                row.entry["key"] = key
                row.update_meta()

    def add_category(self, name):
        name = name.strip()
        if name and name not in self.store["categories"]:
            self.store["categories"].append(name)
            save_store(self.store)
            self._refresh_filters()
            self._refresh_categories_page()

    def delete_category(self, name):
        self.store["categories"] = [
            c for c in self.store["categories"] if c != name]
        for t in self.store["tracks"]:
            if name in (t.get("categories") or []):
                t["categories"] = [c for c in t["categories"] if c != name]
        if self.current_filter == name:
            self.current_filter = None
        save_store(self.store)
        self._refresh_filters()
        self._refresh_list()
        self._refresh_categories_page()

    def toggle_category(self, entry, category):
        """Add the track to a category if it's not in it, else remove it."""
        for t in self.store["tracks"]:
            if self._same_track(t, entry):
                cats = t.setdefault("categories", [])
                if category in cats:
                    cats.remove(category)
                else:
                    cats.append(category)
                entry["categories"] = list(cats)
                break
        save_store(self.store)
        self._refresh_list()
        self._refresh_categories_page()

    def new_category_and_add(self, entry):
        name, ok = QInputDialog.getText(self, "New category", "Category name:")
        if ok and name.strip():
            self.add_category(name.strip())
            self.toggle_category(entry, name.strip())

    def toggle_favorite(self, entry):
        new_val = not entry.get("favorite")
        for t in self.store["tracks"]:
            if self._same_track(t, entry):
                t["favorite"] = new_val
                entry["favorite"] = new_val
                break
        save_store(self.store)
        self._refresh_list()

    def rename_track(self, entry):
        title, ok = QInputDialog.getText(
            self, "Rename", "Title:", text=entry.get("title", ""))
        if not ok:
            return
        artist, ok2 = QInputDialog.getText(
            self, "Rename", "Artist:", text=entry.get("artist", ""))
        if not ok2:
            return
        for t in self.store["tracks"]:
            if self._same_track(t, entry):
                t["title"] = title.strip() or t.get("title")
                t["artist"] = artist.strip() or t.get("artist")
                entry["title"] = t["title"]
                entry["artist"] = t["artist"]
                break
        save_store(self.store)
        self._refresh_list()
        # update now-playing labels if this is the current track
        if entry.get("file") == self.now_file:
            self.now_title.setText(entry["title"])
            self.now_artist.setText(entry["artist"])

    def delete_track(self, entry):
        box = QMessageBox(self)
        box.setWindowTitle("Remove track")
        box.setText(f"Remove “{entry.get('title', 'this track')}”?")
        box.setInformativeText(
            "This deletes the file from your folder too. This can't be undone.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return
        # stop playback if this track is playing
        if self.player.source().toLocalFile() == entry.get("file"):
            self.player.stop()
        # delete the media file and its stored thumbnail
        for path in (entry.get("file"), entry.get("thumb")):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        self.store["tracks"] = [
            t for t in self.store["tracks"] if not self._same_track(t, entry)]
        save_store(self.store)
        self._refresh_list()
        self._refresh_categories_page()

    # -- artwork --
    def _set_art(self, path):
        if path and os.path.exists(path):
            pix = QPixmap(path).scaled(
                250, 250, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self.art.setPixmap(pix)
        else:
            self.art.setPixmap(QPixmap())
            self.art.setText("♪")

    # -- adaptive theme (tint from album art) --
    def _dominant_color(self, path):
        if not path or not os.path.exists(path):
            return None
        img = QImage(path)
        if img.isNull():
            return None
        small = img.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return QColor(small.pixel(0, 0))

    def _paint_accent(self, color):
        dark = color.darker(220)
        self.play_btn.setStyleSheet(
            "#primary { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {dark.name()}, stop:1 {color.name()}); "
            "color: #ffffff; border: none; border-radius: 22px; "
            "padding: 13px; font-size: 15px; font-weight: 700; }")
        self.seek.setStyleSheet(
            "QSlider::sub-page:horizontal { background: qlineargradient("
            f"x1:0, y1:0, x2:1, y2:0, stop:0 {dark.name()}, stop:1 {color.name()}); "
            "border-radius: 3px; }")

    def _apply_theme(self, color, thumb):
        # blurred album art behind the sidebar
        if thumb and os.path.exists(thumb):
            self.side.set_art(QPixmap(thumb))
        else:
            self.side.set_art(None)
        # smoothly fade the accent from the current color to the new one
        target = color if color is not None else QColor("#453aa8")
        start = getattr(self, "_accent", QColor("#453aa8"))
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.valueChanged.connect(self._paint_accent)
        self._anim.start()
        self._accent = target

    def _on_volume(self, v):
        self.audio.setVolume(v / 100)
        SETTINGS["volume"] = v
        save_settings(SETTINGS)

    # -- playback --
    def _play_entry(self, entry):
        self.play_order = self._visible_tracks()
        self.play_index = next(
            (i for i, t in enumerate(self.play_order)
             if t.get("file") == entry.get("file")), -1)
        if self.play_index == -1:
            self.play_order = [entry]
            self.play_index = 0
        self._load_and_play(entry)

    def _is_video(self, path):
        return os.path.splitext(path or "")[1].lower() in (
            ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")

    def _show_media(self, entry):
        """Show the video surface for video files, the album art otherwise."""
        if self._is_video(entry.get("file")):
            self.art.hide()
            self.video.show()
        else:
            self.video.hide()
            self.art.show()

    def _load_and_play(self, entry):
        if not os.path.exists(entry["file"]):
            self.now_title.setText("File missing")
            self.now_artist.setText(entry["file"])
            return
        self.player.setSource(QUrl.fromLocalFile(entry["file"]))
        self.now_title.setText(entry["title"])
        self.now_artist.setText(entry["artist"])
        self._set_art(entry.get("thumb"))
        self._apply_theme(self._dominant_color(entry.get("thumb")), entry.get("thumb"))
        self._show_media(entry)
        self.now_file = entry.get("file")
        self._highlight_now()
        SETTINGS["last_file"] = self.now_file
        save_settings(SETTINGS)
        self.player.play()

    def toggle_play(self):
        """Public play/pause used by keyboard shortcut + media button."""
        self._toggle()

    def seek_by(self, ms):
        pos = max(0, self.player.position() + ms)
        self.player.setPosition(pos)

    def _play_next(self):
        if not self.play_order:
            return
        if self.repeat == 2: 
            self._load_and_play(self.play_order[self.play_index])
            return
        if self.shuffle and len(self.play_order) > 1:
            nxt = self.play_index
            while nxt == self.play_index:
                nxt = random.randrange(len(self.play_order))
            self.play_index = nxt
        else:
            self.play_index += 1
            if self.play_index >= len(self.play_order):
                if self.repeat == 1:      
                    self.play_index = 0
                else:                    
                    self.play_index = len(self.play_order) - 1
                    return
        self._load_and_play(self.play_order[self.play_index])

    def _play_prev(self):
        if not self.play_order:
            return
        if self.player.position() > 3000:
            self.player.setPosition(0)
            return
        self.play_index -= 1
        if self.play_index < 0:
            self.play_index = len(self.play_order) - 1 if self.repeat == 1 else 0
        self._load_and_play(self.play_order[self.play_index])

    def _set_shuffle(self, on):
        self.shuffle = on

    def _cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self.repeat_btn.setText(["Repeat: Off", "Repeat: All", "Repeat: One"][self.repeat])
        self.repeat_btn.setChecked(self.repeat != 0)

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._play_next()

    def _toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        elif self.player.source().isValid():
            self.player.play()

    def _on_state(self, state):
        if state == QMediaPlayer.PlayingState:
            self.play_btn.setText("‖  Pause")
        else:
            self.play_btn.setText("▶  Play")

    def _on_pos(self, pos):
        if not self._seeking:
            self.seek.setValue(pos)
        self.t_cur.setText(fmt_time(pos))

    def _on_dur(self, dur):
        self.seek.setRange(0, dur)
        self.t_end.setText(fmt_time(dur))

    def _seek_release(self):
        self.player.setPosition(self.seek.value())
        self._seeking = False


# Animated Milky Way background
class Starfield(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._t = 0.0
        self.stars = []
        self.reduced = SETTINGS.get("reduce_motion", False)
        self._init_stars(int(SETTINGS.get("star_density", 230)))
        self.shooting = []   # active shooting stars
        # Soft galaxy-haze blobs (position, radius-factor, color)
        self.haze = [
            (0.20, 0.15, QColor(70, 55, 140)),
            (0.42, 0.38, QColor(55, 60, 150)),
            (0.60, 0.58, QColor(80, 55, 135)),
            (0.78, 0.80, QColor(50, 55, 130)),
        ]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        if not self.reduced:
            self.timer.start(33)  # ~30 fps

    def set_reduced(self, reduced):
        self.reduced = reduced
        if reduced:
            self.timer.stop()
            self.shooting = []
        elif not self.timer.isActive():
            self.timer.start(33)
        self.update()

    def set_density(self, n):
        self.stars = []
        self._init_stars(int(n))
        self.update()

    def _init_stars(self, n):
        tints = [
            QColor(255, 255, 255),   # white
            QColor(210, 220, 255),   # pale blue
            QColor(225, 210, 255),   # pale violet
            QColor(200, 215, 255),   # cool blue
        ]
        for _ in range(n):
            self.stars.append({
                "x": random.random(),
                "y": random.random(),
                "r": random.uniform(0.5, 1.7),
                "base": random.uniform(0.15, 0.6),      # dim - not too bright
                "tw": random.uniform(0.4, 1.8),         # twinkle speed
                "ph": random.uniform(0, math.tau),      # twinkle phase
                "drift": random.uniform(0.004, 0.016),  # slow horizontal drift
                "color": random.choice(tints),
            })

    def _spawn_shooting(self):
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        side = random.randint(0, 3)          
        m = 40
        if side == 0:      # top
            sx, sy = random.uniform(0, w), -m
        elif side == 1:    # right
            sx, sy = w + m, random.uniform(0, h)
        elif side == 2:    # bottom
            sx, sy = random.uniform(0, w), h + m
        else:              # left
            sx, sy = -m, random.uniform(0, h)
        # Aim toward a random point inside so it crosses the screen
        tx = random.uniform(0.2 * w, 0.8 * w)
        ty = random.uniform(0.2 * h, 0.8 * h)
        ang = math.atan2(ty - sy, tx - sx)
        speed = random.uniform(11, 18)
        self.shooting.append({
            "x": sx, "y": sy,
            "vx": math.cos(ang) * speed,
            "vy": math.sin(ang) * speed,
            "len": random.uniform(90, 190),   # trail length
            "life": 0.0,
            "max": random.uniform(60, 110),   # frames before it fades out
        })

    def _tick(self):
        self._t += 0.033
        for s in self.stars:
            s["x"] -= s["drift"] * 0.004
            if s["x"] < -0.02:
                s["x"] += 1.04
                s["y"] = random.random()

        # Occasionally launch a shooting star (rarely, and never too many)
        if len(self.shooting) < 2 and random.random() < 0.012:
            self._spawn_shooting()
        w, h = self.width(), self.height()
        for sh in self.shooting:
            sh["x"] += sh["vx"]
            sh["y"] += sh["vy"]
            sh["life"] += 1
        self.shooting = [
            sh for sh in self.shooting
            if sh["life"] < sh["max"]
            and -60 < sh["x"] < w + 60 and -60 < sh["y"] < h + 60
        ]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Deep night sky - very dark, faint purple-blue tint
        g = QLinearGradient(0, 0, w * 0.55, h)
        g.setColorAt(0.0, QColor(12, 9, 26))
        g.setColorAt(0.5, QColor(6, 6, 17))
        g.setColorAt(1.0, QColor(2, 2, 7))
        p.fillRect(self.rect(), g)

        # Milky Way haze - faint diagonal glow made of soft blobs
        p.setPen(Qt.NoPen)
        diag = math.hypot(w, h)
        for fx, fy, color in self.haze:
            cx, cy = fx * w, fy * h
            rad = diag * 0.30
            rg = QRadialGradient(cx, cy, rad)
            c = QColor(color)
            c.setAlpha(18)
            rg.setColorAt(0.0, c)
            edge = QColor(color)
            edge.setAlpha(0)
            rg.setColorAt(1.0, edge)
            p.setBrush(rg)
            p.drawEllipse(int(cx - rad), int(cy - rad), int(rad * 2), int(rad * 2))

        # Stars
        for s in self.stars:
            tw = 0.5 + 0.5 * math.sin(self._t * s["tw"] + s["ph"])
            alpha = max(0.0, min(0.75, s["base"] * (0.45 + 0.55 * tw)))
            c = QColor(s["color"])
            c.setAlphaF(alpha)
            x, y, r = s["x"] * w, s["y"] * h, s["r"]
            # subtle glow for the brighter stars
            if s["r"] > 1.15:
                glow = QRadialGradient(x, y, r * 4)
                gc = QColor(s["color"])
                gc.setAlphaF(alpha * 0.35)
                glow.setColorAt(0.0, gc)
                gc2 = QColor(s["color"])
                gc2.setAlpha(0)
                glow.setColorAt(1.0, gc2)
                p.setBrush(glow)
                p.setPen(Qt.NoPen)
                p.drawEllipse(int(x - r * 4), int(y - r * 4), int(r * 8), int(r * 8))
            p.setBrush(c)
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(x - r), int(y - r), int(r * 2), int(r * 2))

        # Shooting stars - a bright head with a fading trail behind it
        for sh in self.shooting:
            # fade in at the start, fade out near the end
            frac = sh["life"] / sh["max"]
            fade = min(1.0, sh["life"] / 8.0) * (1.0 - max(0.0, (frac - 0.7) / 0.3))
            fade = max(0.0, min(1.0, fade))
            if fade <= 0:
                continue
            hx, hy = sh["x"], sh["y"]
            speed = math.hypot(sh["vx"], sh["vy"]) or 1.0
            ux, uy = sh["vx"] / speed, sh["vy"] / speed
            tx, ty = hx - ux * sh["len"], hy - uy * sh["len"]

            grad = QLinearGradient(tx, ty, hx, hy)
            tail = QColor(200, 210, 255)
            tail.setAlpha(0)
            grad.setColorAt(0.0, tail)
            mid = QColor(210, 220, 255)
            mid.setAlpha(int(35 * fade))
            grad.setColorAt(0.7, mid)
            headc = QColor(255, 255, 255)
            headc.setAlpha(int(120 * fade))
            grad.setColorAt(1.0, headc)

            pen = QPen(QBrush(grad), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(int(tx), int(ty), int(hx), int(hy))

            # bright head + soft glow
            glow = QRadialGradient(hx, hy, 6)
            g0 = QColor(255, 255, 255)
            g0.setAlphaF(0.45 * fade)
            glow.setColorAt(0.0, g0)
            g1 = QColor(255, 255, 255)
            g1.setAlpha(0)
            glow.setColorAt(1.0, g1)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(int(hx - 6), int(hy - 6), 12, 12)

        p.end()


# Settings tab
class SettingsTab(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self._build()

    def _label(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("field")
        return lbl

    def _build(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(40, 34, 40, 34)
        page.setSpacing(0)
        center = QHBoxLayout()
        center.addStretch(1)
        panel = QWidget()
        panel.setMinimumWidth(420)
        panel.setMaximumWidth(660)
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("title")
        col.addWidget(title)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(24, 24, 24, 24)
        cl.setSpacing(16)

        cl.addWidget(self._label("Default download folder"))
        frow = QHBoxLayout()
        self.folder = QLineEdit(SETTINGS.get("out_dir", ""))
        self.folder.setReadOnly(True)
        frow.addWidget(self.folder, 1)
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._choose_folder)
        frow.addWidget(browse, 0)
        cl.addLayout(frow)

        cl.addWidget(self._label("Default format"))
        self.fmt = QComboBox()
        self.fmt.addItems(["MP3 (audio)", "MP4 (video)", "WAV (audio)"])
        self.fmt.setCurrentIndex(
            {"mp3": 0, "mp4": 1, "wav": 2}.get(SETTINGS.get("format", "mp3"), 0))
        self.fmt.currentIndexChanged.connect(self._save_format)
        cl.addWidget(self.fmt)

        self.reduce = QCheckBox("Reduce motion (pause the animated starfield)")
        self.reduce.setObjectName("chk")
        self.reduce.setChecked(SETTINGS.get("reduce_motion", False))
        self.reduce.toggled.connect(self._toggle_reduce)
        cl.addWidget(self.reduce)

        cl.addWidget(self._label("Star density"))
        self.density = QSlider(Qt.Horizontal)
        self.density.setRange(40, 400)
        self.density.setValue(int(SETTINGS.get("star_density", 230)))
        self.density.valueChanged.connect(self._set_density)
        cl.addWidget(self.density)

        col.addWidget(card)
        note = QLabel("Folder and format defaults apply to new downloads.")
        note.setObjectName("subtitle")
        col.addWidget(note)
        col.addStretch()

        center.addWidget(panel, 0)
        center.addStretch(1)
        page.addLayout(center)
        page.addStretch(1)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Choose folder", SETTINGS.get("out_dir", ""))
        if path:
            SETTINGS["out_dir"] = path
            save_settings(SETTINGS)
            self.folder.setText(path)
            self.app.download_tab.out_dir = path
            self.app.download_tab.folder_label.setText(path)

    def _save_format(self, idx):
        SETTINGS["format"] = {0: "mp3", 1: "mp4", 2: "wav"}.get(idx, "mp3")
        save_settings(SETTINGS)

    def _toggle_reduce(self, on):
        SETTINGS["reduce_motion"] = on
        save_settings(SETTINGS)
        self.app.set_reduced(on)

    def _set_density(self, n):
        SETTINGS["star_density"] = n
        save_settings(SETTINGS)
        self.app.set_density(n)


class Beatpull(Starfield):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BeatPull")
        self.setMinimumSize(760, 560)
        self.resize(int(SETTINGS.get("win_w", 980)), int(SETTINGS.get("win_h", 700)))
        self.setAcceptDrops(True)

        self.tabs = QTabWidget(self)
        self.download_tab = DownloadTab()
        self.library_tab = LibraryTab()
        self.settings_tab = SettingsTab(self)
        self.download_tab.track_added.connect(self.library_tab.add_track)
        self.tabs.addTab(self.download_tab, "Download")
        self.tabs.addTab(self.library_tab, "Library")
        self.tabs.addTab(self.settings_tab, "Settings")

        # version label in the top-right of the tab bar (auto-reflects updates)
        self.ver_label = QLabel(f"v{app_version()}")
        self.ver_label.setObjectName("verLabel")
        self.ver_label.setContentsMargins(0, 0, 16, 0)
        self.tabs.setCornerWidget(self.ver_label, Qt.TopRightCorner)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        self.setStyleSheet(STYLE)

        self._sc_find = QShortcut(QKeySequence("Ctrl+F"), self)
        self._sc_find.activated.connect(self._focus_search)

        # App-wide media hotkeys: Space = play/pause, Left/Right = skip 5s.
        # An application event filter catches keys no matter which widget has
        # focus (buttons, the track list, etc.) - typing fields are excluded.
        QApplication.instance().installEventFilter(self)

    def _focus_search(self):
        self.tabs.setCurrentWidget(self.library_tab)
        self.library_tab._show_page(0)
        self.library_tab.search.setFocus()

    #neegy123
    def eventFilter(self, obj, e):
        if e.type() == QEvent.KeyPress:
            fw = QApplication.focusWidget()
            # don't steal keys while typing or navigating menus/dropdowns
            if isinstance(fw, (QLineEdit, QTextEdit, QComboBox, QMenu)):
                return False
            k = e.key()
            if k == Qt.Key_Space:
                self.library_tab.toggle_play()
                return True
            if k == Qt.Key_Right:
                self.library_tab.seek_by(5000)
                return True
            if k == Qt.Key_Left:
                self.library_tab.seek_by(-5000)
                return True
        return False

    def closeEvent(self, e):
        SETTINGS["win_w"] = self.width()
        SETTINGS["win_h"] = self.height()
        save_settings(SETTINGS)
        super().closeEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        weblinks, localfiles = [], []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    localfiles.append(u.toLocalFile())
                else:
                    weblinks.append(u.toString())
        elif md.hasText():
            weblinks.append(md.text().strip())
        if weblinks:
            self.download_tab.url_input.setText(" ".join(weblinks))
            self.tabs.setCurrentWidget(self.download_tab)
        if localfiles:
            n = self.library_tab.add_local_files(localfiles)
            if n:
                self.tabs.setCurrentWidget(self.library_tab)
        e.acceptProposedAction()


# Look & feel
STYLE = """
QWidget {
    background: transparent;
    color: #e6eeff;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
}
QTabWidget::pane { border: none; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent;
    color: #8296bf;
    padding: 12px 26px;
    font-size: 14px;
    font-weight: 600;
    border: none;
}
QTabBar::tab:selected {
    color: #ffffff;
    border-bottom: 3px solid #6f7ce8;
}
#verLabel { color: #8296bf; font-size: 12px; font-weight: 600; }
#title { font-size: 28px; font-weight: 800; color: #ffffff; }
#subtitle { font-size: 13px; color: #8296bf; }
#card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(120,110,220,0.18);
    border-radius: 18px;
}
#previewBox {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(120,110,220,0.2);
    border-radius: 12px;
}
#previewThumb {
    background: rgba(0,0,0,0.35);
    border-radius: 8px; color: #4a5a86; font-size: 26px;
}
#field {
    font-size: 11px; font-weight: 600; color: #9fb2d8;
    text-transform: uppercase; letter-spacing: 1px;
}
QLineEdit, QComboBox {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(120,110,220,0.25);
    border-radius: 10px; padding: 10px 12px; font-size: 14px; color: #e6eeff;
    min-height: 20px;
}
QComboBox { padding-right: 28px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #6f7ce8; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    border: none; width: 26px;
}
QComboBox QAbstractItemView {
    background: #0a0f22; selection-background-color: #4a3fb0;
    color: #e6eeff; border-radius: 8px;
}
#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #362a86, stop:1 #453aa8);
    color: #ffffff; border: none; border-radius: 22px;
    padding: 13px; font-size: 15px; font-weight: 700;
}
#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #443aa6, stop:1 #5a4fce);
}
#primary:disabled { background: #2a3350; color: #7186ad; }
#ghost {
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(120,110,220,0.3);
    border-radius: 10px; padding: 8px 16px; color: #e6eeff; font-weight: 600;
}
#ghost:hover { background: rgba(120,110,220,0.2); }
QProgressBar {
    background: rgba(255,255,255,0.07); border: none;
    border-radius: 6px; height: 10px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4a3fb0, stop:1 #6f7ce8);
    border-radius: 6px;
}
#log {
    background: rgba(0,0,0,0.3);
    border: 1px solid rgba(120,110,220,0.15);
    border-radius: 10px; font-size: 12px; color: #b8c6e6;
}
#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(14,11,34,0.28), stop:1 rgba(3,3,10,0.22));
    border-right: 1px solid rgba(120,110,220,0.15);
}
#sideScrim {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(4,4,12,0.12), stop:1 rgba(2,2,8,0.30));
    border: none;
}
#art {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(120,110,220,0.22);
    border-radius: 16px; font-size: 60px; color: #4a5a86;
}
#nowTitle { font-size: 19px; font-weight: 800; color: #ffffff; }
#nowArtist { font-size: 13px; color: #8296bf; }
#timeLbl { font-size: 11px; color: #7186ad; }
#scroll { background: transparent; border: none; }
#subtab {
    background: transparent; border: none; color: #8296bf;
    font-size: 15px; font-weight: 700; padding: 4px 4px 6px 4px;
    margin-right: 14px;
}
#subtab:checked { color: #ffffff; border-bottom: 3px solid #6f7ce8; }
#chip {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(120,110,220,0.2);
    border-radius: 13px; padding: 5px 14px; font-size: 12px;
    font-weight: 600; color: #b8c0e0;
}
#chip:checked {
    background: rgba(120,110,220,0.28);
    border: 1px solid rgba(120,110,220,0.5); color: #ffffff;
}
/* Track rows: blocky (square corners) as requested */
#trackRow {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(120,110,220,0.12);
    border-radius: 0px;
}
#trackRow:hover { background: rgba(120,110,220,0.12); }
#trackRow[playing="true"] {
    background: rgba(120,110,220,0.22);
    border: 1px solid rgba(150,140,240,0.55);
    border-left: 3px solid #8f86ff;
}
#starBtn {
    background: transparent; border: none; color: #c9b45a;
    font-size: 18px; padding: 2px;
}
#starBtn:hover { color: #ffe17a; }
#fsBtn {
    background: rgba(0,0,0,0.62); color: #ffffff; border: none;
    border-radius: 17px; font-size: 16px;
}
#fsBtn:hover { background: rgba(0,0,0,0.85); }
#chk { color: #b8c0e0; font-size: 13px; spacing: 8px; }
#chk::indicator {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid rgba(120,110,220,0.4); background: rgba(255,255,255,0.05);
}
#chk::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #453aa8, stop:1 #6f7ce8);
    border: 1px solid #6f7ce8;
}
#rowThumb {
    background: rgba(255,255,255,0.05);
    border-radius: 0px; font-size: 22px; color: #4a5a86;
}
#rowTitle { font-size: 14px; font-weight: 700; color: #e6eeff; }
#rowArtist { font-size: 12px; color: #8296bf; }
/* Category management rows keep the rounded style */
#catRow {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(120,110,220,0.12);
    border-radius: 12px;
}
#catRow:hover { background: rgba(120,110,220,0.10); }
QMenu {
    background: #0b0f26;
    border: 1px solid rgba(120,110,220,0.3);
    border-radius: 10px; padding: 6px; color: #e6eeff;
}
QMenu::item { padding: 7px 22px; border-radius: 6px; }
QMenu::item:selected { background: rgba(120,110,220,0.35); }
QMenu::separator { height: 1px; background: rgba(120,110,220,0.2); margin: 5px 8px; }
QSlider::groove:horizontal {
    height: 5px; background: rgba(255,255,255,0.12); border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4a3fb0, stop:1 #6f7ce8);
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #ffffff; width: 14px; height: 14px;
    margin: -5px 0; border-radius: 7px;
}
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical {
    background: rgba(120,110,220,0.3); border-radius: 4px;
}
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


def _load_app_icon():
    """Use a custom icon if the user drops one next to main.py.
    Looked-for names (first match wins): icon.ico, icon.png, icon.jpg.
    Loads via QPixmap so a failed decode is detected (returns an empty icon)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("icon.ico", "icon.png", "icon.jpg", "icon.jpeg"):
        path = os.path.join(here, name)
        if os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                return QIcon(pix)
    return QIcon()


def main():
    print(f"[beatpull] v{APP_VERSION} | analysis available: {ANALYSIS_AVAILABLE} "
          f"| ffmpeg found: {shutil.which('ffmpeg') is not None}",
          file=sys.stderr)
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))

    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "beatpull.app")
        except Exception:
            pass

    icon = _load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    win = Beatpull()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()