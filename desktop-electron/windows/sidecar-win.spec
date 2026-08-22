"""PyInstaller onedir build for Serena's Windows Flask sidecar."""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


WINDOWS_DIR = Path(SPECPATH).resolve()
DESKTOP_DIR = WINDOWS_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent
ENTRYPOINT = WINDOWS_DIR / "sidecar-win.py"

# collect_submodules runs while evaluating the spec, before Analysis applies
# pathex, so expose the repository packages to the hook helper explicitly.
sys.path.insert(0, str(REPO_ROOT))

datas = []
for source, destination in (
    (REPO_ROOT / "ui" / "static", "ui/static"),
    (REPO_ROOT / "static", "static"),
    (REPO_ROOT / "voice" / "call" / "vocabulary.txt", "voice/call"),
):
    if source.exists():
        datas.append((str(source), destination))

hiddenimports = sorted(
    set(
        collect_submodules(
            "core",
            filter=lambda name: ".tests" not in name,
        )
        + [
            "flask_sock",
            "simple_websocket",
            "ui.web",
            "voice.call.browser_auth",
            "voice.call.orchestrator",
            "voice.call.tts",
        ]
    )
)

# These modules are either GTK/VTE-specific or own Linux desktop audio and
# process integrations. Browser call transport remains bundled through
# voice.call; the Linux desk and overlay runtimes do not belong in Windows.
excludes = [
    "cairo",
    "desktop.app_gtk",
    "gi",
    "gi.repository.Gdk",
    "gi.repository.Gtk",
    "gi.repository.Vte",
    "gi.repository.WebKit2",
    "gtk",
    "pygtk",
    "pulsectl",
    "sounddevice",
    "voice.desktop",
    "voice.desk.client",
    "voice.desk.duplex",
    "voice.desk.fallback",
    "voice.desk.io",
    "voice.desk.say",
    "voice.desk.transport",
    "voice.desk.wake_acceptance",
    "voice.desk.wake_listener",
    "vte",
]

# numpy ships compiled extensions and its own metadata; listing the package as
# a hidden import alone bundles a hollow copy that fails at runtime with a
# missing numpy._core. collect_all takes the binaries and data with it.
# collect_all returns (datas, binaries, hiddenimports), in that order.
numpy_datas, numpy_binaries, numpy_modules = collect_all("numpy")
hiddenimports = sorted(set(hiddenimports) | set(numpy_modules))
datas += numpy_datas

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(REPO_ROOT)],
    binaries=numpy_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="serena-web-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="serena-web-sidecar",
)
