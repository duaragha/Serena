"""Opt-in live GUI acceptance test. Operates ONLY its own visible test window."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture(path, x):
    import tkinter as tk

    root = tk.Tk(className="SerenaComputerTest")
    root.title("Serena computer acceptance test")
    root.geometry(f"720x600+{x}+260")
    code = secrets.token_hex(3).upper()
    state = {
        "code": code,
        "text": "",
        "confirmed": False,
        "clicks": 0,
        "dragged": False,
        "scrolls": 0,
    }

    def save():
        path.write_text(json.dumps(state))

    tk.Label(root, text="SERENA · COMPUTER TEST", font=("sans", 22)).pack(pady=20)
    tk.Label(root, text=f"visible code: {code}", font=("monospace", 25)).pack(pady=10)
    entry = tk.Entry(root, font=("sans", 22), width=25)
    entry.pack(pady=20)

    def typed(_event=None):
        state["text"] = entry.get()
        save()

    entry.bind("<KeyRelease>", typed)

    def select_all(_event):
        entry.selection_range(0, "end")
        return "break"

    entry.bind("<Control-a>", select_all)
    status = tk.StringVar(value="waiting for input")

    def confirm():
        state["clicks"] += 1
        state["text"] = entry.get()
        state["confirmed"] = state["text"] == code
        status.set(
            "SUCCESS · code verified" if state["confirmed"] else "clicked · code does not match"
        )
        save()

    button = tk.Button(root, text="confirm", command=confirm, font=("sans", 19))
    button.pack(pady=10)
    tk.Label(root, textvariable=status, font=("sans", 18)).pack(pady=12)
    canvas = tk.Canvas(root, width=500, height=110, bg="#e5d8ed")
    canvas.pack()
    canvas.create_text(250, 20, text="drag here · scroll here")

    def dragged(_event):
        state["dragged"] = True
        save()

    def scroll(_event):
        state["scrolls"] += 1
        save()

    canvas.bind("<B1-Motion>", dragged)
    canvas.bind("<Button-4>", scroll)
    canvas.bind("<Button-5>", scroll)
    root.update()
    state["entry"] = [
        entry.winfo_x() + entry.winfo_width() // 2,
        entry.winfo_y() + entry.winfo_height() // 2,
    ]
    state["button"] = [
        button.winfo_x() + button.winfo_width() // 2,
        button.winfo_y() + button.winfo_height() // 2,
    ]
    state["canvas"] = [canvas.winfo_x() + 100, canvas.winfo_y() + 65]
    save()
    change_path = path.with_suffix(".change")
    def change():
        if change_path.exists():
            status.set(change_path.read_text())
            change_path.unlink()
        root.after(100, change)
    root.after(100, change)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--x", type=int, default=500)
    args = parser.parse_args()
    if args.fixture:
        fixture(args.fixture, args.x)
        return
    if not args.output:
        parser.error("--output must be a directory in the synced Projects tree")
    from core.computer_client import ComputerClient
    from core.computer_platform import desktop_environment

    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "fixture-state.json"
    state_path.unlink(missing_ok=True)
    client = ComputerClient()
    status = client.ensure_running()
    if status.get("session") and status["session"]["state"] == "active":
        raise RuntimeError("an existing user session is active; smoke test will not interrupt it")
    env = desktop_environment()
    process = subprocess.Popen(
        [sys.executable, __file__, "--fixture", str(state_path), "--x", str(args.x)], env=env
    )
    receipt = {"backend": status["backend"], "monitors": status["monitors"], "checks": {}}
    try:
        deadline = time.monotonic() + 10
        while not state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        state = json.loads(state_path.read_text())
        window_id = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--name", "^Serena computer acceptance test$"],
            env=env,
            text=True,
        ).splitlines()[-1]
        subprocess.run(["xdotool", "windowactivate", "--sync", window_id], env=env, check=True)
        result = client.call(
            "begin",
            mode="control",
            target="window:" + window_id,
            request="Exercise typing, clicking, scrolling and dragging in this acceptance test window",
            seconds=180,
        )
        sid = result["session"]["id"]
        frame = client.call("observe", session_id=sid)
        receipt["capture_ms"] = frame["capture_ms"]
        receipt["rect"] = frame["rect"]

        def point(key):
            x, y = state[key]
            return {"x": x, "y": y}

        actions = [
            {"type": "click", **point("entry")},
            {"type": "type", "text": "Serena Ω café"},
            {"type": "keypress", "keys": ["CTRL", "a"]},
            {"type": "type", "text": state["code"]},
            {"type": "click", **point("button")},
            {"type": "scroll", **point("canvas"), "scroll_y": 200},
            {
                "type": "drag",
                "path": [point("canvas"), {"x": state["canvas"][0] + 80, "y": state["canvas"][1]}],
            },
        ]
        request_id = secrets.token_hex(8)
        batch = dict(
            session_id=sid,
            frame_id=frame["frame_id"],
            request_id=request_id,
            intent="exercise only the test fixture",
            actions=actions,
        )
        action_result = client.call("act", **batch)
        receipt["action"] = {k: v for k, v in action_result.items() if k != "frame"}
        time.sleep(0.2)
        actual = json.loads(state_path.read_text())
        assert action_result["ok"], action_result
        assert actual["confirmed"] and actual["dragged"] and actual["scrolls"] == 2, actual
        receipt["checks"].update(click=True, type=True, keypress=True, scroll=True, drag=True)
        replay = client.call("act", **batch)
        assert replay["replayed"] and json.loads(state_path.read_text())["clicks"] == 1
        receipt["checks"]["idempotent_retry"] = True
        import base64

        (directory / "fixture-verified.jpg").write_bytes(
            base64.b64decode(action_result["frame"]["data"])
        )
        started = time.monotonic()
        # A real X11 shortcut event exercises the installed passive grab.
        subprocess.run(["xdotool", "key", "ctrl+alt+shift+Escape"], env=env, check=True)
        while (
            client.call("status")["session"]["state"] == "active" and time.monotonic() - started < 3
        ):
            time.sleep(0.01)
        stopped = client.call("status")["session"]
        assert stopped["state"] == "stopped" and stopped["reason"] == "Ctrl+Alt+Shift+Escape", (
            stopped
        )
        receipt["shortcut_stop_ms"] = round((time.monotonic() - started) * 1000, 2)
        receipt["checks"]["stop_shortcut"] = True
        if args.model:
            subprocess.run(["xdotool", "windowactivate", "--sync", window_id], env=env, check=True)
            result = client.call(
                "run",
                mode="control",
                target="window:" + window_id,
                seconds=180,
                request="In this test window, clear the text field, type the visible code in it, then click confirm. Verify the success text. Use computer tools, and do not touch any other app.",
            )
            model_sid = result["session"]["id"]
            after = 0
            observations = []
            deadline = time.monotonic() + 190
            while time.monotonic() < deadline:
                output = client.call("events", after=after, timeout=10)
                after = output["last_id"]
                for event in output["events"]:
                    if event.get("session_id") != model_sid:
                        continue
                    if event["type"] == "delta":
                        print(event["text"], end="", flush=True)
                    if event["type"] in {"observation", "error"}:
                        observations.append(event)
                if client.call("status")["session"]["state"] != "active":
                    break
            actual = json.loads(state_path.read_text())
            receipt["model_observations"] = observations
            assert actual["confirmed"] and actual["clicks"] == 2, (actual, observations)
            assert any(
                "serena_computer.act" in item.get("tool_calls", []) for item in observations
            ), observations
            receipt["checks"]["astra_image_and_input"] = True
        if args.watch:
            # Wait for the previous runner to finish closing its subscription process.
            time.sleep(1)
            result = client.call("run", mode="watch", target="window:" + window_id, seconds=120,
                                 request="Watch the status label below confirm and report its text when it changes. Include the exact new status text.")
            watch_sid = result["session"]["id"]
            after = 0
            observations = []
            deadline = time.monotonic() + 115
            while time.monotonic() < deadline:
                output = client.call("events", after=after, timeout=10)
                after = output["last_id"]
                for event in output["events"]:
                    if event.get("session_id") != watch_sid or event["type"] != "observation":
                        continue
                    observations.append(event)
                    print("\nwatch: " + event["text"], flush=True)
                    if len(observations) == 1:
                        state_path.with_suffix(".change").write_text("phase two: purple triangle")
                if any("purple triangle" in event["text"].lower() for event in observations):
                    break
                if client.call("status")["session"]["state"] != "active":
                    break
            receipt["watch_observations"] = observations
            assert len(observations) >= 2 and "purple triangle" in observations[-1]["text"].lower(), observations
            assert not any(event.get("tool_calls") for event in observations)
            receipt["checks"]["live_watch_updates"] = True
    finally:
        client.call("stop", reason="acceptance test cleanup")
        process.terminate()
        process.wait(timeout=3)
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print("\n" + json.dumps(receipt["checks"], indent=2))


if __name__ == "__main__":
    main()
