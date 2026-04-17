"""Wake word training script for "Hey Serena" openwakeword model.

Generates synthetic audio samples using Piper TTS, then either:
  (a) Trains a full openwakeword model and exports to ONNX, or
  (b) Trains a custom verifier model on top of an existing base model

The full training path (a) requires PyTorch + heavy ML deps. The verifier
path (b) works with what's already installed and is good enough for
personal use with a single speaker.

Usage:
    python -m serena.scripts.train_wakeword

The script is interactive and guides you through each step.
"""

from __future__ import annotations

import random
import sys
import time
import wave
from pathlib import Path

import numpy as np

# Project paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MODELS_DIR = _PROJECT_ROOT / "models"
_TRAINING_DIR = _MODELS_DIR / "training_data"
_POSITIVE_DIR = _TRAINING_DIR / "positive"
_NEGATIVE_DIR = _TRAINING_DIR / "negative"
_PIPER_MODELS_DIR = _MODELS_DIR / "piper"

# Wake word phrase and variations
_WAKE_PHRASES = [
    "Hey Serena",
    "hey serena",
    "Hey, Serena",
    "hey, serena",
    "Hey Serena!",
    "hey Serena",
]

# Negative phrases — common speech that should NOT trigger wake word
_NEGATIVE_PHRASES = [
    "Hey Siri",
    "Hey there",
    "Hey everyone",
    "Serena Williams",
    "the serene lake",
    "Hey Sarah",
    "Hey Sabrina",
    "Hey Sierra",
    "Hey Selena",
    "arena seating",
    "Hey Christina",
    "How are you",
    "Good morning",
    "What time is it",
    "Turn off the lights",
    "Play some music",
    "Set a timer",
    "Remind me to",
    "What's the weather",
    "Open the door",
    "Hey Google",
    "Alexa",
    "Hey Jarvis",
    "Computer",
    "The arena",
    "Subpoena",
    "hyena",
    "serene afternoon",
    "I saw the arena",
    "ballerina dancing",
    "The antenna signal",
    "Hey can you help me",
    "I need to see a doctor",
    "Let me check the calendar",
    "Have you eaten yet",
    "Where are my keys",
    "I'll be there in a minute",
    "Can you call me back",
    "What's on the agenda",
    "The conference room",
    "Send that email",
]

# Piper voices to use for variety (single-speaker models)
_PIPER_VOICES = [
    "en_US-amy-medium",
    "en_US-lessac-medium",
    "en_US-ryan-medium",
    "en_US-arctic-medium",
    "en_US-libritts_r-medium",
    "en_US-hfc_female-medium",
    "en_US-hfc_male-medium",
    "en_GB-alan-medium",
    "en_GB-alba-medium",
    "en_GB-aru-medium",
    "en_GB-cori-medium",
    "en_GB-jenny_dioco-medium",
    "en_GB-northern_english_male-medium",
    "en_GB-semaine-medium",
    "en_GB-southern_english_female-medium",
    "en_GB-vctk-medium",
]

# Speed variations for naturalness (piper length_scale: <1 = faster, >1 = slower)
_SPEED_VARIATIONS = [0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2]

# Noise scale variations for tonal variety
_NOISE_VARIATIONS = [0.3, 0.5, 0.667, 0.8, 1.0]


def _print_header(text: str) -> None:
    width = 60
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
    print()


def _print_step(step: int, total: int, text: str) -> None:
    print(f"  [{step}/{total}] {text}")


def _ensure_piper() -> bool:
    """Check that Piper TTS is available."""
    try:
        from piper import PiperVoice  # noqa: F401
        return True
    except ImportError:
        print("Error: piper-tts not installed.")
        print("Run: uv pip install piper-tts")
        return False


def _download_voice(voice_name: str) -> Path | None:
    """Download a piper voice model if not already present. Returns model path."""
    model_path = _PIPER_MODELS_DIR / f"{voice_name}.onnx"
    config_path = _PIPER_MODELS_DIR / f"{voice_name}.onnx.json"

    if model_path.exists() and config_path.exists():
        return model_path

    try:
        from piper.download_voices import download_voice
        _PIPER_MODELS_DIR.mkdir(parents=True, exist_ok=True)
        download_voice(voice_name, _PIPER_MODELS_DIR)
        if model_path.exists():
            return model_path
    except Exception as e:
        print(f"    Warning: Could not download {voice_name}: {e}")

    return None


def _synthesize_wav(voice_path: Path, text: str, output_path: Path,
                    length_scale: float = 1.0, noise_scale: float = 0.667,
                    speaker_id: int | None = None) -> bool:
    """Synthesize text to a 16kHz 16-bit mono WAV file using Piper."""
    from piper import PiperVoice
    from piper.config import SynthesisConfig

    try:
        voice = PiperVoice.load(str(voice_path))
        syn_config = SynthesisConfig(
            length_scale=length_scale,
            noise_scale=noise_scale,
            speaker_id=speaker_id,
        )

        with wave.open(str(output_path), "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=syn_config)

        # Verify it was created and has content
        if output_path.exists() and output_path.stat().st_size > 100:
            # Resample to 16kHz if needed (openwakeword expects 16kHz)
            _ensure_16khz(output_path)
            return True

    except Exception as e:
        print(f"    Synthesis error for '{text}' with {voice_path.stem}: {e}")

    return False


def _ensure_16khz(wav_path: Path) -> None:
    """Resample a WAV file to 16kHz 16-bit mono if it isn't already."""
    with wave.open(str(wav_path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw_data = wf.readframes(n_frames)

    if sample_rate == 16000 and n_channels == 1 and sampwidth == 2:
        return  # Already correct format

    # Convert to numpy for resampling
    if sampwidth == 2:
        dtype = np.int16
    elif sampwidth == 4:
        dtype = np.int32
    else:
        return  # Unsupported, leave as-is

    audio = np.frombuffer(raw_data, dtype=dtype).astype(np.float32)

    # Mix to mono if stereo
    if n_channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    elif n_channels > 2:
        audio = audio.reshape(-1, n_channels).mean(axis=1)

    # Resample to 16kHz
    if sample_rate != 16000:
        from scipy.signal import resample
        n_target = int(len(audio) * 16000 / sample_rate)
        audio = resample(audio, n_target)

    # Normalize to int16 range
    if audio.max() > 0:
        audio = audio / max(abs(audio.max()), abs(audio.min())) * 32767
    audio = audio.astype(np.int16)

    # Write back
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.tobytes())


def _generate_samples(
    phrases: list[str],
    output_dir: Path,
    voices: list[Path],
    target_count: int,
    label: str,
) -> int:
    """Generate synthetic audio samples using multiple Piper voices.

    Varies speed, noise, and speaker to create diverse training data.
    Returns the number of samples successfully generated.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(output_dir.glob("*.wav")))
    if existing >= target_count:
        print(f"  {label}: {existing} samples already exist (target: {target_count}), skipping.")
        return existing

    count = existing
    attempts = 0
    max_attempts = target_count * 3  # Don't loop forever

    print(f"  Generating {label} samples ({existing} existing, need {target_count - existing} more)...")
    start = time.perf_counter()

    while count < target_count and attempts < max_attempts:
        phrase = random.choice(phrases)
        voice_path = random.choice(voices)
        speed = random.choice(_SPEED_VARIATIONS)
        noise = random.choice(_NOISE_VARIATIONS)

        filename = f"{label}_{count:04d}.wav"
        out_path = output_dir / filename

        ok = _synthesize_wav(voice_path, phrase, out_path,
                             length_scale=speed, noise_scale=noise)
        if ok:
            count += 1
            if count % 50 == 0:
                elapsed = time.perf_counter() - start
                rate = count / elapsed if elapsed > 0 else 0
                print(f"    ... {count}/{target_count} ({rate:.1f} samples/sec)")
        else:
            attempts += 1

    elapsed = time.perf_counter() - start
    print(f"  Done: {count} {label} samples in {elapsed:.1f}s")
    return count


def _step_download_voices() -> list[Path]:
    """Download Piper voice models for diverse audio generation."""
    _print_step(1, 4, "Downloading Piper voice models for diverse speaker coverage...")

    available: list[Path] = []
    for voice_name in _PIPER_VOICES:
        sys.stdout.write(f"    {voice_name}... ")
        sys.stdout.flush()
        path = _download_voice(voice_name)
        if path:
            print("ok")
            available.append(path)
        else:
            print("skipped")

    print(f"\n  {len(available)}/{len(_PIPER_VOICES)} voices available.")

    if len(available) < 2:
        print("\n  Error: Need at least 2 voices for meaningful diversity.")
        print("  Check your internet connection and try again.")
        sys.exit(1)

    return available


def _step_generate_positive(voices: list[Path], count: int) -> int:
    """Generate positive wake word samples."""
    _print_step(2, 4, f"Generating {count} positive samples ('Hey Serena')...")
    return _generate_samples(_WAKE_PHRASES, _POSITIVE_DIR, voices, count, "positive")


def _step_generate_negative(voices: list[Path], count: int) -> int:
    """Generate negative samples (non-wake-word speech)."""
    _print_step(3, 4, f"Generating {count} negative samples (other speech)...")
    return _generate_samples(_NEGATIVE_PHRASES, _NEGATIVE_DIR, voices, count, "negative")


def _step_train_verifier() -> None:
    """Train a custom verifier model using openwakeword's built-in utility.

    This trains a lightweight logistic regression model on top of the
    hey_jarvis base model. It's the practical path for CPU-only setups
    since it doesn't need the heavy PyTorch training pipeline.
    """
    _print_step(4, 4, "Training custom verifier model...")

    pos_clips = sorted(_POSITIVE_DIR.glob("*.wav"))
    neg_clips = sorted(_NEGATIVE_DIR.glob("*.wav"))

    if len(pos_clips) < 10:
        print(f"  Error: Only {len(pos_clips)} positive clips found. Need at least 10.")
        return
    if len(neg_clips) < 10:
        print(f"  Error: Only {len(neg_clips)} negative clips found. Need at least 10.")
        return

    print(f"  Positive clips: {len(pos_clips)}")
    print(f"  Negative clips: {len(neg_clips)}")

    verifier_path = _MODELS_DIR / "hey_serena_verifier.pkl"

    try:
        from openwakeword import train_custom_verifier

        print("  Training verifier (this may take a few minutes)...")
        train_custom_verifier(
            positive_reference_clips=[str(p) for p in pos_clips],
            negative_reference_clips=[str(p) for p in neg_clips],
            output_path=str(verifier_path),
            model_name="hey_jarvis",
        )
        print(f"\n  Verifier model saved to: {verifier_path}")

    except Exception as e:
        print(f"\n  Verifier training failed: {e}")
        print("  The audio samples are still saved and can be used for manual training.")
        return


def _try_full_training() -> bool:
    """Attempt full openwakeword model training if PyTorch deps are available.

    Returns True if training was attempted (regardless of success).
    Returns False if dependencies are missing.
    """
    try:
        import torch  # noqa: F401
        import torchinfo  # noqa: F401
        import torchmetrics  # noqa: F401
    except ImportError:
        return False

    print("\n  PyTorch training dependencies detected. Attempting full model training...")
    print("  This trains a dedicated ONNX wake word model (not just a verifier).")
    print()

    try:
        from openwakeword.train import Model as TrainModel
        from openwakeword.utils import AudioFeatures

        # Load audio features
        print("  Computing audio features from training samples...")
        feature_extractor = AudioFeatures()

        # Process positive samples
        pos_clips = sorted(_POSITIVE_DIR.glob("*.wav"))
        neg_clips = sorted(_NEGATIVE_DIR.glob("*.wav"))

        print(f"  Processing {len(pos_clips)} positive clips...")
        pos_features = []
        for clip in pos_clips:
            import scipy.io.wavfile
            sr, audio = scipy.io.wavfile.read(str(clip))
            features = feature_extractor.compute_features(audio)
            if features is not None:
                pos_features.append(features)

        print(f"  Processing {len(neg_clips)} negative clips...")
        neg_features = []
        for clip in neg_clips:
            import scipy.io.wavfile
            sr, audio = scipy.io.wavfile.read(str(clip))
            features = feature_extractor.compute_features(audio)
            if features is not None:
                neg_features.append(features)

        if not pos_features or not neg_features:
            print("  Error: Could not extract features from audio samples.")
            return True

        # Combine features
        X_pos = np.vstack(pos_features)
        X_neg = np.vstack(neg_features)
        y_pos = np.ones(len(X_pos))
        y_neg = np.zeros(len(X_neg))

        X = np.vstack([X_pos, X_neg])
        y = np.concatenate([y_pos, y_neg])

        print(f"  Training data: {len(X_pos)} positive, {len(X_neg)} negative features")

        # Train model
        input_shape = X_pos[0].shape
        model = TrainModel(n_classes=1, input_shape=input_shape)
        print(f"  Model input shape: {input_shape}")
        print("  Training (this may take a while)...")

        # Simple training loop
        import torch
        from torch.utils.data import DataLoader, TensorDataset

        dataset = TensorDataset(
            torch.FloatTensor(X),
            torch.FloatTensor(y),
        )
        loader = DataLoader(dataset, batch_size=128, shuffle=True)

        model.to(model.device)
        for epoch in range(50):
            total_loss = 0
            for batch_x, batch_y in loader:
                batch_x = batch_x.to(model.device)
                batch_y = batch_y.to(model.device)

                model.optimizer.zero_grad()
                pred = model(batch_x)
                loss = model.loss(pred.squeeze(), batch_y)
                loss.backward()
                model.optimizer.step()
                total_loss += loss.item()

            if (epoch + 1) % 10 == 0:
                avg_loss = total_loss / len(loader)
                print(f"    Epoch {epoch + 1}/50 — loss: {avg_loss:.4f}")

        # Export to ONNX
        onnx_path = _MODELS_DIR / "hey_serena.onnx"
        model.export_to_onnx(str(onnx_path), class_mapping="hey_serena")
        print(f"\n  Model exported to: {onnx_path}")
        return True

    except Exception as e:
        print(f"\n  Full training failed: {e}")
        print("  Falling back to verifier-based approach.")
        return True


def _record_personal_samples() -> None:
    """Record real audio samples from the user's voice for verifier training."""
    try:
        import sounddevice as sd
    except ImportError:
        print("  sounddevice not available, skipping personal recording.")
        return

    print()
    print("  " + "-" * 50)
    print("  Optional: Record your own voice samples")
    print("  " + "-" * 50)
    print()
    print("  The verifier works best when it also has samples of YOUR voice")
    print("  saying 'Hey Serena'. This makes it more accurate for you")
    print("  specifically and reduces false triggers from other people.")
    print()

    resp = input("  Record personal voice samples? [y/N] ").strip().lower()
    if resp != "y":
        print("  Skipping personal recording.")
        return

    personal_dir = _TRAINING_DIR / "personal_positive"
    personal_dir.mkdir(parents=True, exist_ok=True)

    sample_rate = 16000
    duration = 2.5  # seconds per clip
    n_samples = 10

    print()
    print(f"  Recording {n_samples} clips of you saying 'Hey Serena'.")
    print(f"  Each clip is {duration:.1f} seconds. Speak naturally.")
    print("  Vary your tone, speed, and volume slightly between clips.")
    print()

    for i in range(n_samples):
        input(f"  Press Enter to record clip {i + 1}/{n_samples}...")
        print("    Recording...", end=" ", flush=True)

        audio = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
        print("done.")

        out_path = personal_dir / f"personal_{i:03d}.wav"
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())

    print(f"\n  Saved {n_samples} personal clips to {personal_dir}")

    # Add personal clips to positive training set
    personal_clips = list(personal_dir.glob("*.wav"))
    for clip in personal_clips:
        dest = _POSITIVE_DIR / f"personal_{clip.name}"
        if not dest.exists():
            import shutil
            shutil.copy2(clip, dest)

    print(f"  Copied personal clips to positive training set.")


def _print_training_guide() -> None:
    """Print the manual training guide for full ONNX model training."""
    print()
    print("  " + "-" * 50)
    print("  Full ONNX Model Training Guide")
    print("  " + "-" * 50)
    print()
    print("  The verifier approach (what this script does) layers a small")
    print("  classifier on top of an existing wake word model. For a fully")
    print("  custom 'Hey Serena' ONNX model, you need the full training pipeline.")
    print()
    print("  Requirements:")
    print("    pip install torch torchinfo torchmetrics speechbrain")
    print("    pip install audiomentations torch_audiomentations mutagen acoustics")
    print("    pip install pronouncing torchaudio")
    print()
    print("  Option 1 — openwakeword training notebook:")
    print("    https://github.com/dscripka/openWakeWord/tree/main/notebooks")
    print("    - Use Google Colab for free GPU access")
    print("    - Upload the samples from models/training_data/positive/")
    print("    - Follow the notebook to train and export ONNX")
    print()
    print("  Option 2 — Local training with PyTorch:")
    print("    Install the deps above, then re-run this script.")
    print("    If PyTorch is detected, full training will be attempted")
    print("    automatically before falling back to the verifier.")
    print()
    print("  Option 3 — Use hey_jarvis as base + verifier (current approach):")
    print("    This is what we just trained. It uses the built-in hey_jarvis")
    print("    model as the base detector and adds a speaker-specific verifier")
    print("    that rejects false positives. Works well for single-user setups.")
    print()
    print("  The trained model should be saved to:")
    print(f"    {_MODELS_DIR / 'hey_serena.onnx'}")
    print()


def _print_summary() -> None:
    """Print a summary of what was generated/trained."""
    print()
    print("  " + "-" * 50)
    print("  Summary")
    print("  " + "-" * 50)
    print()

    pos_count = len(list(_POSITIVE_DIR.glob("*.wav"))) if _POSITIVE_DIR.exists() else 0
    neg_count = len(list(_NEGATIVE_DIR.glob("*.wav"))) if _NEGATIVE_DIR.exists() else 0
    onnx_exists = (_MODELS_DIR / "hey_serena.onnx").exists()
    verifier_exists = (_MODELS_DIR / "hey_serena_verifier.pkl").exists()

    print(f"  Training data:")
    print(f"    Positive samples: {pos_count}")
    print(f"    Negative samples: {neg_count}")
    print(f"    Location: {_TRAINING_DIR}")
    print()
    print(f"  Models:")
    print(f"    ONNX model:    {'FOUND' if onnx_exists else 'not trained (see guide above)'}")
    print(f"    Verifier:      {'FOUND' if verifier_exists else 'not trained'}")
    print()

    if verifier_exists and not onnx_exists:
        print("  Next steps:")
        print("    The verifier model works with the hey_jarvis base model.")
        print("    For a dedicated 'Hey Serena' model, follow the training guide above.")
        print()
    elif onnx_exists:
        print("  Your custom wake word model is ready.")
        print("  It will be loaded automatically when you start Serena.")
        print()


def main() -> None:
    _print_header("Serena -- Wake Word Training")

    print("  This script generates synthetic audio samples of 'Hey Serena'")
    print("  using multiple Piper TTS voices, then trains a wake word model")
    print("  for openwakeword.")
    print()
    print("  Steps:")
    print("    1. Download diverse Piper voice models")
    print("    2. Generate ~500 positive samples ('Hey Serena')")
    print("    3. Generate ~500 negative samples (other speech)")
    print("    4. Train wake word model")
    print()

    if not _ensure_piper():
        sys.exit(1)

    # Check for scipy (needed for resampling)
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("Error: scipy not installed (needed for audio resampling).")
        print("Run: uv pip install scipy")
        sys.exit(1)

    # Check for sklearn (needed for verifier training)
    try:
        import sklearn  # noqa: F401
    except ImportError:
        print("Warning: scikit-learn not installed. Verifier training will be skipped.")
        print("Run: uv pip install scikit-learn")
        print()

    resp = input("  Ready to start? [Y/n] ").strip().lower()
    if resp == "n":
        print("  Aborted.")
        return

    print()

    # Step 1: Download voices
    voices = _step_download_voices()
    print()

    # Step 2: Generate positive samples
    n_positive = 500
    n_pos = _step_generate_positive(voices, n_positive)
    print()

    # Step 3: Generate negative samples
    n_negative = 500
    n_neg = _step_generate_negative(voices, n_negative)
    print()

    # Optional: Record personal voice samples
    _record_personal_samples()

    # Step 4: Train
    # Try full training first if deps are available
    did_full = _try_full_training()

    if not did_full:
        # Fall back to verifier training
        _step_train_verifier()

    # Print training guide for full ONNX model
    if not (_MODELS_DIR / "hey_serena.onnx").exists():
        _print_training_guide()

    _print_summary()


if __name__ == "__main__":
    main()
