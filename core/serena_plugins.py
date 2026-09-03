"""Typed plugin/skill manifests and their install lifecycle.

Serena is extensible in exactly one direction: a manifest declares, up front,
what a plugin wants to add and what it needs permission to touch. Nothing here
imports, executes, downloads, or installs anything on its own. The registry
records intent and approval; running the code is a separate, deliberate step
outside this module.

Two rules shape the whole design.

Autonomous installation is not available. Every transition that grants a
plugin more reach (staging a change, installing it, enabling it) requires an
explicit call with an actor recorded against it, and staged changes are held
as a visible diff until approved. Serena does not get to decide on her own
that she should have a new capability.

Secrets are declared by reference, never by value. A manifest naming an
environment variable or a credentials file is fine; a manifest carrying an
actual token is rejected, so a plugin file cannot become a place secrets
quietly live.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "serena" / "plugins.sqlite3"

# staged -> installed -> enabled <-> disabled -> removed
LIFECYCLE_STATES = ("staged", "installed", "enabled", "disabled", "removed")
_TRANSITIONS: dict[str, frozenset[str]] = {
    "staged": frozenset({"installed", "removed"}),
    "installed": frozenset({"enabled", "removed"}),
    "enabled": frozenset({"disabled", "removed"}),
    "disabled": frozenset({"enabled", "removed"}),
    "removed": frozenset(),
}

# Every scope a manifest may request. Unknown scopes are refused rather than
# ignored, so a typo cannot silently become "no permission requested".
PERMISSION_SCOPES = frozenset(
    {
        "memory.read",
        "memory.propose",
        "chat.read",
        "chat.append",
        "fleet.read",
        "notify.send",
        "schedule.register",
        "tool.invoke",
        "webhook.receive",
    }
)
# Scopes that can act on Raghav's behalf or reach outside the machine. These
# require approval even when the plugin is otherwise installed.
SENSITIVE_SCOPES = frozenset(
    {"memory.propose", "chat.append", "notify.send", "webhook.receive"}
)
# Where a plugin lives and whose reach it borrows. A project plugin is loaded
# only while Serena is working inside that one checkout; a global plugin is
# part of Serena everywhere. There is deliberately no default: an installation
# that never said which one it wanted does not get to guess, and the loader
# refuses it.
PLUGIN_SCOPE_KINDS = frozenset({"project", "global"})
GLOBAL_SCOPE_KEY = "serena.global"

UI_SURFACES = frozenset({"fleet_tab", "chat_sidebar", "coding_panel", "voice_overlay"})
HOOK_EVENTS = frozenset(
    {
        "fleet.run.completed",
        "fleet.run.failed",
        "chat.turn.completed",
        "memory.proposal.created",
        "notification.sent",
        "schedule.tick",
    }
)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,4}$")
_NAME = re.compile(r"^[A-Za-z0-9][\w .+-]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?$")
_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")
_SECRET_REF = re.compile(r"^(?:env:[A-Z][A-Z0-9_]{2,63}|file:[\w./-]{3,200})$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "name",
        "version",
        "description",
        "entrypoint",
        "scope",
        "tools",
        "skills",
        "ui",
        "hooks",
        "permissions",
        "secrets",
        "health",
    }
)
# A manifest must not be a place to smuggle a credential.
_FORBIDDEN_SECRET_KEYS = frozenset({"value", "token", "secret", "password", "key"})
# Paths a plugin may never declare, regardless of approval.
#
# The first group is the usual "do not touch the machine" set. The second is
# the part that matters more: who Serena is and what she is allowed to do. A
# plugin that could edit Persona.md, the policy file, an authority module, or
# this registry itself could quietly rewrite her identity or hand itself more
# permission than it was granted, so those are refused before approval is even
# on the table.
_PROTECTED_PREFIXES = (
    ".git/",
    ".ssh/",
    "systemd/",
    "config/serena-policy.json",
    "Persona.md",
    "Tooling.md",
    "cli.py",
    "core/serena_plugins.py",
    "core/plugin_loader.py",
    "core/work_authority.py",
    "core/memory_authority.py",
    "core/notification_authority.py",
    "core/control_plane.py",
    "core/fleet_isolation.py",
)


class PluginManifestError(ValueError):
    """The manifest was not a valid, safe Serena plugin declaration."""


class PluginLifecycleError(RuntimeError):
    """The requested lifecycle transition is not allowed."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    description: str
    entrypoint: str
    scope: dict[str, Any] = field(default_factory=dict)
    tools: tuple[dict[str, Any], ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    ui: tuple[dict[str, Any], ...] = ()
    hooks: tuple[dict[str, Any], ...] = ()
    permissions: dict[str, Any] = field(default_factory=dict)
    secrets: tuple[dict[str, Any], ...] = ()
    health: dict[str, Any] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(self.permissions.get("scopes") or ())

    @property
    def sensitive_scopes(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.scopes) & SENSITIVE_SCOPES))

    @property
    def scope_kind(self) -> str:
        """"project", "global", or "" when the manifest never said.

        An empty answer is not a default. The loader treats it as unloadable.
        """

        kind = str(self.scope.get("kind") or "")
        return kind if kind in PLUGIN_SCOPE_KINDS else ""

    @property
    def scope_key(self) -> str:
        if self.scope_kind == "global":
            return GLOBAL_SCOPE_KEY
        if self.scope_kind == "project":
            return str(self.scope.get("project_root") or "")
        return ""

    @property
    def handler_names(self) -> tuple[str, ...]:
        """Every function name this manifest is allowed to call, and no other.

        The loader dispatches by name, so this is the complete list of entry
        points the plugin's module may expose to Serena.
        """

        names = {str(tool.get("name")) for tool in self.tools}
        names |= {str(skill.get("handler")) for skill in self.skills}
        names |= {str(hook.get("handler")) for hook in self.hooks}
        if self.health.get("check"):
            names.add(str(self.health["check"]))
        return tuple(sorted(name for name in names if name))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("tools", "skills", "ui", "hooks", "secrets"):
            payload[key] = list(payload[key])
        return payload

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)

    def manifest_hash(self) -> str:
        """A stable fingerprint of exactly what was approved.

        The loader compares this against the approved stage before it will run
        anything, so editing the stored manifest after approval cannot widen
        what the plugin is allowed to do.
        """

        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


def _text(value: object, name: str, pattern: re.Pattern[str] | None = None, limit: int = 200) -> str:
    clean = " ".join(str(value or "").split())
    if not clean or len(clean) > limit:
        raise PluginManifestError(f"plugin {name} is missing or too long")
    if pattern is not None and not pattern.match(clean):
        raise PluginManifestError(f"plugin {name} is not a valid {name}: {clean[:60]!r}")
    return clean


def _objects(value: object, name: str, limit: int = 32) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PluginManifestError(f"plugin {name} must be a list")
    if len(value) > limit:
        raise PluginManifestError(f"plugin {name} declares too many entries")
    for entry in value:
        if not isinstance(entry, dict):
            raise PluginManifestError(f"plugin {name} entries must be objects")
    return tuple(dict(entry) for entry in value)


def _safe_relative_path(raw: object, name: str) -> str:
    candidate = str(raw or "").strip().replace("\\", "/")
    if not candidate:
        raise PluginManifestError(f"plugin {name} is required")
    if candidate.startswith("/") or candidate.startswith("~") or ".." in candidate.split("/"):
        raise PluginManifestError(f"plugin {name} must be a relative path inside the repository")
    if re.match(r"^[A-Za-z]:", candidate):
        raise PluginManifestError(f"plugin {name} must not be an absolute path")
    lowered = candidate.lower()
    for prefix in _PROTECTED_PREFIXES:
        if lowered == prefix.lower() or lowered.startswith(prefix.lower()):
            raise PluginManifestError(f"plugin {name} targets a protected path: {candidate}")
    return candidate


def _validate_scope(raw: object) -> dict[str, Any]:
    """Validate where a plugin claims to live.

    A manifest may omit this, and older approved manifests do, but omitting it
    is not the same as choosing. `PluginLoader` refuses to run a plugin whose
    scope is empty, so the only way to actually execute is to say plainly
    whether this belongs to one project or to Serena everywhere.
    """

    if raw is None or raw == {}:
        # An older manifest that predates scopes round-trips as an empty
        # object. Treat that exactly like "never said", so it keeps validating
        # and keeps being unloadable.
        return {}
    if not isinstance(raw, dict):
        raise PluginManifestError("plugin scope must be an object")
    unknown = set(raw) - {"kind", "project_root"}
    if unknown:
        raise PluginManifestError(
            "plugin scope has unknown field(s): " + ", ".join(sorted(unknown))
        )
    kind = str(raw.get("kind") or "").strip()
    if kind not in PLUGIN_SCOPE_KINDS:
        raise PluginManifestError(
            "plugin scope kind must be 'project' or 'global', not " + repr(kind)
        )
    if kind == "global":
        if raw.get("project_root"):
            raise PluginManifestError("a global plugin scope must not name a project_root")
        return {"kind": "global"}

    root = str(raw.get("project_root") or "").strip()
    if not root:
        raise PluginManifestError("a project plugin scope must name its project_root")
    if not root.startswith("/") or "~" in root or ".." in root.split("/"):
        raise PluginManifestError(
            "plugin scope project_root must be an absolute path without .. or ~"
        )
    return {"kind": "project", "project_root": root.rstrip("/") or "/"}


def validate_manifest(value: object) -> PluginManifest:
    """Parse and strictly validate one plugin manifest."""

    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PluginManifestError("plugin manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PluginManifestError("plugin manifest must be a JSON object")

    unknown = set(value) - MANIFEST_KEYS
    if unknown:
        raise PluginManifestError(
            "plugin manifest has unknown field(s): " + ", ".join(sorted(unknown))
        )
    if int(value.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise PluginManifestError("plugin manifest has an unsupported schema version")

    identifier = _text(value.get("id"), "id", _PLUGIN_ID, 64)
    name = _text(value.get("name"), "name", _NAME, 64)
    version = _text(value.get("version"), "version", _VERSION, 32)
    description = _text(value.get("description"), "description", None, 500)
    entrypoint = _safe_relative_path(value.get("entrypoint"), "entrypoint")

    scope = _validate_scope(value.get("scope"))

    tools = _objects(value.get("tools"), "tools")
    for tool in tools:
        _text(tool.get("name"), "tool name", _TOOL_NAME, 48)
        _text(tool.get("description"), "tool description", None, 300)
        for scope_name in tool.get("scopes") or []:
            if scope_name not in PERMISSION_SCOPES:
                raise PluginManifestError(f"tool declares an unknown scope: {scope_name}")

    skills = _objects(value.get("skills"), "skills", 32)
    for skill in skills:
        _text(skill.get("name"), "skill name", _TOOL_NAME, 48)
        _text(skill.get("description"), "skill description", None, 300)
        _text(skill.get("handler"), "skill handler", _TOOL_NAME, 48)
        for scope_name in skill.get("scopes") or []:
            if scope_name not in PERMISSION_SCOPES:
                raise PluginManifestError(f"skill declares an unknown scope: {scope_name}")

    ui = _objects(value.get("ui"), "ui", 16)
    for contribution in ui:
        surface = str(contribution.get("surface") or "")
        if surface not in UI_SURFACES:
            raise PluginManifestError(f"plugin declares an unknown UI surface: {surface}")
        _text(contribution.get("title"), "UI title", None, 80)

    hooks = _objects(value.get("hooks"), "hooks", 16)
    for hook in hooks:
        event = str(hook.get("event") or "")
        if event not in HOOK_EVENTS:
            raise PluginManifestError(f"plugin declares an unknown hook event: {event}")
        _text(hook.get("handler"), "hook handler", _TOOL_NAME, 48)

    permissions = value.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise PluginManifestError("plugin permissions must be an object")
    unknown_permission = set(permissions) - {"scopes", "filesystem", "network"}
    if unknown_permission:
        raise PluginManifestError(
            "plugin permissions has unknown field(s): " + ", ".join(sorted(unknown_permission))
        )
    scopes = permissions.get("scopes") or []
    if not isinstance(scopes, list):
        raise PluginManifestError("plugin permission scopes must be a list")
    for scope_name in scopes:
        if scope_name not in PERMISSION_SCOPES:
            raise PluginManifestError(
                f"plugin requests an unknown permission scope: {scope_name}"
            )
    if len(set(scopes)) != len(scopes):
        raise PluginManifestError("plugin permission scopes contain duplicates")

    filesystem = permissions.get("filesystem") or []
    if not isinstance(filesystem, list):
        raise PluginManifestError("plugin filesystem permissions must be a list")
    for entry in filesystem:
        _safe_relative_path(entry, "filesystem permission")

    network = permissions.get("network") or []
    if not isinstance(network, list):
        raise PluginManifestError("plugin network permissions must be a list")
    for host in network:
        candidate = str(host or "").strip().lower()
        if candidate in {"*", "0.0.0.0", "any"} or "*" in candidate:
            raise PluginManifestError("plugin network permissions must not use a wildcard host")
        if not _HOSTNAME.match(candidate):
            raise PluginManifestError(f"plugin network permission is not a hostname: {host!r}")

    secrets = _objects(value.get("secrets"), "secrets", 16)
    for secret in secrets:
        leaked = sorted(set(secret) & _FORBIDDEN_SECRET_KEYS)
        if leaked:
            raise PluginManifestError(
                "plugin secrets must be declared by reference, not by value "
                "(remove: " + ", ".join(leaked) + ")"
            )
        _text(secret.get("name"), "secret name", re.compile(r"^[A-Z][A-Z0-9_]{2,63}$"), 64)
        reference = str(secret.get("ref") or "").strip()
        if not _SECRET_REF.match(reference):
            raise PluginManifestError(
                "plugin secret ref must look like env:NAME or file:relative/path"
            )
        if reference.startswith("file:"):
            _safe_relative_path(reference.removeprefix("file:"), "secret ref")

    health = value.get("health") or {}
    if not isinstance(health, dict):
        raise PluginManifestError("plugin health must be an object")
    unknown_health = set(health) - {"check", "interval_seconds"}
    if unknown_health:
        raise PluginManifestError(
            "plugin health has unknown field(s): " + ", ".join(sorted(unknown_health))
        )
    if health:
        _text(health.get("check"), "health check", _TOOL_NAME, 48)
        interval = health.get("interval_seconds", 300)
        if not isinstance(interval, int) or not 30 <= interval <= 86_400:
            raise PluginManifestError(
                "plugin health interval_seconds must be an integer between 30 and 86400"
            )

    return PluginManifest(
        id=identifier,
        name=name,
        version=version,
        description=description,
        entrypoint=entrypoint,
        scope=scope,
        tools=tools,
        skills=skills,
        ui=ui,
        hooks=hooks,
        permissions={
            "scopes": list(scopes),
            "filesystem": [str(item).strip() for item in filesystem],
            "network": [str(item).strip().lower() for item in network],
        },
        secrets=secrets,
        health=dict(health),
    )


def diff_manifests(current: PluginManifest | None, proposed: PluginManifest) -> dict[str, Any]:
    """A human-readable staged change, focused on what gains reach."""

    before = current.to_dict() if current is not None else {}
    after = proposed.to_dict()
    changed = sorted(
        key for key in MANIFEST_KEYS if before.get(key) != after.get(key) and key != "schema_version"
    )
    old_scopes = set(before.get("permissions", {}).get("scopes") or [])
    new_scopes = set(after.get("permissions", {}).get("scopes") or [])
    old_hosts = set(before.get("permissions", {}).get("network") or [])
    new_hosts = set(after.get("permissions", {}).get("network") or [])
    added_scopes = sorted(new_scopes - old_scopes)
    old_reach = str((before.get("scope") or {}).get("kind") or "")
    new_reach = str((after.get("scope") or {}).get("kind") or "")
    # Moving a plugin out of one project and into all of Serena is a real
    # widening even when it asks for no new permission scope at all.
    widens_scope = new_reach == "global" and old_reach == "project"
    return {
        "is_new": current is None,
        "changed_fields": changed,
        "added_scopes": added_scopes,
        "removed_scopes": sorted(old_scopes - new_scopes),
        "added_network_hosts": sorted(new_hosts - old_hosts),
        "scope_before": old_reach,
        "scope_after": new_reach,
        "widens_scope": widens_scope,
        "escalates_privilege": bool(
            (set(added_scopes) & SENSITIVE_SCOPES) or (new_hosts - old_hosts) or widens_scope
        ),
        "before": before,
        "after": after,
    }


def _scope_of_json(manifest_json: str) -> tuple[str, str]:
    """Recover (scope_kind, scope_key) from a stored manifest, forgivingly."""

    try:
        manifest = validate_manifest(manifest_json)
    except PluginManifestError:
        return "", ""
    return manifest.scope_kind, manifest.scope_key


class PluginAmbiguityError(PluginLifecycleError):
    """One plugin id exists in more than one scope, so the caller must say which."""


class PluginRegistry:
    """Durable record of staged, installed, and enabled plugins."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_PLUGIN_DB_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH).expanduser()
        self._initialize()

    def stage(
        self,
        manifest: object,
        *,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Record a proposed manifest as a visible, unapproved staged change."""

        parsed = validate_manifest(manifest)
        actor_name = " ".join(str(actor or "").split())
        if not actor_name:
            raise PluginLifecycleError("staging a plugin change requires a named actor")
        # Compare against the plugin in this manifest's own scope. A global
        # plugin of the same name is a different plugin, not a previous
        # version of this one.
        current = self.get(
            parsed.id, scope_kind=parsed.scope_kind, scope_key=parsed.scope_key
        )
        current_manifest = (
            validate_manifest(current["manifest"]) if current is not None else None
        )
        change = diff_manifests(current_manifest, parsed)
        stage_id = str(uuid.uuid4())
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO plugin_stages(
                    stage_id, plugin_id, scope_kind, scope_key, manifest_json,
                    diff_json, state, actor, reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    stage_id,
                    parsed.id,
                    parsed.scope_kind,
                    parsed.scope_key,
                    parsed.canonical(),
                    json.dumps(change, default=str)[:64_000],
                    actor_name,
                    " ".join(str(reason or "").split())[:500],
                    now,
                    now,
                ),
            )
        return {
            "stage_id": stage_id,
            "plugin_id": parsed.id,
            "scope_kind": parsed.scope_kind,
            "scope_key": parsed.scope_key,
            "state": "pending",
            "diff": change,
        }

    def pending_stages(self, plugin_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["state = 'pending'"]
        params: list[object] = []
        if plugin_id:
            clauses.append("plugin_id = ?")
            params.append(plugin_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plugin_stages WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at",
                tuple(params),
            ).fetchall()
        return [_stage_dict(row) for row in rows]

    def approve_stage(self, stage_id: str, *, actor: str) -> dict[str, Any]:
        """Approve a staged manifest, moving the plugin to `installed`.

        Approval is the only way a manifest becomes real, and it is never
        implicit: a plugin cannot install itself, and Serena cannot approve on
        Raghav's behalf without an actor being recorded.
        """

        actor_name = " ".join(str(actor or "").split())
        if not actor_name:
            raise PluginLifecycleError("approving a plugin change requires a named actor")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM plugin_stages WHERE stage_id = ?", (stage_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown plugin stage {stage_id}")
            if str(row["state"]) != "pending":
                raise PluginLifecycleError(
                    f"plugin stage {stage_id} is already {row['state']}"
                )
            manifest = validate_manifest(str(row["manifest_json"]))
            target_state = "installed"
            connection.execute(
                """
                INSERT INTO plugins(
                    plugin_id, scope_kind, scope_key, name, version, manifest_json,
                    state, health_state, installed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?)
                ON CONFLICT(plugin_id, scope_kind, scope_key) DO UPDATE SET
                    name = excluded.name, version = excluded.version,
                    manifest_json = excluded.manifest_json, state = excluded.state,
                    installed_by = excluded.installed_by, updated_at = excluded.updated_at
                """,
                (
                    manifest.id,
                    manifest.scope_kind,
                    manifest.scope_key,
                    manifest.name,
                    manifest.version,
                    manifest.canonical(),
                    target_state,
                    actor_name,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE plugin_stages SET state = 'approved', approved_by = ?, updated_at = ? "
                "WHERE stage_id = ?",
                (actor_name, now, stage_id),
            )
            self._log(
                connection,
                manifest.id,
                "approved",
                actor_name,
                target_state,
                now,
                scope_kind=manifest.scope_kind,
                scope_key=manifest.scope_key,
            )
        return self.require(
            manifest.id, scope_kind=manifest.scope_kind, scope_key=manifest.scope_key
        )

    def reject_stage(self, stage_id: str, *, actor: str, reason: str = "") -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE plugin_stages SET state = 'rejected', approved_by = ?, "
                "reason = ?, updated_at = ? WHERE stage_id = ? AND state = 'pending'",
                (
                    " ".join(str(actor or "").split())[:120],
                    " ".join(str(reason or "").split())[:500],
                    now,
                    stage_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(f"no pending plugin stage {stage_id}")

    def transition(
        self,
        plugin_id: str,
        target: str,
        *,
        actor: str,
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any]:
        """Move a plugin through its lifecycle, refusing illegal jumps."""

        if target not in LIFECYCLE_STATES:
            raise PluginLifecycleError(f"unknown plugin lifecycle state {target!r}")
        actor_name = " ".join(str(actor or "").split())
        if not actor_name:
            raise PluginLifecycleError("a plugin lifecycle change requires a named actor")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._rows_for(connection, plugin_id, scope_kind, scope_key)
            if not rows:
                raise KeyError(f"unknown plugin {plugin_id}")
            if len(rows) > 1:
                raise PluginAmbiguityError(
                    f"plugin {plugin_id} exists in more than one scope; "
                    "name the scope you mean"
                )
            row = rows[0]
            found_kind = str(row["scope_kind"])
            found_key = str(row["scope_key"])
            current = str(row["state"])
            if target not in _TRANSITIONS[current]:
                raise PluginLifecycleError(
                    f"plugin {plugin_id} cannot move from {current} to {target}"
                )
            if target == "enabled":
                manifest = validate_manifest(str(row["manifest_json"]))
                if manifest.sensitive_scopes:
                    approved = connection.execute(
                        "SELECT 1 FROM plugin_stages WHERE plugin_id = ? AND state = 'approved' "
                        "AND manifest_json = ? AND scope_kind = ? AND scope_key = ? LIMIT 1",
                        (plugin_id, str(row["manifest_json"]), found_kind, found_key),
                    ).fetchone()
                    if approved is None:
                        raise PluginLifecycleError(
                            f"plugin {plugin_id} requests sensitive scopes "
                            f"({', '.join(manifest.sensitive_scopes)}) and needs an approved "
                            "staged manifest before it can be enabled"
                        )
            connection.execute(
                "UPDATE plugins SET state = ?, updated_at = ? "
                "WHERE plugin_id = ? AND scope_kind = ? AND scope_key = ?",
                (target, now, plugin_id, found_kind, found_key),
            )
            self._log(
                connection,
                plugin_id,
                target,
                actor_name,
                target,
                now,
                scope_kind=found_kind,
                scope_key=found_key,
            )
        return self.require(plugin_id, scope_kind=found_kind, scope_key=found_key)

    def record_health(
        self,
        plugin_id: str,
        *,
        healthy: bool,
        detail: str = "",
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._rows_for(connection, plugin_id, scope_kind, scope_key)
            if not rows:
                raise KeyError(f"unknown plugin {plugin_id}")
            if len(rows) > 1:
                raise PluginAmbiguityError(
                    f"plugin {plugin_id} exists in more than one scope; "
                    "name the scope you mean"
                )
            connection.execute(
                "UPDATE plugins SET health_state = ?, health_detail = ?, "
                "health_checked_at = ?, updated_at = ? "
                "WHERE plugin_id = ? AND scope_kind = ? AND scope_key = ?",
                (
                    "healthy" if healthy else "unhealthy",
                    " ".join(str(detail or "").split())[:500],
                    now,
                    now,
                    plugin_id,
                    str(rows[0]["scope_kind"]),
                    str(rows[0]["scope_key"]),
                ),
            )

    @staticmethod
    def _rows_for(
        connection: sqlite3.Connection,
        plugin_id: str,
        scope_kind: str | None,
        scope_key: str | None,
    ) -> list[sqlite3.Row]:
        clauses = ["plugin_id = ?"]
        params: list[object] = [plugin_id]
        if scope_kind is not None:
            clauses.append("scope_kind = ?")
            params.append(scope_kind)
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        return connection.execute(
            "SELECT * FROM plugins WHERE " + " AND ".join(clauses) + " ORDER BY scope_kind",
            tuple(params),
        ).fetchall()

    def get(
        self,
        plugin_id: str,
        *,
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any] | None:
        """One plugin, or nothing.

        When the same id exists in more than one scope this refuses rather
        than picking one, because quietly choosing between a project plugin
        and a global plugin of the same name is exactly how one would end up
        running with the other's permissions.
        """

        with self._connect() as connection:
            rows = self._rows_for(connection, plugin_id, scope_kind, scope_key)
        if not rows:
            return None
        if len(rows) > 1:
            scopes = ", ".join(
                f"{row['scope_kind'] or 'unscoped'}:{row['scope_key'] or '-'}" for row in rows
            )
            raise PluginAmbiguityError(
                f"plugin {plugin_id} exists in more than one scope ({scopes}); "
                "name the scope you mean"
            )
        return _plugin_dict(rows[0])

    def require(
        self,
        plugin_id: str,
        *,
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any]:
        found = self.get(plugin_id, scope_kind=scope_kind, scope_key=scope_key)
        if found is None:
            raise KeyError(f"unknown plugin {plugin_id}")
        return found

    def list(self, *, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            if state not in LIFECYCLE_STATES:
                raise PluginLifecycleError(f"unknown plugin lifecycle state {state!r}")
            clauses.append("state = ?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plugins" + where + " ORDER BY plugin_id", tuple(params)
            ).fetchall()
        return [_plugin_dict(row) for row in rows]

    def enabled_scopes(
        self,
        plugin_id: str,
        *,
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> tuple[str, ...]:
        """Scopes a plugin actually holds right now.

        A staged or disabled plugin holds nothing. This is the function a
        caller should ask before honouring a plugin's request, rather than
        trusting the manifest on disk.
        """

        try:
            record = self.get(plugin_id, scope_kind=scope_kind, scope_key=scope_key)
        except PluginAmbiguityError:
            # Two plugins wear this name. Neither gets the benefit of the doubt.
            return ()
        if record is None or record["state"] != "enabled":
            return ()
        return tuple(validate_manifest(record["manifest"]).scopes)

    def approved_manifest_hashes(
        self,
        plugin_id: str,
        *,
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> frozenset[str]:
        """Fingerprints of every manifest an operator actually approved.

        The loader checks the installed record against this before running
        anything, so a manifest edited in the database after approval is
        refused rather than executed. Approvals do not cross scopes: approving
        a global plugin never vouches for a project plugin of the same name.
        """

        clauses = ["plugin_id = ?", "state = 'approved'"]
        params: list[object] = [plugin_id]
        if scope_kind is not None:
            clauses.append("scope_kind = ?")
            params.append(scope_kind)
        if scope_key is not None:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM plugin_stages WHERE " + " AND ".join(clauses),
                tuple(params),
            ).fetchall()
        hashes = set()
        for row in rows:
            with suppress(PluginManifestError):
                hashes.add(validate_manifest(str(row["manifest_json"])).manifest_hash())
        return frozenset(hashes)

    def record_receipt(
        self,
        plugin_id: str,
        *,
        action: str,
        decision: str,
        actor: str = "serena.loader",
        detail: str = "",
        scope_kind: str | None = None,
        scope_key: str | None = None,
    ) -> str:
        """Write one durable audit receipt for a runtime event.

        Every load, call, hook delivery, health check, and refusal lands here.
        A denial is as much a receipt as a success: the record of what a plugin
        tried and was not allowed to do is the interesting half.
        """

        receipt_id = str(uuid.uuid4())
        try:
            record = self.get(plugin_id, scope_kind=scope_kind, scope_key=scope_key)
        except PluginAmbiguityError:
            record = None
        state = str(record["state"]) if record is not None else "unknown"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO plugin_audit("
                "  plugin_id, action, actor, state, created_at, receipt_id, decision,"
                "  detail, scope_kind, scope_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plugin_id,
                    " ".join(str(action or "").split())[:80],
                    " ".join(str(actor or "").split())[:120],
                    state,
                    time.time(),
                    receipt_id,
                    " ".join(str(decision or "").split())[:32],
                    " ".join(str(detail or "").split())[:1000],
                    scope_kind or (str(record["scope_kind"]) if record else ""),
                    scope_key or (str(record["scope_key"]) if record else ""),
                ),
            )
        return receipt_id

    def audit(self, plugin_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM plugin_audit WHERE plugin_id = ? ORDER BY created_at DESC LIMIT ?",
                (plugin_id, min(500, max(1, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _log(
        connection: sqlite3.Connection,
        plugin_id: str,
        action: str,
        actor: str,
        state: str,
        now: float,
        *,
        scope_kind: str = "",
        scope_key: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO plugin_audit("
            "  plugin_id, action, actor, state, created_at, scope_kind, scope_key"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plugin_id, action, actor, state, now, scope_kind, scope_key),
        )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _migrate_scope_columns(connection: sqlite3.Connection) -> None:
        """Give an older registry the scope columns without losing a row.

        The first version of this table keyed a plugin by id alone, which meant
        a project plugin and a global plugin sharing a name were the same row.
        Widening the key needs a rebuild, so the old rows are read, their scope
        recovered from the manifest each one already stored, and written back
        under the composite key.
        """

        stage_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(plugin_stages)").fetchall()
        }
        for column in ("scope_kind", "scope_key"):
            if column not in stage_columns:
                connection.execute(
                    f"ALTER TABLE plugin_stages ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        if stage_columns and "scope_kind" not in stage_columns:
            for row in connection.execute(
                "SELECT stage_id, manifest_json FROM plugin_stages"
            ).fetchall():
                kind, key = _scope_of_json(str(row["manifest_json"]))
                connection.execute(
                    "UPDATE plugin_stages SET scope_kind = ?, scope_key = ? WHERE stage_id = ?",
                    (kind, key, str(row["stage_id"])),
                )

        plugin_columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(plugins)").fetchall()
        ]
        if not plugin_columns or "scope_kind" in plugin_columns:
            return
        legacy = connection.execute("SELECT * FROM plugins").fetchall()
        connection.executescript(
            """
            CREATE TABLE plugins_scoped (
                plugin_id TEXT NOT NULL,
                scope_kind TEXT NOT NULL DEFAULT '',
                scope_key TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                state TEXT NOT NULL,
                health_state TEXT NOT NULL DEFAULT 'unknown',
                health_detail TEXT NOT NULL DEFAULT '',
                health_checked_at REAL,
                installed_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (plugin_id, scope_kind, scope_key)
            );
            """
        )
        for row in legacy:
            kind, key = _scope_of_json(str(row["manifest_json"]))
            connection.execute(
                "INSERT OR REPLACE INTO plugins_scoped("
                "  plugin_id, scope_kind, scope_key, name, version, manifest_json, state,"
                "  health_state, health_detail, health_checked_at, installed_by,"
                "  created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(row["plugin_id"]),
                    kind,
                    key,
                    str(row["name"]),
                    str(row["version"]),
                    str(row["manifest_json"]),
                    str(row["state"]),
                    str(row["health_state"]),
                    str(row["health_detail"] or ""),
                    row["health_checked_at"],
                    str(row["installed_by"] or ""),
                    float(row["created_at"]),
                    float(row["updated_at"]),
                ),
            )
        connection.executescript(
            "DROP TABLE plugins; ALTER TABLE plugins_scoped RENAME TO plugins;"
        )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plugins (
                    plugin_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL DEFAULT '',
                    scope_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    health_state TEXT NOT NULL DEFAULT 'unknown',
                    health_detail TEXT NOT NULL DEFAULT '',
                    health_checked_at REAL,
                    installed_by TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (plugin_id, scope_kind, scope_key)
                );
                CREATE TABLE IF NOT EXISTS plugin_stages (
                    stage_id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL DEFAULT '',
                    scope_key TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    approved_by TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plugin_stages_state_idx
                    ON plugin_stages(state, plugin_id, created_at);
                CREATE TABLE IF NOT EXISTS plugin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plugin_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS plugin_audit_idx
                    ON plugin_audit(plugin_id, created_at);
                """
            )
            # Runtime receipts were added after the first registries existed,
            # so widen an old audit table in place rather than losing its
            # history to a rebuild.
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(plugin_audit)").fetchall()
            }
            for column, definition in (
                ("receipt_id", "TEXT NOT NULL DEFAULT ''"),
                ("decision", "TEXT NOT NULL DEFAULT ''"),
                ("detail", "TEXT NOT NULL DEFAULT ''"),
                ("scope_kind", "TEXT NOT NULL DEFAULT ''"),
                ("scope_key", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE plugin_audit ADD COLUMN {column} {definition}"
                    )
            self._migrate_scope_columns(connection)
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)


def _plugin_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        manifest = json.loads(str(row["manifest_json"] or "{}"))
    except json.JSONDecodeError:
        manifest = {}
    return {
        "plugin_id": str(row["plugin_id"]),
        "scope_kind": str(row["scope_kind"] or ""),
        "scope_key": str(row["scope_key"] or ""),
        "name": str(row["name"]),
        "version": str(row["version"]),
        "state": str(row["state"]),
        "health_state": str(row["health_state"]),
        "health_detail": str(row["health_detail"] or ""),
        "health_checked_at": row["health_checked_at"],
        "installed_by": str(row["installed_by"] or ""),
        "manifest": manifest,
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _stage_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        diff = json.loads(str(row["diff_json"] or "{}"))
    except json.JSONDecodeError:
        diff = {}
    try:
        manifest = json.loads(str(row["manifest_json"] or "{}"))
    except json.JSONDecodeError:
        manifest = {}
    return {
        "stage_id": str(row["stage_id"]),
        "plugin_id": str(row["plugin_id"]),
        "state": str(row["state"]),
        "actor": str(row["actor"]),
        "approved_by": row["approved_by"],
        "reason": str(row["reason"] or ""),
        "manifest": manifest,
        "diff": diff,
        "created_at": float(row["created_at"]),
    }
