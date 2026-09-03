"""Incremental plain-prose sentence splitting for streaming TTS."""

from __future__ import annotations

import re

_BOUNDARY = re.compile(r"[.!?]+(?:[\"')\]]+)?(?=\s|$)")
_CLAUSE_BOUNDARY = re.compile(r"[,;:](?=\s)")
_SPACE = re.compile(r"\s+")
_ABBREVIATIONS = {
    "dr.",
    "mr.",
    "mrs.",
    "ms.",
    "prof.",
    "sr.",
    "jr.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
}


class IncrementalSentenceSplitter:
    def __init__(
        self,
        *,
        max_chars: int = 260,
        first_clause_chars: int = 12,
        first_clause_hard_chars: int = 56,
    ) -> None:
        if max_chars < 32:
            raise ValueError("max_chars must be at least 32")
        if not 8 <= first_clause_chars <= first_clause_hard_chars:
            raise ValueError("first clause thresholds are invalid")
        if first_clause_hard_chars > max_chars:
            raise ValueError("first clause hard limit cannot exceed max_chars")
        self.max_chars = max_chars
        self.first_clause_chars = first_clause_chars
        self.first_clause_hard_chars = first_clause_hard_chars
        self._buffer = ""
        self._emitted = False

    @property
    def pending(self) -> str:
        return self._buffer

    def reset(self) -> None:
        self._buffer = ""
        self._emitted = False

    def feed(self, delta: str) -> list[str]:
        if not isinstance(delta, str):
            raise TypeError("sentence delta must be text")
        self._buffer += delta
        return self._drain(allow_tail_boundary=True)

    def flush(self) -> list[str]:
        sentences = self._drain(allow_tail_boundary=True)
        tail = _clean(self._buffer)
        self._buffer = ""
        if tail:
            sentences.append(tail)
        return sentences

    def _drain(self, *, allow_tail_boundary: bool) -> list[str]:
        out: list[str] = []
        while self._buffer:
            boundary = self._first_boundary(allow_tail_boundary)
            clause = self._first_clause_boundary()
            cut = min(
                value for value in (boundary, clause) if value is not None
            ) if boundary is not None or clause is not None else None
            if cut is not None:
                sentence = _clean(self._buffer[:cut])
                self._buffer = self._buffer[cut:].lstrip()
                if sentence:
                    out.append(sentence)
                    self._emitted = True
                continue
            if len(self._buffer) > self.max_chars:
                cut = self._buffer.rfind(" ", 0, self.max_chars + 1)
                if cut < 1:
                    cut = self.max_chars
                sentence = _clean(self._buffer[:cut])
                self._buffer = self._buffer[cut:].lstrip()
                if sentence:
                    out.append(sentence)
                    self._emitted = True
                continue
            break
        return out

    def _first_boundary(self, allow_tail: bool) -> int | None:
        for match in _BOUNDARY.finditer(self._buffer):
            end = match.end()
            if end == len(self._buffer) and not allow_tail:
                continue
            candidate = self._buffer[:end].strip().lower()
            final_word = candidate.rsplit(" ", 1)[-1].strip('"\')]}')
            if final_word in _ABBREVIATIONS:
                continue
            if _looks_like_decimal(self._buffer, match.start()):
                continue
            return end
        return None

    def _first_clause_boundary(self) -> int | None:
        if self._emitted or len(self._buffer) < self.first_clause_chars:
            return None
        for match in _CLAUSE_BOUNDARY.finditer(self._buffer):
            if match.end() >= 8:
                return match.end()
        if len(self._buffer) < self.first_clause_hard_chars:
            return None
        cut = self._buffer.rfind(" ", 0, self.first_clause_hard_chars + 1)
        return cut if cut >= 8 else self.first_clause_hard_chars


def _looks_like_decimal(text: str, punctuation_index: int) -> bool:
    return (
        text[punctuation_index : punctuation_index + 1] == "."
        and punctuation_index > 0
        and punctuation_index + 1 < len(text)
        and text[punctuation_index - 1].isdigit()
        and text[punctuation_index + 1].isdigit()
    )


def _clean(text: str) -> str:
    return _SPACE.sub(" ", text).strip()
