"""Visible local ownership indicator and stop button; never takes keyboard focus."""

from __future__ import annotations

import tkinter as tk
from contextlib import suppress

from core.computer_client import ComputerClient


def main():
    client = ComputerClient(timeout=1)
    root = tk.Tk(className="SerenaComputer")
    root.withdraw()
    root.title("Serena computer use")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#211b29")
    root.geometry("430x112+20+20")
    label = tk.Label(
        root,
        text="",
        bg="#211b29",
        fg="#f8e6ff",
        font=("sans", 11),
        anchor="w",
        justify="left",
        wraplength=410,
    )
    label.pack(fill="x", padx=10, pady=8)

    def stop():
        with suppress(Exception):
            client.call("stop", reason="stopped from the desktop indicator")

    tk.Button(
        root,
        text="stop · Ctrl+Alt+Shift+Esc",
        command=stop,
        bg="#d58fbd",
        fg="#17121c",
        takefocus=False,
    ).pack(anchor="e", padx=10, pady=(0, 8))
    failures = 0
    visible_session = ""

    def poll():
        nonlocal failures, visible_session
        try:
            result = client.call(
                "indicator", visible_session=visible_session if root.winfo_viewable() else ""
            )
            failures = 0
            session = result.get("session")
            if session and session["state"] == "active":
                mode = "controlling" if session["mode"] == "control" else "watching"
                observation = session.get("observation") or "waiting for a visual observation"
                label.configure(text=f"serena is {mode} · {session['target']}\n{observation[:110]}")
                root.deiconify()
                visible_session = session["id"]
            else:
                root.withdraw()
                visible_session = ""
        except Exception:
            failures += 1
            if failures >= 5:
                root.destroy()
                return
        root.after(350, poll)

    root.after(0, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
