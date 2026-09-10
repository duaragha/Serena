"""Visible local ownership indicator and stop button; never takes keyboard focus."""

from __future__ import annotations

import time
import tkinter as tk
from contextlib import suppress
from tkinter import font

from core.computer_client import ComputerClient


def focus_label(session):
    context = session.get("focused_window") or {}
    title = " ".join(context.get("title", "").split())
    # WM_CLASS commonly contains the same app name twice, with different casing.
    app = " ".join(
        dict((part.casefold(), part) for part in context.get("app", "").split()).values()
    )
    if title or app:
        return " · ".join(value for value in (app, title) if value)
    return f"{session['target']} · waiting for window details"


class ComputerIndicator:
    def __init__(self, root, client):
        self.root = root
        self.client = client
        self.failures = 0
        self.visible_session = ""
        self.shown = None
        self.width = min(520, root.winfo_screenwidth() - 40)
        self.max_height = min(640, root.winfo_screenheight() - 40)
        self.height = 112
        root.withdraw()
        root.title("Serena computer use")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#211b29")
        root.geometry(f"{self.width}x{self.height}+20+20")
        self.header = tk.Label(
            root,
            text="",
            bg="#211b29",
            fg="#d58fbd",
            font=("sans", 11),
            anchor="w",
            justify="left",
            wraplength=self.width - 24,
        )
        self.header.pack(fill="x", padx=12, pady=(10, 6))
        body = tk.Frame(root, bg="#211b29")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.text = tk.Text(
            body,
            height=1,
            width=1,
            wrap="word",
            bg="#211b29",
            fg="#f8e6ff",
            font=("sans", 11),
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            takefocus=False,
            cursor="arrow",
            state="disabled",
        )
        self.scrollbar = tk.Scrollbar(body, command=self.text.yview, takefocus=False)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.button = tk.Button(
            root,
            text="stop · Ctrl+Alt+Shift+Esc",
            command=self.stop,
            bg="#d58fbd",
            fg="#17121c",
            takefocus=False,
        )
        self.button.pack(anchor="e", padx=12, pady=(0, 10))

    def stop(self):
        with suppress(Exception):
            self.client.call("stop", reason="stopped from the desktop indicator")

    def heartbeat(self, height=None):
        return self.client.call(
            "indicator",
            visible_session=self.visible_session if self.root.winfo_viewable() else "",
            rect={"x": 20, "y": 20, "width": self.width, "height": height or self.height},
        )

    def show(self, session):
        automated = session.get("driver") == "astra"
        mode = (
            ("controlling" if session["mode"] == "control" else "watching")
            if automated
            else "sharing"
        )
        if automated:
            started = session.get("inspection_started_at")
            elapsed = max(0, int(time.time() - started)) if started else 0
            waiting = {
                "starting": "starting gpt-6 astra · medium · fast",
                "thinking": f"astra is reading your screen… {elapsed}s",
                "screen_changed": "screen activity · checking the current view…",
            }.get(
                session.get("observation_state"),
                "screen checked · no new guidance; watching for changes",
            )
        else:
            waiting = "screen shared with your chat; automatic coaching is off"
        observation = session.get("observation") or waiting
        if session.get("observation_preview"):
            observation = "draft · " + session["observation_preview"]
        focus = focus_label(session)
        shown = (session["id"], mode, focus, observation)
        if shown == self.shown:
            return  # Keep the user's scroll position when the advice has not changed.
        if not self.root.winfo_viewable():
            # A withdrawn Text widget has no usable wrapping width yet.
            self.heartbeat()
            self.root.deiconify()
            self.root.update_idletasks()
        self.header.configure(text=f"serena is {mode}\n{focus}")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", observation)
        self.text.configure(state="disabled")
        self.scrollbar.pack_forget()
        self.root.update_idletasks()
        line_height = font.Font(font=self.text.cget("font")).metrics("linespace")
        chrome = self.header.winfo_reqheight() + self.button.winfo_reqheight() + 36
        max_lines = max(1, (self.max_height - chrome) // line_height)
        lines = (self.text.count("1.0", "end-1c", "displaylines") or (0,))[0] + 1
        if lines > max_lines:
            self.scrollbar.pack(side="right", fill="y")
            self.root.update_idletasks()
            lines = (self.text.count("1.0", "end-1c", "displaylines") or (0,))[0] + 1
        lines = min(lines, max_lines)
        height = chrome + lines * line_height
        # Reserve the whole resize area before mapping any new pixels. The next
        # heartbeat contracts the screenshot mask after Tk completes the resize.
        self.heartbeat(max(self.height, height))
        self.text.configure(height=lines)
        self.root.geometry(f"{self.width}x{height}+20+20")
        self.root.deiconify()
        self.root.update_idletasks()
        self.height = height
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
