"""Test-connection: spawn an MCP server briefly and run the JSON-RPC
`initialize` handshake to verify it responds with valid serverInfo."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

from core.mcp.secrets import resolved_headers, server_environment
from core.security_policy import SecurityPolicyError, URLPolicy, redact_credentials

_INIT_TIMEOUT = 8.0  # seconds to wait for serverInfo

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "serena", "version": "0.1"},
    },
}


def _stdio_test(server: dict) -> dict:
    cmd = server.get("command") or ""
    args = server.get("args") or []
    if not cmd:
        return {"ok": False, "message": "no command"}
    binpath = shutil.which(cmd) or cmd

    try:
        env = server_environment(server)
    except SecurityPolicyError as exc:
        return {"ok": False, "message": f"environment policy refused spawn: {exc}"}

    cwd = server.get("cwd") or None
    try:
        proc = subprocess.Popen(
            [binpath, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            bufsize=0,
        )
    except (OSError, FileNotFoundError) as e:
        return {"ok": False, "message": f"spawn failed: {e}"}

    result: dict[str, Any] = {"ok": False, "message": "no response"}
    deadline = time.time() + _INIT_TIMEOUT
    secret_values = [
        env.get(str(name), "") for name in (server.get("secrets") or [])
    ]

    def _read():
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            if not line:
                result["message"] = "server exited before responding"
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                result["message"] = "non-JSON response: " + redact_credentials(
                    line[:120], secret_values=secret_values
                )
                return
            if obj.get("id") == 1 and "result" in obj:
                info = obj["result"].get("serverInfo") or {}
                result["ok"] = True
                result["message"] = f"OK · {info.get('name', '?')} v{info.get('version', '?')}"
                result["server_info"] = info
            else:
                result["message"] = f"unexpected response: {str(obj)[:200]}"
        except Exception as e:
            result["message"] = f"read failed: {e}"

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(_INITIALIZE) + "\n")
        proc.stdin.flush()
    except (OSError, BrokenPipeError) as e:
        result["message"] = f"write failed: {e}"

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=max(0.1, deadline - time.time()))

    # Kill the process group cleanly
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    except OSError:
        pass

    if not result["ok"] and result["message"] == "no response":
        stderr = redact_credentials(
            (proc.stderr.read() if proc.stderr else "") or "",
            secret_values=secret_values,
        )
        if stderr:
            result["message"] = f"timed out · stderr: {stderr[:200]}"
        else:
            result["message"] = f"timed out after {_INIT_TIMEOUT}s"
    return result


def _http_test(server: dict) -> dict:
    import urllib.error
    import urllib.request

    url = server.get("url") or ""
    if not url:
        return {"ok": False, "message": "no url"}
    try:
        policy = URLPolicy(
            allowed_domains=tuple(server.get("allowed_domains") or ()),
            allow_private_network=bool(server.get("allow_private_network", False)),
            allow_http=bool(server.get("allow_private_network", False)),
        )
        policy.validate(url)

        class PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                policy.validate_redirect(req.full_url, newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        body = json.dumps(_INITIALIZE).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        headers = resolved_headers(server)
        for hk, hv in headers.items():
            req.add_header(hk, hv)
        opener = urllib.request.build_opener(PolicyRedirectHandler())
        with opener.open(req, timeout=_INIT_TIMEOUT) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
        # Streamable HTTP can be SSE (data: ...). Try both.
        for line in payload.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == 1 and "result" in obj:
                info = obj["result"].get("serverInfo") or {}
                return {
                    "ok": True,
                    "message": f"OK · {info.get('name', '?')} v{info.get('version', '?')}",
                    "server_info": info,
                }
        return {
            "ok": False,
            "message": "unexpected response: "
            + redact_credentials(payload[:200], secret_values=list(headers.values())),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "message": f"connection failed: {e.reason}"}
    except SecurityPolicyError as e:
        return {"ok": False, "message": f"URL policy refused connection: {e}"}
    except Exception as e:
        return {"ok": False, "message": f"error: {e}"}


def test_server(server: dict) -> dict:
    transport = (server.get("transport") or "stdio").lower()
    if transport == "http":
        return _http_test(server)
    return _stdio_test(server)
