"""The overlay's type bar: input channel when the microphone is unusable.

Typing must produce the same turn speaking does, and the overlay must still
be there to type into after a spoken conversation ends, otherwise the
fallback disappears at exactly the moment the microphone is the problem.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from voice.brain_bridge import new_typed_call_id, parse_client_message

DESKTOP = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fix the phev tracker", "fix the phev tracker"),
        ("  spaced   out   words  ", "spaced out words"),
        ("line\nbreaks\tcollapse", "line breaks collapse"),
    ],
)
def test_typed_messages_are_accepted_and_normalised(text: str, expected: str) -> None:
    out = parse_client_message(json.dumps({"type": "typed", "text": text}))
    assert out is not None
    assert json.loads(out) == {"type": "typed", "text": expected}


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "typed"},
        {"type": "typed", "text": ""},
        {"type": "typed", "text": "   "},
        {"type": "typed", "text": 42},
        {"type": "typed", "text": "x" * 4_001},
        {"type": "typed", "text": "ok", "extra": "field"},
    ],
)
def test_malformed_typed_messages_are_rejected(payload: dict) -> None:
    assert parse_client_message(json.dumps(payload)) is None


def test_amplitude_still_works_alongside_typing() -> None:
    out = parse_client_message(json.dumps({"type": "amplitude", "value": 0.5}))
    assert out is not None and json.loads(out)["type"] == "amplitude"


@pytest.mark.parametrize("kind", ["transcription", "response"])
def test_desk_turn_text_is_accepted_without_its_private_envelope(kind: str) -> None:
    out = parse_client_message(json.dumps({"type": kind, "text": "  visible words  "}))
    assert out is not None
    assert json.loads(out) == {"type": kind, "text": "visible words"}

    assert parse_client_message(
        json.dumps(
            {
                "type": kind,
                "text": "visible words",
                "meta": {"tool_result": "private"},
            }
        )
    ) is None


def test_typed_turn_ids_cannot_collide_within_one_second() -> None:
    first = new_typed_call_id()
    second = new_typed_call_id()
    assert first.startswith("desk-typed-")
    assert second.startswith("desk-typed-")
    assert first != second


def test_overlay_history_is_collapsed_bounded_and_text_only() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")

    history_tag = html.split('id="conversation-history"', 1)[0].rsplit("<details", 1)[1]
    assert " open" not in history_tag
    assert 'id="conversation-history-list"' in html
    assert "max-height: 260px" in css and "overflow-y: auto" in css
    assert "const HISTORY_LIMIT = 12" in app
    assert "conversationHistory.splice" in app
    assert "body.textContent = text" in app
    history_renderer = app.split("function renderConversationHistory", 1)[1].split(
        "function typewriterEffect", 1
    )[0]
    assert ".innerHTML" not in history_renderer


def test_overlay_survives_a_finished_conversation() -> None:
    """A clean desk-voice exit must not take the type bar down with it."""
    source = (DESKTOP / "supervisor.py").read_text(encoding="utf-8")
    assert 'if name == "desk-voice" and return_code == 0:' in source
    assert "keeping the overlay and bridge up for typing" in source


def test_wake_restarts_the_unit_so_the_microphone_returns() -> None:
    """With the unit staying up, a plain `start` would never rearm the mic."""
    source = (DESKTOP.parent / "desk" / "wake_listener.py").read_text(encoding="utf-8")
    assert '"restart",' in source
    assert '"start",' not in source.split("FULL_VOICE_UNIT")[0][-400:]


@pytest.mark.parametrize("script", ["main.js", "preload.js", "renderer/app.js"])
def test_electron_sources_parse(script: str) -> None:
    result = subprocess.run(
        ["node", "--check", str(DESKTOP / script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_type_bar_is_wired_end_to_end() -> None:
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")

    assert 'id="type-input"' in html and 'id="type-bar"' in html
    assert "#type-bar" in css
    assert "sendTyped" in preload and "typed-message" in preload
    assert "typed-message" in main and "type: 'typed'" in main
    assert "sendTyped" in app
    # Typing must never be swallowed by the overlay's global shortcuts.
    assert "stopPropagation" in app


def test_a_new_message_interrupts_her_instead_of_being_refused() -> None:
    """She speaks far slower than he types.

    Serialising typed turns meant the second message got "one sec, still on the
    last one" almost every time, which is not how interrupting someone works.
    """
    source = (DESKTOP.parent / "brain_bridge.py").read_text(encoding="utf-8")
    # The docstring still quotes the old line; it must not be a reply any more.
    assert '"text": "one sec' not in source
    assert "_interrupt_typed_turn" in source
    assert "previous.cancel()" in source


def test_interrupted_playback_kills_the_player() -> None:
    """aplay is a separate process; cancelling the task leaves it talking."""
    source = (DESKTOP.parent / "desk" / "say.py").read_text(encoding="utf-8")
    play = source.split("async def speak_clause", 1)[1].split("\nclass ", 1)[0]
    assert "except asyncio.CancelledError:" in play
    assert "await player.kill()" in play
    # The engine is a separate process too, and it keeps synthesising the
    # sentence she was interrupted out of unless the generation is cancelled.
    assert "await cancel(generation)" in play


def test_the_speed_slider_is_wired_end_to_end() -> None:
    """A slider that moved nothing would be worse than no slider."""
    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")

    assert 'id="speed-range"' in html and 'type="range"' in html
    assert "#speed-range" in css
    assert "setVoiceSpeed" in preload and "set-voice-speed" in preload
    assert "set-voice-speed" in main and "voice_speed" in main
    assert "setVoiceSpeed" in app
    # The saved rate must come back after a restart, not reset to 1.
    assert "onVoiceSpeed" in app and "onVoiceSpeed" in preload


def test_the_overlay_and_python_agree_on_the_settings_file() -> None:
    """Two processes writing different paths is the classic way a slider
    silently does nothing."""
    from voice.call.voice_speed import DEFAULT_SPEED_PATH

    main = (DESKTOP / "main.js").read_text(encoding="utf-8")
    assert f"'{DEFAULT_SPEED_PATH.name}'" in main
    assert "'.config', 'serena'" in main


def test_the_voice_mute_button_is_persistent_and_wired_to_laptop_playback() -> None:
    from voice.desk.output_mute import DEFAULT_OUTPUT_MUTE_PATH

    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")
    playback = (DESKTOP.parent / "desk" / "io.py").read_text(encoding="utf-8")
    typed = (DESKTOP.parent / "desk" / "say.py").read_text(encoding="utf-8")

    assert 'id="voice-mute"' in html and 'aria-pressed="false"' in html
    assert "#voice-mute" in css and '[aria-pressed="true"]' in css
    assert "setVoiceMuted" in app and "onVoiceMuted" in app
    assert "set-voice-muted" in preload and "voice-muted" in preload
    assert f"'{DEFAULT_OUTPUT_MUTE_PATH.name}'" in main
    assert "set-voice-muted" in main and "voice-muted" in main
    assert "read_voice_output_muted" in playback
    assert "read_voice_output_muted" in typed


def test_the_microphone_mute_is_serena_only_and_wired_end_to_end() -> None:
    """Muting Serena must leave the system source available to OpenWhispr."""

    from voice.desk.input_mute import DEFAULT_INPUT_MUTE_PATH

    html = (DESKTOP / "renderer" / "index.html").read_text(encoding="utf-8")
    css = (DESKTOP / "renderer" / "styles.css").read_text(encoding="utf-8")
    app = (DESKTOP / "renderer" / "app.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")
    client = (DESKTOP.parent / "desk" / "client.py").read_text(encoding="utf-8")
    listener = (DESKTOP.parent / "desk" / "wake_listener.py").read_text(
        encoding="utf-8"
    )

    assert 'id="microphone-mute"' in html and 'aria-pressed="false"' in html
    assert "#microphone-mute" in css and 'mic off' in app
    assert "setMicrophoneMuted" in app and "onMicrophoneMuted" in app
    assert "set-microphone-muted" in preload and "microphone-muted" in preload
    assert f"'{DEFAULT_INPUT_MUTE_PATH.name}'" in main
    assert "set-microphone-muted" in main and "microphone-muted" in main
    assert "read_voice_input_muted" in client
    assert "read_voice_input_muted" in listener
    assert "pactl" not in main and "wpctl" not in main


def test_the_dot_goes_idle_only_after_the_last_word_is_played(monkeypatch) -> None:
    """Live she went idle the instant the reply text finished appearing.

    The turn must hold the overlay until the player has actually finished,
    and the response text may reach the screen long before that.
    """
    import asyncio

    from voice.desk import say
    from voice import brain_bridge

    states: list[str] = []
    monkeypatch.setattr(say, "set_state", states.append)

    played: list[str] = []

    async def speak_stream(queue) -> None:
        while True:
            clause = await queue.get()
            if clause is None:
                return
            await asyncio.sleep(0.05)
            played.append(clause)

    async def stream_turn(text, *, call_id, turn_id, timeout, on_sentence=None):
        await on_sentence("first clause,")
        await on_sentence("and the rest of it.")
        return "first clause, and the rest of it."

    monkeypatch.setattr(say, "speak_stream", speak_stream)
    monkeypatch.setattr(say, "stream_turn", stream_turn)
    sent: list[str] = []

    async def broadcast(message, *, exclude=None) -> None:
        sent.append(message)
        if '"response"' in message:
            # Nothing must have gone idle by the time the text is on screen.
            assert states[-1] != "idle"

    monkeypatch.setattr(brain_bridge, "broadcast", broadcast)
    monkeypatch.setattr(brain_bridge, "_typed_turn", None)

    asyncio.run(brain_bridge.run_typed_turn("what car do i drive"))

    assert played == ["first clause,", "and the rest of it."]
    assert states == ["thinking", "speaking", "idle"]


def test_an_interrupted_turn_does_not_drop_the_dot_on_its_replacement(monkeypatch) -> None:
    """The turn that was talked over has already handed the overlay on.

    Typing again while she is speaking cancels the old turn. If that old turn
    still wrote idle on its way out, it would blank the dot field on the reply
    that replaced it.
    """
    import asyncio

    from voice.desk import say
    from voice import brain_bridge

    states: list[str] = []
    monkeypatch.setattr(say, "set_state", states.append)

    async def scenario() -> None:
        newer = asyncio.create_task(asyncio.sleep(0.01))
        monkeypatch.setattr(brain_bridge, "_typed_turn", newer)
        brain_bridge._finish_typed_turn()
        await newer

    asyncio.run(scenario())

    assert states == []


def test_typed_turn_retries_the_brain_socket_while_it_warms(monkeypatch) -> None:
    import asyncio

    from voice.desk import say
    from voice import brain_bridge

    attempts = 0

    async def stream_turn(_text, *, call_id, turn_id, timeout, on_sentence=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError(111, "Connection refused")
        return "ready after startup"

    async def speak_stream(queue) -> None:
        while await queue.get() is not None:
            pass

    async def broadcast(_message, *, exclude=None) -> None:
        return None

    monkeypatch.setattr(say, "stream_turn", stream_turn)
    monkeypatch.setattr(say, "speak_stream", speak_stream)
    monkeypatch.setattr(say, "set_state", lambda _state: None)
    monkeypatch.setattr(brain_bridge, "broadcast", broadcast)
    monkeypatch.setattr(brain_bridge, "_typed_turn", None)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(brain_bridge.asyncio, "sleep", lambda _seconds: real_sleep(0))

    asyncio.run(brain_bridge.run_typed_turn("wait for the brain"))

    assert attempts == 2


def test_her_voice_is_warmed_before_the_first_typed_message() -> None:
    """Cold, the first PCM landed about seven seconds after the first clause.

    Paid on the first typed turn after a restart, that whole cold start was
    silence with the reply already on screen.
    """
    source = (DESKTOP.parent / "brain_bridge.py").read_text(encoding="utf-8")
    assert "warm_voice" in source
    assert "asyncio.create_task(warm_voice())" in source


def test_ownership_is_claimed_before_the_old_turn_dies() -> None:
    """Two messages arriving together must serialize, not both run.

    Sol reproduced the race: A and B both captured turn C, both awaited it,
    then both ran at once with only B recorded as the owner.
    """
    import asyncio

    from voice import brain_bridge

    async def scenario():
        order = []

        async def fake_turn(name, delay):
            await brain_bridge._interrupt_typed_turn()
            order.append(f"{name}-start")
            try:
                await asyncio.sleep(delay)
                order.append(f"{name}-finish")
            except asyncio.CancelledError:
                order.append(f"{name}-cancelled")
                raise

        first = asyncio.create_task(fake_turn("A", 5.0))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(fake_turn("B", 0.05))
        third = asyncio.create_task(fake_turn("C", 0.05))
        await asyncio.gather(second, third, return_exceptions=True)
        await asyncio.gather(first, return_exceptions=True)
        brain_bridge._typed_turn = None
        return order

    order = asyncio.run(scenario())
    # A must be cancelled, and at most one of B/C may actually finish.
    assert "A-cancelled" in order
    finished = [item for item in order if item.endswith("-finish")]
    assert len(finished) == 1, order


def test_the_player_is_reaped_on_every_exit_path() -> None:
    """A cancellation between the text broadcast and the queue sentinel used
    to orphan the player, which held the tts lock forever and hung every
    later typed message behind it."""
    source = (DESKTOP.parent / "brain_bridge.py").read_text(encoding="utf-8")
    body = source.split("async def run_typed_turn", 1)[1].split("\nasync def ", 1)[0]
    assert "finally:" in body
    reap = body.split("finally:", 1)[1]
    assert "player.cancel()" in reap and "await player" in reap


def test_cancellation_during_close_still_kills_the_speaker() -> None:
    """Sol reproduced player_killed=False when the cancel landed inside
    close(); the old clause then talked over the reply that interrupted it."""
    source = (DESKTOP.parent / "desk" / "say.py").read_text(encoding="utf-8")
    body = source.split("async def speak_clause", 1)[1].split("\nclass ", 1)[0]
    inner_try = body.split("try:", 2)[2].split("except asyncio.CancelledError:")[0]
    assert "await player.close()" in inner_try


def test_coding_panel_shows_durable_job_evidence_and_controls() -> None:
    panel = (DESKTOP / "renderer" / "code-panel.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")

    for label in (
        "project",
        "brief",
        "progress",
        "changes",
        "tests",
        "live proof",
        "evidence",
    ):
        assert f"'{label}'" in panel
    for control in ("status", "cancel", "steer", "resume"):
        assert f"'{control}'" in panel
    assert "sendCodeControl" in preload
    assert "showCodePanel" in preload
    assert "case 'code_snapshot'" in main
    assert "code-control-result" in main
    assert "currentCodeSnapshot = msg.snapshot" in main
    assert "event.sender.send('code-snapshot', currentCodeSnapshot)" in main
    assert "ipcMain.on('show-code-panel', showCodePanel)" in main
    assert "codePanelAvailable" in main
    assert "code-panel__reopen" in panel
    assert "if (data.snapshot) {" in (DESKTOP / "renderer" / "index.html").read_text(
        encoding="utf-8"
    )
