"""Main voice loop — orchestrates wake word → listen → transcribe → think → speak."""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic
import numpy as np
import sounddevice as sd

from serena.brain.claude import ClaudeBrain
from serena.config import SerenaConfig
from serena.voice.audio import AudioCapture, play_audio_async
from serena.voice.stt import SpeechToText
from serena.voice.tts import TextToSpeech
from serena.voice.vad import VoiceActivityDetector
from serena.voice.wakeword import WakeWordDetector

if TYPE_CHECKING:
    from serena.ipc.server import IPCServer

logger = logging.getLogger(__name__)

# Project directory for matching spoken project names
_PROJECTS_DIR = Path.home() / "Documents" / "Projects"

# Phrases that indicate the user doesn't want to pick a project
_SKIP_PHRASES = {"nothing", "no", "nah", "just chatting", "just chat", "nevermind", "never mind", "none"}

# Retry configuration for Claude API calls
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds — doubles each retry

# Mic reconnection delay
_MIC_RECONNECT_DELAY = 5.0

# Follow-up mode: seconds to keep listening after a response before going idle
_FOLLOW_UP_TIMEOUT = 25.0


class State(enum.Enum):
    IDLE = "idle"           # Listening for wake word
    LISTENING = "listening"  # Wake word heard, capturing speech
    THINKING = "thinking"    # Transcribing + Claude processing
    SPEAKING = "speaking"    # TTS playback


class VoiceLoop:
    """Orchestrates the full voice conversation loop.

    Flow: wake word → VAD capture → STT → Claude → TTS → repeat
    """

    def __init__(
        self,
        config: SerenaConfig,
        brain: ClaudeBrain,
        *,
        ipc: IPCServer | None = None,
    ) -> None:
        self._config = config
        self._brain = brain
        self._ipc = ipc
        self._state = State.IDLE
        self._running = False

        # Components -- initialized lazily in start()
        self._audio: AudioCapture | None = None
        self._wakeword: WakeWordDetector | None = None
        self._vad: VoiceActivityDetector | None = None
        self._stt: SpeechToText | None = None
        self._tts: TextToSpeech | None = None

        # Active project (set via startup greeting or "switch to" command)
        self._active_project: str | None = None

        # CodeTool reference for voice commands (wired in via set_code_tool)
        self._code_tool: Any = None

        # Signals
        self._wake_event = asyncio.Event()
        self._utterance_ready = asyncio.Event()
        self._utterance_audio: np.ndarray | None = None
        self._muted = False
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None

    @property
    def state(self) -> State:
        return self._state

    @property
    def active_project(self) -> str | None:
        return self._active_project

    def set_code_tool(self, tool: Any) -> None:
        """Wire in the CodeTool so voice commands can control it."""
        self._code_tool = tool

    def _set_state(self, new_state: State) -> None:
        if new_state != self._state:
            logger.info("State: %s -> %s", self._state.value, new_state.value)
            self._state = new_state
            if self._ipc:
                logger.info("IPC: broadcasting state '%s' to %d clients", new_state.value, self._ipc.client_count)
                asyncio.create_task(self._send_state_safe(new_state.value))

    async def _send_state_safe(self, state: str) -> None:
        try:
            await self._ipc.send_state(state)
            logger.info("IPC: state '%s' sent successfully", state)
        except Exception:
            logger.exception("IPC: failed to send state")

    # --- Startup greeting (Task 6.1) ---

    async def _startup_greeting(self) -> None:
        """Speak a time-appropriate greeting and ask what project to work on.

        Listens for the user's response without requiring a wake word,
        then tries to match the spoken text to a project directory.
        """
        # Build greeting based on time of day
        hour = datetime.now().hour
        if hour < 12:
            greeting = "good morning. what are we working on today?"
        elif hour < 17:
            greeting = "hey. what are we working on today?"
        else:
            greeting = "good evening. what are we working on today?"

        logger.info("Startup greeting: %s", greeting)
        print(f"  Serena: {greeting}")

        # Speak the greeting
        self._muted = True
        self._set_state(State.SPEAKING)
        try:
            await self._tts.speak(greeting)
        finally:
            await asyncio.sleep(0.5)
            self._muted = False

        # Listen for the user's response (no wake word needed)
        self._set_state(State.LISTENING)
        self._vad.start_listening()
        self._utterance_ready.clear()
        print("  Listening...")

        try:
            await asyncio.wait_for(self._utterance_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.info("No response to startup greeting, skipping project selection")
            self._vad.stop_listening()
            return

        if self._utterance_audio is None:
            self._vad.stop_listening()
            return

        audio = self._utterance_audio
        self._utterance_audio = None

        # Transcribe
        self._set_state(State.THINKING)
        audio_float = audio.astype(np.float32) / 32768.0
        text = self._stt.transcribe(audio_float)

        if not text:
            logger.info("Empty transcription from startup greeting response")
            return

        print(f"  You: {text}")
        text_lower = text.lower().strip().rstrip(".")

        # Check if user wants to skip project selection
        if any(phrase in text_lower for phrase in _SKIP_PHRASES):
            reply = "alright, we can just chat."
            print(f"  Serena: {reply}")
            self._muted = True
            self._set_state(State.SPEAKING)
            try:
                await self._tts.speak(reply)
            finally:
                await asyncio.sleep(0.5)
                self._muted = False
            self._wake_event.set()  # enter conversation mode immediately
            return

        # Try to match to a project directory
        matched = self._match_project(text_lower)

        if matched:
            self._active_project = matched
            reply = f"got it, working on {matched}."
            logger.info("Active project set to: %s", matched)
        else:
            reply = "i don't see that project. we can just chat for now."
            logger.info("No project match for: %s", text)

        print(f"  Serena: {reply}")
        self._muted = True
        self._set_state(State.SPEAKING)
        try:
            await self._tts.speak(reply)
        finally:
            await asyncio.sleep(0.5)
            self._muted = False

        # After greeting + project selection, immediately enter conversation mode
        # so the user doesn't have to say the wake word.
        self._wake_event.set()

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Simple character-level similarity ratio (0-1). Cheap Levenshtein alternative."""
        if not a or not b:
            return 0.0
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()

    def _match_project(self, text: str) -> str | None:
        """Fuzzy-match spoken text to a project directory name.

        Handles STT mishearings like 'kompeki' → 'konpeki' using
        similarity matching with a 0.6 threshold.
        """
        if not _PROJECTS_DIR.is_dir():
            logger.warning("Projects directory not found: %s", _PROJECTS_DIR)
            return None

        project_dirs = sorted(
            [d.name for d in _PROJECTS_DIR.iterdir() if d.is_dir()],
            key=lambda n: len(n),
            reverse=True,
        )

        # Strip common filler words
        cleaned = re.sub(
            r"\b(the|a|an|project|repo|repository|working on|work on|let'?s do|let'?s work on|i want to|admin)\b",
            "",
            text,
        ).strip()

        # Extract individual words from cleaned text for matching
        words = cleaned.split()

        # Pass 1: exact substring match
        for name in project_dirs:
            name_lower = name.lower()
            if name_lower in cleaned or name_lower in text:
                logger.info("Project match (exact): '%s' in '%s'", name, text)
                return name

        # Pass 2: reverse substring match
        if len(cleaned) >= 3:
            for name in project_dirs:
                if cleaned in name.lower():
                    logger.info("Project match (reverse): '%s' contains '%s'", name, cleaned)
                    return name

        # Pass 3: fuzzy match each spoken word against project names
        best_match = None
        best_score = 0.0
        for word in words:
            if len(word) < 3:
                continue
            for name in project_dirs:
                score = self._similarity(word.lower(), name.lower())
                if score > best_score:
                    best_score = score
                    best_match = name

        if best_match and best_score >= 0.55:
            logger.info(
                "Project match (fuzzy): '%s' ~ '%s' (score=%.2f)",
                text, best_match, best_score,
            )
            return best_match

        logger.info("No project match for: %s (best: %s @ %.2f)", text, best_match, best_score)
        return None

    # --- Voice commands (Task 6.6) ---

    async def _check_voice_command(self, text: str) -> bool:
        """Check if transcribed text is a voice command.

        Returns True if a command was handled (skip sending to Claude),
        False if it should proceed as normal conversation.
        """
        t = text.lower().strip().rstrip(".")

        # --- Stop / Cancel / Abort ---
        if t in ("stop", "cancel", "abort", "stop it", "cancel that"):
            return await self._cmd_stop()

        # --- Status ---
        if t in ("what are you doing", "what are you doing?", "status", "what's happening"):
            return await self._cmd_status()

        # --- Show code panel ---
        if t.startswith("show") and any(w in t for w in ("me", "code", "output")):
            return await self._cmd_toggle_code_panel(visible=True)

        # --- Hide code panel ---
        if t.startswith("hide") and any(w in t for w in ("code", "output", "panel", "that")):
            return await self._cmd_toggle_code_panel(visible=False)
        if t == "hide":
            return await self._cmd_toggle_code_panel(visible=False)

        # --- Switch project ---
        switch_match = re.match(r"switch\s+to\s+(.+)", t)
        if switch_match:
            return await self._cmd_switch_project(switch_match.group(1).strip())

        return False

    async def _cmd_stop(self) -> bool:
        """Stop a running CodeSession if one exists."""
        if self._code_tool and hasattr(self._code_tool, "session") and self._code_tool.session:
            try:
                await self._code_tool.session.cancel()
            except Exception:
                logger.exception("Failed to cancel code session")
            reply = "stopped."
        else:
            reply = "nothing to stop."
        await self._speak_fallback(reply)
        print(f"  Serena: {reply}")
        return True

    async def _cmd_status(self) -> bool:
        """Report current status."""
        if self._code_tool and hasattr(self._code_tool, "session") and self._code_tool.session:
            status = getattr(self._code_tool.session, "status_summary", None)
            if callable(status):
                reply = status()
            else:
                reply = "i'm working on some code right now."
        else:
            reply = "nothing right now."
        await self._speak_fallback(reply)
        print(f"  Serena: {reply}")
        return True

    async def _cmd_toggle_code_panel(self, *, visible: bool) -> bool:
        """Send IPC message to toggle the code output panel."""
        if self._ipc:
            await self._ipc.broadcast({
                "type": "toggle_code_panel",
                "visible": visible,
            })
        action = "showing" if visible else "hiding"
        reply = f"{action} the code panel."
        await self._speak_fallback(reply)
        print(f"  Serena: {reply}")
        return True

    async def _cmd_switch_project(self, project_text: str) -> bool:
        """Switch the active project."""
        matched = self._match_project(project_text)
        if matched:
            self._active_project = matched
            reply = f"switched to {matched}."
            logger.info("Active project switched to: %s", matched)
        else:
            reply = "i don't see that project."
        await self._speak_fallback(reply)
        print(f"  Serena: {reply}")
        return True

    # --- Lifecycle ---

    async def start(self) -> None:
        """Initialize all components and start the voice loop."""
        logger.info("Initializing voice pipeline...")

        # Capture the running event loop — needed for thread-safe event signaling
        self._asyncio_loop = asyncio.get_running_loop()

        # Load models
        self._stt = SpeechToText(self._config.stt)
        self._tts = TextToSpeech(self._config.tts)
        self._wakeword = WakeWordDetector(self._config.wake_word)
        self._vad = VoiceActivityDetector()

        # Set up audio capture
        self._audio = AudioCapture()
        self._audio.add_callback(self._on_audio_chunk)
        self._audio.start()

        self._running = True
        self._set_state(State.IDLE)

        # Startup greeting — ask what project to work on
        await self._startup_greeting()

        # Start the main processing loop and stdin listener
        asyncio.create_task(self._loop())
        asyncio.create_task(self._stdin_listener())

        logger.info("Voice pipeline ready — say '%s' or press ENTER to start", self._wakeword.model_name)
        print(f"\n  Serena is listening. Say '{self._wakeword.model_name}' or press ENTER to talk.\n")

    async def _stdin_listener(self) -> None:
        """Listen for ENTER key press as an alternative wake trigger."""
        import sys

        # Only works when stdin is a real terminal (not background process)
        if not sys.stdin.isatty():
            logger.info("stdin is not a TTY, skipping stdin listener")
            return

        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break  # EOF
                if self._state == State.IDLE:
                    self._wake_event.set()
            except Exception:
                break

    async def stop(self) -> None:
        """Shut down all components."""
        self._running = False
        if self._audio:
            self._audio.stop()
        logger.info("Voice pipeline stopped")

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        """Audio callback — runs in the sounddevice thread.

        Routes audio to wake word detector or VAD depending on state.
        Skips processing entirely during speaking/thinking to free CPU.
        """
        if not self._running:
            return

        state = self._state

        # Only process audio in states that need it
        if state == State.IDLE:
            if self._wakeword and self._wakeword.process(chunk):
                self._signal_wake()

        elif state == State.LISTENING and not self._muted:
            if self._vad:
                utterance = self._vad.process(chunk)
                if utterance is not None:
                    self._utterance_audio = utterance
                    self._signal_utterance()

        # THINKING and SPEAKING states: do nothing with audio (save CPU)

    def _signal_wake(self) -> None:
        """Thread-safe signal from audio thread that wake word detected."""
        if self._asyncio_loop:
            self._asyncio_loop.call_soon_threadsafe(self._wake_event.set)

    def _signal_utterance(self) -> None:
        """Thread-safe signal from audio thread that utterance is complete."""
        if self._asyncio_loop:
            self._asyncio_loop.call_soon_threadsafe(self._utterance_ready.set)

    async def _speak_fallback(self, message: str) -> None:
        """Speak a message directly via TTS without going through Claude.

        Used for error recovery messages when the API is unavailable.
        """
        if not self._tts:
            logger.warning("TTS not initialized, cannot speak fallback")
            return

        try:
            self._muted = True
            self._set_state(State.SPEAKING)
            await self._tts.speak(message)
        except Exception:
            logger.exception("Fallback TTS also failed")
        finally:
            await asyncio.sleep(0.5)
            self._muted = False

    async def _think_with_retry(self, text: str) -> str:
        """Call brain.think() with retry + exponential backoff.

        The brain itself already returns a friendly error string on API
        failure instead of raising, so retries here cover transient
        network errors and unexpected exceptions.
        """
        last_error: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await self._brain.think(text)
            except anthropic.APIError as exc:
                last_error = exc
                logger.warning(
                    "Claude API error (attempt %d/%d): %s",
                    attempt, _MAX_RETRIES, exc,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Unexpected error calling brain (attempt %d/%d): %s",
                    attempt, _MAX_RETRIES, exc,
                )

            if attempt < _MAX_RETRIES:
                delay = _RETRY_BACKOFF_BASE ** attempt
                logger.info("Retrying in %.1fs...", delay)
                await asyncio.sleep(delay)

        # All retries exhausted
        logger.error("All %d retries failed: %s", _MAX_RETRIES, last_error)
        return "I'm having trouble connecting right now, give me a moment."

    async def _reconnect_mic(self) -> None:
        """Attempt to restart audio capture after a mic disconnection."""
        logger.warning("Mic disconnected — attempting reconnect in %.0fs", _MIC_RECONNECT_DELAY)

        if self._audio:
            try:
                self._audio.stop()
            except Exception:
                pass

        await asyncio.sleep(_MIC_RECONNECT_DELAY)

        try:
            self._audio = AudioCapture()
            self._audio.add_callback(self._on_audio_chunk)
            self._audio.start()
            logger.info("Mic reconnected successfully")
        except (sd.PortAudioError, OSError) as exc:
            logger.error("Mic reconnection failed: %s", exc)

    async def _loop(self) -> None:
        """Main voice loop — runs continuously until stopped.

        After each exchange, enters follow-up mode for _FOLLOW_UP_TIMEOUT
        seconds. If the user speaks again within that window, the conversation
        continues without needing the wake word. If silence, goes back to idle.
        """
        while self._running:
            try:
                # Wait for wake word (or follow-up from previous exchange)
                self._set_state(State.IDLE)
                self._wake_event.clear()
                await self._wake_event.wait()

                if not self._running:
                    break

                # Conversation loop — keeps going until follow-up times out
                while self._running:
                    result = await self._single_exchange()
                    if not result:
                        break  # empty transcription or timeout, exit conversation

                    # Follow-up mode: listen for more speech without wake word
                    print("  (listening for follow-up...)")
                    self._set_state(State.LISTENING)
                    self._vad.start_listening()
                    self._utterance_ready.clear()

                    try:
                        await asyncio.wait_for(
                            self._utterance_ready.wait(),
                            timeout=_FOLLOW_UP_TIMEOUT,
                        )
                        # User spoke again — continue the conversation
                        if self._utterance_audio is not None:
                            continue
                    except asyncio.TimeoutError:
                        # No follow-up — go back to idle
                        self._vad.stop_listening()
                        logger.info("Follow-up timeout, returning to idle")
                        break

            except asyncio.CancelledError:
                break
            except (sd.PortAudioError, OSError) as exc:
                logger.error("Audio device error: %s", exc)
                await self._speak_fallback(
                    "I lost my microphone connection. Trying to reconnect."
                )
                await self._reconnect_mic()
            except Exception:
                logger.exception("Error in voice loop")
                await asyncio.sleep(1.0)

    async def _single_exchange(self) -> bool:
        """Handle one listen → transcribe → think → speak cycle.

        Returns True if the exchange completed (conversation can continue),
        False if it should exit (empty transcription, timeout, etc.).
        """
        # If we already have a captured utterance (from follow-up mode), use it.
        # Otherwise, start a fresh listen.
        if self._utterance_audio is None:
            if not self._vad.is_listening:
                print("  Listening...")
                self._set_state(State.LISTENING)
                self._vad.start_listening()
                self._utterance_ready.clear()

                try:
                    await asyncio.wait_for(self._utterance_ready.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning("No speech detected within 15s")
                    self._vad.stop_listening()
                    return False

        if self._utterance_audio is None:
            return False

        audio = self._utterance_audio
        self._utterance_audio = None

        # Transcribe
        self._set_state(State.THINKING)
        print("  Processing...")

        audio_float = audio.astype(np.float32) / 32768.0
        text = self._stt.transcribe(audio_float)

        if not text:
            logger.info("Empty transcription")
            return False

        print(f"  You: {text}")
        if self._ipc:
            asyncio.create_task(self._ipc.send_transcription(text))

        # Check for voice commands before sending to Claude
        if await self._check_voice_command(text):
            return True

        # Think
        response = await self._think_with_retry(text)
        print(f"  Serena: {response}")
        if self._ipc:
            asyncio.create_task(self._ipc.send_response(response))

        # Speak
        self._set_state(State.SPEAKING)
        self._muted = True
        try:
            await self._tts.speak(response)
        finally:
            await asyncio.sleep(0.5)
            self._muted = False

        return True
