"""Visible local ownership indicator and stop button; never takes keyboard focus.

The indicator is deliberately local and boring to the computer-use protocol: it
only renders the state returned by the helper and reports its current rectangle
back for screenshot masking.  The richer card is still lightweight enough to
refresh while the visual worker is thinking.
"""

from __future__ import annotations

import time
import tkinter as tk
from contextlib import suppress
from tkinter import font

from core.computer_client import ComputerClient

PALETTE = {
    "background": "#101522",
    "surface": "#171e2f",
    "surface_alt": "#1d263a",
    "border": "#2b3852",
    "text": "#f4f7ff",
    "muted": "#9ba8c2",
    "subtle": "#6f7d99",
    "accent": "#8ea7ff",
    "success": "#62d6b0",
    "warning": "#ffc56d",
    "danger": "#ff7891",
    "danger_dark": "#6f2c43",
}

STATE_STYLES = {
    "starting": ("STARTING", PALETTE["accent"]),
    "thinking": ("THINKING", PALETTE["warning"]),
    "screen_changed": ("CHANGED", PALETTE["accent"]),
    "sharing": ("SHARING", PALETTE["accent"]),
    "watching": ("WATCHING", PALETTE["success"]),
}


def _trim(value, limit):
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _friendly_app(value):
    parts = []
    seen = set()
    aliases = {
        "microsoft-edge": "Microsoft Edge",
        "microsoft edge": "Microsoft Edge",
        "google-chrome": "Chrome",
        "google chrome": "Chrome",
        "firefox": "Firefox",
        "gnome-terminal": "Terminal",
        "x-terminal-emulator": "Terminal",
    }
    for part in str(value or "").replace("_", "-").split():
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append(aliases.get(key, part))
    if not parts:
        return ""
    return " ".join(parts)


def focus_label(session):
    context = session.get("focused_window") or {}
    title = _trim(context.get("title", ""), 88)
    app = _friendly_app(context.get("app", ""))
    if title or app:
        return " · ".join(_trim(value, 88) for value in (app, title) if value)
    return f"{session.get('target', 'desktop')} · waiting for window details"


class ComputerIndicator:
    def __init__(self, root, client):
        self.root = root
        self.client = client
        self.failures = 0
        self.visible_session = ""
        self.shown = None
        self._session = None
        self.collapsed = False
        self._drag_origin = None
        self._heartbeat_job = None
        self._copy_reset_job = None
        self._last_observation = ""
        self.x = 20
        self.y = 20
        self.screen_width = max(360, root.winfo_screenwidth())
        self.screen_height = max(240, root.winfo_screenheight())
        self.width = min(500, max(360, self.screen_width - 40))
        self.max_height = min(560, max(240, self.screen_height - 40))
        self.height = 118
        self.collapsed_height = 82
        root.withdraw()
        root.title("Serena computer use")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        with suppress(tk.TclError):
            root.attributes("-alpha", 0.98)
        root.configure(bg=PALETTE["background"])
        root.geometry(self._geometry(self.width, self.height, self.x, self.y))

        self.card = tk.Frame(
            root,
            bg=PALETTE["surface"],
            bd=0,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["border"],
            highlightthickness=1,
        )
        self.card.pack(fill="both", expand=True)
        self.accent_strip = tk.Frame(self.card, bg=PALETTE["accent"], width=4)
        self.accent_strip.pack(side="left", fill="y")
        self.content = tk.Frame(self.card, bg=PALETTE["surface"])
        self.content.pack(side="left", fill="both", expand=True)

        self.header_frame = tk.Frame(self.content, bg=PALETTE["surface"])
        self.header_frame.pack(fill="x", padx=14, pady=(12, 5))
        self.collapse_button = self._small_button(
            self.header_frame, "⌃", self.toggle_collapsed, width=2
        )
        self.collapse_button.pack(side="right", padx=(8, 0))
        self.header_hint = tk.Label(
            self.header_frame,
            text="drag to move",
            bg=PALETTE["surface"],
            fg=PALETTE["subtle"],
            font=("sans", 9),
            anchor="e",
        )
        self.header_hint.pack(side="right", padx=(8, 0))
        self.status_dot = tk.Canvas(
            self.header_frame,
            width=12,
            height=12,
            bg=PALETTE["surface"],
            bd=0,
            highlightthickness=0,
        )
        self.status_dot.create_oval(2, 2, 10, 10, fill=PALETTE["accent"], outline="")
        self.status_dot.pack(side="left", padx=(0, 7))
        self.brand = tk.Label(
            self.header_frame,
            text="SERENA · COMPUTER USE",
            bg=PALETTE["surface"],
            fg=PALETTE["text"],
            font=("sans", 10, "bold"),
            anchor="w",
        )
        self.brand.pack(side="left")
        self.state_badge = tk.Label(
            self.header_frame,
            text="STARTING",
            bg=PALETTE["surface_alt"],
            fg=PALETTE["accent"],
            font=("sans", 8, "bold"),
            padx=7,
            pady=3,
        )
        self.state_badge.pack(side="left", padx=(9, 0))

        self.context_label = tk.Label(
            self.content,
            text="",
            bg=PALETTE["surface"],
            fg=PALETTE["muted"],
            font=("sans", 9),
            anchor="w",
            justify="left",
            wraplength=self.width - 44,
        )
        self.context_label.pack(fill="x", padx=14, pady=(0, 8))

        self.detail_frame = tk.Frame(self.content, bg=PALETTE["surface_alt"])
        self.detail_frame.pack(fill="both", expand=True, padx=14, pady=(0, 9))
        body = tk.Frame(self.detail_frame, bg=PALETTE["surface_alt"])
        body.pack(fill="both", expand=True, padx=10, pady=9)
        self.text = tk.Text(
            body,
            height=1,
            width=1,
            wrap="word",
            bg=PALETTE["surface_alt"],
            fg=PALETTE["text"],
            font=("sans", 10),
            borderwidth=0,
            highlightthickness=0,
            padx=1,
            pady=0,
            takefocus=False,
            cursor="arrow",
            state="disabled",
        )
        self.scrollbar = tk.Scrollbar(
            body,
            command=self.text.yview,
            takefocus=False,
            width=8,
            troughcolor=PALETTE["surface_alt"],
            bg=PALETTE["border"],
            activebackground=PALETTE["accent"],
            relief="flat",
            bd=0,
        )
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.footer = tk.Frame(self.content, bg=PALETTE["surface"])
        self.footer.pack(fill="x", padx=14, pady=(0, 12))
        self.meta_label = tk.Label(
            self.footer,
            text="",
            bg=PALETTE["surface"],
            fg=PALETTE["subtle"],
            font=("sans", 8),
            anchor="w",
        )
        self.meta_label.pack(side="left", fill="x", expand=True)
        self.copy_button = self._small_button(self.footer, "copy", self.copy_text, width=5)
        self.copy_button.pack(side="right", padx=(6, 0))
        self.stop_button = tk.Button(
            self.footer,
            text="stop",
            command=self.stop,
            bg=PALETTE["danger_dark"],
            fg=PALETTE["danger"],
            activebackground=PALETTE["danger"],
            activeforeground=PALETTE["background"],
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            font=("sans", 8, "bold"),
            takefocus=False,
        )
        self.stop_button.pack(side="right")

        self._bind_drag(self.header_frame)
        for widget in (self.status_dot, self.brand, self.state_badge, self.header_hint):
            self._bind_drag(widget)

    @staticmethod
    def _geometry(width, height, x, y):
        return f"{int(width)}x{int(height)}+{int(x)}+{int(y)}"

    def _small_button(self, parent, text, command, *, width=None):
        kwargs = {
            "text": text,
            "command": command,
            "bg": PALETTE["surface_alt"],
            "fg": PALETTE["muted"],
            "activebackground": PALETTE["border"],
            "activeforeground": PALETTE["text"],
            "relief": "flat",
            "bd": 0,
            "padx": 5,
            "pady": 2,
            "font": ("sans", 8, "bold"),
            "takefocus": False,
            "cursor": "hand2",
        }
        if width is not None:
            kwargs["width"] = width
        return tk.Button(parent, **kwargs)

    def _bind_drag(self, widget):
        widget.configure(cursor="fleur")
        widget.bind("<ButtonPress-1>", self._begin_drag, add="+")
        widget.bind("<B1-Motion>", self._drag, add="+")
        widget.bind("<ButtonRelease-1>", self._end_drag, add="+")

    def _clamp_position(self, x, y):
        max_x = max(0, self.screen_width - self.width)
        max_y = max(0, self.screen_height - self.height)
        return max(0, min(int(x), max_x)), max(0, min(int(y), max_y))

    def _begin_drag(self, event):
        self._drag_origin = (
            self.root.winfo_pointerx(),
            self.root.winfo_pointery(),
            self.x,
            self.y,
        )
        return "break"

    def _drag(self, event):
        if not self._drag_origin:
            return "break"
        start_x, start_y, origin_x, origin_y = self._drag_origin
        self.x, self.y = self._clamp_position(
            origin_x + self.root.winfo_pointerx() - start_x,
            origin_y + self.root.winfo_pointery() - start_y,
        )
        self.root.geometry(self._geometry(self.width, self.height, self.x, self.y))
        self._queue_heartbeat()
        return "break"

    def _end_drag(self, event):
        self._drag_origin = None
        self._queue_heartbeat()
        return "break"

    def _queue_heartbeat(self):
        if self._heartbeat_job is None:
            self._heartbeat_job = self.root.after(60, self._flush_heartbeat)

    def _flush_heartbeat(self):
        self._heartbeat_job = None
        with suppress(Exception):
            self.heartbeat()

    def stop(self):
        self.stop_button.configure(text="stopping…")
        with suppress(Exception):
            self.client.call("stop", reason="stopped from the desktop indicator")

    def copy_text(self):
        if not self._last_observation:
            return
        with suppress(tk.TclError):
            self.root.clipboard_clear()
            self.root.clipboard_append(self._last_observation)
            self.root.update()
            self.copy_button.configure(text="copied")
            if self._copy_reset_job:
                self.root.after_cancel(self._copy_reset_job)
            self._copy_reset_job = self.root.after(
                1400, lambda: self.copy_button.configure(text="copy")
            )

    def toggle_collapsed(self):
        self.collapsed = not self.collapsed
        self.collapse_button.configure(text="⌄" if self.collapsed else "⌃")
        if self.collapsed:
            self.detail_frame.pack_forget()
        else:
            self.detail_frame.pack(fill="both", expand=True, padx=14, pady=(0, 9))
        self.shown = None
        if self._session:
            self.show(self._session)
        self._queue_heartbeat()

    @staticmethod
    def _line_count(text_widget):
        result = text_widget.count("1.0", "end-1c", "displaylines")
        if isinstance(result, tuple):
            result = result[0] if result else 0
        return max(1, int(result or 0))

    @staticmethod
    def _elapsed(session):
        started = session.get("inspection_started_at")
        return max(0, int(time.time() - started)) if started else 0

    def _state_details(self, session):
        automated = session.get("driver") == "astra"
        mode = (
            ("controlling" if session.get("mode") == "control" else "watching")
            if automated
            else "sharing"
        )
        state = session.get("observation_state") or ("watching" if automated else "sharing")
        if not automated:
            state = "sharing"
        badge, accent = STATE_STYLES.get(state, STATE_STYLES["watching"])
        if automated:
            elapsed = self._elapsed(session)
            waiting = {
                "starting": "warming the visual worker…",
                "thinking": f"astra is reading your screen… {elapsed}s",
                "screen_changed": "screen changed · checking the current view…",
            }.get(state, "watching for the next useful change…")
        else:
            elapsed = 0
            waiting = "screen shared with your chat · automatic coaching is off"
        observation = session.get("observation") or waiting
        if session.get("observation_preview"):
            observation = "draft · " + session["observation_preview"]
        last_ms = session.get("last_model_ms")
        if automated and last_ms:
            timing = f"last check {last_ms / 1000:.1f}s"
        elif automated and state == "thinking":
            timing = f"checking · {elapsed}s"
        else:
            timing = "live"
        return automated, mode, state, badge, accent, observation, timing

    def heartbeat(self, height=None):
        return self.client.call(
            "indicator",
            visible_session=self.visible_session if self.root.winfo_viewable() else "",
            rect={
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": int(height or self.height),
            },
        )

    def show(self, session):
        self._session = session
        automated, mode, state, badge, accent, observation, timing = self._state_details(session)
        focus = focus_label(session)
        target = _trim(session.get("target", "desktop"), 46)
        model = "gpt-6-astra · medium · fast" if automated else "connected chat"
        shown = (
            session["id"],
            mode,
            state,
            focus,
            target,
            observation,
            timing,
            self.collapsed,
        )
        if shown == self.shown:
            return
        if not self.root.winfo_viewable():
            self.root.deiconify()
            self.root.update_idletasks()

        self.accent_strip.configure(bg=accent)
        self.status_dot.itemconfigure(1, fill=accent)
        self.state_badge.configure(text=badge, fg=accent)
        self.context_label.configure(text=f"{focus}  ·  scope {target}")
        self.meta_label.configure(text=f"{model}   ·   {timing}   ·   drag header to move")
        self.stop_button.configure(text="stop")
        self._last_observation = observation
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", observation)
        self.text.configure(state="disabled")

        if self.collapsed:
            self.scrollbar.pack_forget()
            self.detail_frame.pack_forget()
            self.root.update_idletasks()
            height = max(self.collapsed_height, self.card.winfo_reqheight())
        else:
            if not self.detail_frame.winfo_manager():
                self.detail_frame.pack(fill="both", expand=True, padx=14, pady=(0, 9))
            self.scrollbar.pack_forget()
            self.root.update_idletasks()
            line_height = font.Font(font=self.text.cget("font")).metrics("linespace")
            chrome = (
                self.header_frame.winfo_reqheight()
                + self.context_label.winfo_reqheight()
                + self.footer.winfo_reqheight()
                + 42
            )
            max_lines = max(1, (self.max_height - chrome) // max(1, line_height))
            lines = self._line_count(self.text)
            if lines > max_lines:
                self.scrollbar.pack(side="right", fill="y")
                self.root.update_idletasks()
                lines = self._line_count(self.text)
            lines = min(lines, max_lines)
            self.text.configure(height=lines)
            self.root.update_idletasks()
            natural_height = self.card.winfo_reqheight()
            height = min(
                self.max_height,
                max(self.collapsed_height, chrome + lines * line_height, natural_height),
            )

        self.x, self.y = self._clamp_position(self.x, self.y)
        self.height = int(height)
        self.visible_session = session["id"]
        # Mask the larger/new position before the window is resized. This keeps
        # the card's own paint out of the next screenshot during a transition.
        with suppress(Exception):
            self.heartbeat(self.height)
        self.root.geometry(self._geometry(self.width, self.height, self.x, self.y))
        self.root.deiconify()
        self.root.update_idletasks()
        self.text.yview_moveto(0)
        self.visible_session = session["id"]
        self.shown = shown

    def poll(self):
        try:
            result = self.heartbeat()
            session = result.get("session")
            if session and session["state"] == "active":
                self.show(session)
            else:
                self.root.withdraw()
                self.visible_session = ""
                self.shown = None
                self._session = None
            self.failures = 0
        except Exception:
            self.failures += 1
            if self.failures >= 5:
                self.root.destroy()
                return
        self.root.after(100 if self.visible_session else 350, self.poll)


def main():
    root = tk.Tk(className="SerenaComputer")
    indicator = ComputerIndicator(root, ComputerClient(timeout=1))
    root.after(0, indicator.poll)
    root.mainloop()


if __name__ == "__main__":
    main()
