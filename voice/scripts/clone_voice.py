"""Voice cloning setup script for Serena's TTS voice.

Evaluates available TTS options for CPU-only systems and sets up the best
available voice for Serena. Since ChatterBox TTS needs GPU for reasonable
performance, this script:

  1. Records a reference audio sample of the desired voice
  2. Benchmarks available TTS engines on your hardware
  3. Configures the best option (local or cloud fallback)

TTS Options (ranked by quality for CPU-only):

  LOCAL:
  - Piper TTS: 80ms latency, good quality, no cloning, CPU-native (CURRENT)
  - Kokoro-82M: ~300ms, higher quality than Piper, no cloning, CPU-friendly
  - ChatterBox: Best quality + cloning, but ~15-30s per utterance on CPU (unusable)
  - XTTS v2: Voice cloning, but ~10-20s on CPU (unusable)
  - Bark: Expressive, but 30-60s on CPU (unusable)

  CLOUD (needs API key):
  - OpenAI TTS: $15/M chars, ~200ms, great quality, 6 preset voices
  - OpenAI TTS Mini: $0.60/M chars, ~150ms, good quality, cheap
  - ElevenLabs: $60-120/M chars, ~75ms, best quality, voice cloning

  VERDICT: For CPU-only, stick with Piper for speed. OpenAI TTS is the best
  cloud fallback if you want higher quality. Record a reference sample now
  so you're ready for ChatterBox when you get a GPU.

Usage:
    python -m serena.scripts.clone_voice
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

# Project paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MODELS_DIR = _PROJECT_ROOT / "models"
_VOICE_DIR = _MODELS_DIR / "serena_voice"
_REFERENCE_WAV = _VOICE_DIR / "reference.wav"
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"


def _print_header(text: str) -> None:
    width = 60
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
    print()


def _print_step(step: int, total: int, text: str) -> None:
    print(f"  [{step}/{total}] {text}")


def _has_gpu() -> bool:
    """Check if a CUDA GPU is available."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _step_record_reference() -> Path | None:
    """Record a reference audio sample for future voice cloning.

    The reference sample is used by ChatterBox (or similar) to clone a voice.
    Even if cloning isn't possible right now (CPU-only), having the sample
    ready means you can clone later when you get GPU access.
    """
    _print_step(1, 4, "Reference audio sample")
    print()

    _VOICE_DIR.mkdir(parents=True, exist_ok=True)

    if _REFERENCE_WAV.exists():
        # Check duration
        try:
            with wave.open(str(_REFERENCE_WAV), "rb") as wf:
                duration = wf.getnframes() / wf.getframerate()
            print(f"  Existing reference sample found: {_REFERENCE_WAV}")
            print(f"  Duration: {duration:.1f}s")
            print()
            resp = input("  Re-record? [y/N] ").strip().lower()
            if resp != "y":
                return _REFERENCE_WAV
        except Exception:
            pass

    print()
    print("  Recording a reference audio sample for voice cloning.")
    print("  This sample will be used by ChatterBox or similar tools")
    print("  to clone a voice for Serena's TTS output.")
    print()
    print("  Tips for a good reference sample:")
    print("    - Use a quiet room with no background noise")
    print("    - Speak clearly and naturally (don't over-enunciate)")
    print("    - Read 2-3 sentences in the tone you want Serena to have")
    print("    - 10-15 seconds is ideal")
    print()
    print("  Suggested text to read:")
    print('    "Good morning. I checked your calendar and you have three')
    print('     meetings today. The weather looks clear, around fifteen')
    print('     degrees. Want me to go through the agenda?"')
    print()

    # Check for sounddevice
    try:
        import sounddevice as sd
    except ImportError:
        print("  Error: sounddevice not installed.")
        print("  Run: uv pip install sounddevice")
        print()
        print("  Alternatively, record a WAV file manually and save it to:")
        print(f"    {_REFERENCE_WAV}")
        print("  Format: 16kHz, 16-bit, mono, 10-15 seconds")
        return None

    # Select audio device
    print("  Available audio input devices:")
    devices = sd.query_devices()
    input_devices = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            marker = " *" if i == sd.default.device[0] else ""
            print(f"    [{i}] {d['name']} ({d['max_input_channels']} ch){marker}")
            input_devices.append(i)

    print()
    device_str = input(f"  Device number [{sd.default.device[0]}]: ").strip()
    device_id = int(device_str) if device_str else sd.default.device[0]

    sample_rate = 16000
    duration = 15.0

    print()
    input("  Press Enter to start recording (15 seconds)...")
    print("  Recording...", end=" ", flush=True)

    try:
        audio = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            device=device_id,
        )
        sd.wait()
        print("done.")
    except Exception as e:
        print(f"failed: {e}")
        print()
        print("  Record manually and save to:")
        print(f"    {_REFERENCE_WAV}")
        return None

    # Trim silence from end (simple energy-based)
    audio_flat = audio.flatten()
    energy = np.abs(audio_flat).astype(np.float32)
    # Find last sample above noise floor
    threshold = max(energy.mean() * 0.5, 100)
    nonsilent = np.where(energy > threshold)[0]
    if len(nonsilent) > 0:
        end_idx = min(nonsilent[-1] + sample_rate, len(audio_flat))  # 1s padding
        audio_flat = audio_flat[:end_idx]

    # Save
    with wave.open(str(_REFERENCE_WAV), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_flat.tobytes())

    actual_duration = len(audio_flat) / sample_rate
    print(f"\n  Saved reference sample ({actual_duration:.1f}s) to:")
    print(f"    {_REFERENCE_WAV}")

    # Playback check
    print()
    resp = input("  Play back the recording? [Y/n] ").strip().lower()
    if resp != "n":
        try:
            proc = subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(_REFERENCE_WAV)],
                timeout=30,
            )
        except FileNotFoundError:
            print("  ffplay not found. Install ffmpeg to play audio.")
        except subprocess.TimeoutExpired:
            pass

        print()
        resp = input("  Happy with the recording? [Y/n] ").strip().lower()
        if resp == "n":
            print("  Re-run the script to record again.")
            _REFERENCE_WAV.unlink(missing_ok=True)
            return None

    return _REFERENCE_WAV


def _step_benchmark_tts() -> dict[str, dict]:
    """Benchmark available TTS engines on this hardware."""
    _print_step(2, 4, "Benchmarking TTS engines...")
    print()

    test_text = "Good morning. You have two meetings today and the weather looks clear."
    results: dict[str, dict] = {}

    # --- Piper TTS ---
    print("  Testing Piper TTS...")
    try:
        from piper import PiperVoice
        from piper.config import SynthesisConfig
        from piper.download_voices import download_voice

        piper_model_dir = _MODELS_DIR / "piper"
        model_name = "en_US-amy-medium"
        model_path = piper_model_dir / f"{model_name}.onnx"

        if not model_path.exists():
            print("    Downloading piper model...")
            piper_model_dir.mkdir(parents=True, exist_ok=True)
            download_voice(model_name, piper_model_dir)

        if model_path.exists():
            voice = PiperVoice.load(str(model_path))
            import tempfile
            import wave as wave_mod

            # Warm up
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                with wave_mod.open(tmp.name, "wb") as wf:
                    voice.synthesize_wav("Hello.", wf)

            # Benchmark
            times = []
            for _ in range(3):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    start = time.perf_counter()
                    with wave_mod.open(tmp.name, "wb") as wf:
                        voice.synthesize_wav(test_text, wf)
                    elapsed = time.perf_counter() - start
                    times.append(elapsed)

                    # Get audio duration
                    with wave_mod.open(tmp.name, "rb") as wf:
                        audio_duration = wf.getnframes() / wf.getframerate()
                    Path(tmp.name).unlink(missing_ok=True)

            avg_time = sum(times) / len(times)
            rtf = avg_time / audio_duration if audio_duration > 0 else 0
            results["piper"] = {
                "name": "Piper TTS",
                "avg_latency_ms": avg_time * 1000,
                "audio_duration_s": audio_duration,
                "rtf": rtf,
                "voice_cloning": False,
                "quality": "Good",
                "status": "available",
            }
            print(f"    Piper: {avg_time*1000:.0f}ms latency, RTF {rtf:.3f} (audio: {audio_duration:.1f}s)")
        else:
            print("    Piper model not found, skipping.")

    except Exception as e:
        print(f"    Piper error: {e}")

    # --- ChatterBox TTS ---
    print("  Testing ChatterBox TTS...")
    try:
        from chatterbox.tts import ChatterBoxTTS

        has_gpu = _has_gpu()
        device = "cuda" if has_gpu else "cpu"

        print(f"    Loading ChatterBox on {device}...")
        model = ChatterBoxTTS.from_pretrained(device=device)

        # Warm up
        _ = model.generate(text="Hello.", audio_prompt=None)

        # Benchmark
        ref_audio = str(_REFERENCE_WAV) if _REFERENCE_WAV.exists() else None
        times = []
        for _ in range(2):  # Fewer runs since CPU is slow
            start = time.perf_counter()
            wav = model.generate(text=test_text, audio_prompt=ref_audio)
            elapsed = time.perf_counter() - start
            times.append(elapsed)

        avg_time = sum(times) / len(times)
        # Estimate audio duration (ChatterBox returns tensor)
        if hasattr(wav, 'shape'):
            audio_duration = wav.shape[-1] / 24000  # 24kHz default
        else:
            audio_duration = 3.0  # estimate
        rtf = avg_time / audio_duration if audio_duration > 0 else 0

        results["chatterbox"] = {
            "name": "ChatterBox TTS",
            "avg_latency_ms": avg_time * 1000,
            "audio_duration_s": audio_duration,
            "rtf": rtf,
            "voice_cloning": True,
            "quality": "Excellent",
            "device": device,
            "status": "available",
        }
        speed_verdict = "fast" if avg_time < 2 else "slow" if avg_time < 10 else "unusable for real-time"
        print(f"    ChatterBox ({device}): {avg_time*1000:.0f}ms latency, RTF {rtf:.3f} ({speed_verdict})")

    except ImportError:
        results["chatterbox"] = {
            "name": "ChatterBox TTS",
            "status": "not_installed",
            "note": "pip install chatterbox-tts (needs PyTorch + GPU for real-time)",
        }
        print("    ChatterBox not installed. Skipping.")
    except Exception as e:
        results["chatterbox"] = {
            "name": "ChatterBox TTS",
            "status": "error",
            "error": str(e),
        }
        print(f"    ChatterBox error: {e}")

    # --- OpenAI TTS ---
    print("  Checking OpenAI TTS...")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            import httpx

            start = time.perf_counter()
            response = httpx.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "tts-1",
                    "input": test_text,
                    "voice": "nova",  # Closest to a natural female voice
                    "response_format": "wav",
                    "speed": 1.1,
                },
                timeout=30.0,
            )
            elapsed = time.perf_counter() - start

            if response.status_code == 200:
                # Save to check duration
                import tempfile
                import wave as wave_mod
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(response.content)
                    tmp_path = tmp.name
                try:
                    with wave_mod.open(tmp_path, "rb") as wf:
                        audio_duration = wf.getnframes() / wf.getframerate()
                except Exception:
                    audio_duration = 3.0  # estimate
                finally:
                    Path(tmp_path).unlink(missing_ok=True)

                results["openai"] = {
                    "name": "OpenAI TTS",
                    "avg_latency_ms": elapsed * 1000,
                    "audio_duration_s": audio_duration,
                    "voice_cloning": False,
                    "quality": "Great",
                    "voices": ["alloy", "ash", "ballad", "coral", "echo", "fable",
                               "nova", "onyx", "sage", "shimmer"],
                    "recommended_voice": "nova",
                    "pricing": "$15/M chars (tts-1), $30/M chars (tts-1-hd)",
                    "status": "available",
                }
                print(f"    OpenAI TTS: {elapsed*1000:.0f}ms latency ({audio_duration:.1f}s audio)")
            else:
                results["openai"] = {
                    "name": "OpenAI TTS",
                    "status": "error",
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }
                print(f"    OpenAI TTS error: HTTP {response.status_code}")

        except Exception as e:
            results["openai"] = {
                "name": "OpenAI TTS",
                "status": "error",
                "error": str(e),
            }
            print(f"    OpenAI TTS error: {e}")
    else:
        results["openai"] = {
            "name": "OpenAI TTS",
            "status": "no_api_key",
            "note": "Set OPENAI_API_KEY env var to benchmark. $15/M chars.",
        }
        print("    OPENAI_API_KEY not set. Skipping benchmark (would need API key).")

    # --- ElevenLabs ---
    print("  Checking ElevenLabs...")
    xi_key = os.environ.get("ELEVEN_API_KEY", "") or os.environ.get("ELEVENLABS_API_KEY", "")
    if xi_key:
        results["elevenlabs"] = {
            "name": "ElevenLabs",
            "status": "api_key_found",
            "quality": "Best",
            "voice_cloning": True,
            "pricing": "$60-120/M chars",
            "note": "Voice cloning available via ElevenLabs dashboard",
        }
        print("    ElevenLabs API key found. Voice cloning available via their dashboard.")
    else:
        results["elevenlabs"] = {
            "name": "ElevenLabs",
            "status": "no_api_key",
            "note": "Set ELEVEN_API_KEY for cloud TTS with voice cloning. $5/mo starter.",
        }
        print("    No ElevenLabs API key. Skipping.")

    print()
    return results


def _step_recommend(results: dict[str, dict]) -> str:
    """Analyze benchmark results and recommend the best TTS setup."""
    _print_step(3, 4, "Analysis and recommendation")
    print()

    has_gpu = _has_gpu()
    chatterbox_ok = (
        results.get("chatterbox", {}).get("status") == "available"
        and results["chatterbox"].get("avg_latency_ms", 99999) < 3000
    )
    openai_ok = results.get("openai", {}).get("status") == "available"
    piper_ok = results.get("piper", {}).get("status") == "available"

    print("  " + "-" * 50)
    print("  Benchmark Results")
    print("  " + "-" * 50)
    print()

    for key, info in results.items():
        status = info.get("status", "unknown")
        name = info.get("name", key)

        if status == "available":
            latency = info.get("avg_latency_ms", 0)
            cloning = "yes" if info.get("voice_cloning") else "no"
            quality = info.get("quality", "?")
            print(f"  {name}:")
            print(f"    Latency: {latency:.0f}ms | Quality: {quality} | Voice cloning: {cloning}")
            if "device" in info:
                print(f"    Device: {info['device']}")
            if "rtf" in info:
                print(f"    Real-time factor: {info['rtf']:.3f} (< 1.0 = real-time capable)")
        elif status == "not_installed":
            print(f"  {name}: not installed")
        elif status == "no_api_key":
            print(f"  {name}: no API key configured")
        elif status == "error":
            print(f"  {name}: error — {info.get('error', 'unknown')}")
        elif status == "api_key_found":
            print(f"  {name}: API key found (not benchmarked)")
        print()

    # Decide recommendation
    print("  " + "-" * 50)
    print("  Recommendation")
    print("  " + "-" * 50)
    print()

    recommendation = "piper"  # default

    if chatterbox_ok:
        recommendation = "chatterbox"
        print("  ChatterBox is fast enough on your hardware.")
        print("  Using it as primary TTS with voice cloning.")
        if _REFERENCE_WAV.exists():
            print(f"  Reference audio: {_REFERENCE_WAV}")
    elif openai_ok:
        recommendation = "openai"
        print("  ChatterBox is too slow on CPU for real-time use.")
        print("  OpenAI TTS is available as a higher-quality cloud option.")
        print("  Using OpenAI TTS as primary, Piper as offline fallback.")
        print()
        print("  Estimated cost for always-on assistant: ~$2-5/month")
        print("  (average voice response is ~200 chars, ~50 responses/day)")
    elif piper_ok:
        recommendation = "piper"
        print("  No GPU detected. ChatterBox would be too slow on CPU.")
        print("  No cloud API keys configured.")
        print()
        print("  Sticking with Piper TTS (current setup).")
        print("  Piper is fast and works well — just no voice cloning.")
        print()
        print("  To upgrade later:")
        print("    - Set OPENAI_API_KEY for cloud TTS ($15/M chars)")
        print("    - Or get a GPU and use ChatterBox for local voice cloning")
    else:
        print("  No TTS engines available. Install piper-tts at minimum.")
        return "piper"

    if _REFERENCE_WAV.exists() and recommendation != "chatterbox":
        print()
        print(f"  Reference audio sample saved for future cloning: {_REFERENCE_WAV}")
        print("  When you get GPU access, you can clone this voice with ChatterBox.")

    return recommendation


def _step_configure(recommendation: str, results: dict[str, dict]) -> None:
    """Update config.yaml with the recommended TTS configuration."""
    _print_step(4, 4, "Updating configuration...")
    print()

    import yaml

    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    tts_config = config.get("tts", {})
    old_engine = tts_config.get("engine", "piper")

    if recommendation == "chatterbox":
        tts_config["engine"] = "chatterbox"
        tts_config["voice_ref"] = str(_REFERENCE_WAV) if _REFERENCE_WAV.exists() else ""
        tts_config["fallback_engine"] = "piper"
        tts_config["piper_model"] = tts_config.get("piper_model", "en_US-amy-medium")
        tts_config["speed"] = tts_config.get("speed", 1.1)

    elif recommendation == "openai":
        tts_config["engine"] = "openai"
        tts_config["openai_model"] = "tts-1"
        tts_config["openai_voice"] = "nova"
        tts_config["fallback_engine"] = "piper"
        tts_config["piper_model"] = tts_config.get("piper_model", "en_US-amy-medium")
        tts_config["speed"] = tts_config.get("speed", 1.1)

    elif recommendation == "piper":
        tts_config["engine"] = "piper"
        tts_config["piper_model"] = tts_config.get("piper_model", "en_US-amy-medium")
        tts_config["speed"] = tts_config.get("speed", 1.1)
        # Keep voice_ref if it exists for future use
        if _REFERENCE_WAV.exists():
            tts_config["voice_ref"] = str(_REFERENCE_WAV)

    config["tts"] = tts_config

    if old_engine != tts_config["engine"]:
        print(f"  TTS engine: {old_engine} -> {tts_config['engine']}")
    else:
        print(f"  TTS engine: {tts_config['engine']} (unchanged)")

    # Write config
    with open(_CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"  Config saved to: {_CONFIG_PATH}")
    print()

    # Save benchmark results for reference
    results_path = _VOICE_DIR / "benchmark_results.json"
    _VOICE_DIR.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Benchmark results saved to: {results_path}")


def _print_openai_tts_integration() -> None:
    """Show how the OpenAI TTS engine works in the TTS module."""
    print()
    print("  " + "-" * 50)
    print("  OpenAI TTS Integration Notes")
    print("  " + "-" * 50)
    print()
    print("  The TTS module (serena/voice/tts.py) will need an OpenAI")
    print("  TTS backend. Here's the API pattern:")
    print()
    print("    POST https://api.openai.com/v1/audio/speech")
    print("    {")
    print('      "model": "tts-1",        // or "tts-1-hd" for higher quality')
    print('      "input": "text here",')
    print('      "voice": "nova",          // natural female voice')
    print('      "response_format": "wav", // or mp3, opus, aac, flac, pcm')
    print('      "speed": 1.1              // 0.25 to 4.0')
    print("    }")
    print()
    print("  Available voices: alloy, ash, ballad, coral, echo, fable,")
    print("                    nova, onyx, sage, shimmer")
    print()
    print("  'nova' is recommended — most natural female voice.")
    print("  'shimmer' is also good — warm and expressive.")
    print()
    print("  Cost estimate for Serena:")
    print("    ~200 chars per response x 50 responses/day = 10K chars/day")
    print("    = 300K chars/month = ~$4.50/month (tts-1)")
    print()


def _print_summary(recommendation: str) -> None:
    """Print final summary."""
    print()
    print("  " + "-" * 50)
    print("  Summary")
    print("  " + "-" * 50)
    print()

    has_ref = _REFERENCE_WAV.exists()
    print(f"  TTS engine:      {recommendation}")
    print(f"  Reference audio: {'saved' if has_ref else 'not recorded'}")
    if has_ref:
        print(f"    Path: {_REFERENCE_WAV}")
    print(f"  GPU available:   {'yes' if _has_gpu() else 'no'}")
    print()

    if recommendation == "piper":
        print("  Current setup: Piper TTS (fast, local, no cloning)")
        print("  Serena sounds good but uses a preset voice.")
        print()
        print("  Upgrade paths:")
        print("    1. Set OPENAI_API_KEY -> re-run this script -> cloud TTS")
        print("    2. Get a GPU -> install chatterbox-tts -> voice cloning")
        print("    3. Wait for Kokoro or Orpheus CPU builds (in development)")
    elif recommendation == "openai":
        print("  Current setup: OpenAI TTS (cloud, high quality)")
        print("  Piper TTS as offline/fallback.")
        print()
        print("  Serena will sound great. No voice cloning (fixed preset voices).")
        print("  If you want a unique Serena voice, get a GPU for ChatterBox.")
    elif recommendation == "chatterbox":
        print("  Current setup: ChatterBox TTS (local, voice cloning)")
        if has_ref:
            print("  Serena will use your cloned voice.")
        else:
            print("  Record a reference sample to enable voice cloning.")

    print()


def main() -> None:
    _print_header("Serena -- Voice Cloning Setup")

    print("  This script evaluates TTS options for Serena's voice output")
    print("  and configures the best available option for your hardware.")
    print()
    print("  Steps:")
    print("    1. Record a reference audio sample (for future cloning)")
    print("    2. Benchmark available TTS engines")
    print("    3. Analyze results and recommend best option")
    print("    4. Update config.yaml")
    print()

    resp = input("  Ready to start? [Y/n] ").strip().lower()
    if resp == "n":
        print("  Aborted.")
        return

    print()

    # Step 1: Record reference audio
    ref_path = _step_record_reference()
    print()

    # Step 2: Benchmark
    results = _step_benchmark_tts()

    # Step 3: Recommend
    recommendation = _step_recommend(results)
    print()

    # Step 4: Configure
    resp = input("  Apply this configuration? [Y/n] ").strip().lower()
    if resp == "n":
        print("  Configuration not changed.")
    else:
        _step_configure(recommendation, results)

    # Print integration notes if OpenAI was selected
    if recommendation == "openai":
        _print_openai_tts_integration()

    _print_summary(recommendation)


if __name__ == "__main__":
    main()
