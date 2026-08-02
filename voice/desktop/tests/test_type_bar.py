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

from voice.brain_bridge import parse_client_message

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
