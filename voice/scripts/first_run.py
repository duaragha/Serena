"""First-run setup and diagnostics for Serena.

Usage: python -m serena.scripts.first_run

Walks through system dependencies, environment, microphone, TTS, API
keys, and optional Google Calendar OAuth -- reporting what's ready and
what needs attention.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# -- ANSI helpers -----------------------------------------------------------

_BOLD = "\033[1m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_OK = f"  [{_GREEN}OK{_RESET}]"
_MISSING = f"  [{_RED}MISSING{_RESET}]"
_WARN = f"  [{_YELLOW}WARN{_RESET}]"
_SKIP = f"  [{_DIM}SKIP{_RESET}]"


def _header(title: str) -> None:
    print(f"\n{_BOLD}{_CYAN}--- {title} ---{_RESET}\n")


def _result(label: str, ok: bool, detail: str = "") -> bool:
    tag = _OK if ok else _MISSING
    suffix = f"  {_DIM}{detail}{_RESET}" if detail else ""
    print(f"{tag}  {label}{suffix}")
    return ok


def _warn(label: str, detail: str = "") -> None:
    suffix = f"  {_DIM}{detail}{_RESET}" if detail else ""
    print(f"{_WARN}  {label}{suffix}")


# -- Checks -----------------------------------------------------------------


def check_python_version() -> bool:
    """Require Python 3.11+."""
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    ok = (v.major, v.minor) >= (3, 11)
    return _result(
        "Python version",
        ok,
        f"{version_str}" + ("" if ok else " (need 3.11+)"),
    )


def check_system_dep(name: str, *, package_hint: str = "") -> bool:
    """Check if a system binary is on PATH."""
    found = shutil.which(name) is not None
    hint = f"install: {package_hint}" if package_hint and not found else ""
    return _result(name, found, hint)


def check_system_deps() -> list[bool]:
    results = [
        check_system_dep("ffplay", package_hint="sudo apt install ffmpeg"),
        check_system_dep("piper", package_hint="pip install piper-tts"),
    ]
    # portaudio is a shared lib, not a binary -- check via pkg-config or
    # by trying to import sounddevice.
    try:
        import sounddevice  # noqa: F401
        results.append(_result("portaudio (via sounddevice)", True))
    except (ImportError, OSError) as exc:
        results.append(
            _result(
                "portaudio",
                False,
                f"{exc}  install: sudo apt install portaudio19-dev",
            )
        )
    return results


def check_venv_and_packages() -> list[bool]:
    """Verify we're inside the project venv and key packages are importable."""
    results: list[bool] = []

    # Check venv
    in_venv = sys.prefix != sys.base_prefix
    results.append(_result("Virtual environment active", in_venv, sys.prefix))

    packages = [
        ("anthropic", "anthropic"),
        ("faster_whisper", "faster-whisper"),
        ("openwakeword", "openwakeword"),
        ("piper", "piper-tts"),
        ("sounddevice", "sounddevice"),
        ("apscheduler", "APScheduler"),
        ("websockets", "websockets"),
        ("yaml", "pyyaml"),
        ("numpy", "numpy"),
        ("httpx", "httpx"),
        ("psutil", "psutil"),
    ]

    for module, pip_name in packages:
        try:
            __import__(module)
            results.append(_result(f"  {pip_name}", True))
        except ImportError:
            results.append(_result(f"  {pip_name}", False, f"pip install {pip_name}"))

    return results


def test_microphone() -> bool:
    """Record 3 seconds of audio, then play it back."""
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        return _result("Microphone test", False, "sounddevice not available")

    duration = 3
    sample_rate = 16000

    print(f"\n  Recording {duration}s of audio from default mic...")
    try:
        audio = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
    except Exception as exc:
        return _result("Microphone test", False, str(exc))

    peak = np.max(np.abs(audio))
    if peak < 100:
        _warn("Very low audio level -- check mic volume or device selection")

    # Save to temp WAV and play back
    try:
        import wave

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()

        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())

        print("  Playing back...")
        result = subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
            timeout=10,
        )
        Path(tmp_path).unlink(missing_ok=True)

        ok = result.returncode == 0
        return _result("Microphone test", ok, f"peak level: {peak}")

    except FileNotFoundError:
        return _result("Microphone playback", False, "ffplay not found")
    except subprocess.TimeoutExpired:
        return _result("Microphone playback", False, "playback timed out")
    except Exception as exc:
        return _result("Microphone test", False, str(exc))


def test_tts() -> bool:
    """Synthesize 'Hello, I'm Serena' and play it."""
    try:
        from serena.config import TTSConfig
        from serena.voice.tts import TextToSpeech

        tts = TextToSpeech(TTSConfig())
    except ImportError as exc:
        return _result("TTS test", False, str(exc))

    print("\n  Synthesizing speech...")
    try:
        import asyncio

        async def _synth() -> str:
            return await tts.synthesize("Hello, I'm Serena.")

        wav_path = asyncio.run(_synth())

        print("  Playing TTS output...")
        result = subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
            timeout=15,
        )
        Path(wav_path).unlink(missing_ok=True)

        ok = result.returncode == 0
        return _result("TTS test", ok)

    except Exception as exc:
        return _result("TTS test", False, str(exc))


def check_api_key() -> bool:
    """Check if ANTHROPIC_API_KEY is set."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        masked = key[:8] + "..." + key[-4:]
        return _result("ANTHROPIC_API_KEY", True, masked)
    return _result("ANTHROPIC_API_KEY", False, "export ANTHROPIC_API_KEY=sk-...")


def check_google_calendar() -> bool:
    """Check if Google Calendar credentials exist."""
    creds_path = Path("~/.config/serena/google_credentials.json").expanduser()
    token_path = Path("~/.config/serena/google_token.json").expanduser()

    if token_path.exists():
        return _result("Google Calendar OAuth", True, "token present")
    elif creds_path.exists():
        _warn("Google Calendar", "credentials found but no token -- run OAuth setup")
        return False
    else:
        _result("Google Calendar credentials", False, str(creds_path))
        return False


def offer_google_oauth() -> None:
    """Optionally run Google Calendar OAuth setup."""
    print(f"\n  {_DIM}Would you like to set up Google Calendar now? (y/N){_RESET} ", end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer in ("y", "yes"):
        try:
            subprocess.run(
                [sys.executable, "-m", "serena.scripts.setup_google_oauth"],
                check=False,
            )
        except Exception as exc:
            print(f"  {_RED}OAuth setup failed: {exc}{_RESET}")
    else:
        print(f"{_SKIP}  Google Calendar OAuth setup")


# -- Main -------------------------------------------------------------------


def main() -> None:
    print()
    print(f"  {_BOLD}Serena First-Run Setup{_RESET}")
    print(f"  {_DIM}Checking your system...{_RESET}")

    passed = 0
    failed = 0
    total = 0

    def track(ok: bool) -> None:
        nonlocal passed, failed, total
        total += 1
        if ok:
            passed += 1
        else:
            failed += 1

    def track_all(results: list[bool]) -> None:
        for r in results:
            track(r)

    # 1. Python version
    _header("Python")
    track(check_python_version())

    # 2. System dependencies
    _header("System Dependencies")
    track_all(check_system_deps())

    # 3. Venv and packages
    _header("Python Packages")
    track_all(check_venv_and_packages())

    # 4. API key
    _header("API Keys")
    track(check_api_key())

    # 5. Google Calendar
    _header("Google Calendar")
    cal_ok = check_google_calendar()
    track(cal_ok)
    if not cal_ok:
        offer_google_oauth()

    # 6. Microphone test (interactive)
    _header("Microphone")
    print(f"  {_DIM}Test your microphone? (Y/n){_RESET} ", end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        print()

    if answer not in ("n", "no"):
        track(test_microphone())
    else:
        print(f"{_SKIP}  Microphone test")

    # 7. TTS test (interactive)
    _header("Text-to-Speech")
    print(f"  {_DIM}Test TTS synthesis? (Y/n){_RESET} ", end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        print()

    if answer not in ("n", "no"):
        track(test_tts())
    else:
        print(f"{_SKIP}  TTS test")

    # -- Summary ---
    _header("Summary")
    color = _GREEN if failed == 0 else (_YELLOW if failed <= 2 else _RED)
    print(f"  {color}{passed}/{total} checks passed{_RESET}")
    if failed > 0:
        print(f"  {_RED}{failed} issue(s) need attention{_RESET}")
    else:
        print(f"\n  {_GREEN}{_BOLD}Serena is ready to go.{_RESET}")
        print(f"  {_DIM}Run with: serena  (or: python -m serena.main){_RESET}")
    print()


if __name__ == "__main__":
    main()
