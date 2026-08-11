"""
Beatpull launcher / auto-updater.

This is the program your friends actually run. It is what gets bundled into the
.exe by PyInstaller. It carries a copy of your app code (main.py) and all the
heavy libraries (PySide6, yt-dlp, librosa, ffmpeg) *inside the exe*, so those
only ever download once.

On every launch it:
  1. makes sure a working copy of main.py exists in the user's app folder,
  2. asks your server (version.json) whether a newer main.py is available,
  3. if so, downloads just that small file (a "light update"),
  4. runs the app.

Because only main.py is fetched, updates are tiny. The catch: if a future
version of main.py needs a brand-new library that isn't already bundled in the
exe, you must rebuild and re-send the exe that one time.
"""

import json
import os
import shutil
import sys
import traceback
import urllib.request

# Imported here only so PyInstaller bundles *just* the Qt modules the app uses
# (main.py is loaded dynamically, so the bundler can't see its imports). This
# keeps the build from pulling in all of Qt (WebEngine, QML, Charts, etc.).
try:
    from PySide6 import (  # noqa: F401
        QtCore, QtGui, QtWidgets, QtMultimedia, QtMultimediaWidgets,
    )
except Exception:
    pass

# ---------------------------------------------------------------------------
# CONFIG - edit these two lines after you create your GitHub repo
# ---------------------------------------------------------------------------
# Raw URL of a small JSON file you host, e.g. on GitHub:
#   {"version": "1.1.0",
#    "main_url": "https://raw.githubusercontent.com/USER/REPO/main/main.py"}
VERSION_URL = "https://raw.githubusercontent.com/Stemzy/BeatPull/refs/heads/main/version.json"

# The version baked into THIS exe. Bump it every time you build a new exe so
# the app doesn't re-download an older hosted main.py than the one it shipped
# with. Keep it in sync with the version.json you publish.
BUNDLED_VERSION = "1.0.13"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = os.path.join(os.path.expanduser("~"), ".beatpull")
CODE_DIR = os.path.join(APP_DIR, "app")
LOCAL_MAIN = os.path.join(CODE_DIR, "main.py")
LOCAL_VER = os.path.join(CODE_DIR, "version.txt")


def _bundle_dir():
    """Folder where PyInstaller extracted our bundled data files (main.py,
    icon.png). Falls back to this script's folder when run from source."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


BUNDLED_MAIN = os.path.join(_bundle_dir(), "main.py")
BUNDLED_ICON = os.path.join(_bundle_dir(), "icon.png")


def _parse(v):
    return [int(x) for x in str(v).split(".") if x.isdigit()]


def _read_local_version():
    try:
        with open(LOCAL_VER, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def ensure_code():
    """Make sure a runnable copy of main.py (and icon) exists on disk. We seed
    it from the copy bundled inside the exe, and also REFRESH it whenever this
    exe ships a newer BUNDLED_VERSION than what's cached (so a rebuilt exe always
    replaces stale cached code)."""
    os.makedirs(CODE_DIR, exist_ok=True)
    local = _read_local_version()
    need_seed = not os.path.exists(LOCAL_MAIN)
    newer_bundle = local is None or _parse(BUNDLED_VERSION) > _parse(local)
    if os.path.exists(BUNDLED_MAIN) and (need_seed or newer_bundle):
        try:
            shutil.copy(BUNDLED_MAIN, LOCAL_MAIN)
            with open(LOCAL_VER, "w", encoding="utf-8") as f:
                f.write(BUNDLED_VERSION)
        except Exception:
            pass
    # keep an icon next to main.py so the app window/taskbar icon works
    if os.path.exists(BUNDLED_ICON):
        dest = os.path.join(CODE_DIR, "icon.png")
        if not os.path.exists(dest):
            try:
                shutil.copy(BUNDLED_ICON, dest)
            except Exception:
                pass


def check_update():
    """Look for a newer main.py and download it if found. Silent on any failure
    (offline, server down, etc.) so the app still opens with what it has."""
    try:
        req = urllib.request.Request(VERSION_URL, headers={"User-Agent": "Beatpull"})
        with urllib.request.urlopen(req, timeout=8) as r:
            meta = json.loads(r.read().decode("utf-8", "replace"))
        remote = meta.get("version")
        main_url = meta.get("main_url")
        local = _read_local_version() or BUNDLED_VERSION
        if not (remote and main_url):
            return
        if _parse(remote) <= _parse(local):
            return  # already up to date

        req2 = urllib.request.Request(main_url, headers={"User-Agent": "Beatpull"})
        data = urllib.request.urlopen(req2, timeout=30).read()
        # tiny sanity check so a bad/empty response can't brick the app
        if data and b"def main" in data:
            with open(LOCAL_MAIN, "wb") as f:
                f.write(data)
            with open(LOCAL_VER, "w", encoding="utf-8") as f:
                f.write(str(remote))
    except Exception:
        pass


def add_ffmpeg_to_path():
    """Make the bundled ffmpeg.exe findable by yt-dlp and librosa by putting its
    folder on PATH. Checks the PyInstaller temp dir (onefile), the exe's folder
    (onedir), and this script's folder (running from source)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    candidates.append(os.path.dirname(sys.executable))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for d in candidates:
        if os.path.exists(os.path.join(d, "ffmpeg.exe")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return


def run_main():
    path = LOCAL_MAIN if os.path.exists(LOCAL_MAIN) else BUNDLED_MAIN
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    globals_dict = {"__name__": "__main__", "__file__": path}
    exec(compile(code, path, "exec"), globals_dict)


if __name__ == "__main__":
    add_ffmpeg_to_path()
    ensure_code()
    check_update()
    try:
        run_main()
    except Exception:
        traceback.print_exc()
        try:
            input("Beatpull hit an error. Press Enter to close…")
        except Exception:
            pass
