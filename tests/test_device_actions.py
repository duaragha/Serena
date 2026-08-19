"""Scenes, dry runs, rollback, and the adapters, none of which touch real hardware.

Every test here either runs in dry-run mode or drives a fake. No test in this
file can reach pactl, adb, a phone, a broker, or a light.
"""

import json
import subprocess

import pytest

from core.action_authority import (
    BASIS_ORIGIN_TURN,
    ActionAuthority,
)
from core.adapters.base import AdapterRegistry, AdapterStatus, DeviceCommand, DeviceResult
from core.device_actions import (
    DeviceActionError,
    DeviceActionRunner,
    build_default_runner,
    load_scenes,
    parse_scene,
)


class FakeAdapter:
    """Records what it was asked to do and never leaves the process."""

    name = "fake"

    def __init__(self, *, available=True, fail_on=(), postconditions=None, no_undo=()):
        self.available = available
        self.fail_on = set(fail_on)
        self.postconditions = dict(postconditions or {})
        self.no_undo = set(no_undo)
        self.executed: list[str] = []
        self.compensated: list[str] = []

    def capabilities(self):
        return {
            "fake.read": "read",
            "fake.dim": "reversible",
            "fake.bright": "reversible",
            "fake.publish": "external",
            "fake.shred": "irreversible",
        }

    def status(self):
        return AdapterStatus(self.available, "" if self.available else "the fake is unplugged")

    def describe(self, command):
        return f"would {command.capability} {command.target}".strip()

    def execute(self, command):
        self.executed.append(command.capability)
        if command.capability in self.fail_on:
            return DeviceResult(
                False, "failed", command.capability, command.target, "the fake refused"
            )
        return DeviceResult(True, "completed", command.capability, command.target, "did it")

    def compensate(self, command):
        if command.capability in self.no_undo:
            return None
        self.compensated.append(command.capability)
        return DeviceResult(True, "completed", command.capability, command.target, "undone")

    def postcondition(self, command):
        return self.postconditions.get(command.capability)


@pytest.fixture()
def authority(tmp_path):
    return ActionAuthority(
        tmp_path / "action.sqlite3",
        audit_path=tmp_path / "action.jsonl",
        publish_events=False,
    )


@pytest.fixture()
def runner(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter()
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench, default_dry_run=True)
    made.adapter = adapter
    return made


def _proof(runner, covers, source="voice"):
    return runner.authority.issue_turn_proof(source=source, covers=covers).proof_id


def _live(runner, capability, **kwargs):
    """One authorized action, with the turn proof a real broker would attach."""

    return runner.run(
        capability,
        source="voice",
        authorization_basis=BASIS_ORIGIN_TURN,
        origin_proof=_proof(runner, [(capability, kwargs.get("target", ""))]),
        dry_run=False,
        **kwargs,
    )


def _dry(runner, capability, **kwargs):
    return runner.run(
        capability,
        source="voice",
        authorization_basis=BASIS_ORIGIN_TURN,
        origin_proof=_proof(runner, [(capability, kwargs.get("target", ""))]),
        **kwargs,
    )


def _run_scene(runner, name, **kwargs):
    """A scene proof covers exactly the steps the scene declares, and nothing else."""

    return runner.run_scene(
        name,
        source="voice",
        authorization_basis=BASIS_ORIGIN_TURN,
        origin_proof=_proof(runner, runner.scene_actions(name)),
        **kwargs,
    )


# -- everything goes through the authority ----------------------------------


def test_an_unauthorized_action_never_reaches_the_adapter(runner):
    report = runner.run("fake.dim", authorization_basis="none", dry_run=False)
    assert report.ok is False
    assert report.steps[0].status == "denied"
    assert runner.adapter.executed == []


def test_the_global_stop_reaches_the_device_layer(runner, authority):
    # Warranted before the stop, so this proves the stop wins rather than the
    # action simply lacking authority.
    proof = _proof(runner, [("fake.dim", "")])
    authority.engage_lock(reason="he said stop")
    report = runner.run(
        "fake.dim",
        source="voice",
        authorization_basis=BASIS_ORIGIN_TURN,
        origin_proof=proof,
        dry_run=False,
    )
    assert report.ok is False
    assert "emergency lock" in report.steps[0].detail
    assert runner.adapter.executed == []


def test_an_irreversible_device_action_needs_a_confirmation(runner):
    report = _live(runner, "fake.shred", target="everything")
    assert report.ok is False
    assert report.steps[0].tier == 3
    assert runner.adapter.executed == []


def test_an_unattended_source_cannot_drive_a_device_on_a_turn(runner):
    report = runner.run(
        "fake.publish", source="scheduler", authorization_basis=BASIS_ORIGIN_TURN, dry_run=False
    )
    assert report.ok is False
    assert runner.adapter.executed == []


def test_an_authorized_action_runs_and_is_recorded(runner, authority):
    report = _live(runner, "fake.dim")
    assert report.ok is True
    assert runner.adapter.executed == ["fake.dim"]
    row = authority.history(limit=1)[0]
    assert row["capability"] == "fake.dim"
    assert row["outcome_status"] == "completed"


# -- dry run ----------------------------------------------------------------


def test_dry_run_is_the_default_and_executes_nothing(runner):
    report = _dry(runner, "fake.dim")
    assert report.dry_run is True
    assert report.steps[0].status == "simulated"
    assert report.steps[0].simulated is True
    assert runner.adapter.executed == []


def test_a_dry_run_says_when_the_device_is_not_even_there(authority):
    bench = AdapterRegistry()
    bench.register(FakeAdapter(available=False))
    made = DeviceActionRunner(authority=authority, adapters=bench)
    report = _dry(made, "fake.dim", dry_run=True)
    assert report.steps[0].ok is False
    assert "unplugged" in report.steps[0].detail


def test_a_simulated_step_is_never_reported_as_completed(runner):
    report = _dry(runner, "fake.dim", dry_run=True)
    assert report.steps[0].status != "completed"


# -- postconditions ---------------------------------------------------------


def test_a_failed_postcondition_overrides_a_cheerful_adapter(authority):
    bench = AdapterRegistry()
    bench.register(FakeAdapter(postconditions={"fake.dim": False}))
    made = DeviceActionRunner(authority=authority, adapters=bench)
    report = _live(made, "fake.dim")
    assert report.ok is False
    assert report.steps[0].postcondition_ok is False
    assert "did not actually change" in report.steps[0].detail


def test_a_passing_postcondition_is_recorded(authority):
    bench = AdapterRegistry()
    bench.register(FakeAdapter(postconditions={"fake.dim": True}))
    made = DeviceActionRunner(authority=authority, adapters=bench)
    report = _live(made, "fake.dim")
    assert report.ok is True
    assert report.steps[0].postcondition_checked is True


# -- idempotency ------------------------------------------------------------


def test_the_same_key_does_not_press_the_button_twice(runner):
    first = _live(runner, "fake.dim", idempotency_key="wind-down-1")
    second = _live(runner, "fake.dim", idempotency_key="wind-down-1")
    assert first.ok is True and second.ok is True
    assert runner.adapter.executed == ["fake.dim"]
    assert second.steps[0].reused_idempotent_result is True


def test_without_a_key_the_action_runs_each_time(runner):
    _live(runner, "fake.dim")
    _live(runner, "fake.dim")
    assert runner.adapter.executed == ["fake.dim", "fake.dim"]


# -- scenes -----------------------------------------------------------------


def _scene(**overrides):
    body = {
        "description": "wind the house down",
        "steps": [
            {"capability": "fake.dim", "target": "lamp"},
            {"capability": "fake.bright", "target": "desk"},
        ],
    }
    body.update(overrides)
    return parse_scene("wind down", body)


def test_a_scene_runs_every_step_in_order(runner):
    runner.add_scene(_scene())
    report = _run_scene(runner, "wind down", dry_run=False)
    assert report.ok is True
    assert runner.adapter.executed == ["fake.dim", "fake.bright"]


def test_a_scene_dry_run_touches_nothing(runner):
    runner.add_scene(_scene())
    report = _run_scene(runner, "wind down", dry_run=True)
    assert report.dry_run is True
    assert all(step.status == "simulated" for step in report.steps)
    assert runner.adapter.executed == []


def test_a_scene_stops_at_a_failure_and_reports_the_partial_state(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.bright"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "wind down",
            {
                "steps": [
                    {"capability": "fake.dim"},
                    {"capability": "fake.bright"},
                    {"capability": "fake.dim"},
                ]
            },
        )
    )
    report = _run_scene(made, "wind down", dry_run=False)
    assert report.ok is False
    assert report.stopped_early is True
    assert report.partial is True
    assert len(report.steps) == 2
    assert adapter.executed == ["fake.dim", "fake.bright"]


def test_rollback_undoes_completed_steps_newest_first(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.publish"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "evening",
            {
                "steps": [
                    {"capability": "fake.dim"},
                    {"capability": "fake.bright"},
                    {"capability": "fake.publish", "on_failure": "rollback"},
                ]
            },
        )
    )
    report = _run_scene(made, "evening", dry_run=False)
    assert report.ok is False
    assert report.rolled_back is True
    assert adapter.compensated == ["fake.bright", "fake.dim"]
    assert report.uncompensated == []


def test_a_step_that_cannot_be_undone_is_named_not_hidden(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.bright"}, no_undo={"fake.dim"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "evening",
            {
                "steps": [
                    {"capability": "fake.dim"},
                    {"capability": "fake.bright", "on_failure": "rollback"},
                ]
            },
        )
    )
    report = _run_scene(made, "evening", dry_run=False)
    assert report.rolled_back is True
    assert report.uncompensated == ["fake.dim"]
    assert "no honest undo" in report.steps[0].compensation_detail


def test_an_optional_step_may_fail_without_failing_the_scene(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.publish"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "morning",
            {
                "steps": [
                    {"capability": "fake.publish", "optional": True},
                    {"capability": "fake.dim"},
                ]
            },
        )
    )
    report = _run_scene(made, "morning", dry_run=False)
    assert report.ok is True
    assert adapter.executed == ["fake.publish", "fake.dim"]


def test_continue_keeps_going_but_the_scene_is_still_not_ok(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.publish"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "morning",
            {
                "steps": [
                    {"capability": "fake.publish", "on_failure": "continue"},
                    {"capability": "fake.dim"},
                ]
            },
        )
    )
    report = _run_scene(made, "morning", dry_run=False)
    assert report.ok is False
    assert report.stopped_early is False
    assert adapter.executed == ["fake.publish", "fake.dim"]


def test_unknown_scene_and_unknown_capability_are_refused(runner):
    with pytest.raises(DeviceActionError, match="no scene called"):
        runner.run_scene("nothing like this")
    report = runner.run("nobody.owns_this", authorization_basis=BASIS_ORIGIN_TURN)
    assert report.ok is False
    assert report.steps[0].status == "unsupported"


def test_report_summary_reads_like_a_person(authority):
    bench = AdapterRegistry()
    bench.register(FakeAdapter(fail_on={"fake.bright"}))
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(_scene())
    report = _run_scene(made, "wind down", dry_run=False)
    assert "wind down" in report.summary()
    assert "1 of 2" in report.summary()


def test_a_stop_engaged_mid_scene_also_stops_the_rollback(authority):
    """The hole: compensation called the adapter directly, around the authority."""

    bench = AdapterRegistry()

    class Stopper(FakeAdapter):
        def execute(self, command):
            result = super().execute(command)
            if command.capability == "fake.bright":
                authority.engage_lock(reason="he said stop mid-scene")
            return result

    adapter = Stopper(fail_on={"fake.publish"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "evening",
            {
                "steps": [
                    {"capability": "fake.dim"},
                    {"capability": "fake.bright"},
                    {"capability": "fake.publish", "on_failure": "rollback"},
                ]
            },
        )
    )
    report = _run_scene(made, "evening", dry_run=False)
    assert report.rolled_back is True
    # Nothing was undone, because undoing is doing and everything is stopped.
    assert adapter.compensated == []
    assert set(report.uncompensated) == {"fake.dim", "fake.bright"}
    assert any(
        "not allowed" in step.compensation_detail for step in report.steps if step.ok
    )


def test_compensation_is_recorded_as_its_own_authorized_action(authority):
    bench = AdapterRegistry()
    adapter = FakeAdapter(fail_on={"fake.publish"})
    bench.register(adapter)
    made = DeviceActionRunner(authority=authority, adapters=bench)
    made.add_scene(
        parse_scene(
            "evening",
            {
                "steps": [
                    {"capability": "fake.dim"},
                    {"capability": "fake.publish", "on_failure": "rollback"},
                ]
            },
        )
    )
    _run_scene(made, "evening", dry_run=False)
    compensations = [
        row for row in authority.history(limit=50) if row["decided_basis"] == "compensation"
    ]
    assert compensations
    assert all(row["allowed"] == 1 for row in compensations)
    assert all(row["outcome_status"] in {"completed", "failed"} for row in compensations)


def test_a_simulated_step_is_never_compensated(authority):
    """A dry run did not happen, so there is nothing to undo and no adapter call."""

    decision = authority.authorize_compensation(original_request_id="nothing-like-this")
    assert decision.allowed is False
    assert "no recorded action" in decision.reason


def test_an_adapter_that_hangs_is_abandoned_at_the_timeout(authority):
    """The hole: the runner handed the timeout to the adapter and then waited forever."""

    import threading

    release = threading.Event()

    class Hanger(FakeAdapter):
        def execute(self, command):
            self.executed.append(command.capability)
            release.wait(30)
            return DeviceResult(True, "completed", command.capability, command.target, "late")

    bench = AdapterRegistry()
    bench.register(Hanger())
    made = DeviceActionRunner(authority=authority, adapters=bench)
    try:
        report = made.run(
            "fake.dim",
            source="voice",
            authorization_basis=BASIS_ORIGIN_TURN,
            origin_proof=_proof(made, [("fake.dim", "")]),
            dry_run=False,
            timeout_seconds=0.2,
        )
        assert report.ok is False
        assert report.steps[0].status == "timeout"
        assert "abandoned" in report.steps[0].detail
        assert authority.history(limit=1)[0]["outcome_status"] == "failed"
    finally:
        release.set()


def test_an_adapter_that_raises_is_reported_not_propagated(authority):
    class Exploder(FakeAdapter):
        def execute(self, command):
            raise RuntimeError("pactl fell over")

    bench = AdapterRegistry()
    bench.register(Exploder())
    made = DeviceActionRunner(authority=authority, adapters=bench)
    report = _live(made, "fake.dim")
    assert report.ok is False
    assert "pactl fell over" in report.steps[0].detail


# -- scene file parsing -----------------------------------------------------


def test_scene_file_round_trips(tmp_path):
    path = tmp_path / "scenes.json"
    path.write_text(
        json.dumps(
            {
                "scenes": {
                    "wind down": {
                        "description": "evening",
                        "steps": [{"capability": "fake.dim", "target": "lamp"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    scenes = load_scenes(path)
    assert set(scenes) == {"wind down"}
    assert scenes["wind down"].steps[0].capability == "fake.dim"


def test_a_missing_scene_file_is_simply_no_scenes(tmp_path):
    assert load_scenes(tmp_path / "absent.json") == {}


def test_a_broken_scene_file_is_refused_loudly(tmp_path):
    path = tmp_path / "scenes.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DeviceActionError):
        load_scenes(path)


def test_scene_validation_rejects_nonsense():
    with pytest.raises(DeviceActionError, match="no steps"):
        parse_scene("empty", {"steps": []})
    with pytest.raises(DeviceActionError, match="unknown failure mode"):
        parse_scene("x", {"steps": [{"capability": "fake.dim", "on_failure": "panic"}]})
    with pytest.raises(DeviceActionError, match="timeout"):
        parse_scene("x", {"steps": [{"capability": "fake.dim", "timeout_seconds": 9_999}]})
    with pytest.raises(DeviceActionError, match="no capability"):
        parse_scene("x", {"steps": [{"target": "lamp"}]})


def test_the_shipped_example_scene_file_parses():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config" / "serena-scenes.example.json"
    scenes = load_scenes(example)
    assert scenes
    for scene in scenes.values():
        assert scene.steps


# -- evidence does not leak -------------------------------------------------


def test_the_origin_turn_is_not_copied_into_action_evidence(runner, authority):
    _live(runner, "fake.dim", params={"origin": {"text": "a private thing he said"}})
    row = authority.history(limit=1)[0]
    assert "a private thing he said" not in json.dumps(dict(row))
    assert "withheld" in row["context_json"]


# -- the real adapters, without the real hardware ---------------------------


def test_the_default_bench_registers_every_adapter_present_or_not(tmp_path, authority):
    made = build_default_runner(authority=authority, scene_path=tmp_path / "none.json")
    assert set(made.adapters.names()) == {"laptop", "android", "home", "mqtt"}
    snapshot = made.availability()
    for name, entry in snapshot.items():
        assert "available" in entry, name
        assert isinstance(entry["capabilities"], dict)


def test_home_assistant_is_unavailable_and_says_why_when_unconfigured(monkeypatch):
    from core.adapters.home_assistant import HomeAssistantAdapter

    monkeypatch.delenv("SERENA_HOME_ASSISTANT_URL", raising=False)
    monkeypatch.delenv("SERENA_HOME_ASSISTANT_TOKEN", raising=False)
    adapter = HomeAssistantAdapter()
    status = adapter.status()
    assert status.available is False
    assert "SERENA_HOME_ASSISTANT_URL" in status.reason

    result = adapter.execute(DeviceCommand("home.light_on", "light.kitchen"))
    assert result.ok is False
    assert result.status == "unavailable"


def test_home_assistant_calls_the_right_service_against_a_fake_transport():
    from core.adapters.home_assistant import HomeAssistantAdapter

    calls = []

    class Response:
        def __init__(self, body):
            self._body = json.dumps(body).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, timeout=None):
        calls.append((request.method, request.full_url))
        if request.full_url.endswith("/api/"):
            return Response({"message": "API running."})
        return Response([{"entity_id": "light.kitchen", "state": "on"}])

    adapter = HomeAssistantAdapter(
        base_url="http://127.0.0.1:8123", token="fake-token", opener=opener
    )
    result = adapter.execute(DeviceCommand("home.light_on", "light.kitchen"))
    assert result.ok is True
    assert ("POST", "http://127.0.0.1:8123/api/services/light/turn_on") in calls


def test_home_assistant_refuses_an_entity_that_is_not_the_right_domain():
    from core.adapters.home_assistant import HomeAssistantAdapter

    adapter = HomeAssistantAdapter(
        base_url="http://127.0.0.1:8123",
        token="fake",
        opener=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    adapter.status = lambda: AdapterStatus(True)
    result = adapter.execute(DeviceCommand("home.light_on", "switch.kettle"))
    assert result.ok is False
    assert "not a light entity" in result.detail


def test_home_assistant_never_follows_a_redirect_with_the_token():
    """The hole: urlopen follows 302 by default, taking the bearer token along."""

    from urllib import error as urlerror

    from core.adapters.home_assistant import _no_redirect_opener

    handler = next(
        item for item in _no_redirect_opener().handlers if hasattr(item, "redirect_request")
    )
    with pytest.raises(urlerror.HTTPError, match="never followed"):
        handler.redirect_request(
            _FakeRequest("http://127.0.0.1:8123/api/"),
            None,
            302,
            "Found",
            {},
            "http://evil.example/steal",
        )


class _FakeRequest:
    def __init__(self, url):
        self.full_url = url


def test_mqtt_is_unavailable_without_a_broker(monkeypatch):
    from core.adapters.home_assistant import MqttAdapter

    monkeypatch.delenv("SERENA_MQTT_HOST", raising=False)
    status = MqttAdapter().status()
    assert status.available is False
    assert "SERENA_MQTT_HOST" in status.reason


def test_mqtt_refuses_a_bad_topic_instead_of_publishing_somewhere_else():
    """The hole: an invalid topic was rewritten to serena/invalid and published."""

    from core.adapters.home_assistant import MqttAdapter

    published = []

    class Client:
        def connect(self, *_a, **_k):
            return None

        def publish(self, topic, payload):
            published.append((topic, payload))

        def disconnect(self):
            return None

    adapter = MqttAdapter(host="127.0.0.1", client_factory=Client)
    for bad in ("", "a/#/b", "a/+/b", "../escape"):
        result = adapter.execute(
            DeviceCommand("mqtt.publish", bad, params={"payload": "1"})
        )
        assert result.ok is False, bad
        assert result.status == "rejected", bad
    assert published == []

    good = adapter.execute(
        DeviceCommand("mqtt.publish", "lamp/set", params={"payload": "on"})
    )
    assert good.ok is True
    assert published == [("serena/lamp/set", "on")]


def test_adb_reports_no_device_rather_than_pretending(monkeypatch):
    from core.adapters.android_adb import AndroidAdbAdapter

    def runner(argv, **_kwargs):
        assert argv[1] == "devices"
        return subprocess.CompletedProcess(argv, 0, "List of devices attached\n\n", "")

    adapter = AndroidAdbAdapter(runner=runner, adb_path="/usr/bin/adb")
    status = adapter.status()
    assert status.available is False
    assert "no Android device is connected" in status.reason

    result = adapter.execute(DeviceCommand("android.screen_on"))
    assert result.ok is False
    assert result.status == "unavailable"


def test_adb_notices_an_unauthorized_phone():
    from core.adapters.android_adb import AndroidAdbAdapter

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 0, "List of devices attached\nR58N12ABCDE\tunauthorized\n", ""
        )

    status = AndroidAdbAdapter(runner=runner, adb_path="/usr/bin/adb").status()
    assert status.available is False
    assert "has not authorized" in status.reason


def test_adb_builds_the_expected_keyevent_for_a_connected_fake_phone():
    from core.adapters.android_adb import AndroidAdbAdapter

    seen = []

    def runner(argv, **_kwargs):
        seen.append(argv)
        if argv[1] == "devices":
            return subprocess.CompletedProcess(
                argv, 0, "List of devices attached\nR58N12ABCDE\tdevice\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    adapter = AndroidAdbAdapter(runner=runner, adb_path="/usr/bin/adb", serial="R58N12ABCDE")
    result = adapter.execute(DeviceCommand("android.screen_on"))
    assert result.ok is True
    assert seen[-1] == [
        "/usr/bin/adb", "-s", "R58N12ABCDE", "shell", "input", "keyevent", "224"
    ]


def test_adb_refuses_an_app_target_that_is_not_a_package():
    from core.adapters.android_adb import AndroidAdbAdapter

    def runner(argv, **_kwargs):
        if argv[1] == "devices":
            return subprocess.CompletedProcess(
                argv, 0, "List of devices attached\nR58N12ABCDE\tdevice\n", ""
            )
        raise AssertionError("a rejected target must never reach adb")

    adapter = AndroidAdbAdapter(runner=runner, adb_path="/usr/bin/adb")
    result = adapter.execute(DeviceCommand("android.open_app", "spotify; rm -rf /"))
    assert result.ok is False
    assert result.status == "rejected"


def test_adb_has_no_shell_or_delete_capability():
    from core.adapters.android_adb import AndroidAdbAdapter

    names = set(AndroidAdbAdapter().capabilities())
    assert not {name for name in names if "shell" in name or "uninstall" in name or "wipe" in name}


def test_laptop_adapter_refuses_without_the_originating_turn():
    from core.adapters.laptop import LaptopAdapter

    adapter = LaptopAdapter(
        executor=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
        context_reader=lambda: {"session_type": "x11"},
    )
    result = adapter.execute(DeviceCommand("laptop.volume_up"))
    assert result.ok is False
    assert result.status == "unauthorized"


def test_laptop_adapter_delegates_to_the_existing_broker():
    from core.adapters.laptop import LaptopAdapter

    seen = {}

    class Result:
        ok = True
        status = "completed"
        message = "done"
        receipt_id = "abc"

    def executor(action, target, *, origin):
        seen.update({"action": action, "target": target, "origin": dict(origin)})
        return Result()

    adapter = LaptopAdapter(executor=executor, context_reader=lambda: {"session_type": "x11"})
    result = adapter.execute(
        DeviceCommand("laptop.volume_up", params={"origin": {"protocol": "voice"}})
    )
    assert result.ok is True
    assert seen["action"] == "volume_up"
    assert seen["origin"]["protocol"] == "voice"
