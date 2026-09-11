import os
import tkinter as tk

import pytest

from core.computer_indicator import ComputerIndicator


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="requires X11")
def test_wrapped_guidance_remains_visible_after_streaming_and_collapse():
    class Client:
        def call(self, *args, **kwargs):
            return {}

    root = tk.Tk()
    try:
        hud = ComputerIndicator(root, Client())
        session = {
            "id": "layout-test", "driver": "astra", "mode": "watch",
            "target": "desktop", "observation_state": "watching",
            "observation": "short reply",
        }
        hud.show(session)
        session["observation"] = (
            "open the authentication tab to inspect mfa enforcement; this is an "
            "organization-wide setting that needs to be checked before continuing."
        )
        hud.show(session)
        assert hud.text.dlineinfo("end-2c") is not None
        assert hud.text.yview()[1] == 1.0
        hud.toggle_collapsed()
        hud.toggle_collapsed()
        assert hud.text.dlineinfo("end-2c") is not None
        assert hud.detail_frame.winfo_y() < hud.footer.winfo_y()
        session["observation"] = "long guidance with wrapped text. " * 200
        hud.show(session)
        assert hud.scrollbar.winfo_ismapped()
        hud.text.yview_moveto(1)
        root.update_idletasks()
        assert hud.text.dlineinfo("end-2c") is not None
        assert hud.stop_button.winfo_ismapped()
    finally:
        root.destroy()
