"""Her hands on the macOS VM, reached the only way it is reachable.

``BlueBubbles-macOS`` is a VirtualBox guest on the PC behind NAT, so nothing on
the tailnet can address it directly. VirtualBox forwards the guest's port 22 to
the PC's own loopback, which means every call is two hops: this machine to the
PC, then the PC's loopback into the guest. That shape is the reason these are
tools rather than "she has a shell" -- the command line is fiddly enough that
open-coding it in a model's head would go wrong quietly.

Raghav asked for full control on 2026-09-18, writes and lifecycle included,
after being told what that allows. So ``mac_run`` is a real shell and
``mac_control`` really does stop the VM. The guest runs the BlueBubbles bridge;
stopping it stops iMessage.
"""

from __future__ import annotations

import asyncio
import subprocess

from claude_agent_sdk import create_sdk_mcp_server, tool
from mcp.types import ToolAnnotations

# The PC's own loopback, from the PC. VirtualBox NAT publishes the guest there.
PC_HOST = "pc"
GUEST_SSH_PORT = 2224
GUEST_USER = "raghav"
VM_NAME = "BlueBubbles-macOS"
VBOXMANAGE = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
RUN_TIMEOUT_SECONDS = 120
CONTROL_TIMEOUT_SECONDS = 180

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
_FULL_CONTROL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

_CONTROL_VERBS = {
    "status": None,
    "start": ["startvm", VM_NAME, "--type", "headless"],
    "stop": ["controlvm", VM_NAME, "acpipowerbutton"],
    "kill": ["controlvm", VM_NAME, "poweroff"],
    "save": ["controlvm", VM_NAME, "savestate"],
    "snapshot": ["snapshot", VM_NAME, "take"],
}


def _ssh(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def _guest(command: str, timeout: int = RUN_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run one command inside the macOS guest, two hops out.

    The inner command is quoted once for the PC's shell and once more for the
    guest's, which is exactly the kind of thing that is wrong every other time
    it is written by hand.
    """

    # The middle hop is cmd.exe, which does not understand POSIX single quotes
    # -- shlex.quote here sent the command to Windows instead of to the guest,
    # and a pipe came back as "'head' is not recognized". Double quotes are
    # what cmd passes through, so the guest's own shell sees the whole string.
    safe = command.replace('"', '\\"')
    inner = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
        f'-p {GUEST_SSH_PORT} {GUEST_USER}@127.0.0.1 "{safe}"'
    )
    return _ssh(["ssh", "-o", "BatchMode=yes", PC_HOST, inner], timeout)


def _vbox(args: list[str], timeout: int = CONTROL_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    return _ssh(["ssh", "-o", "BatchMode=yes", PC_HOST,
                 f'cmd /c ""{VBOXMANAGE}" {quoted}"'], timeout)


def _render(code: int, out: str, err: str) -> dict:
    body = out or "(no output)"
    if err:
        body = f"{body}\n[stderr] {err}" if out else f"[stderr] {err}"
    if code != 0:
        body = f"[exit {code}] {body}"
    return {"content": [{"type": "text", "text": body[:20000]}]}


@tool(
    "mac_status",
    "Report the macOS VM: whether the VirtualBox guest is running, its macOS "
    "version, uptime, and whether the BlueBubbles bridge is up. Read-only.",
    {"detail": str},
    annotations=_READ_ONLY,
)
async def mac_status(_args):
    code, out, err = await asyncio.to_thread(_vbox, ["showvminfo", VM_NAME, "--machinereadable"])
    state = "unknown"
    for line in out.splitlines():
        if line.startswith("VMState="):
            state = line.split("=", 1)[1].strip('"')
    if state != "running":
        return _render(code, f"VM {VM_NAME}: {state}", err)
    gcode, gout, gerr = await asyncio.to_thread(
        _guest, "sw_vers -productVersion; uptime; pgrep -fl BlueBubbles | head -3", 60)
    return _render(gcode, f"VM {VM_NAME}: running\n{gout}", gerr)


@tool(
    "mac_run",
    "Run a shell command inside the macOS VM and return its output. This is a "
    "real shell with full write access: it can edit files, install things, and "
    "restart the BlueBubbles bridge. Prefer the narrowest command that answers "
    "the question.",
    {"command": str},
    annotations=_FULL_CONTROL,
)
async def mac_run(args):
    command = str((args or {}).get("command") or "").strip()
    if not command:
        return _render(1, "", "no command given")
    code, out, err = await asyncio.to_thread(_guest, command)
    return _render(code, out, err)


@tool(
    "mac_control",
    "Control the macOS VM itself: status, start, stop (graceful), kill (hard "
    "power off), save (suspend to disk), snapshot. Stopping the VM stops the "
    "BlueBubbles iMessage bridge with it.",
    {"action": str, "name": str},
    annotations=_FULL_CONTROL,
)
async def mac_control(args):
    action = str((args or {}).get("action") or "status").strip().lower()
    if action not in _CONTROL_VERBS:
        return _render(1, "", f"unknown action {action!r}; "
                              f"try {', '.join(sorted(_CONTROL_VERBS))}")
    if action == "status":
        return await mac_status({})
    argv = list(_CONTROL_VERBS[action])
    if action == "snapshot":
        argv.append(str((args or {}).get("name") or "serena-snapshot"))
    code, out, err = await asyncio.to_thread(_vbox, argv)
    return _render(code, out or f"{action} sent to {VM_NAME}", err)


VM_TOOLS = (mac_status, mac_run, mac_control)
VM_TOOL_NAMES = [f"mcp__serena-vm__{item.name}" for item in VM_TOOLS]


def vm_tools_server():
    return create_sdk_mcp_server(name="serena-vm", tools=list(VM_TOOLS))
