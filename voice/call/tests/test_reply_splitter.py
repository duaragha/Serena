"""First-clause latency contract for the brain-delta splitter (task 4).

The regression this guards: true-streaming TTS backends were constructed
with first_clause_chars=first_clause_hard_chars=260, which disabled early
first-clause emission entirely, first audio waited for a full sentence
boundary or a 260-char hard cut. Every backend now gets the fast-clause
defaults, tunable by env.
"""

from __future__ import annotations

import pytest

from voice.call.orchestrator import _reply_splitter


class _TrueStreamTTS:
    supports_true_stream = True


class _SentenceTTS:
    supports_true_stream = False


@pytest.mark.parametrize("tts", [_TrueStreamTTS(), _SentenceTTS(), object()])
def test_every_backend_gets_fast_first_clause(tts, monkeypatch):
    monkeypatch.delenv("SERENA_CALL_FIRST_CLAUSE_CHARS", raising=False)
    monkeypatch.delenv("SERENA_CALL_FIRST_CLAUSE_HARD_CHARS", raising=False)
    splitter = _reply_splitter(tts)
    # A long opening sentence must yield an early clause, not buffer to 260.
    out = splitter.feed(
        "Right now the konpeki launch is code complete, and the worktree has "
        "been sitting uncommitted for a week which is the actual blocker"
    )
    assert out, "first clause must flush before a sentence boundary"
    assert len(out[0]) <= 56 + 16  # hard cut + trailing word tolerance


def test_true_stream_first_clause_not_260(monkeypatch):
    monkeypatch.delenv("SERENA_CALL_FIRST_CLAUSE_CHARS", raising=False)
    monkeypatch.delenv("SERENA_CALL_FIRST_CLAUSE_HARD_CHARS", raising=False)
    splitter = _reply_splitter(_TrueStreamTTS())
    # 100 chars with a clause boundary: the old 260/260 config emitted
    # nothing here; the fix must emit the first clause.
    text = "Same status as an hour ago, konpeki still needs the supabase restore before anything else can move"
    out = splitter.feed(text)
    assert out and out[0].startswith("Same status as an hour ago,")


def test_env_knobs_override(monkeypatch):
    monkeypatch.setenv("SERENA_CALL_FIRST_CLAUSE_CHARS", "20")
    monkeypatch.setenv("SERENA_CALL_FIRST_CLAUSE_HARD_CHARS", "80")
    splitter = _reply_splitter(_TrueStreamTTS())
    assert splitter.first_clause_chars == 20
    assert splitter.first_clause_hard_chars == 80
