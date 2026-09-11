"""Bounded native installation diagnostics, never a coding session or repair."""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from core.billing import strip_metered_auth_env
from core.security_policy import redact_credentials


async def claude_doctor(binary, cwd, env, *, timeout=20, max_bytes=1024 * 1024):
    command = [str(binary), "doctor"]
    launch = command
    job = None
    process = None
    clean = strip_metered_auth_env(dict(env))
    clean.update(TERM="dumb", CI="1", NO_COLOR="1")
    if os.name == "nt":
        from core.workspace_windows_job import WindowsJob

        job = WindowsJob()
        launch = ([sys.executable, "--workspace-child"] if getattr(sys, "frozen", False) else
                  [getattr(sys, "_base_executable", sys.executable), "-I", "-S",
                   str(Path(__file__).with_name("workspace_windows_bootstrap.py"))])
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *launch, cwd=str(cwd), env=clean, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
            if job is not None:
                job.assign(process.pid)
                process.stdin.write((json.dumps(command) + "\n").encode())
                await process.stdin.drain()
            process.stdin.close()
            chunks, size = [], 0
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError("Installation diagnostics exceeded the output limit")
                chunks.append(chunk)
            code = await process.wait()
            return {"command": "claude doctor", "exitCode": code,
                    "output": redact_credentials(b"".join(chunks).decode("utf-8", errors="replace"))}
    except TimeoutError as error:
        raise RuntimeError("Installation diagnostics timed out; its process was stopped") from error
    finally:
        if job is not None:
            job.close()
        if process is not None:
            if os.name != "nt":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
