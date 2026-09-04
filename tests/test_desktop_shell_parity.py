from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = (ROOT / "ui" / "web.py").read_text(encoding="utf-8")


def test_folder_picker_supports_electron_and_gtk_shells():
    assert "window.serenaDesktop.pickFolder({ title, startDir })" in WEB_SOURCE
    assert "|| window.gtkSend" in WEB_SOURCE
    assert "Folder picker requires the GTK desktop shell" not in WEB_SOURCE


def test_desktop_features_use_the_shared_shell_capability():
    assert "function _hasDesktopShell()" in WEB_SOURCE
    assert "if (_hasDesktopShell()) {" in WEB_SOURCE
    assert "if (!_hasDesktopShell()) return;" in WEB_SOURCE
    assert "if (!window.__gtkBridge) return;" not in WEB_SOURCE
