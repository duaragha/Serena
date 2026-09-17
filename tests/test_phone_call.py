"""Serena's SIP line: the PC-side client and the container bridge's pure parts."""

from __future__ import annotations

import json
import queue
import threading
import urllib.request

import pytest

from core import phone_call
from voice.phone import bridge


@pytest.fixture
def line(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_call.Path, "home", classmethod(lambda cls: tmp_path))
    config_dir = tmp_path / ".config" / "serena"
    config_dir.mkdir(parents=True)
    (config_dir / "phone-call-token").write_text("secret", encoding="utf-8")
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        return 202, {"ok": True, "request_id": f"r{len(calls)}"}

    monkeypatch.setattr(phone_call, "_request", fake_request)
    return calls


def test_enabled_needs_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_call.Path, "home", classmethod(lambda cls: tmp_path))
    assert not phone_call.enabled()
    token = tmp_path / ".config" / "serena" / "phone-call-token"
    token.parent.mkdir(parents=True)
    token.write_text("x", encoding="utf-8")
    assert phone_call.enabled()
    (token.parent / "phone-call.json").write_text('{"enabled": false}', encoding="utf-8")
    assert not phone_call.enabled()


def test_place_rings_once_per_window(line):
    assert phone_call.place("task 7 is done", now=1_000)
    assert not phone_call.place("task 8 is done", now=1_300)
    assert phone_call.place("task 9 is done", now=1_700)
    assert [body["text"] for _, _, body in line] == ["task 7 is done", "task 9 is done"]


def test_force_skips_the_window_but_a_key_never_repeats(line):
    assert phone_call.place("first", key="task:1:done", now=1_000)
    assert phone_call.place("second", force=True, now=1_010)
    assert not phone_call.place("first again", key="task:1:done", force=True, now=5_000)
    assert len(line) == 2


def test_refusal_is_an_error(tmp_path, monkeypatch, line):
    monkeypatch.setattr(phone_call, "_request",
                        lambda *args, **kwargs: (409, {"error": "already on a call"}))
    with pytest.raises(phone_call.PhoneCallError, match="already on a call"):
        phone_call.place("hello", now=1_000)
    # A refused call does not consume the window.
    assert not (tmp_path / ".local" / "state" / "serena" / "phone-call-state.json").exists()


def test_call_channel_is_registered():
    from core.notification_authority import CHANNELS
    from core.notification_senders import DEFAULT_SENDERS

    assert "call" in CHANNELS
    assert DEFAULT_SENDERS["call"].__name__ == "send_call"


def test_ring_phone_stays_quiet_at_night(monkeypatch):
    from core import scheduler_actions
    from core.notification_senders import default_authority

    monkeypatch.setattr(phone_call, "enabled", lambda: True)
    monkeypatch.setattr(default_authority().policy.__class__, "in_quiet_hours",
                        lambda self, moment: True)
    sent = []
    monkeypatch.setattr("core.notification_senders.notify",
                        lambda *args, **kwargs: sent.append(args))
    assert scheduler_actions._ring_phone("hi", "task:1:done") is False
    assert sent == []


def _config(**overrides):
    values = dict(sip_user="its_serena", sip_password="pw", sip_domain="sip.linphone.org",
                  raghav_user="its_raghav", voice_url="ws://x/ws/desk",
                  voice_token="voice", control_token="control",
                  control_host="127.0.0.1", control_port=0)
    values.update(overrides)
    return bridge.PhoneConfig(**values)


def test_only_raghav_gets_through():
    config = _config()
    assert bridge.caller_allowed("its_raghav", "sip.linphone.org", config)
    assert bridge.caller_allowed("ITS_RAGHAV", "SIP.linphone.org", config)
    assert not bridge.caller_allowed("its_raghav", "evil.example", config)
    assert not bridge.caller_allowed("someone", "sip.linphone.org", config)
    assert not bridge.caller_allowed("", "", config)


def test_config_requires_its_secrets(tmp_path):
    sip = tmp_path / "sip.env"
    sip.write_text("SIP_USER=its_serena\nSIP_PASSWORD=pw\nRAGHAV_SIP=its_raghav\n",
                   encoding="utf-8")
    env = {"PHONE_SIP_ENV": str(sip),
           "SERENA_VOICE_TOKEN_FILE": str(tmp_path / "missing"),
           "PHONE_CONTROL_TOKEN_FILE": str(tmp_path / "missing")}
    with pytest.raises(ValueError, match="voice token"):
        bridge.PhoneConfig.from_env(env)
    (tmp_path / "voice").write_text("v\n", encoding="utf-8")
    (tmp_path / "control").write_text("c\n", encoding="utf-8")
    env.update(SERENA_VOICE_TOKEN_FILE=str(tmp_path / "voice"),
               PHONE_CONTROL_TOKEN_FILE=str(tmp_path / "control"))
    config = bridge.PhoneConfig.from_env(env)
    assert (config.sip_user, config.raghav_user, config.voice_token) == (
        "its_serena", "its_raghav", "v")


def _tone(level: int, samples: int = 320) -> bytes:
    import numpy as np

    return (np.ones(samples) * level).astype("<i2").tobytes()


def test_voice_gate_needs_sustained_speech():
    gate = bridge.VoiceGate(threshold=700, sustain_ms=100)
    assert not any(gate.feed(_tone(2000)) for _ in range(4))
    assert gate.feed(_tone(2000))
    assert len(gate.frames) == 5
    gate.feed(_tone(10))
    assert gate.run == 0 and gate.frames == []


class _FakeBridge:
    def __init__(self):
        self.calls = []

    def health(self):
        return {"ok": True}

    def request_call(self, text):
        self.calls.append(text)
        return 202, {"ok": True, "request_id": "abc"}

    def request_hangup(self):
        self.calls.append("<hangup>")


def _post(port, path, body, token):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_control_server_checks_the_token():
    fake = _FakeBridge()
    server = bridge.ControlServer(_config(), fake)
    server.start()
    port = server.httpd.server_address[1]
    try:
        assert _post(port, "/call", {"text": "hi"}, "wrong")[0] == 401
        assert _post(port, "/call", {"text": "   "}, "control")[0] == 400
        status, payload = _post(port, "/call", {"text": "task  7\ndone"}, "control")
        assert (status, payload["request_id"]) == (202, "abc")
        assert _post(port, "/hangup", {}, "control")[0] == 202
    finally:
        server.stop()
    assert fake.calls == ["task 7 done", "<hangup>"]


def test_bridge_refuses_a_second_call():
    phone = bridge.PhoneBridge(_config())
    assert phone.request_call("x")[0] == 503
    phone._set_state(registered=True)
    assert phone.request_call("x")[0] == 202
    assert phone.request_call("y")[0] == 409
    assert phone.commands.get_nowait()[0] == "call"


class _Event:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


class _Frame:
    def __init__(self, pcm):
        self.pcm = pcm


class _FakeTransport:
    def __init__(self):
        self.events = queue.Queue()
        self.generation = 0
        self.failure = ""
        self.sent = []
        self.cancelled = 0
        self.hung_up = False

    def connect(self):
        pass

    def wait_ready(self, timeout=None):
        return True

    def begin_listening(self):
        self.generation += 1
        return self.generation

    def send_mic_frame(self, pcm):
        self.sent.append(pcm)

    def cancel(self):
        self.cancelled += 1

    def hangup(self):
        self.hung_up = True


class _FakeAudio:
    def __init__(self):
        self.frames = queue.Queue()
        self.played = []
        self.flushed = 0

    def drain(self):
        pass

    def play(self, pcm, rate):
        self.played.append((len(pcm), rate))

    def flush(self):
        self.flushed += 1


def test_conversation_relays_both_directions_and_takes_interruptions():
    transport = _FakeTransport()
    audio = _FakeAudio()
    finished = []
    talk = bridge.Conversation(
        _config(), audio, opening="", transport_factory=lambda: transport,
        speak=lambda text: (b"", 24_000), on_finished=finished.append)
    talk.start()

    for _ in range(10):  # 200 ms of him talking -> exactly one wire frame
        audio.frames.put(_tone(3000))
    deadline = threading.Event()
    for _ in range(100):
        if transport.sent:
            break
        deadline.wait(0.02)
    assert len(transport.sent) == 1 and len(transport.sent[0]) == 6400

    transport.events.put(_Event("control", {"type": "stt.result", "generation": 1,
                                            "text": "what's up"}))
    transport.events.put(_Event("control", {"type": "audio.start", "generation": 1,
                                            "sample_rate": 24_000}))
    transport.events.put(_Event("audio", _Frame(b"\x01\x00" * 480)))
    for _ in range(100):
        if audio.played:
            break
        deadline.wait(0.02)
    assert audio.played == [(960, 24_000)]

    for _ in range(20):  # he talks over her
        audio.frames.put(_tone(3000))
    for _ in range(100):
        if transport.cancelled:
            break
        deadline.wait(0.02)
    assert transport.cancelled == 1 and audio.flushed == 1
    assert transport.generation == 2

    talk.stop()
    talk.thread.join(timeout=5)
    assert transport.hung_up and finished == ["hung up"]
    assert talk.transcript == [("raghav", "what's up")]


def test_extra_callers_must_be_listed_explicitly():
    config = _config(allowed_callers=("its_serena",))
    assert bridge.caller_allowed("its_serena", "sip.linphone.org", config)
    assert bridge.caller_allowed("its_raghav", "sip.linphone.org", config)
    assert not bridge.caller_allowed("its_serena", "other.example", config)


def test_caller_gain_boosts_and_clips():
    import numpy as np

    loud = bridge.apply_gain(_tone(2000, 4), 4.0)
    assert np.frombuffer(loud, dtype="<i2").tolist() == [8000] * 4
    clipped = bridge.apply_gain(_tone(20000, 2), 4.0)
    assert np.frombuffer(clipped, dtype="<i2").tolist() == [32767] * 2


def test_flush_drops_speech_that_was_already_queued():
    link = bridge.AudioLink()
    link.play(b"\x00\x00" * 10, 24_000)
    link.flush()
    link.play(b"\x01\x00" * 10, 24_000)
    queued = []
    while not link._outbox.empty():
        queued.append(link._outbox.get_nowait())
    assert [epoch for epoch, _, _ in queued] == [1]
