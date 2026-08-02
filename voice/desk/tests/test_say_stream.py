"""Her voice while a typed reply is still being written.

Two things went wrong live on 2026-08-01 after a typed message: the reply
text finished appearing about five seconds before she made any sound, and
then she stopped after six words with the dot field already back to idle.
The tests here pin the pieces of that: the engine is warmed before anyone is
waiting, playback starts on the first chunk of audio rather than the last,
every clause gets its own generation, a clause that produces nothing is
reported instead of swallowed, and the player is only finished when the
speaker process is.
"""

from __future__ import annotations

import asyncio

import pytest

from voice.desk import say


class _Chunk:
    def __init__(self, pcm: bytes, sample_rate: int = 24_000) -> None:
        self.pcm = pcm
        self.sample_rate = sample_rate


class _Backend:
    """A stand-in for Pocket that records how it was driven."""

    def __init__(self, *, chunks_per_clause: int = 3, silent: tuple[str, ...] = ()) -> None:
        self.chunks_per_clause = chunks_per_clause
        self.silent = silent
        self.spoken: list[tuple[int, str]] = []
        self.allowed: list[int] = []
        self.retired: list[int] = []
        self.cancelled: list[int] = []
        self.warms = 0
        self.emitted: list[str] = []

    async def warm(self) -> None:
        self.warms += 1

    def allow_generation(self, generation: int) -> None:
        self.allowed.append(generation)

    def retire_generation(self, generation: int) -> None:
        self.retired.append(generation)

    async def cancel(self, generation: int | None = None) -> None:
        self.cancelled.append(generation)

    async def stream(self, sentence: str, *, generation: int):
        self.spoken.append((generation, sentence))
        if sentence in self.silent:
            return
        for index in range(self.chunks_per_clause):
            await asyncio.sleep(0)
            self.emitted.append(f"{sentence}#{index}")
            yield _Chunk(b"\x01\x00" * 240)


class _Player:
    """A stand-in for the aplay subprocess, recording call order."""

    opened: list["_Player"] = []

    def __init__(self, rate: int, log: list[str]) -> None:
        self.rate = rate
        self.log = log
        self.fed: list[bytes] = []
        self.closed = False
        self.killed = False

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)
        self.log.append("fed")

    async def close(self) -> None:
        self.closed = True
        self.log.append("closed")

    async def kill(self) -> None:
        self.killed = True
        self.log.append("killed")


@pytest.fixture
def players(monkeypatch):
    log: list[str] = []
    made: list[_Player] = []

    async def _open(rate: int):
        player = _Player(rate, log)
        made.append(player)
        log.append("opened")
        return player

    monkeypatch.setattr(say._StreamingPlayer, "open", staticmethod(_open))
    return made, log


async def _drain(backend, clauses: list[str]) -> None:
    """Feed the clauses through the real queue the typed turn uses."""
    queue: asyncio.Queue = asyncio.Queue()
    for clause in clauses:
        await queue.put(clause)
    await queue.put(None)
    say._tts_backend = backend
    try:
        await say.speak_stream(queue)
    finally:
        say._tts_backend = None


def test_every_clause_after_the_first_is_still_spoken(players) -> None:
    """Live she said "you drive an Outlander PHEV," and stopped there.

    Each clause must reach the speaker, and each must carry its own
    generation: sending them all as generation 0 made cancellation mean
    "everything she will ever say", so it could never be aimed at one
    sentence.
    """
    made, _log = players
    backend = _Backend()
    clauses = ["you drive an Outlander PHEV,", "the plug-in hybrid one."]

    asyncio.run(_drain(backend, clauses))

    assert [sentence for _generation, sentence in backend.spoken] == clauses
    generations = [generation for generation, _sentence in backend.spoken]
    assert generations[0] != generations[1]
    assert generations == sorted(generations)
    assert backend.allowed == generations
    assert backend.retired == generations
    assert len(made) == 2
    assert all(player.closed for player in made)


def test_playback_starts_on_the_first_chunk_not_the_last(players) -> None:
    """Buffering a whole clause spends its entire synthesis as silence.

    That wasted second landed twice: before her first word, and again in the
    gap between clauses, which is what made her sound like she had trailed
    off mid-sentence.
    """
    made, log = players
    backend = _Backend(chunks_per_clause=4)

    asyncio.run(say.speak_clause(backend, "a clause with several chunks"))

    assert log[:2] == ["opened", "fed"]
    assert log.count("fed") == 4
    assert log[-1] == "closed"
    # The speaker was open and fed before synthesis had finished.
    assert len(backend.emitted) == 4
    assert made[0].fed


def test_a_clause_that_makes_no_audio_is_reported_not_swallowed(players, capsys) -> None:
    """Silence nobody logs looks exactly like her giving up mid-sentence."""
    made, _log = players
    backend = _Backend(silent=("nothing comes back",))

    asyncio.run(_drain(backend, ["nothing comes back", "but this one speaks."]))

    assert "no audio came back for clause" in capsys.readouterr().out
    # The next clause still gets said.
    assert [sentence for _generation, sentence in backend.spoken] == [
        "nothing comes back",
        "but this one speaks.",
    ]
    assert len(made) == 1


def test_an_interrupt_stops_the_speaker_and_the_engine(players) -> None:
    """Both are separate processes and keep going over the next reply."""
    made, log = players
    backend = _Backend(chunks_per_clause=200)

    async def scenario() -> None:
        task = asyncio.create_task(say.speak_clause(backend, "a long sentence"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert made and made[0].killed
    assert not made[0].closed
    assert backend.cancelled == [backend.spoken[0][0]]
    assert backend.retired == [backend.spoken[0][0]]


def test_the_engine_is_warmed_before_anyone_is_waiting(monkeypatch) -> None:
    """Cold, the first PCM arrived about seven seconds after the first clause.

    Building the backend lazily on that first clause charged the whole cold
    start to the first thing Raghav typed after a restart, so the reply had
    finished appearing before she made a sound.
    """
    backend = _Backend()
    monkeypatch.setattr(say, "_tts_backend", backend)

    warmed = asyncio.run(say.warm_backend())

    assert warmed is backend
    assert backend.warms == 1


def test_the_player_is_only_done_when_the_speaker_process_is(monkeypatch) -> None:
    """"The queue drained" is not "she stopped talking".

    Going idle on the first is what dropped the dot field while audio was
    still coming out of the laptop, so closing the player has to wait for the
    speaker process to finish playing what it was handed.
    """
    monkeypatch.setattr(
        say._StreamingPlayer,
        "argv",
        staticmethod(lambda rate: ["sh", "-c", "cat >/dev/null; sleep 0.4"]),
    )

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        player = await say._StreamingPlayer.open(24_000)
        await player.feed(b"\x01\x00" * 240)
        await player.close()
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) >= 0.4
