from __future__ import annotations

import hashlib
import io
import stat
from pathlib import Path

import voice.call.activate_wakeword as activation


def test_activation_is_one_model_to_frozen_live_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "hey_serena.onnx"
    source.write_bytes(b"onnx")
    manifest = tmp_path / "wakeword-acceptance.json"
    provenance = tmp_path / "hey_serena.provenance.json"
    model = tmp_path / "installed" / "hey_serena.onnx"
    token = tmp_path / "chat_token"
    token.write_text("private-token-value", encoding="utf-8")
    token.chmod(0o600)
    observation = tmp_path / "wakeword-acceptance.sqlite3"
    commands: list[list[str]] = []

    def fake_install(*_args, **_kwargs):
        return {"release": str(tmp_path / "release")}

    def fake_run(command: list[str]):
        commands.append(command)
        if "freeze" in command:
            manifest.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(activation, "install_exported_model", fake_install)
    monkeypatch.setattr(
        activation,
        "ensure_openwakeword_feature_models",
        lambda: {"ok": True, "models": {}},
    )
    monkeypatch.setattr(
        activation,
        "verify_installed_model",
        lambda: {"model_sha256": "a" * 64},
    )
    monkeypatch.setattr(activation, "install_units", lambda: list(activation.UNITS))
    monkeypatch.setattr(activation, "_run", fake_run)
    monkeypatch.setattr(
        activation,
        "_service_states",
        lambda names: {name: "active" for name in names},
    )
    monkeypatch.setattr(activation, "DEFAULT_ACCEPTANCE_MANIFEST", manifest)
    monkeypatch.setattr(activation, "DEFAULT_RUNTIME_PROVENANCE", provenance)
    monkeypatch.setattr(activation, "DEFAULT_RUNTIME_MODEL", model)
    monkeypatch.setattr(activation, "OBSERVATION_DB", observation)
    monkeypatch.setattr(activation, "CHAT_TOKEN", token)

    result = activation.activate(source)

    assert result["ok"] is True
    assert result["threshold"] == 0.55
    assert result["passive_observation"] == "started"
    assert any("doctor" in command for command in commands)
    freeze = next(command for command in commands if "freeze" in command)
    assert freeze[freeze.index("--threshold") + 1] == "0.55"
    start = next(command for command in commands if "enable" in command)
    assert "serena-wake-listener.service" in start
    assert "serena-work-supervisor.service" in start
    assert "serena-dot-overlay.service" not in start
    assert "serena-desk.service" not in start
    assert "serena-wakeword-acceptance.service" in start
    stop_legacy = next(command for command in commands if "disable" in command)
    assert "serena-desk.service" in stop_legacy
    assert "serena-brain-bridge.service" in stop_legacy
    assert "serena-dot-overlay.service" in stop_legacy


def test_feature_model_bootstrap_is_pinned_atomic_and_cached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payloads = {
        "embedding_model.onnx": b"embedding-model",
        "melspectrogram.onnx": b"melspectrogram-model",
    }
    specs = {
        name: {
            "url": f"https://models.invalid/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in payloads.items()
    }
    monkeypatch.setattr(activation, "OPENWAKEWORD_FEATURE_MODELS", specs)
    opened: list[str] = []

    def opener(url: str, *, timeout: float):
        assert timeout == 60
        opened.append(url)
        return io.BytesIO(payloads[url.rsplit("/", 1)[-1]])

    first = activation.ensure_openwakeword_feature_models(tmp_path, opener=opener)

    assert first["ok"] is True
    assert len(opened) == 2
    for name, payload in payloads.items():
        target = tmp_path / name
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == 0o444

    opened.clear()

    def fail_if_opened(*_args, **_kwargs):
        raise AssertionError("cached feature models must not hit the network")

    second = activation.ensure_openwakeword_feature_models(
        tmp_path,
        opener=fail_if_opened,
    )

    assert opened == []
    assert all(
        model["status"] == "cached" for model in second["models"].values()
    )


def test_activation_preserves_existing_observation_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "hey_serena.onnx"
    source.write_bytes(b"onnx")
    observation = tmp_path / "wakeword-acceptance.sqlite3"
    observation.write_bytes(b"evidence")
    monkeypatch.setattr(activation, "OBSERVATION_DB", observation)

    try:
        activation.activate(source, start_services=False)
    except ValueError as exc:
        assert "passive observation database already exists" in str(exc)
    else:
        raise AssertionError("activation replaced existing observation evidence")
