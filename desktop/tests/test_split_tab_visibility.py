"""Regression coverage for native split geometry across top-level web tabs."""

import time

import pytest

app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp
Gtk = app_gtk.Gtk
Gdk = app_gtk.Gdk
Vte = app_gtk.Vte
WebKit2 = app_gtk.WebKit2


class _Harness:
    _on_overlay_position = ChatsApp._on_overlay_position
    _set_rect = ChatsApp._set_rect
    _redraw_terminal_underlay = ChatsApp._redraw_terminal_underlay
    _set_terminal_view_visible = ChatsApp._set_terminal_view_visible
    _queue_vte_focus = ChatsApp._queue_vte_focus
    _apply_split_ratio = ChatsApp._apply_split_ratio
    _on_paned_allocate = ChatsApp._on_paned_allocate
    _wire_paned_persist = ChatsApp._wire_paned_persist

    def __init__(self):
        self._vtes = {}
        self._runtime_focus_sid = "left"
        self.js_calls = []

    def _kick_split_winch(self):
        pass

    def _eval_js(self, script):
        self.js_calls.append(script)


def _flush_gtk(cycles=30):
    for _ in range(cycles):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.002)


def _root_pixel(window, x, y):
    root = Gdk.get_default_root_window()
    if root is None or window.get_window() is None:
        return None
    win_x, win_y = window.get_position()
    pixbuf = Gdk.pixbuf_get_from_window(root, win_x + x, win_y + y, 1, 1)
    if pixbuf is None:
        return None
    pixels = bytes(pixbuf.get_pixels())
    return tuple(pixels[:3]) if len(pixels) >= 3 else None


def _wait_for_pixel(window, x, y, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        _flush_gtk(2)
        Gdk.flush()
        last = _root_pixel(window, x, y)
        if last is not None and predicate(last):
            return last
        time.sleep(0.01)
    return last


class _DamageProbe(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.damage = []

    def queue_draw_area(self, x, y, width, height):
        self.damage.append((x, y, width, height))
        return super().queue_draw_area(x, y, width, height)


def test_split_ratio_survives_hidden_chat_tab():
    ok, _argv = Gtk.init_check([])
    if not ok:
        pytest.skip("GTK display unavailable")

    harness = _Harness()
    harness._split_active = True
    harness._stack_hidden = True
    harness._terminal_view_visible = True
    harness._split_ratio = 0.5
    harness._split_pos_settling = False
    harness._split_sids = ("left", "right")
    harness._vte_rect = (548, 135, 1190, 700)
    harness.overlay = Gtk.Overlay()
    harness._stack = Gtk.Stack()
    harness._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
    harness._paned.set_wide_handle(True)
    harness.web = _DamageProbe()
    harness.overlay.add(harness.web)
    harness.overlay.add_overlay(harness._stack)
    harness.overlay.add_overlay(harness._paned)
    harness.overlay.connect("get-child-position", harness._on_overlay_position)
    harness._paned.connect("size-allocate", harness._on_paned_allocate)
    left, right = Vte.Terminal(), Vte.Terminal()
    left.set_size(100, 40)
    right.set_size(100, 40)
    harness._vtes = {"left": left, "right": right}
    harness._paned.pack1(left, True, True)
    harness._paned.pack2(right, True, True)

    window = Gtk.OffscreenWindow()
    window.set_default_size(1919, 900)
    window.add(harness.overlay)
    window.show_all()
    _flush_gtk()
    harness._split_pos_settling = True
    harness._apply_split_ratio()
    _flush_gtk()
    harness._wire_paned_persist()

    assert left.get_allocated_width() > 400
    assert right.get_allocated_width() > 400
    original_rect = harness._vte_rect
    original_width = harness._paned.get_allocated_width()
    original_position = harness._paned.get_position()
    original_child_widths = (
        left.get_allocated_width(),
        right.get_allocated_width(),
    )
    original_columns = (left.get_column_count(), right.get_column_count())
    original_rows = (left.get_row_count(), right.get_row_count())

    # Hidden DOM geometry is ignored. Top-tab visibility hides the native
    # overlay nodes, damages the WebKit underlay, and retains exact PTY geometry.
    assert harness._set_rect({"x": 0, "y": 0, "w": 0, "h": 0}) is False
    assert harness._vte_rect == original_rect
    for _ in range(10):
        harness._set_terminal_view_visible(False)
        _flush_gtk()
        assert harness._paned.get_visible() is False
        assert harness._paned.get_child_visible() is True
        assert harness._paned.get_mapped() is False
        assert harness._paned.get_parent() is harness.overlay
        assert harness._stack.get_parent() is harness.overlay
        assert harness._paned.get_opacity() == pytest.approx(1.0)
        assert harness._paned.get_sensitive() is False
        assert harness.overlay.get_overlay_pass_through(harness._paned) is True
        assert left.get_mapped() is False
        assert right.get_mapped() is False
        assert harness._paned.get_allocated_width() == original_width
        assert harness._paned.get_position() == original_position
        assert (left.get_allocated_width(), right.get_allocated_width()) == original_child_widths
        assert (left.get_column_count(), right.get_column_count()) == original_columns
        assert (left.get_row_count(), right.get_row_count()) == original_rows
        assert harness._split_ratio == pytest.approx(0.5)
        parked = app_gtk.Gdk.Rectangle()
        assert harness._on_overlay_position(harness.overlay, harness._paned, parked)
        assert parked.x <= -(original_width + 32)
        assert parked.width == original_width

        harness._set_terminal_view_visible(
            True, {"x": 548, "y": 135, "w": 1190, "h": 700}
        )
        _flush_gtk()

        assert harness._paned.get_visible() is True
        assert harness._paned.get_child_visible() is True
        assert harness._paned.get_mapped() is True
        assert harness._paned.get_parent() is harness.overlay
        assert harness._stack.get_parent() is harness.overlay
        restored = app_gtk.Gdk.Rectangle()
        assert harness._on_overlay_position(harness.overlay, harness._paned, restored)
        assert (restored.x, restored.y, restored.width, restored.height) == original_rect
        assert harness._paned.get_opacity() == pytest.approx(1.0)
        assert harness._paned.get_sensitive() is True
        assert harness.overlay.get_overlay_pass_through(harness._paned) is False
        assert left.get_mapped() is True
        assert right.get_mapped() is True
        assert harness._paned.get_allocated_width() == original_width
        assert harness._paned.get_position() == original_position
        assert (left.get_allocated_width(), right.get_allocated_width()) == original_child_widths
        assert (left.get_column_count(), right.get_column_count()) == original_columns
        assert (left.get_row_count(), right.get_row_count()) == original_rows
        assert harness._split_ratio == pytest.approx(0.5)
    assert not any("onGtkPanedPos" in script for script in harness.js_calls)
    assert harness.web.damage.count(original_rect) >= 20
    window.destroy()


def test_single_terminal_is_unmapped_without_losing_geometry():
    ok, _argv = Gtk.init_check([])
    if not ok:
        pytest.skip("GTK display unavailable")

    harness = _Harness()
    harness._split_active = False
    harness._stack_hidden = False
    harness._terminal_view_visible = True
    harness._split_ratio = 0.5
    harness._split_pos_settling = False
    harness._split_sids = (None, None)
    harness._vte_rect = (548, 135, 1190, 700)
    harness.overlay = Gtk.Overlay()
    harness._stack = Gtk.Stack()
    harness._stack.set_transition_type(Gtk.StackTransitionType.NONE)
    harness._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
    harness.web = _DamageProbe()
    harness.overlay.add(harness.web)
    harness.overlay.add_overlay(harness._stack)
    harness.overlay.add_overlay(harness._paned)
    harness.overlay.connect("get-child-position", harness._on_overlay_position)
    terminal = Vte.Terminal()
    terminal.set_size(100, 40)
    harness._vtes = {"solo": terminal}
    harness._runtime_focus_sid = None
    harness._stack.add_named(terminal, "solo")
    harness._stack.set_visible_child_name("solo")

    window = Gtk.OffscreenWindow()
    window.set_default_size(1919, 900)
    window.add(harness.overlay)
    window.show_all()
    _flush_gtk()

    original_allocation = (
        harness._stack.get_allocated_width(),
        terminal.get_allocated_width(),
        terminal.get_column_count(),
        terminal.get_row_count(),
    )
    assert original_allocation[0] == 1190
    assert original_allocation[1] == 1190

    for _ in range(10):
        harness._set_terminal_view_visible(False)
        _flush_gtk()
        assert harness._stack.get_visible() is False
        assert harness._stack.get_child_visible() is True
        assert harness._stack.get_mapped() is False
        assert harness._stack.get_parent() is harness.overlay
        assert harness._paned.get_parent() is harness.overlay
        assert terminal.get_mapped() is False
        assert harness._stack.get_allocated_width() == original_allocation[0]
        assert (
            terminal.get_allocated_width(),
            terminal.get_column_count(),
            terminal.get_row_count(),
        ) == original_allocation[1:]

        # A late terminal/split lifecycle call may invoke show(), but the
        # hidden-tab allocation parks it completely outside WebKit's viewport.
        harness._stack.show()
        harness._paned.show()
        _flush_gtk()
        assert harness._stack.get_mapped() is True
        assert terminal.get_mapped() is True
        parked = app_gtk.Gdk.Rectangle()
        assert harness._on_overlay_position(harness.overlay, harness._stack, parked)
        assert parked.x <= -(original_allocation[0] + 32)
        assert parked.width == original_allocation[0]

        harness._set_terminal_view_visible(
            True, {"x": 548, "y": 135, "w": 1190, "h": 700}
        )
        _flush_gtk()
        assert harness._stack.get_child_visible() is True
        assert harness._stack.get_mapped() is True
        assert harness._stack.get_parent() is harness.overlay
        assert harness._paned.get_parent() is harness.overlay
        restored = app_gtk.Gdk.Rectangle()
        assert harness._on_overlay_position(harness.overlay, harness._stack, restored)
        assert (restored.x, restored.y, restored.width, restored.height) == harness._vte_rect
        assert terminal.get_mapped() is True
        assert (
            harness._stack.get_allocated_width(),
            terminal.get_allocated_width(),
            terminal.get_column_count(),
            terminal.get_row_count(),
        ) == original_allocation

    assert harness.web.damage.count(harness._vte_rect) >= 20
    window.destroy()


def test_split_unmap_repaints_real_webkit_pixels():
    """Prove the exposed compositor pixels change from VTE to WebKit.

    Widget visibility alone missed the original regression: GTK reported both
    VTEs unmapped while their last black frame still covered Fleet. This uses a
    real toplevel WebKit/VTE composition and samples the rendered X root.
    """
    ok, _argv = Gtk.init_check([])
    if not ok:
        pytest.skip("GTK display unavailable")

    harness = _Harness()
    harness._split_active = True
    harness._stack_hidden = True
    harness._terminal_view_visible = True
    harness._split_ratio = 0.5
    harness._split_pos_settling = False
    harness._split_sids = ("left", "right")
    harness._vte_rect = (50, 40, 300, 140)
    harness._runtime_focus_sid = None
    harness.overlay = Gtk.Overlay()
    harness._stack = Gtk.Stack()
    harness._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
    harness.web = WebKit2.WebView()
    harness.web.load_html(
        "<style>html,body{margin:0;width:100%;height:100%;"
        "background:rgb(24,170,88)}</style>",
        None,
    )
    harness.overlay.add(harness.web)
    harness.overlay.add_overlay(harness._stack)
    harness.overlay.add_overlay(harness._paned)
    harness.overlay.connect("get-child-position", harness._on_overlay_position)
    left, right = Vte.Terminal(), Vte.Terminal()
    harness._vtes = {"left": left, "right": right}
    harness._paned.pack1(left, True, True)
    harness._paned.pack2(right, True, True)

    window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    window.set_decorated(False)
    window.set_accept_focus(False)
    window.set_keep_above(True)
    window.set_default_size(400, 220)
    monitor = Gdk.Display.get_default().get_primary_monitor()
    geometry = monitor.get_geometry()
    window.move(
        geometry.x + max(0, geometry.width - 420),
        geometry.y + max(0, geometry.height - 240),
    )
    window.add(harness.overlay)

    green = (24, 170, 88)

    def is_green(pixel):
        return all(abs(pixel[i] - green[i]) <= 8 for i in range(3))

    def is_terminal(pixel):
        return max(pixel) < 80

    samples = ((100, 100), (300, 100))

    try:
        window.show_all()
        window.present()
        for sample_x, sample_y in samples:
            terminal_pixel = _wait_for_pixel(
                window, sample_x, sample_y, is_terminal
            )
            if terminal_pixel is None:
                pytest.skip("root-window pixel capture unavailable")
            assert is_terminal(terminal_pixel), terminal_pixel

        harness._set_terminal_view_visible(False)
        for sample_x, sample_y in samples:
            exposed_pixel = _wait_for_pixel(window, sample_x, sample_y, is_green)
            assert is_green(exposed_pixel), exposed_pixel

        # Repeat the exact Chats -> Fleet -> Chats -> Fleet lifecycle. The
        # second exposure catches one-frame backing-surface regressions.
        harness._set_terminal_view_visible(
            True, {"x": 50, "y": 40, "w": 300, "h": 140}
        )
        for sample_x, sample_y in samples:
            assert is_terminal(
                _wait_for_pixel(window, sample_x, sample_y, is_terminal)
            )
        harness._set_terminal_view_visible(False)
        for sample_x, sample_y in samples:
            assert is_green(_wait_for_pixel(window, sample_x, sample_y, is_green))
    finally:
        window.destroy()
        _flush_gtk()
