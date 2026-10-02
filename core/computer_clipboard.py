"""Carry clipboard text between his screen and Serena's own desktop.

Her desktop is a separate X server, so it has its own CLIPBOARD selection and
nothing he copies reaches it on its own. His copies always reach her desktop,
so he can paste into the viewer. Her copies reach his clipboard only while the
viewer is his focused window, i.e. he copied inside it himself: the worker pressing
Ctrl+C while he works elsewhere never replaces what he copied. When his
clipboard empties (a password manager's timeout, or the owning app quitting),
hers is emptied too, so a pasted password does not linger there.

Text only, up to MAX_BYTES; larger selections use INCR transfers and are left
alone. Each display has its own connection and thread, because an Xlib
connection must not be shared across threads.
"""

from __future__ import annotations

import contextlib
import queue
import select
import threading

from core.computer_platform import ComputerError

MAX_BYTES = 200_000  # one ChangeProperty request without INCR or BIG-REQUESTS
TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "STRING", "TEXT", "text/plain")


class _Side:
    """One display's CLIPBOARD: notices new owners, fetches text, serves offers."""

    def __init__(self, name, on_copy, on_clear):
        from Xlib import X, display
        from Xlib.ext import xfixes

        self.name = name
        self.on_copy = on_copy
        self.on_clear = on_clear
        self.d = display.Display(name)
        if not self.d.has_extension("XFIXES"):
            self.d.close()
            raise ComputerError(f"{name} has no XFIXES; clipboard cannot be bridged")
        self.d.set_error_handler(lambda *_args: None)  # a requestor may vanish mid-answer
        self.d.xfixes_query_version()
        # python-xlib can load the extension module twice, so its event classes
        # fail isinstance checks; match on the event code instead.
        self.xfixes_event = self.d.query_extension("XFIXES").first_event
        self.atom = self.d.intern_atom
        self.clipboard = self.atom("CLIPBOARD")
        self.targets = self.atom("TARGETS")
        self.incr = self.atom("INCR")
        self.utf8 = self.atom("UTF8_STRING")
        self.string = self.atom("STRING")
        self.text_atoms = [self.atom(name) for name in TEXT_TARGETS]
        self.property = self.atom("SERENA_CLIPBOARD")
        screen = self.d.screen()
        self.window = screen.root.create_window(-10, -10, 1, 1, 0, screen.root_depth)
        self.d.xfixes_select_selection_input(
            self.window,
            self.clipboard,
            xfixes.XFixesSetSelectionOwnerNotifyMask
            | xfixes.XFixesSelectionWindowDestroyNotifyMask
            | xfixes.XFixesSelectionClientCloseNotifyMask,
        )
        self.d.flush()
        self.X = X
        self.data = None
        self.asked = None
        self.commands = queue.SimpleQueue()

    # Called from other threads: queued, then applied on this side's thread.
    def offer(self, data):
        self.commands.put(("offer", data))

    def clear(self):
        self.commands.put(("clear", None))

    def pull(self):
        self.commands.put(("pull", None))

    def run(self, stop):
        try:
            while not stop.is_set():
                self._apply_commands()
                if not self.d.pending_events():
                    select.select([self.d.fileno()], [], [], 0.05)
                while self.d.pending_events():
                    event = self.d.next_event()
                    # One odd event or vanished requestor must not end the bridge.
                    with contextlib.suppress(Exception):
                        self._handle(event)
                # Replies such as ConvertSelection sit in the output buffer otherwise.
                self.d.flush()
        finally:
            with contextlib.suppress(Exception):
                self.d.close()

    def _owner(self):
        owner = self.d.get_selection_owner(self.clipboard)
        return getattr(owner, "id", owner) or 0

    def _apply_commands(self):
        from Xlib.protocol import request

        while True:
            try:
                kind, data = self.commands.get_nowait()
            except queue.Empty:
                return
            if kind == "offer":
                self.data = data
                self.window.set_selection_owner(self.clipboard, self.X.CurrentTime)
            elif kind == "clear" and self._owner() == self.window.id:
                self.data = None
                request.SetSelectionOwner(
                    display=self.d.display,
                    window=self.X.NONE,
                    selection=self.clipboard,
                    time=self.X.CurrentTime,
                )
            elif kind == "pull" and self._owner() not in (0, self.window.id):
                self._ask(self.utf8)
            self.d.flush()

    def _ask(self, target, timestamp=None):
        self.asked = target
        self.window.convert_selection(
            self.clipboard, target, self.property, timestamp or self.X.CurrentTime
        )

    def _handle(self, event):
        from Xlib.ext import xfixes

        X = self.X
        if event.type == self.xfixes_event:
            if event.selection != self.clipboard:
                return
            owner = getattr(event.owner, "id", event.owner) or 0
            if owner == self.window.id:
                return  # our own offer
            if event.sub_code == xfixes.XFixesSetSelectionOwnerNotify and owner:
                self._ask(self.utf8, event.timestamp)
            else:
                self.on_clear()
        elif (
            event.type == X.SelectionNotify
            and getattr(event.requestor, "id", event.requestor) == self.window.id
        ):
            self._receive(event)
        elif event.type == X.SelectionRequest:
            self._answer(event)
        elif event.type == X.SelectionClear and event.atom == self.clipboard:
            self.data = None

    def _receive(self, event):
        X = self.X
        if event.property == X.NONE:
            if self.asked == self.utf8:
                self._ask(self.string)  # an older owner that only speaks STRING
            return
        prop = self.window.get_full_property(self.property, X.AnyPropertyType)
        self.window.delete_property(self.property)
        if prop is None or prop.property_type == self.incr or prop.format != 8:
            return
        data = prop.value if isinstance(prop.value, bytes) else bytes(prop.value)
        if self.asked == self.string:
            data = data.decode("latin-1").encode("utf-8")
        if 0 < len(data) <= MAX_BYTES:
            self.on_copy(data)

    def _answer(self, event):
        from Xlib import Xatom
        from Xlib.protocol import event as xevent

        X = self.X
        prop = event.property or event.target  # obsolete clients pass None
        requestor = event.requestor
        if self.data is None or event.selection != self.clipboard:
            prop = X.NONE
        elif event.target == self.targets:
            requestor.change_property(prop, Xatom.ATOM, 32, [self.targets, *self.text_atoms])
        elif event.target == self.string:
            text = self.data.decode("utf-8", "replace").encode("latin-1", "replace")
            requestor.change_property(prop, self.string, 8, text)
        elif event.target in self.text_atoms:
            requestor.change_property(prop, self.utf8, 8, self.data)
        else:
            prop = X.NONE
        requestor.send_event(
            xevent.SelectionNotify(
                time=event.time,
                requestor=requestor,
                selection=event.selection,
                target=event.target,
                property=prop,
            )
        )
        self.d.flush()


class ClipboardBridge:
    def __init__(self, host, nested, *, viewer=None):
        self.host_name = host
        self.nested_name = nested
        # Returns his viewer window id (host display) or None.
        self.viewer = viewer
        self.viewer_id = None
        self.stop_event = threading.Event()
        self.threads = []
        self.host = self.nested = self.focus = None

    def start(self):
        from Xlib import display

        self.host = _Side(self.host_name, self._host_copied, self._host_cleared)
        self.nested = _Side(self.nested_name, self._nested_copied, lambda: None)
        # Only the nested side's thread checks his focus, on its own connection.
        self.focus = display.Display(self.host_name)
        for side in (self.host, self.nested):
            thread = threading.Thread(
                target=side.run,
                args=(self.stop_event,),
                name=f"computer-clipboard-{side.name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)
        # What he copied before her desktop opened pastes there too.
        self.host.pull()
        return self

    def _host_copied(self, data):
        self.nested.offer(data)

    def _host_cleared(self):
        self.nested.clear()

    def _nested_copied(self, data):
        if self._viewer_focused():
            self.host.offer(data)

    def _viewer_focused(self):
        try:
            root = self.focus.screen().root
            prop = root.get_full_property(self.focus.intern_atom("_NET_ACTIVE_WINDOW"), 0)
            active = int(prop.value[0]) if prop is not None and len(prop.value) else 0
        except Exception:
            return False
        if not active:
            return False
        if self.viewer and active != self.viewer_id:
            # The viewer's id changes when her desktop restarts; look it up again.
            with contextlib.suppress(Exception):
                found = self.viewer()
                self.viewer_id = int(found) if found else None
        return active == self.viewer_id

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1)
        if self.focus is not None:
            with contextlib.suppress(Exception):
                self.focus.close()
