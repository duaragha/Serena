"""The part of the plugin system that actually runs something.

`core.serena_plugins` records what a plugin declared and whether Raghav
approved it. This module is the only place that turns an approved manifest
into a running process, and it is deliberately narrow about how.

The shape of the constraint, in order of how much it matters:

Serena never imports plugin code. Not `importlib`, not `exec`, not an entry
point group. A plugin runs as a separate interpreter started with `-I -c`, so
its `sys.path` does not contain Serena's package tree and it cannot reach into
her internals even by accident. The one import that happens is the child
loading its own approved entrypoint file, by absolute path, inside that
isolated process.

Approval is re-checked at load, not trusted from install time. The installed
manifest is hashed and compared against the manifests an operator actually
approved. A row edited in the database after approval does not run.

Reach is asked for, never taken. The child gets an environment built from
empty, holding only the secret references its manifest declared. It has no
PATH, so a stray `os.system` finds nothing. Every file read, file write,
network fetch, and Serena capability is a request back over the pipe that this
process answers against the manifest, the live lifecycle state, and the
protected-path list. Disabling a plugin revokes its reach on the very next
call, because the check reads the registry every time rather than caching what
it was allowed to do at startup.

Nothing here installs, enables, or approves anything. A plugin that is not
already `enabled` by a named actor cannot be loaded at all.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.serena_plugins import (
    GLOBAL_SCOPE_KEY,
    HOOK_EVENTS,
    PERMISSION_SCOPES,
    PluginManifest,
    PluginRegistry,
    validate_manifest,
)

DEFAULT_GLOBAL_ROOT = Path.home() / ".local" / "share" / "serena" / "plugins"

# A plugin gets a small, boring amount of time and output. Anything past this
# is a plugin problem, not a reason to hold Serena up.
START_TIMEOUT_SECONDS = 20.0
CALL_TIMEOUT_SECONDS = 30.0
# The whole hook fan-out shares one budget, so a crowd of hung plugins costs
# the emitting path one wait rather than one wait each.
HOOK_TOTAL_TIMEOUT_SECONDS = 45.0
MAX_RESULT_BYTES = 256_000
# One ceiling for both directions of mediated file access.
MAX_FILE_BYTES = 1_000_000
MAX_READ_BYTES = MAX_FILE_BYTES
MAX_STDERR_BYTES = 8_000

# Built-in mediated capabilities, checked against the manifest's declared
# filesystem and network permissions rather than its Serena scopes.
_FILE_CAPABILITIES = frozenset({"fs.read", "fs.write"})
_NET_CAPABILITIES = frozenset({"net.fetch"})


class PluginRuntimeError(RuntimeError):
    """The plugin could not be run, or asked for something it may not have."""


@dataclass(frozen=True, slots=True)
class CallResult:
    """What came back from one plugin call, and the receipt that proves it."""

    ok: bool
    result: Any = None
    error: str = ""
    receipt_id: str = ""
    denials: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(slots=True)
class _Denial:
    capability: str
    reason: str


# --------------------------------------------------------------- the guest
#
# This source is handed to a fresh interpreter with `-c`. It imports only the
# standard library and the one approved entrypoint. Keeping it as a string,
# rather than a module on disk, is what stops the child's `sys.path` from ever
# containing Serena's `core/` package.

_GUEST_SOURCE = r'''
import importlib.util, json, sys, sysconfig, types

# Belt and braces with the interpreter's own -I -S. A virtualenv that has
# Serena installed as an editable package leaves a .pth file behind that would
# otherwise put her whole source tree on this path, so anything that is not
# the standard library is dropped before the plugin gets a say. A plugin gets
# the stdlib and its own approved file. Nothing else is importable.
_stdlib = {sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")}
sys.path[:] = [
    _entry
    for _entry in sys.path
    if _entry
    and any(_root and (_entry == _root or _entry.startswith(_root + "/")) for _root in _stdlib)
]

_out = sys.stdout
_in = sys.stdin
_seq = 0


def _send(payload):
    _out.write(json.dumps(payload, default=str) + "\n")
    _out.flush()


def _request(capability, args):
    """Ask the host for something. The host is free to say no."""
    global _seq
    _seq += 1
    _send({"capability": capability, "args": args, "seq": _seq})
    line = _in.readline()
    if not line:
        raise RuntimeError("serena closed the channel")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise PermissionError(reply.get("error") or "refused by serena")
    return reply.get("result")


_config = json.loads(_in.readline())
_entrypoint = _config["entrypoint"]
_allowed = set(_config["handlers"])

api = types.ModuleType("serena_plugin_api")
api.plugin_id = _config["plugin_id"]
api.scope = _config["scope"]
api.read_file = lambda path: _request("fs.read", {"path": path})
api.write_file = lambda path, text: _request("fs.write", {"path": path, "text": text})
api.fetch = lambda url, method="GET": _request("net.fetch", {"url": url, "method": method})
api.capability = lambda name, **args: _request(name, args)
sys.modules["serena_plugin_api"] = api

try:
    _spec = importlib.util.spec_from_file_location("serena_plugin_entry", _entrypoint)
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
except BaseException as exc:
    _send({"ready": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    raise SystemExit(1)

_send({"ready": True})

while True:
    _line = _in.readline()
    if not _line:
        break
    try:
        _message = json.loads(_line)
    except ValueError:
        continue
    if _message.get("op") == "shutdown":
        break
    _name = _message.get("name") or ""
    if _name not in _allowed:
        _send({"done": True, "ok": False, "error": "handler %r is not declared" % _name})
        continue
    _handler = getattr(_module, _name, None)
    if not callable(_handler):
        _send({"done": True, "ok": False, "error": "handler %r is not defined" % _name})
        continue
    try:
        _value = _handler(_message.get("payload") or {})
        _send({"done": True, "ok": True, "result": _value})
    except BaseException as exc:
        _send({"done": True, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
'''


def _contained(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and refuse anything that leaves `root`.

    Both sides are fully resolved first, so a symlink pointing out of the
    plugin's own directory is caught rather than followed.
    """

    real_root = Path(os.path.realpath(root))
    real_candidate = Path(os.path.realpath(candidate))
    try:
        real_candidate.relative_to(real_root)
    except ValueError as exc:
        raise PluginRuntimeError(
            f"{candidate} resolves outside the plugin's scope root {real_root}"
        ) from exc
    return real_candidate


class PluginLoader:
    """Runs approved plugins, and only in the ways they were approved for."""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        *,
        project_root: str | os.PathLike[str] | None = None,
        global_root: str | os.PathLike[str] | None = None,
        python_executable: str | None = None,
        secret_env: Mapping[str, str] | None = None,
        url_policy: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self.registry = registry or PluginRegistry()
        self.project_root = Path(project_root).resolve() if project_root else None
        configured_global = os.environ.get("SERENA_PLUGIN_GLOBAL_ROOT", "").strip()
        self.global_root = Path(global_root or configured_global or DEFAULT_GLOBAL_ROOT)
        self.python_executable = python_executable or sys.executable
        # Where declared `env:` secret references are resolved from. Defaults
        # to Serena's own environment, but a caller can hand in a narrower map
        # so the loader never even sees the rest.
        self._secret_env = dict(secret_env) if secret_env is not None else dict(os.environ)
        # ws-7 owns the real URL/SSRF policy. Until it lands, this is a
        # conservative local check with the same signature, so swapping in the
        # shared authority later is a constructor argument, not a rewrite.
        self._url_policy = url_policy
        self._capabilities: dict[str, Callable[[str, Mapping[str, Any]], Any]] = {}
        self._processes: dict[str, _PluginProcess] = {}

    # ---------------------------------------------------------- capabilities

    def register_capability(
        self, scope: str, handler: Callable[[str, Mapping[str, Any]], Any]
    ) -> None:
        """Let plugins holding `scope` reach one real piece of Serena.

        Nothing is wired by default. An enabled plugin that declared
        `memory.read` still gets refused until Serena explicitly registers a
        `memory.read` handler here, so the manifest scope is necessary but
        never sufficient.
        """

        if scope not in PERMISSION_SCOPES:
            raise PluginRuntimeError(f"unknown permission scope {scope!r}")
        self._capabilities[scope] = handler

    # ----------------------------------------------------------------- load

    def resolve_entrypoint(self, manifest: PluginManifest) -> Path:
        """Where this plugin's code is allowed to live, and nowhere else."""

        kind = manifest.scope_kind
        if not kind:
            raise PluginRuntimeError(
                f"plugin {manifest.id} does not say whether it is project-local or "
                "global; an unscoped plugin is never loaded"
            )
        if kind == "global":
            root = self.global_root / manifest.id
        else:
            declared = Path(manifest.scope_key)
            if self.project_root is None:
                raise PluginRuntimeError(
                    f"plugin {manifest.id} is project-local to {declared}, but this "
                    "loader is not running inside a project"
                )
            if Path(os.path.realpath(declared)) != Path(os.path.realpath(self.project_root)):
                raise PluginRuntimeError(
                    f"plugin {manifest.id} is project-local to {declared} and will not "
                    f"load inside {self.project_root}"
                )
            root = self.project_root
        entrypoint = _contained(root, root / manifest.entrypoint)
        if not entrypoint.is_file():
            raise PluginRuntimeError(f"plugin {manifest.id} entrypoint {entrypoint} is missing")
        return entrypoint

    def _record_scope(self, plugin_id: str) -> tuple[str | None, str | None]:
        """Which scope this loader means when it names a plugin.

        A loader running inside a project means that project's plugin; a
        loader with no project means the global one. Saying so explicitly is
        what stops a project plugin and a global plugin of the same name from
        being mistaken for each other.
        """

        if self.project_root is not None:
            return "project", str(self.project_root)
        return "global", GLOBAL_SCOPE_KEY

    def _approved_manifest(self, plugin_id: str) -> PluginManifest:
        """The manifest, only if it is still the one that was approved."""

        kind, key = self._record_scope(plugin_id)
        record = self.registry.get(plugin_id, scope_kind=kind, scope_key=key)
        if record is None:
            # Fall back to an unqualified lookup so a legacy unscoped row is
            # still found and then refused by the scope check below, rather
            # than looking like it does not exist at all.
            record = self.registry.require(plugin_id)
        state = str(record["state"])
        if state != "enabled":
            raise PluginRuntimeError(
                f"plugin {plugin_id} is {state}; only an enabled plugin can run"
            )
        manifest = validate_manifest(record["manifest"])
        approved = self.registry.approved_manifest_hashes(
            plugin_id,
            scope_kind=str(record["scope_kind"]) or None,
            scope_key=str(record["scope_key"]) or None,
        )
        if manifest.manifest_hash() not in approved:
            raise PluginRuntimeError(
                f"plugin {plugin_id} no longer matches an approved manifest and will "
                "not be loaded"
            )
        return manifest

    def _child_environment(self, manifest: PluginManifest, entrypoint: Path) -> dict[str, str]:
        """Build the child's environment from nothing.

        Serena's own environment is not inherited, so tokens that happen to be
        sitting in it cannot leak into a plugin. There is no PATH on purpose:
        a plugin that tries to shell out finds no binaries.
        """

        env = {
            "PATH": "",
            "HOME": str(entrypoint.parent),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SERENA_PLUGIN_ID": manifest.id,
            "SERENA_PLUGIN_SCOPE": manifest.scope_kind,
        }
        for secret in manifest.secrets:
            name = str(secret.get("name") or "")
            reference = str(secret.get("ref") or "")
            if not name:
                continue
            if reference.startswith("env:"):
                value = self._secret_env.get(reference.removeprefix("env:"))
                if value is None:
                    # Starting anyway would hand the plugin a half-configured
                    # world and let it fail somewhere less obvious. Refuse now.
                    raise PluginRuntimeError(
                        f"plugin {manifest.id} declared the secret {name} as "
                        f"{reference}, and that is not set"
                    )
                env[name] = str(value)
            elif reference.startswith("file:"):
                # Hand over the resolved path, never the contents. Reading it
                # still goes through the mediated fs.read.
                root = self._scope_root(manifest)
                try:
                    env[name] = str(
                        _contained(root, root / reference.removeprefix("file:"))
                    )
                except PluginRuntimeError as exc:
                    raise PluginRuntimeError(
                        f"plugin {manifest.id} declared the secret {name} as "
                        f"{reference}, which does not resolve inside its scope: {exc}"
                    ) from exc
        return env

    def _scope_root(self, manifest: PluginManifest) -> Path:
        if manifest.scope_kind == "global":
            return self.global_root / manifest.id
        if self.project_root is not None:
            return self.project_root
        raise PluginRuntimeError(
            f"plugin {manifest.id} is project-local but this loader is not "
            "running inside a project"
        )

    def load(self, plugin_id: str) -> CallResult:
        """Start one approved, enabled plugin in its own isolated process."""

        started = time.monotonic()
        try:
            manifest = self._approved_manifest(plugin_id)
            entrypoint = self.resolve_entrypoint(manifest)
        except (PluginRuntimeError, KeyError) as exc:
            receipt = self.registry.record_receipt(
                plugin_id, action="load", decision="refused", detail=str(exc)
            )
            return CallResult(False, error=str(exc), receipt_id=receipt)

        self.unload(plugin_id)
        try:
            process = _PluginProcess(
                manifest=manifest,
                entrypoint=entrypoint,
                python_executable=self.python_executable,
                env=self._child_environment(manifest, entrypoint),
            )
            process.start()
        except Exception as exc:  # a broken plugin must not take Serena with it
            detail = f"{type(exc).__name__}: {exc}"
            receipt = self.registry.record_receipt(
                plugin_id, action="load", decision="failed", detail=detail
            )
            return CallResult(False, error=detail, receipt_id=receipt)

        self._processes[plugin_id] = process
        receipt = self.registry.record_receipt(
            plugin_id,
            action="load",
            decision="allowed",
            detail=f"scope={manifest.scope_kind} entrypoint={entrypoint}",
        )
        return CallResult(
            True,
            result={"scope": manifest.scope_kind, "entrypoint": str(entrypoint)},
            receipt_id=receipt,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def unload(self, plugin_id: str) -> bool:
        """Stop a running plugin. Safe to call when it is not running."""

        process = self._processes.pop(plugin_id, None)
        if process is None:
            return False
        process.close()
        self.registry.record_receipt(plugin_id, action="unload", decision="allowed")
        return True

    def close(self) -> None:
        for plugin_id in list(self._processes):
            self.unload(plugin_id)

    # ----------------------------------------------------------------- calls

    def _invoke(
        self,
        plugin_id: str,
        handler: str,
        payload: Mapping[str, Any] | None,
        *,
        action: str,
        timeout: float,
    ) -> CallResult:
        started = time.monotonic()
        try:
            manifest = self._approved_manifest(plugin_id)
        except (PluginRuntimeError, KeyError) as exc:
            # Disable or remove takes effect here, on the very next call.
            self.unload(plugin_id)
            receipt = self.registry.record_receipt(
                plugin_id, action=action, decision="refused", detail=str(exc)
            )
            return CallResult(False, error=str(exc), receipt_id=receipt)

        if handler not in manifest.handler_names:
            detail = f"{handler} is not declared by the approved manifest"
            receipt = self.registry.record_receipt(
                plugin_id, action=action, decision="refused", detail=detail
            )
            return CallResult(False, error=detail, receipt_id=receipt)

        process = self._processes.get(plugin_id)
        if process is None:
            loaded = self.load(plugin_id)
            if not loaded.ok:
                return loaded
            process = self._processes[plugin_id]

        denials: list[_Denial] = []
        try:
            reply = process.call(
                handler,
                dict(payload or {}),
                timeout=timeout,
                serve=lambda capability, args: self._serve(manifest, capability, args, denials),
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.unload(plugin_id)
            receipt = self.registry.record_receipt(
                plugin_id, action=action, decision="failed", detail=detail
            )
            return CallResult(False, error=detail, receipt_id=receipt, duration_ms=_ms(started))

        denial_text = tuple(f"{item.capability}: {item.reason}" for item in denials)
        ok = bool(reply.get("ok"))
        receipt = self.registry.record_receipt(
            plugin_id,
            action=action,
            decision="allowed" if ok else "failed",
            detail=f"handler={handler} denials={len(denial_text)} "
            + (str(reply.get("error") or "")[:300] if not ok else ""),
        )
        return CallResult(
            ok,
            result=reply.get("result"),
            error=str(reply.get("error") or ""),
            receipt_id=receipt,
            denials=denial_text,
            duration_ms=_ms(started),
        )

    def call_tool(
        self, plugin_id: str, tool: str, payload: Mapping[str, Any] | None = None
    ) -> CallResult:
        return self._invoke(
            plugin_id, tool, payload, action="tool.call", timeout=CALL_TIMEOUT_SECONDS
        )

    def call_skill(
        self, plugin_id: str, skill: str, payload: Mapping[str, Any] | None = None
    ) -> CallResult:
        """Run a declared skill by its public name.

        Skills are named for people; the manifest maps that name onto the
        handler the module actually defines.
        """

        try:
            manifest = self._approved_manifest(plugin_id)
        except (PluginRuntimeError, KeyError) as exc:
            receipt = self.registry.record_receipt(
                plugin_id, action="skill.call", decision="refused", detail=str(exc)
            )
            return CallResult(False, error=str(exc), receipt_id=receipt)
        handler = next(
            (
                str(entry.get("handler"))
                for entry in manifest.skills
                if str(entry.get("name")) == skill
            ),
            "",
        )
        if not handler:
            detail = f"{plugin_id} declares no skill named {skill!r}"
            receipt = self.registry.record_receipt(
                plugin_id, action="skill.call", decision="refused", detail=detail
            )
            return CallResult(False, error=detail, receipt_id=receipt)
        return self._invoke(
            plugin_id, handler, payload, action="skill.call", timeout=CALL_TIMEOUT_SECONDS
        )

    def dispatch_hook(
        self, event: str, payload: Mapping[str, Any] | None = None
    ) -> list[tuple[str, CallResult]]:
        """Deliver one event to every enabled plugin that asked for it.

        A plugin that fails, hangs, or was disabled a second ago cannot stop
        the event reaching the others, and cannot stop the caller either.
        """

        if event not in HOOK_EVENTS:
            raise PluginRuntimeError(f"unknown hook event {event!r}")
        results: list[tuple[str, CallResult]] = []
        # One aggregate budget for the whole fan-out. Ten hung plugins must
        # cost the emitter one deadline, not ten, because the caller here is a
        # real Serena path (a finished Fleet run, a completed turn) that is not
        # allowed to stall behind someone's plugin.
        deadline = time.monotonic() + HOOK_TOTAL_TIMEOUT_SECONDS
        for record in self.registry.list(state="enabled"):
            plugin_id = str(record["plugin_id"])
            try:
                manifest = validate_manifest(record["manifest"])
            except Exception:
                continue
            handlers = [
                str(hook.get("handler"))
                for hook in manifest.hooks
                if str(hook.get("event")) == event
            ]
            for handler in handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f"{event} fan-out ran out of time before this plugin"
                    receipt = self.registry.record_receipt(
                        plugin_id,
                        action=f"hook:{event}",
                        decision="skipped",
                        detail=detail,
                    )
                    results.append(
                        (plugin_id, CallResult(False, error=detail, receipt_id=receipt))
                    )
                    continue
                results.append(
                    (
                        plugin_id,
                        self._invoke(
                            plugin_id,
                            handler,
                            payload,
                            action=f"hook:{event}",
                            timeout=min(CALL_TIMEOUT_SECONDS, remaining),
                        ),
                    )
                )
        return results

    def health_check(self, plugin_id: str) -> CallResult:
        """Run the declared health check and record what it said."""

        try:
            manifest = self._approved_manifest(plugin_id)
        except (PluginRuntimeError, KeyError) as exc:
            receipt = self.registry.record_receipt(
                plugin_id, action="health", decision="refused", detail=str(exc)
            )
            return CallResult(False, error=str(exc), receipt_id=receipt)
        check = str(manifest.health.get("check") or "")
        if not check:
            detail = f"{plugin_id} declares no health check"
            receipt = self.registry.record_receipt(
                plugin_id, action="health", decision="refused", detail=detail
            )
            return CallResult(False, error=detail, receipt_id=receipt)
        outcome = self._invoke(
            plugin_id, check, {}, action="health", timeout=CALL_TIMEOUT_SECONDS
        )
        with _suppressed():
            self.registry.record_health(
                plugin_id,
                healthy=outcome.ok,
                detail=outcome.error or str(outcome.result or "")[:400],
            )
        return outcome

    def ui_contributions(self, surface: str | None = None) -> list[dict[str, Any]]:
        """Declared UI contributions from enabled plugins only.

        This returns data for Serena's own surfaces to render. A plugin never
        gets to hand back markup or code to execute.
        """

        found: list[dict[str, Any]] = []
        for record in self.registry.list(state="enabled"):
            try:
                manifest = validate_manifest(record["manifest"])
            except Exception:
                continue
            for contribution in manifest.ui:
                if surface is not None and str(contribution.get("surface")) != surface:
                    continue
                found.append(
                    {
                        "plugin_id": manifest.id,
                        "surface": str(contribution.get("surface") or ""),
                        "title": str(contribution.get("title") or ""),
                        "scope": manifest.scope_kind,
                    }
                )
        return found

    # ---------------------------------------------------------- mediation

    def _serve(
        self,
        manifest: PluginManifest,
        capability: str,
        args: Mapping[str, Any],
        denials: list[_Denial],
    ) -> dict[str, Any]:
        """Answer one capability request from a running plugin.

        Approval and lifecycle state are re-read here, on every request, for
        file and network reach as much as for Serena capabilities. A plugin
        disabled or removed while it is mid-call loses the filesystem at the
        same moment it loses everything else.
        """

        try:
            try:
                live = self._approved_manifest(manifest.id)
            except (PluginRuntimeError, KeyError) as exc:
                raise PluginRuntimeError(
                    f"{manifest.id} may no longer act: {exc}"
                ) from exc
            if live.manifest_hash() != manifest.manifest_hash():
                raise PluginRuntimeError(
                    f"{manifest.id} changed its approved manifest mid-call"
                )
            if capability in _FILE_CAPABILITIES:
                return {"ok": True, "result": self._serve_file(live, capability, args)}
            if capability in _NET_CAPABILITIES:
                return {"ok": True, "result": self._serve_network(live, args)}
            return {"ok": True, "result": self._serve_scope(live, capability, args)}
        except PluginRuntimeError as exc:
            denials.append(_Denial(capability, str(exc)))
            self.registry.record_receipt(
                manifest.id,
                action=f"capability:{capability}",
                decision="denied",
                detail=str(exc),
            )
            return {"ok": False, "error": str(exc)}

    def _declared_file(self, manifest: PluginManifest, raw: object) -> Path:
        """Resolve a path a plugin asked for, or refuse it.

        A plugin may reach its own directory and whatever it declared in
        `permissions.filesystem`. The manifest validator already refused any
        declaration covering Serena's identity or authority, so this only has
        to enforce what was declared, plus containment.
        """

        candidate = str(raw or "").strip()
        if not candidate:
            raise PluginRuntimeError("no path given")
        root = self._scope_root(manifest)
        target = _contained(root, root / candidate.lstrip("/"))
        allowed_roots = [_contained(root, root / entry) for entry in _declared_paths(manifest)]
        own = Path(os.path.realpath(root / manifest.entrypoint)).parent
        allowed_roots.append(own)
        for allowed in allowed_roots:
            if target == allowed or allowed in target.parents:
                return target
        raise PluginRuntimeError(
            f"{candidate} is outside the filesystem permissions this plugin declared"
        )

    def _serve_file(
        self, manifest: PluginManifest, capability: str, args: Mapping[str, Any]
    ) -> Any:
        target = self._declared_file(manifest, args.get("path"))
        if capability == "fs.read":
            if not target.is_file():
                raise PluginRuntimeError(f"{target} is not a readable file")
            if target.stat().st_size > MAX_READ_BYTES:
                raise PluginRuntimeError(f"{target} is larger than a plugin may read")
            return target.read_text(encoding="utf-8", errors="replace")
        text = str(args.get("text") or "")
        if len(text.encode("utf-8")) > MAX_READ_BYTES:
            raise PluginRuntimeError("refusing an oversized plugin write")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {"written": len(text)}

    def _serve_network(self, manifest: PluginManifest, args: Mapping[str, Any]) -> Any:
        url = str(args.get("url") or "").strip()
        hosts = {str(host).lower() for host in (manifest.permissions.get("network") or [])}
        if not hosts:
            raise PluginRuntimeError("this plugin declared no network permissions")
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme != "https":
            raise PluginRuntimeError("plugin network access is https only")
        host = (parts.hostname or "").lower()
        if host not in hosts:
            raise PluginRuntimeError(f"{host or url!r} is not a host this plugin declared")
        if self._url_policy is not None:
            allowed, reason = self._url_policy(url)
            if not allowed:
                raise PluginRuntimeError(reason or "refused by Serena's URL policy")
            return {"url": url, "allowed": True}
        # Without the shared policy authority wired in, fail closed rather
        # than open a socket this module cannot fully vet.
        raise PluginRuntimeError(
            "no URL policy is wired into this loader, so outbound plugin requests "
            "are refused"
        )

    def _serve_scope(
        self, manifest: PluginManifest, capability: str, args: Mapping[str, Any]
    ) -> Any:
        if capability not in PERMISSION_SCOPES:
            raise PluginRuntimeError(f"unknown capability {capability!r}")
        # Read the live registry, not the manifest we started with, so a
        # plugin disabled mid-call loses the scope immediately.
        held = self.registry.enabled_scopes(manifest.id)
        if capability not in held:
            raise PluginRuntimeError(f"{manifest.id} does not hold {capability}")
        handler = self._capabilities.get(capability)
        if handler is None:
            raise PluginRuntimeError(f"Serena has no handler registered for {capability}")
        return handler(manifest.id, dict(args))


def _declared_paths(manifest: PluginManifest) -> list[str]:
    return [str(entry) for entry in (manifest.permissions.get("filesystem") or []) if entry]


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class _suppressed:
    """A tiny local `suppress(Exception)`; a bookkeeping failure is not fatal."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


class _PluginProcess:
    """One plugin's isolated interpreter and the pipe Serena talks to it on."""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        entrypoint: Path,
        python_executable: str,
        env: Mapping[str, str],
    ) -> None:
        self.manifest = manifest
        self.entrypoint = entrypoint
        self.python_executable = python_executable
        self.env = dict(env)
        self.process: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - owned until close()

    def start(self) -> None:
        self.process = subprocess.Popen(
            # -I drops PYTHONPATH and the user site directory; -S stops
            # site-packages (and any editable-install .pth pointing at Serena's
            # own source tree) from being added at all.
            [self.python_executable, "-I", "-S", "-c", _GUEST_SOURCE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=self.env,
            cwd=str(self.entrypoint.parent),
            start_new_session=True,
        )
        self._write(
            {
                "entrypoint": str(self.entrypoint),
                "handlers": list(self.manifest.handler_names),
                "plugin_id": self.manifest.id,
                "scope": self.manifest.scope_kind,
            }
        )
        ready = self._read_json(deadline=time.monotonic() + START_TIMEOUT_SECONDS)
        if not ready.get("ready"):
            detail = str(ready.get("error") or "plugin did not start")
            self.close()
            raise PluginRuntimeError(detail)

    def call(
        self,
        handler: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        serve: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        self._write({"op": "call", "name": handler, "payload": dict(payload)})
        while True:
            message = self._read_json(deadline=deadline)
            if message.get("done"):
                return message
            capability = message.get("capability")
            if capability:
                answer = serve(str(capability), message.get("args") or {})
                answer["seq"] = message.get("seq")
                self._write(answer)
                continue

    def close(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            with _suppressed():
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.write(b'{"op":"shutdown"}\n')
                    process.stdin.flush()
            with _suppressed():
                process.wait(timeout=2)
            if process.poll() is None:
                with _suppressed():
                    process.kill()
                with _suppressed():
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with _suppressed():
                        stream.close()
        with _suppressed():
            self._stderr.close()

    def stderr_tail(self) -> str:
        with _suppressed():
            self._stderr.seek(0)
            return self._stderr.read()[-MAX_STDERR_BYTES:].decode("utf-8", "replace")
        return ""

    # -- pipe plumbing ----------------------------------------------------

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise PluginRuntimeError("the plugin process is not running")
        line = json.dumps(payload, default=str).encode("utf-8") + b"\n"
        try:
            process.stdin.write(line)
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise PluginRuntimeError("the plugin closed its channel") from exc

    def _read_json(self, *, deadline: float) -> dict[str, Any]:
        """Read one JSON line, or give up on the plugin.

        `select` on the raw pipe is what keeps a plugin that decides to sleep
        forever from becoming Serena's problem.
        """

        process = self.process
        if process is None or process.stdout is None:
            raise PluginRuntimeError("the plugin process is not running")
        stream = process.stdout
        while True:
            if b"\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition(b"\n")
                if not line.strip():
                    continue
                if len(line) > MAX_RESULT_BYTES:
                    raise PluginRuntimeError("the plugin sent an oversized message")
                try:
                    parsed = json.loads(line.decode("utf-8", "replace"))
                except ValueError as exc:
                    raise PluginRuntimeError("the plugin sent something that was not JSON") from exc
                if not isinstance(parsed, dict):
                    raise PluginRuntimeError("the plugin sent a non-object message")
                return parsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginRuntimeError("the plugin did not answer in time")
            ready, _, _ = select.select([stream], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(stream.fileno(), 65_536)
            if not chunk:
                tail = self.stderr_tail().strip()
                raise PluginRuntimeError(
                    "the plugin exited without answering" + (f": {tail[-400:]}" if tail else "")
                )
            self._buffer += chunk
            if len(self._buffer) > MAX_RESULT_BYTES * 2:
                raise PluginRuntimeError("the plugin sent an oversized message")


# ------------------------------------------------------- the emit-point seam
#
# The hook events are declared in `core.serena_plugins.HOOK_EVENTS`, but the
# places that can actually raise them live in other people's modules: the
# Fleet supervisor, the resident brain's turn loop, Memory v2's proposal
# engine, the notification authority, and the scheduler tick. Rather than
# reach into those files, this is the one call each of them makes.
#
# It is deliberately impossible to get wrong: it never raises, it never
# blocks past the shared hook budget, and if no plugin is enabled it costs one
# empty database read.

_default_loader: PluginLoader | None = None
_hook_state = threading.local()


def _shared_url_policy(url: str) -> tuple[bool, str]:
    """Apply Serena's DNS and private-network policy after manifest checks."""

    try:
        from urllib.parse import urlsplit

        from core.security_policy import URLPolicy

        hostname = str(urlsplit(url).hostname or "").rstrip(".").lower()
        URLPolicy(allowed_domains=(hostname,)).validate(url)
    except Exception as error:
        return False, str(error) or "refused by Serena's URL policy"
    return True, "validated by Serena's URL policy"


def default_loader(*, project_root: str | os.PathLike[str] | None = None) -> PluginLoader:
    """The process-wide loader, built on first use.

    Built lazily so importing this module never opens a database or starts a
    process. A caller inside a project passes its root so project-local
    plugins resolve to that checkout and no other.
    """

    global _default_loader
    if _default_loader is None or (
        project_root is not None
        and str(_default_loader.project_root or "") != str(Path(project_root).resolve())
    ):
        _default_loader = PluginLoader(
            project_root=project_root,
            url_policy=_shared_url_policy,
        )
    return _default_loader


def emit_plugin_hook(
    event: str,
    payload: Mapping[str, Any] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> list[tuple[str, CallResult]]:
    """Tell any enabled plugin that `event` happened.

    Safe to call from anywhere on any thread. A plugin that is broken, hung,
    or hostile cannot raise into the caller and cannot hold the caller past
    `HOOK_TOTAL_TIMEOUT_SECONDS`, because whatever just happened, Serena's own
    path matters more than a plugin's opinion about it.
    """

    active = set(getattr(_hook_state, "active", ()))
    if event in active:
        return []
    active.add(event)
    _hook_state.active = active
    try:
        return default_loader(project_root=project_root).dispatch_hook(event, payload)
    except Exception:
        return []
    finally:
        active.discard(event)


def reset_default_loader() -> None:
    """Drop the cached loader, shutting down anything it was running."""

    global _default_loader
    if _default_loader is not None:
        with _suppressed():
            _default_loader.close()
    _default_loader = None


__all__ = [
    "CallResult",
    "DEFAULT_GLOBAL_ROOT",
    "GLOBAL_SCOPE_KEY",
    "HOOK_TOTAL_TIMEOUT_SECONDS",
    "PluginLoader",
    "PluginRuntimeError",
    "default_loader",
    "emit_plugin_hook",
    "reset_default_loader",
]
