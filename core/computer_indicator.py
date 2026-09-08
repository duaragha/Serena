"""Visible local ownership indicator and stop button; never takes keyboard focus."""

from __future__ import annotations

import tkinter as tk
from contextlib import suppress
from tkinter import font

from core.computer_client import ComputerClient


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
        mode = "controlling" if session["mode"] == "control" else "watching"
        observation = session.get("observation") or "waiting for a visual observation"
        shown = (session["id"], mode, session["target"], observation)
        if shown == self.shown:
            return  # Keep the user's scroll position when the advice has not changed.
        if not self.root.winfo_viewable():
            # A withdrawn Text widget has no usable wrapping width yet.
            self.heartbeat()
            self.root.deiconify()
            self.root.update_idletasks()
        self.header.configure(text=f"serena is {mode} · {session['target']}")
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
        self.root.after(350, self.poll)


def main():
    root = tk.Tk(className="SerenaComputer")
    indicator = ComputerIndicator(root, ComputerClient(timeout=1))
    root.after(0, indicator.poll)
    root.mainloop()


if __name__ == "__main__":
    main()
