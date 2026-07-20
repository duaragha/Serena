"""Render Serena's master MCP config into Claude / Codex agent configs.

Strategy: shell out to `claude mcp add/remove` and `codex mcp add/remove`
rather than hand-editing ~/.claude.json or ~/.codex/config.toml. Each agent
CLI knows its own schema better than we do, and they preserve the rest of
the config file safely.
"""

from __future__ import annotations

import shutil
import subprocess


def _bin(name: str) -> str:
    return shutil.which(name) or name


def _run(argv: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "timed out after 20s"
    except FileNotFoundError as e:
        return False, str(e)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def render_to_claude(server: dict) -> tuple[bool, str]:
    """Add/replace this server in Claude's config via `claude mcp add`."""
    name = server["name"]
    transport = server.get("transport") or "stdio"
    is_shared = bool(server.get("shared", False)) and transport == "stdio"
    # Idempotency: remove first, then add. claude's add doesn't update.
    _run([_bin("claude"), "mcp", "remove", name])

    if is_shared:
        # === MCP MULTIPLEXER === Shared MCPs are projected as HTTP URLs
        # pointing at Serena's multiplexer endpoint. Claude opens a normal
        # HTTP MCP connection; Serena fans out to the single shared subprocess.
        from core.mcp.multiplex import shared_url
        argv = [_bin("claude"), "mcp", "add", "-s", "user", "--transport", "http",
                name, shared_url(name)]
        return _run(argv)
    # === MCP MULTIPLEXER END ===

    argv = [_bin("claude"), "mcp", "add", "-s", "user", "--transport", transport]
    # env vars: use --env=KEY=VAL form (NOT -e KEY=VAL). claude's commander
    # treats `-e` as variadic and eagerly consumes the following positional
    # (the server name) as another env value, which then fails validation.
    # The `=` form binds the value tightly to the flag.
    for k, v in (server.get("env") or {}).items():
        argv += [f"--env={k}={v}"]
    for var in (server.get("secrets") or []):
        argv += [f"--env={var}=" + "${" + var + "}"]

    if transport == "http":
        url = server.get("url") or ""
        # Same variadic gotcha as --env: `-H` would eat the name/url. Use the
        # `=` form so the value binds tightly to the flag.
        for hk, hv in (server.get("headers") or {}).items():
            argv += [f"--header={hk}: {hv}"]
        argv += [name, url]
    else:
        # stdio — `--` separator REQUIRED so claude doesn't try to parse the
        # MCP server's own flags (e.g. `-y`, `--api-key`) as its own options.
        cmd = server.get("command") or ""
        args = server.get("args") or []
        argv += [name, "--", cmd, *args]

    return _run(argv)


def render_to_codex(server: dict) -> tuple[bool, str]:
    """Add/replace this server in Codex's config via `codex mcp add`."""
    name = server["name"]
    transport = server.get("transport") or "stdio"
    is_shared = bool(server.get("shared", False)) and transport == "stdio"
    _run([_bin("codex"), "mcp", "remove", name])

    if is_shared:
        from core.mcp.multiplex import shared_url
        argv = [_bin("codex"), "mcp", "add", name, "--url", shared_url(name)]
        return _run(argv)

    argv = [_bin("codex"), "mcp", "add"]
    for k, v in (server.get("env") or {}).items():
        argv += ["--env", f"{k}={v}"]
    for var in (server.get("secrets") or []):
        argv += ["--env", f"{var}=" + "${" + var + "}"]

    if transport == "http":
        url = server.get("url") or ""
        argv += [name, "--url", url]
    else:
        cmd = server.get("command") or ""
        args = server.get("args") or []
        argv += [name, "--", cmd, *args]
    return _run(argv)


def remove_from_claude(name: str) -> tuple[bool, str]:
    return _run([_bin("claude"), "mcp", "remove", name])


def remove_from_codex(name: str) -> tuple[bool, str]:
    return _run([_bin("codex"), "mcp", "remove", name])


def sync_server(server: dict) -> dict:
    """Render to whichever targets the server lists. Returns per-target results."""
    targets = server.get("targets") or ["claude", "codex"]
    results: dict[str, dict] = {}
    if not server.get("enabled", True):
        # Disabled: ensure removed from both
        if "claude" in targets:
            ok, msg = remove_from_claude(server["name"])
            results["claude"] = {"ok": ok, "message": msg, "action": "removed"}
        if "codex" in targets:
            ok, msg = remove_from_codex(server["name"])
            results["codex"] = {"ok": ok, "message": msg, "action": "removed"}
        return results
    if "claude" in targets:
        ok, msg = render_to_claude(server)
        results["claude"] = {"ok": ok, "message": msg, "action": "added"}
    else:
        ok, msg = remove_from_claude(server["name"])
        results["claude"] = {"ok": ok, "message": msg, "action": "removed"}
    if "codex" in targets:
        ok, msg = render_to_codex(server)
        results["codex"] = {"ok": ok, "message": msg, "action": "added"}
    else:
        ok, msg = remove_from_codex(server["name"])
        results["codex"] = {"ok": ok, "message": msg, "action": "removed"}
    return results


def remove_everywhere(name: str) -> dict:
    a_ok, a_msg = remove_from_claude(name)
    c_ok, c_msg = remove_from_codex(name)
    return {
        "claude": {"ok": a_ok, "message": a_msg, "action": "removed"},
        "codex":  {"ok": c_ok, "message": c_msg, "action": "removed"},
    }


def _master_config_hash() -> str:
    """Quick fingerprint of the master config + agent config files. If this
    matches the cached hash from the last successful sync, the projected
    state in ~/.claude.json + ~/.codex/config.toml is already correct and we
    can skip the slow shell-out loop (12+ servers × 2 agents × ~1s each)."""
    import hashlib
    from pathlib import Path
    from core.mcp.config import CONFIG_PATH
    h = hashlib.sha256()
    for p in (CONFIG_PATH, Path.home() / ".claude.json", Path.home() / ".codex" / "config.toml"):
        try:
            st = p.stat()
            h.update(p.name.encode())
            h.update(str(st.st_mtime_ns).encode())
            h.update(str(st.st_size).encode())
        except OSError:
            h.update(b"missing")
        h.update(b"|")
    return h.hexdigest()


def _hash_cache_path():
    from pathlib import Path
    p = Path.home() / ".local" / "share" / "chats"
    p.mkdir(parents=True, exist_ok=True)
    return p / "last-mcp-sync.hash"


def sync_all_on_startup(force: bool = False) -> dict:
    """Re-render every enabled server in Serena's master to its targets.
    Called on Serena boot so the master config is authoritative — even if
    an agent config got nuked or the user wiped their dotfile, Serena
    rebuilds the agent-side state from the master.

    Doesn't touch entries that exist in claude/codex but aren't in Serena's
    master — those stay as the user's manually-added servers.

    Skips the heavy subprocess-loop sync if the master + agent configs
    haven't changed since the last successful sync (huge boot speedup).
    Pass `force=True` to bypass the cache.
    """
    from core.mcp.config import list_servers
    cache = _hash_cache_path()
    current_hash = _master_config_hash()
    if not force:
        try:
            cached = cache.read_text(encoding="utf-8").strip()
        except (OSError, FileNotFoundError):
            cached = ""
        if cached == current_hash:
            return {"synced": [], "failed": [], "skipped": True}
    summary = {"synced": [], "failed": [], "skipped": False}
    for server in list_servers():
        if not server.get("enabled", True):
            continue
        try:
            result = sync_server(server)
            ok_targets = [a for a, r in result.items() if r.get("ok")]
            failed = {a: r["message"] for a, r in result.items() if not r.get("ok")}
            if failed:
                summary["failed"].append({"name": server["name"], "errors": failed})
            if ok_targets:
                summary["synced"].append({"name": server["name"], "targets": ok_targets})
        except Exception as e:
            summary["failed"].append({"name": server["name"], "errors": {"_": str(e)}})
    # Persist hash AFTER sync so next boot can short-circuit. Recompute since
    # claude/codex configs may have changed during the sync we just did.
    if not summary["failed"]:
        try:
            cache.write_text(_master_config_hash(), encoding="utf-8")
        except OSError:
            pass
    return summary
