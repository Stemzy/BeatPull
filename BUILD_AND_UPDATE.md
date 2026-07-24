# Turning Beatpull into a downloadable app (Windows) + auto-updates

This turns your Python app into a single Windows program your friends can run
without installing anything, and lets you push small updates that they get
automatically.

**How it works.** You send friends a `Beatpull` folder (zipped) built with
PyInstaller. Inside the exe are all the heavy libraries (PySide6, yt-dlp,
librosa, ffmpeg) plus a copy of your `main.py`. When it runs, `launcher.py`
checks a tiny `version.json` you host online; if your `main.py` is newer, it
downloads just that one small file and runs it. So day-to-day updates are tiny.

> The one catch: if a future `main.py` needs a **new library** that isn't
> already bundled, a light update can't add it — you rebuild and re-send the
> exe that one time. Normal code changes need no rebuild.

---

## Part A — Build the app (do this on a Windows PC)

You only need to do steps 1–4 once to get a working build.

### 1. Install the tools
Install Python 3.11 or 3.12 from python.org (tick **Add Python to PATH**).
Then, in a terminal in this folder:

```
pip install -r requirements.txt
pip install pyinstaller
```

### 2. Put ffmpeg next to the code
Beatpull needs `ffmpeg.exe` to convert audio/video.
- Download a Windows build from https://www.gyan.dev/ffmpeg/builds/ (the
  "essentials" release).
- Copy `ffmpeg.exe` (and `ffprobe.exe` if present) into **this folder**, next to
  `main.py` and `beatpull.spec`.

The spec bundles it automatically if it's there.

### 3. (Optional) Add your icon
Put `icon.png` in this folder for the in-app icon. For the exe's file icon, also
add `icon.ico` (convert your png to .ico at e.g. https://icoconvert.com).

### 4. Build
```
pyinstaller beatpull.spec
```
When it finishes you'll have `dist/Beatpull/Beatpull.exe`. Double-click it to
test. (First build with librosa can take a few minutes and may print warnings —
that's normal.)

### 5. Send it to friends
Zip the whole `dist/Beatpull` folder and send the zip. They unzip it anywhere
and run `Beatpull.exe`.
- Windows SmartScreen may warn "unknown publisher" (because it's unsigned) →
  they click **More info → Run anyway**. This is normal for indie apps.
- For a nicer one-click installer instead of a zip, use **Inno Setup**
  (https://jrsoftware.org/isinfo.php) — optional.

---

## Part B — Set up auto-updates (do this once)

### 1. Make a free GitHub repo
Create a public repo, e.g. `beatpull`. Upload your `main.py` and `version.json`
to it.

### 2. Point the app at your repo
Edit **two files** and rebuild once so the shipped exe knows where to look:

- In `launcher.py`, set `VERSION_URL` to your raw version.json URL, e.g.
  `https://raw.githubusercontent.com/YOURNAME/beatpull/main/version.json`
- In `version.json`, set `main_url` to your raw main.py URL, e.g.
  `https://raw.githubusercontent.com/YOURNAME/beatpull/main/main.py`

Keep `BUNDLED_VERSION` in `launcher.py` equal to the `version` in
`version.json` for each release.

Then rebuild (Part A step 4) and send this exe to friends. This is the version
that knows how to update itself.

---

## Part C — Publishing an update (the easy, everyday part)

Whenever you change `main.py`:

1. Edit `main.py` (your normal work).
2. Bump the version number in `version.json`, e.g. `1.0.0` → `1.0.1`.
3. Push **both** `main.py` and `version.json` to your GitHub repo (replace the
   files on the `main` branch).

That's it. Next time a friend opens Beatpull, the launcher sees the higher
version, downloads the new `main.py` (a few KB), and runs it. Nobody
re-downloads the big app.

**Version numbers:** use `MAJOR.MINOR.PATCH` (e.g. `1.2.3`) and always go up.
The launcher compares them numerically, so `1.0.10` is newer than `1.0.9`.

---

## When you DO need to rebuild + re-send the exe

Only when an update requires something the bundled exe doesn't already contain:
- you added a **new Python library** (new `import`),
- you upgraded PySide/librosa versions,
- you changed ffmpeg.

In those cases: rebuild (Part A), bump the version, and send the new zip once.
After that, light updates resume.

---

## Troubleshooting

- **App opens then closes instantly:** run `Beatpull.exe` from a terminal to see
  the error, or temporarily set `console=True` in `beatpull.spec` and rebuild.
- **"ffmpeg not found":** make sure `ffmpeg.exe` was in the folder at build time.
- **librosa/numba build errors:** update PyInstaller (`pip install -U
  pyinstaller`); if it's too painful, you can ship without BPM/key by removing
  `librosa` from `requirements.txt` and the `librosa`/`numba`/`llvmlite` lines
  in `beatpull.spec` — the app still runs, just without audio analysis.
- **Antivirus flags the exe:** common for unsigned PyInstaller apps; a code
  signing certificate removes it but costs money. For friends, it's safe to
  allow it.
