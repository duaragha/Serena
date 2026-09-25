"""Local operator controls for the shared desktop session."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import click

from core.computer_client import ComputerClient
from core.computer_conversation import origin_arguments
from core.computer_platform import ComputerError


class ComputerGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ComputerError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=ComputerGroup)
def computer():
    """Watch and operate your desktop, or Serena's own, with a Claude visual worker."""


@computer.command()
def serve():
    """Run the desktop service inside your graphical login."""
    from core.computer_service import serve as run

    run()


@computer.command(hidden=True)
@click.option("--desk", type=click.Choice(["host", "isolated"]), default="host")
def indicator(desk):
    from core.computer_indicator import main

    main(desk)


@computer.command()
def mcp():
    """Expose user-requested computer sessions over MCP stdio."""
    from core.computer_mcp import mcp as server

    server.run()


@computer.command(hidden=True)
@click.option("--agent", type=click.Choice(["codex", "claude"]), required=True)
def context_hook(agent):
    """Add this chat's earlier coaching to its next turn."""
    from core.computer_context_hook import main

    main(agent)


@computer.command()
@click.argument("session_id")
def history(session_id):
    """Read the linked chat and durable computer coaching history."""
    click.echo(json.dumps(ComputerClient().call("history", session_id=session_id), indent=2))


@computer.command()
def status():
    """Inspect active displays and the current lease; captures no pixels."""
    click.echo(json.dumps(ComputerClient().ensure_running(), indent=2))


def options(func):
    for decorator in reversed(
        [
            click.argument("task"),
            click.option(
                "--target",
                default=None,
                help=(
                    "isolated (Serena's own desktop), active, desktop, display:NAME, window:ID. "
                    "Default: active for watch, isolated for run."
                ),
            ),
            click.option("--seconds", type=click.IntRange(1, 1800), default=300, show_default=True),
            click.option(
                "--speak", is_flag=True, help="Speak observations through Serena's local voice."
            ),
            click.option(
                "--detach", is_flag=True, help="Leave the task running with its desktop indicator."
            ),
        ]
    ):
        func = decorator(func)
    return func


def run_task(mode, task, target, seconds, speak, detach):
    # A GUI task runs on her own desktop unless it needs one of his windows.
    target = target or ("isolated" if mode == "control" else "active")
    client = ComputerClient()
    client.ensure_running()
    result = client.call(
        "run",
        mode=mode,
        target=target,
        request=task,
        seconds=seconds,
        speak=speak,
        **origin_arguments(),
    )
    session = result["session"]
    click.echo(
        f"{mode} · {session['target']} · {session['id']} · {session.get('worker_model') or result.get('model')}"
    )
    if not detach:
        follow(client, after=0, session_id=session["id"])


@computer.command()
@options
def watch(**kwargs):
    """Describe relevant screen changes until stopped or the lease expires."""
    run_task("watch", **kwargs)


@computer.command()
@options
def run(**kwargs):
    """Complete one GUI task, on Serena's own desktop by default.

    Your mouse and keyboard stay yours: her desktop is headless, and its viewer
    is view-only while she drives. "take over" on her indicator pauses her and
    hands you the viewer; `chats computer resume` hands control back.
    Ctrl+Alt+Shift+Esc stops.
    """
    run_task("control", **kwargs)


@computer.command()
@click.option("--session", "session_id", default="", help="Which paused session, if two are.")
def resume(session_id):
    """Hand control back after you took over; she continues from a fresh screenshot."""
    click.echo(json.dumps(ComputerClient().call("resume", session_id=session_id), indent=2))


@computer.group()
def desktop():
    """Serena's own desktop: its own mouse, keyboard, browser and terminal."""


def _desktop(action, **params):
    client = ComputerClient()
    client.ensure_running()
    click.echo(json.dumps(client.call("desktop", action=action, **params), indent=2))


@desktop.command("status")
def desktop_status():
    """Whether her desktop is running, and where."""
    _desktop("status")


@desktop.command("open")
def desktop_open():
    """Start her desktop (or adopt the running one) with a browser and terminal."""
    _desktop("open")


@desktop.command("close")
def desktop_close():
    """Stop any task there and close her desktop. Browser logins persist."""
    _desktop("close")


@desktop.command("show")
def desktop_show():
    """Open or raise the viewer: view-only while she drives, yours otherwise."""
    _desktop("show")


@desktop.command("hide")
def desktop_hide():
    """Minimize the viewer; her desktop keeps running headless."""
    _desktop("hide")


@desktop.command("launch")
@click.argument("app", type=click.Choice(["browser", "terminal"]))
@click.argument("url", required=False)
def desktop_launch(app, url):
    """Open a browser (optionally at URL) or a terminal on her desktop."""
    _desktop("launch", app=app, url=url)


@computer.command()
@click.argument("task")
@click.option("--mode", type=click.Choice(["watch", "control"]), default="watch")
@click.option("--target", default="active")
@click.option("--seconds", type=click.IntRange(1, 1800), default=300)
@click.option(
    "--interactive",
    is_flag=True,
    help="Share with the connected chat without automatic worker updates.",
)
def begin(task, mode, target, seconds, interactive):
    """Start a bounded session for a connected CLI/MCP agent.

    An agent may execute this command directly when the user requests computer
    use in chat. The user does not need to run it manually. Use their actual
    task and intended target; active can be the chat terminal. Watch mode starts
    coaching by default. --interactive explicitly opens sharing only;
    it does not produce automatic observations.
    """
    client = ComputerClient()
    client.ensure_running()
    click.echo(
        json.dumps(
            client.call(
                "begin",
                mode=mode,
                target=target,
                request=task,
                seconds=seconds,
                interactive=interactive,
                **origin_arguments(),
            ),
            indent=2,
        )
    )


@computer.command()
@click.option("--reason", default="stopped from CLI")
def stop(reason):
    """Cancel capture and release injected input immediately."""
    click.echo(json.dumps(ComputerClient().call("stop", reason=reason)))


@computer.command()
@click.argument("message")
def steer(message):
    """Give a new instruction to the currently running worker turn."""
    click.echo(json.dumps(ComputerClient().call("steer", message=message)))


@computer.command()
@click.option("--output", type=click.Path(path_type=Path), required=True)
def screenshot(output):
    """Save a fresh image from the active authorized session."""
    client = ComputerClient()
    session = client.call("status").get("session")
    if not session or session["state"] != "active":
        raise ComputerError("start a bounded session first")
    frame = client.call("observe", session_id=session["id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as file:
        os.chmod(output, 0o600)
        file.write(base64.b64decode(frame["data"]))
    click.echo(json.dumps({**{k: v for k, v in frame.items() if k != "data"}, "file": str(output)}))


def follow(client, *, after=0, session_id=None):
    streamed = False
    failure = None
    try:
        while True:
            result = client.call("events", after=after, timeout=10)
            after = result["last_id"]
            for event in result["events"]:
                if session_id and event.get("session_id") != session_id:
                    continue
                kind = event["type"]
                if kind == "delta":
                    click.echo(event["text"], nl=False)
                    streamed = True
                elif kind == "observation":
                    if streamed:
                        click.echo()
                    else:
                        click.echo(event["text"])
                    streamed = False
                    click.echo(
                        f"[{event['model_ms']} ms · screenshot {event['captured_at']:.3f}]",
                        err=True,
                    )
                elif kind == "screen_changed":
                    click.echo("\n[screen changed · updating guidance]", err=True)
                    streamed = False
                elif kind == "paused":
                    click.echo(
                        f"\n[paused · {event.get('reason')} · chats computer resume continues]",
                        err=True,
                    )
                    streamed = False
                elif kind == "resumed":
                    click.echo("[resumed · continuing from a fresh screenshot]", err=True)
                elif kind in {"error", "speech_error", "stopped"}:
                    click.echo("\n" + str(event.get("error") or event.get("reason")), err=True)
                    if kind == "error":
                        failure = event["error"]
                if kind == "stopped":
                    if failure:
                        raise ComputerError(failure)
                    return
            status = client.call("status")
            sessions = status.get("sessions") or [status.get("session")]
            current = (
                next((item for item in sessions if item and item["id"] == session_id), None)
                if session_id
                else status.get("session")
            )
            if not current or current["state"] not in {"active", "paused", "resuming"}:
                return
    except KeyboardInterrupt:
        client.call("stop", reason="Ctrl+C in computer CLI")


@computer.command()
def events():
    """Follow text updates from the current session."""
    client = ComputerClient()
    result = client.call("status")
    follow(client, session_id=(result.get("session") or {}).get("id"))


@computer.command()
def install():
    """Enable the X11 helper at login, without starting a capture session."""
    if getattr(sys, "frozen", False):
        raise ComputerError(
            "install the login service using chats computer install from the source CLI, whose path survives AppImage unmounts"
        )
    if sys.platform != "linux":
        raise ComputerError("automatic service installation currently supports Linux X11")
    from core.computer_client import child_command
    from core.computer_hooks import install_context_hooks

    install_context_hooks()

    unit = Path.home() / ".config/systemd/user/serena-computer.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    args = child_command("serve")
    # systemd uses its own quoting and specifier syntax, not shell syntax.
    command = " ".join(
        '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"' for a in args
    )
    unit.write_text(
        "[Unit]\nDescription=Serena computer-use helper\nAfter=graphical-session.target\n"
        "PartOf=graphical-session.target\n[Service]\nType=simple\n"
        f"ExecStart={command}\nWorkingDirectory={Path(__file__).resolve().parents[1]}\n"
        "Restart=on-failure\nRestartSec=3\nUMask=0077\n[Install]\nWantedBy=graphical-session.target\n"
    , encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "serena-computer.service"], check=True)
    # A detached CLI-started service already owns the desktop: never kill a live lease to install.
    try:
        ComputerClient().call("status")
    except ComputerError:
        subprocess.run(["systemctl", "--user", "start", "serena-computer.service"], check=True)
    click.echo(str(unit))


@computer.command()
def install_hooks():
    """Install conversation continuity hooks into the local Codex and Claude CLIs."""
    if getattr(sys, "frozen", False):
        raise ComputerError(
            "install hooks from the source CLI so their command survives app updates"
        )
    from core.computer_hooks import install_context_hooks

    click.echo(json.dumps(install_context_hooks(), indent=2))
