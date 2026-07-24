# PyInstaller spec for Beatpull (Windows).
# Build with:   pyinstaller beatpull.spec
#
# Produces a folder build at dist/Beatpull/ containing Beatpull.exe plus its
# libraries. Zip that folder to send it to friends (see BUILD_AND_UPDATE.md).

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = []
binaries = []
hiddenimports = []

# Pull in everything these packages need (modules + data files).
# NOTE: PySide6 is intentionally NOT collected wholesale here — launcher.py
# imports only QtCore/QtGui/QtWidgets/QtMultimedia, so PyInstaller's built-in
# PySide6 hook bundles just those (much smaller than all of Qt).
# librosa is the heavy one and drags in numpy/scipy/numba/etc.
for pkg in (
    "yt_dlp",
    "librosa",
    "numpy",
    "scipy",
    "numba",
    "llvmlite",
    "soundfile",
    "audioread",
    "soxr",
    "pooch",
    "lazy_loader",
    "joblib",
    "decorator",
    "msgpack",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# librosa / numba / lazy_loader read their own package METADATA at runtime
# (version checks, lazy loading). Without this you get "PackageNotFoundError"
# in the frozen app even though the modules are bundled.
for pkg in (
    "librosa",
    "numba",
    "llvmlite",
    "lazy_loader",
    "pooch",
    "soundfile",
    "soxr",
    "audioread",
    "scikit-learn",
    "decorator",
    "joblib",
    "scipy",
    "numpy",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# Bundle the app code + assets INTO the exe. main.py is what the launcher runs
# (and what light-updates replace). ffmpeg.exe must sit next to this spec.
datas += [("main.py", ".")]
import os
if os.path.exists("icon.png"):
    datas += [("icon.png", ".")]
if os.path.exists("ffmpeg.exe"):
    binaries += [("ffmpeg.exe", ".")]


a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # never used -> keep them out to shrink the build
        "tkinter",
        "sklearn", "matplotlib", "pandas", "IPython", "notebook", "pytest",
        # Qt modules the app doesn't use (big space savers)
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtDesigner", "PySide6.QtWebSockets", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtBluetooth",
        "PySide6.QtSensors", "PySide6.QtPositioning", "PySide6.QtSerialPort",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Beatpull",
    console=True,               # no black console window
    icon="icon.ico" if os.path.exists("icon.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="Beatpull",
)
