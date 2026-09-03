from __future__ import annotations

from ui import web


def test_windows_reserved_device_names_are_recognized_with_extensions():
    for name in ("nul", "NUL.txt", "con ", "COM1.log", "lpt9"):
        assert web._is_windows_reserved_name(name)
    for name in ("null", "com10", "lpt0", "console.txt"):
        assert not web._is_windows_reserved_name(name)


def test_fallback_walk_skips_windows_device_entries(monkeypatch, tmp_path):
    dirs = ["keep", "NUL", ".hidden"]
    files = ["good.txt", "nul", "COM1.log", ".secret"]
    monkeypatch.setattr(web.sys, "platform", "win32")
    monkeypatch.setattr(
        web.os,
        "walk",
        lambda _root: iter([(str(tmp_path), dirs, files)]),
    )

    assert web._fallback_walk(str(tmp_path)) == ["good.txt"]
    assert dirs == ["keep"]


def test_fallback_walk_ignores_one_unrepresentable_path(monkeypatch, tmp_path):
    real_relpath = web.os.path.relpath

    def guarded_relpath(path, root):
        if path.endswith("broken.txt"):
            raise ValueError("device path is on another mount")
        return real_relpath(path, root)

    monkeypatch.setattr(
        web.os,
        "walk",
        lambda _root: iter([(str(tmp_path), [], ["good.txt", "broken.txt"])]),
    )
    monkeypatch.setattr(web.os.path, "relpath", guarded_relpath)

    assert web._fallback_walk(str(tmp_path)) == ["good.txt"]
