"""Adversarial tests for the executable plugin runtime (ws-4).

`tests/test_serena_extensibility.py` proves the manifest and the approval
lifecycle. This file proves the part that actually runs code: that an approved
plugin executes, and that every way a plugin might reach further than it was
granted is refused and written down.

Each test is one thing a hostile or careless plugin would try.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from core import plugin_loader as loader_module
from core.plugin_loader import PluginLoader, PluginRuntimeError
from core.serena_plugins import (
    PluginAmbiguityError,
    PluginManifestError,
    PluginRegistry,
    validate_manifest,
)

PLUGIN_ID = "serena.notes"
# Every manifest below declares NOTES_TOKEN, and the loader refuses to start a
# plugin whose declared secret is missing, so a loader that is meant to run
# something has to be handed it.
SECRET_ENV = {"NOTES_TOKEN": "token-from-serena"}

PLUGIN_SOURCE = '''
import serena_plugin_api as serena


def add_note(payload):
    return {"echo": payload.get("text", "")}


def status(payload):
    return "ok"


def unhealthy(payload):
    raise RuntimeError("the notes file is gone")


def on_turn(payload):
    return {"seen": payload.get("turn")}


def on_turn_secretly(payload):
    """Defined by the module but never declared in the manifest."""
    return "backdoor"


def read_it(payload):
    return serena.read_file(payload["path"])


def read_twice(payload):
    first = serena.read_file(payload["path"])
    second = serena.read_file(payload["path"])
    return [first, second]


def write_it(payload):
    return serena.write_file(payload["path"], payload["text"])


def reach_out(payload):
    return serena.fetch(payload["url"])


def use_memory(payload):
    return serena.capability("memory.read", query=payload.get("q", ""))


def peek_env(payload):
    import os

    return dict(os.environ)


def import_serena(payload):
    import core.serena_plugins

    return "imported serena internals"


def run_shell(payload):
    import subprocess

    return subprocess.run(["id"], capture_output=True).returncode


def hang(payload):
    import time

    time.sleep(60)
    return "never"
'''

HANDLERS = (
    "add_note",
    "status",
    "unhealthy",
    "read_it",
    "read_twice",
    "write_it",
    "reach_out",
    "use_memory",
    "peek_env",
    "import_serena",
    "run_shell",
    "hang",
)


def _manifest(**overrides):
    base = {
        "schema_version": 1,
        "id": PLUGIN_ID,
        "name": "Notes",
        "version": "1.0.0",
        "description": "A small notes surface",
        "entrypoint": "main.py",
        "scope": {"kind": "global"},
        "tools": [
            {"name": name, "description": f"the {name} tool", "scopes": []}
            for name in HANDLERS
        ],
        "skills": [
            {
                "name": "jot",
                "description": "jot one note down",
                "handler": "add_note",
                "scopes": [],
            }
        ],
        "ui": [{"surface": "chat_sidebar", "title": "Notes"}],
        "hooks": [{"event": "chat.turn.completed", "handler": "on_turn"}],
        "permissions": {"scopes": ["memory.read"], "filesystem": ["docs/notes"], "network": []},
        "secrets": [{"name": "NOTES_TOKEN", "ref": "env:NOTES_TOKEN"}],
        "health": {"check": "status", "interval_seconds": 300},
    }
    base.update(overrides)
    return base


@pytest.fixture
def registry(tmp_path):
    return PluginRegistry(tmp_path / "plugins.sqlite3")


def _install(registry, manifest, *, enable=True):
    """Take a manifest all the way through the approval lifecycle."""

    staged = registry.stage(manifest, actor="raghav")
    registry.approve_stage(staged["stage_id"], actor="raghav")
    if enable:
        registry.transition(manifest["id"], "enabled", actor="raghav")
    return staged


def _write_plugin(root, manifest, source=PLUGIN_SOURCE):
    target = root / manifest["entrypoint"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


# ------------------------------------------------- review findings, ws-4
#
# Everything below this point was written against a specific defect the Review
# phase found. Each one is the smallest thing that would have caught it.


def test_two_plugins_sharing_a_name_in_different_scopes_stay_separate(tmp_path, registry):
    """A project plugin and a global plugin of the same name are not one row.

    Review finding 6: the registry keyed a plugin by id alone, so installing
    one of these quietly overwrote the other, and the survivor inherited
    permissions nobody approved for it.
    """

    project = tmp_path / "project"
    local = _manifest(
        entrypoint="plugins/notes/main.py",
        scope={"kind": "project", "project_root": str(project)},
    )
    everywhere = _manifest(scope={"kind": "global"})
    everywhere["permissions"]["scopes"] = ["memory.read", "notify.send"]

    _install(registry, local, enable=False)
    _install(registry, everywhere, enable=False)

    assert len(registry.list()) == 2
    kept_local = registry.get(PLUGIN_ID, scope_kind="project", scope_key=str(project))
    kept_global = registry.get(PLUGIN_ID, scope_kind="global")
    assert kept_local is not None and kept_global is not None
    assert kept_local["manifest"]["permissions"]["scopes"] == ["memory.read"]
    assert kept_global["manifest"]["permissions"]["scopes"] == [
        "memory.read",
        "notify.send",
    ]


def test_an_ambiguous_plugin_name_is_refused_rather_than_guessed(tmp_path, registry):
    project = tmp_path / "project"
    _install(
        registry,
        _manifest(
            entrypoint="plugins/notes/main.py",
            scope={"kind": "project", "project_root": str(project)},
        ),
        enable=False,
    )
    _install(registry, _manifest(scope={"kind": "global"}), enable=False)

    with pytest.raises(PluginAmbiguityError, match="more than one scope"):
        registry.get(PLUGIN_ID)
    with pytest.raises(PluginAmbiguityError, match="more than one scope"):
        registry.transition(PLUGIN_ID, "enabled", actor="raghav")
    # And an ambiguous name holds no scopes at all rather than the wrong ones.
    assert registry.enabled_scopes(PLUGIN_ID) == ()


def test_approving_one_scope_does_not_vouch_for_the_other(tmp_path, registry):
    project = tmp_path / "project"
    _install(registry, _manifest(scope={"kind": "global"}), enable=False)
    local = _manifest(
        entrypoint="plugins/notes/main.py",
        scope={"kind": "project", "project_root": str(project)},
    )
    _install(registry, local, enable=False)

    global_hashes = registry.approved_manifest_hashes(PLUGIN_ID, scope_kind="global")
    local_hashes = registry.approved_manifest_hashes(
        PLUGIN_ID, scope_kind="project", scope_key=str(project)
    )

    assert global_hashes and local_hashes
    assert not (global_hashes & local_hashes)


def test_a_legacy_unscoped_registry_migrates_without_losing_a_plugin(tmp_path):
    """Review finding 6, migration half: old rows must survive the rekey."""

    path = tmp_path / "plugins.sqlite3"
    manifest_json = validate_manifest(_manifest()).canonical()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE plugins (
                plugin_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                state TEXT NOT NULL,
                health_state TEXT NOT NULL DEFAULT 'unknown',
                health_detail TEXT NOT NULL DEFAULT '',
                health_checked_at REAL,
                installed_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE plugin_stages (
                stage_id TEXT PRIMARY KEY,
                plugin_id TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                state TEXT NOT NULL,
                actor TEXT NOT NULL,
                approved_by TEXT,
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO plugins VALUES (?, 'Notes', '1.0.0', ?, 'enabled', 'healthy',"
            " 'fine', 1.0, 'raghav', 1.0, 1.0)",
            (PLUGIN_ID, manifest_json),
        )

    migrated = PluginRegistry(path).require(PLUGIN_ID)

    assert migrated["state"] == "enabled"
    assert migrated["health_state"] == "healthy"
    assert migrated["installed_by"] == "raghav"
    assert migrated["scope_kind"] == "global"


def test_a_missing_declared_secret_refuses_the_load(tmp_path, registry):
    """Review finding 7: a half-configured plugin must not start at all."""

    manifest = _manifest()
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(registry, global_root=tmp_path / "global", secret_env={})

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "NOTES_TOKEN" in outcome.error and "not set" in outcome.error
    assert outcome.receipt_id
    loader.close()


def test_a_secret_file_reference_outside_the_scope_refuses_the_load(tmp_path, registry):
    manifest = _manifest(secrets=[{"name": "NOTES_TOKEN", "ref": "file:creds/token"}])
    root = tmp_path / "global" / PLUGIN_ID
    _write_plugin(root, manifest)
    root.mkdir(parents=True, exist_ok=True)
    (root / "creds").mkdir(parents=True, exist_ok=True)
    (root / "creds" / "token").symlink_to(tmp_path / "elsewhere.txt")
    (tmp_path / "elsewhere.txt").write_text("not yours", encoding="utf-8")
    _install(registry, manifest)
    loader = PluginLoader(registry, global_root=tmp_path / "global", secret_env={})

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "does not resolve inside its scope" in outcome.error
    loader.close()


def test_disabling_mid_call_revokes_filesystem_reach_too(project_plugin, registry):
    """Review finding 7: lifecycle was only re-checked for Serena capabilities.

    The plugin asks Serena to read a declared file. While that request is in
    flight the plugin is disabled, so the read must be refused even though the
    path itself is one it was approved for.
    """

    loader, project = project_plugin
    assert loader.call_tool(PLUGIN_ID, "read_it", {"path": "docs/notes/a.md"}).ok is True

    original = loader._serve_file
    served: list[int] = []

    def disable_after_the_first_read(manifest, capability, args):
        result = original(manifest, capability, args)
        served.append(1)
        if len(served) == 1:
            registry.transition(
                PLUGIN_ID,
                "disabled",
                actor="raghav",
                scope_kind="project",
                scope_key=str(project),
            )
        return result

    loader._serve_file = disable_after_the_first_read

    outcome = loader.call_tool(PLUGIN_ID, "read_twice", {"path": "docs/notes/a.md"})

    assert served == [1], "the second read must never have reached the filesystem"
    assert outcome.ok is False
    assert any("may no longer act" in item for item in outcome.denials)


def test_the_emit_seam_never_raises_into_the_caller(tmp_path, monkeypatch):
    """Review finding 5: the emit points need one call that cannot hurt them.

    The five places that can raise a hook live in other workers' modules, so
    the contract they get is this: it returns a list, and it does not raise,
    whatever the plugin layer is doing.
    """

    monkeypatch.setenv("SERENA_PLUGIN_DB_PATH", str(tmp_path / "plugins.sqlite3"))
    monkeypatch.setenv("SERENA_PLUGIN_GLOBAL_ROOT", str(tmp_path / "global"))
    loader_module.reset_default_loader()


def test_the_emit_seam_refuses_recursive_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_PLUGIN_DB_PATH", str(tmp_path / "plugins.sqlite3"))
    loader_module.reset_default_loader()
    nested: list = []

    def recurse(_loader, event, payload):
        nested.append(loader_module.emit_plugin_hook(event, payload))
        return []

    monkeypatch.setattr(loader_module.PluginLoader, "dispatch_hook", recurse)

    assert loader_module.emit_plugin_hook("notification.sent", {"id": "notice-1"}) == []
    assert nested == [[]]
    loader_module.reset_default_loader()


def test_the_default_loader_wires_the_shared_url_policy(monkeypatch):
    checked: list[str] = []

    def validate(_policy, url):
        checked.append(url)

    monkeypatch.setattr("core.security_policy.URLPolicy.validate", validate)

    allowed, reason = loader_module._shared_url_policy("https://api.example.com/v1")

    assert allowed is True
    assert "validated" in reason
    assert checked == ["https://api.example.com/v1"]

    # No plugins at all: quiet, empty, no exception.
    assert loader_module.emit_plugin_hook("chat.turn.completed", {"turn": 1}) == []

    # An event nobody should be raising is refused inside the seam, not thrown
    # at whoever emitted it.
    assert loader_module.emit_plugin_hook("system.root", {}) == []

    # And a completely broken plugin layer still cannot take the caller down.
    def explode(*_args, **_kwargs):
        raise RuntimeError("the plugin layer is on fire")

    monkeypatch.setattr(loader_module.PluginLoader, "dispatch_hook", explode)
    assert loader_module.emit_plugin_hook("chat.turn.completed", {"turn": 2}) == []
    loader_module.reset_default_loader()


def test_a_crowd_of_hung_plugins_costs_the_emitter_one_deadline(tmp_path, monkeypatch):
    """Review finding 8: hook fan-out shared no budget, so N hangs cost N waits."""

    registry = PluginRegistry(tmp_path / "plugins.sqlite3")
    for index in range(3):
        plugin_id = f"serena.slow{index}"
        manifest = _manifest(id=plugin_id, hooks=[{"event": "schedule.tick", "handler": "hang"}])
        _write_plugin(tmp_path / "global" / plugin_id, manifest)
        _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )
    monkeypatch.setattr(loader_module, "CALL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(loader_module, "HOOK_TOTAL_TIMEOUT_SECONDS", 2.5)

    started = time.monotonic()
    delivered = loader.dispatch_hook("schedule.tick", {})
    elapsed = time.monotonic() - started

    assert len(delivered) == 3
    assert all(result.ok is False for _pid, result in delivered)
    # Three plugins each hanging for the per-call timeout would be ~6s. The
    # shared budget has to stop it well before that.
    assert elapsed < 4.5, f"fan-out took {elapsed:.1f}s, budget was not shared"
    assert any("ran out of time" in result.error for _pid, result in delivered)
    loader.close()


@pytest.fixture
def global_plugin(tmp_path, registry):
    """An approved, enabled, Serena-global plugin that is ready to run."""

    manifest = _manifest()
    global_root = tmp_path / "global"
    _write_plugin(global_root / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry,
        global_root=global_root,
        secret_env={"NOTES_TOKEN": "token-from-serena"},
    )
    yield loader, manifest, global_root
    loader.close()


# ------------------------------------------------------------------ scopes


def test_a_scope_must_say_project_or_global():
    with pytest.raises(PluginManifestError, match="project.*global"):
        validate_manifest(_manifest(scope={"kind": "everywhere"}))


def test_a_global_scope_cannot_also_name_a_project():
    with pytest.raises(PluginManifestError, match="must not name a project_root"):
        validate_manifest(_manifest(scope={"kind": "global", "project_root": "/home/raghav/x"}))


def test_a_project_scope_must_name_its_root():
    with pytest.raises(PluginManifestError, match="must name its project_root"):
        validate_manifest(_manifest(scope={"kind": "project"}))


@pytest.mark.parametrize("root", ["relative/path", "~/projects/x", "/home/../etc"])
def test_a_project_root_must_be_a_plain_absolute_path(root):
    with pytest.raises(PluginManifestError, match="absolute path"):
        validate_manifest(_manifest(scope={"kind": "project", "project_root": root}))


def test_an_unscoped_plugin_is_never_loaded(tmp_path, registry):
    """Omitting the scope is not a quiet vote for 'global'."""

    manifest = _manifest()
    manifest.pop("scope")
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "project-local or global" in outcome.error
    loader.close()


def test_a_project_plugin_will_not_load_in_a_different_project(tmp_path, registry):
    manifest = _manifest(
        entrypoint="plugins/notes/main.py",
        scope={"kind": "project", "project_root": str(tmp_path / "wanted")},
    )
    _write_plugin(tmp_path / "wanted", manifest)
    _install(registry, manifest)
    loader = PluginLoader(registry, project_root=tmp_path / "somewhere-else")

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "will not load inside" in outcome.error
    loader.close()


def test_a_project_plugin_will_not_load_outside_any_project(tmp_path, registry):
    manifest = _manifest(
        entrypoint="plugins/notes/main.py",
        scope={"kind": "project", "project_root": str(tmp_path / "wanted")},
    )
    _write_plugin(tmp_path / "wanted", manifest)
    _install(registry, manifest)
    loader = PluginLoader(registry)

    assert loader.load(PLUGIN_ID).ok is False
    loader.close()


def test_widening_a_plugin_from_one_project_to_all_of_serena_is_an_escalation(registry):
    from core.serena_plugins import diff_manifests

    local = validate_manifest(
        _manifest(scope={"kind": "project", "project_root": "/home/raghav/p"})
    )
    everywhere = validate_manifest(_manifest(scope={"kind": "global"}))

    change = diff_manifests(local, everywhere)

    assert change["widens_scope"] is True
    assert change["escalates_privilege"] is True


# ------------------------------------------------------------ approval gate


def test_only_an_enabled_plugin_runs(tmp_path, registry):
    manifest = _manifest()
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest, enable=False)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "installed" in outcome.error
    loader.close()


def test_a_manifest_edited_after_approval_is_refused(global_plugin, registry):
    """Approval covers one exact manifest, not the plugin's name forever."""

    loader, _manifest_dict, _root = global_plugin
    assert loader.load(PLUGIN_ID).ok is True

    tampered = _manifest()
    tampered["permissions"]["network"] = ["evil.example.com"]
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "UPDATE plugins SET manifest_json = ? WHERE plugin_id = ?",
            (validate_manifest(tampered).canonical(), PLUGIN_ID),
        )

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "no longer matches an approved manifest" in outcome.error


def test_loading_never_installs_anything(tmp_path, registry):
    """The loader has no path that creates or enables a plugin."""

    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )
    outcome = loader.load("serena.never-staged")

    assert outcome.ok is False
    assert registry.get("serena.never-staged") is None
    assert registry.list() == []
    loader.close()


# ------------------------------------------------------------------ running


def test_an_approved_tool_actually_runs(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "add_note", {"text": "milk"})

    assert outcome.ok is True, outcome.error
    assert outcome.result == {"echo": "milk"}
    assert outcome.receipt_id


def test_a_declared_skill_runs_by_its_public_name(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_skill(PLUGIN_ID, "jot", {"text": "eggs"})

    assert outcome.ok is True, outcome.error
    assert outcome.result == {"echo": "eggs"}


def test_an_undeclared_skill_name_is_refused(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_skill(PLUGIN_ID, "exfiltrate", {})

    assert outcome.ok is False
    assert "declares no skill" in outcome.error


def test_an_undeclared_handler_is_refused_before_the_plugin_sees_it(global_plugin):
    """A function the module defines but the manifest never declared is unreachable."""

    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "on_turn_secretly", {})

    assert outcome.ok is False
    assert "not declared by the approved manifest" in outcome.error


def test_a_hook_reaches_the_plugin_that_asked_for_it(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    delivered = loader.dispatch_hook("chat.turn.completed", {"turn": 7})

    assert [(pid, r.ok, r.result) for pid, r in delivered] == [
        (PLUGIN_ID, True, {"seen": 7})
    ]


def test_an_unsubscribed_hook_delivers_nothing(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    assert loader.dispatch_hook("fleet.run.failed", {}) == []


def test_an_unknown_hook_event_is_refused(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    with pytest.raises(PluginRuntimeError, match="unknown hook event"):
        loader.dispatch_hook("system.root", {})


def test_ui_contributions_come_only_from_enabled_plugins(global_plugin, registry):
    loader, _manifest_dict, _root = global_plugin

    assert loader.ui_contributions("chat_sidebar") == [
        {
            "plugin_id": PLUGIN_ID,
            "surface": "chat_sidebar",
            "title": "Notes",
            "scope": "global",
        }
    ]

    registry.transition(PLUGIN_ID, "disabled", actor="raghav")
    assert loader.ui_contributions() == []


def test_a_health_check_records_what_it_found(global_plugin, registry):
    loader, _manifest_dict, _root = global_plugin

    assert loader.health_check(PLUGIN_ID).ok is True
    assert registry.require(PLUGIN_ID)["health_state"] == "healthy"


def test_a_failing_health_check_is_recorded_not_raised(tmp_path, registry):
    manifest = _manifest(health={"check": "unhealthy", "interval_seconds": 60})
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.health_check(PLUGIN_ID)

    assert outcome.ok is False
    assert "the notes file is gone" in outcome.error
    assert registry.require(PLUGIN_ID)["health_state"] == "unhealthy"
    loader.close()


# --------------------------------------------------------- disable / remove


def test_disabling_revokes_reach_on_the_very_next_call(global_plugin, registry):
    loader, _manifest_dict, _root = global_plugin
    assert loader.call_tool(PLUGIN_ID, "add_note", {"text": "before"}).ok is True

    registry.transition(PLUGIN_ID, "disabled", actor="raghav")

    outcome = loader.call_tool(PLUGIN_ID, "add_note", {"text": "after"})
    assert outcome.ok is False
    assert "disabled" in outcome.error


def test_a_removed_plugin_cannot_be_run_again(global_plugin, registry):
    loader, _manifest_dict, _root = global_plugin
    assert loader.call_tool(PLUGIN_ID, "add_note", {"text": "before"}).ok is True

    registry.transition(PLUGIN_ID, "removed", actor="raghav")

    assert loader.call_tool(PLUGIN_ID, "add_note", {}).ok is False
    assert loader.dispatch_hook("chat.turn.completed", {"turn": 1}) == []


# ------------------------------------------------------------- filesystem


@pytest.fixture
def project_plugin(tmp_path, registry):
    project = tmp_path / "project"
    manifest = _manifest(
        entrypoint="plugins/notes/main.py",
        scope={"kind": "project", "project_root": str(project)},
    )
    _write_plugin(project, manifest)
    (project / "docs" / "notes").mkdir(parents=True)
    (project / "docs" / "notes" / "a.md").write_text("declared and allowed", encoding="utf-8")
    (project / "core").mkdir(parents=True)
    (project / "core" / "work_authority.py").write_text("authority", encoding="utf-8")
    _install(registry, manifest)
    loader = PluginLoader(registry, project_root=project, secret_env=SECRET_ENV)
    yield loader, project
    loader.close()


def test_a_declared_path_can_be_read(project_plugin):
    loader, _project = project_plugin

    outcome = loader.call_tool(PLUGIN_ID, "read_it", {"path": "docs/notes/a.md"})

    assert outcome.ok is True, outcome.error
    assert outcome.result == "declared and allowed"


def test_an_undeclared_path_inside_the_project_is_refused(project_plugin):
    """Being inside the checkout is not permission to read the checkout."""

    loader, _project = project_plugin

    outcome = loader.call_tool(PLUGIN_ID, "read_it", {"path": "core/work_authority.py"})

    assert outcome.ok is False
    assert any("outside the filesystem permissions" in item for item in outcome.denials)


@pytest.mark.parametrize(
    "escape", ["../../../etc/passwd", "docs/notes/../../../etc/passwd", "/etc/passwd"]
)
def test_a_path_escaping_the_scope_root_is_refused(project_plugin, escape):
    loader, _project = project_plugin

    outcome = loader.call_tool(PLUGIN_ID, "read_it", {"path": escape})

    assert outcome.ok is False
    assert outcome.denials


def test_a_symlink_out_of_the_project_is_refused(project_plugin, tmp_path):
    loader, project = project_plugin
    secret = tmp_path / "outside.txt"
    secret.write_text("not yours", encoding="utf-8")
    (project / "docs" / "notes" / "link.txt").symlink_to(secret)

    outcome = loader.call_tool(PLUGIN_ID, "read_it", {"path": "docs/notes/link.txt"})

    assert outcome.ok is False
    assert outcome.denials


def test_a_declared_path_can_be_written(project_plugin):
    loader, project = project_plugin

    outcome = loader.call_tool(
        PLUGIN_ID, "write_it", {"path": "docs/notes/new.md", "text": "written"}
    )

    assert outcome.ok is True, outcome.error
    assert (project / "docs" / "notes" / "new.md").read_text() == "written"


def test_a_write_outside_the_declared_paths_is_refused(project_plugin, tmp_path):
    loader, project = project_plugin

    outcome = loader.call_tool(
        PLUGIN_ID, "write_it", {"path": "core/work_authority.py", "text": "owned"}
    )

    assert outcome.ok is False
    assert (project / "core" / "work_authority.py").read_text() == "authority"


@pytest.mark.parametrize(
    "path",
    [
        "Persona.md",
        "config/serena-policy.json",
        "core/work_authority.py",
        "core/memory_authority.py",
        "core/serena_plugins.py",
        "core/plugin_loader.py",
        "cli.py",
        ".ssh/id_rsa",
    ],
)
def test_a_manifest_may_not_declare_serenas_identity_or_authority(path):
    """A plugin cannot even ask for the files that define who Serena is."""

    payload = _manifest()
    payload["permissions"]["filesystem"] = [path]
    with pytest.raises(PluginManifestError, match="protected path"):
        validate_manifest(payload)


# ---------------------------------------------------------------- network


def test_network_is_refused_when_the_manifest_declared_no_hosts(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "reach_out", {"url": "https://evil.example.com/x"})

    assert outcome.ok is False
    assert any("declared no network" in item for item in outcome.denials)


def test_an_undeclared_host_is_refused(tmp_path, registry):
    manifest = _manifest()
    manifest["permissions"]["network"] = ["api.allowed.com"]
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry,
        global_root=tmp_path / "global",
        secret_env=SECRET_ENV,
        url_policy=lambda url: (True, ""),
    )

    refused = loader.call_tool(PLUGIN_ID, "reach_out", {"url": "https://evil.example.com/x"})
    allowed = loader.call_tool(PLUGIN_ID, "reach_out", {"url": "https://api.allowed.com/x"})

    assert refused.ok is False
    assert any("is not a host this plugin declared" in item for item in refused.denials)
    assert allowed.ok is True, allowed.error
    loader.close()


def test_plaintext_http_is_refused_even_for_a_declared_host(tmp_path, registry):
    manifest = _manifest()
    manifest["permissions"]["network"] = ["api.allowed.com"]
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry,
        global_root=tmp_path / "global",
        secret_env=SECRET_ENV,
        url_policy=lambda url: (True, ""),
    )

    outcome = loader.call_tool(PLUGIN_ID, "reach_out", {"url": "http://api.allowed.com/x"})

    assert outcome.ok is False
    assert any("https only" in item for item in outcome.denials)
    loader.close()


def test_without_a_url_policy_the_loader_fails_closed(tmp_path, registry):
    """No shared policy wired in means no outbound request, not a free pass."""

    manifest = _manifest()
    manifest["permissions"]["network"] = ["api.allowed.com"]
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.call_tool(PLUGIN_ID, "reach_out", {"url": "https://api.allowed.com/x"})

    assert outcome.ok is False
    assert any("no URL policy" in item for item in outcome.denials)
    loader.close()


# ------------------------------------------------------------- capabilities


def test_a_held_scope_still_needs_serena_to_offer_the_capability(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "use_memory", {"q": "raghav"})

    assert outcome.ok is False
    assert any("no handler registered" in item for item in outcome.denials)


def test_a_registered_capability_is_reachable_for_a_held_scope(global_plugin):
    loader, _manifest_dict, _root = global_plugin
    seen = []
    loader.register_capability(
        "memory.read", lambda pid, args: seen.append((pid, args)) or ["a memory"]
    )

    outcome = loader.call_tool(PLUGIN_ID, "use_memory", {"q": "raghav"})

    assert outcome.ok is True, outcome.error
    assert outcome.result == ["a memory"]
    assert seen == [(PLUGIN_ID, {"query": "raghav"})]


def test_a_capability_outside_the_manifest_scopes_is_refused(tmp_path, registry):
    """Registering a handler does not hand it to plugins that never asked."""

    manifest = _manifest()
    manifest["permissions"]["scopes"] = []
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )
    loader.register_capability("memory.read", lambda pid, args: ["leaked"])

    outcome = loader.call_tool(PLUGIN_ID, "use_memory", {"q": "x"})

    assert outcome.ok is False
    assert any("does not hold memory.read" in item for item in outcome.denials)
    loader.close()


def test_an_unknown_capability_cannot_be_registered(global_plugin):
    loader, _manifest_dict, _root = global_plugin

    with pytest.raises(PluginRuntimeError, match="unknown permission scope"):
        loader.register_capability("root.everything", lambda pid, args: None)


# ------------------------------------------------------- process boundary


def test_a_plugin_cannot_import_serenas_internals(global_plugin):
    """The child interpreter has no path to Serena's package tree."""

    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "import_serena", {})

    assert outcome.ok is False
    assert "ModuleNotFoundError" in outcome.error


def test_the_plugin_environment_holds_only_declared_secrets(global_plugin, monkeypatch):
    """Serena's own environment does not leak into a plugin."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-seen")
    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "peek_env", {})

    assert outcome.ok is True, outcome.error
    child_env = outcome.result
    assert child_env["NOTES_TOKEN"] == "token-from-serena"
    assert "ANTHROPIC_API_KEY" not in child_env
    assert child_env["PATH"] == ""
    leaked = [key for key in child_env if "KEY" in key or "TOKEN" in key]
    assert leaked == ["NOTES_TOKEN"]


def test_an_undeclared_secret_reference_is_not_passed_through(tmp_path, registry):
    manifest = _manifest(secrets=[])
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest)
    _install(registry, manifest)
    loader = PluginLoader(
        registry,
        global_root=tmp_path / "global",
        secret_env={"NOTES_TOKEN": "token-from-serena"},
    )

    outcome = loader.call_tool(PLUGIN_ID, "peek_env", {})

    assert outcome.ok is True, outcome.error
    assert "NOTES_TOKEN" not in outcome.result
    loader.close()


def test_a_plugin_shelling_out_finds_no_binaries(global_plugin):
    """An empty PATH is not a sandbox, but it is not a working shell either."""

    loader, _manifest_dict, _root = global_plugin

    outcome = loader.call_tool(PLUGIN_ID, "run_shell", {})

    assert outcome.ok is False
    assert "FileNotFoundError" in outcome.error


def test_a_hanging_plugin_does_not_hang_serena(global_plugin, monkeypatch):
    loader, _manifest_dict, _root = global_plugin
    monkeypatch.setattr(loader_module, "CALL_TIMEOUT_SECONDS", 1.0)

    outcome = loader.call_tool(PLUGIN_ID, "hang", {})

    assert outcome.ok is False
    assert "did not answer in time" in outcome.error
    # The next call still works, because the hung process was torn down.
    assert loader.call_tool(PLUGIN_ID, "add_note", {"text": "after"}).ok is True


def test_a_plugin_that_will_not_start_is_reported_not_raised(tmp_path, registry):
    manifest = _manifest()
    _write_plugin(tmp_path / "global" / PLUGIN_ID, manifest, source="raise SystemExit(3)")
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert outcome.receipt_id
    loader.close()


def test_a_missing_entrypoint_is_reported_not_raised(tmp_path, registry):
    manifest = _manifest()
    _install(registry, manifest)
    loader = PluginLoader(
        registry, global_root=tmp_path / "global", secret_env=SECRET_ENV
    )

    outcome = loader.load(PLUGIN_ID)

    assert outcome.ok is False
    assert "is missing" in outcome.error
    loader.close()


# ---------------------------------------------------------------- receipts


def test_every_run_and_refusal_leaves_a_receipt(global_plugin, registry):
    loader, _manifest_dict, _root = global_plugin

    loader.call_tool(PLUGIN_ID, "add_note", {"text": "milk"})
    loader.call_tool(PLUGIN_ID, "read_it", {"path": "/etc/passwd"})

    entries = registry.audit(PLUGIN_ID, limit=100)
    actions = {entry["action"] for entry in entries}
    decisions = {entry["decision"] for entry in entries}

    assert "load" in actions
    assert "tool.call" in actions
    assert "capability:fs.read" in actions
    assert {"allowed", "denied"} <= decisions
    assert all(entry["receipt_id"] for entry in entries if entry["action"] == "tool.call")


def test_a_receipt_survives_an_older_audit_table(tmp_path):
    """An existing registry gets the new receipt columns without losing history."""

    path = tmp_path / "plugins.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE plugin_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            INSERT INTO plugin_audit(plugin_id, action, actor, state, created_at)
            VALUES ('serena.old', 'approved', 'raghav', 'installed', 1.0);
            """
        )

    registry = PluginRegistry(path)
    receipt = registry.record_receipt("serena.old", action="load", decision="allowed")

    history = registry.audit("serena.old")
    assert receipt
    assert {entry["action"] for entry in history} == {"approved", "load"}
    assert any(entry["receipt_id"] == receipt for entry in history)


def test_the_manifest_hash_is_stable_and_covers_permissions():
    first = validate_manifest(_manifest())
    same = validate_manifest(json.loads(json.dumps(_manifest())))
    widened = _manifest()
    widened["permissions"]["scopes"] = ["memory.read", "notify.send"]

    assert first.manifest_hash() == same.manifest_hash()
    assert first.manifest_hash() != validate_manifest(widened).manifest_hash()
