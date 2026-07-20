# Serena wake word

This is the fully local pre-wake path for `hey serena`. Before a detection,
microphone PCM stays in memory on the desk host. The calibration database stores
scores, coarse RMS, timestamps, and labeled attempt windows. It never stores
audio, transcripts, or embeddings.

## Current state

The ONNX runtime, model importer, calibration harness, frozen acceptance
harness, desk pipeline, and systemd units are implemented. The remaining human
step is exporting the custom `hey serena` ONNX model from the official
openWakeWord Colab, importing it here, and collecting the real-room evidence.

No NVIDIA GPU, CUDA installation, or real voice recordings are required on this
machine. Training uses synthetic Piper speech in a hosted Colab GPU runtime.
Wake inference is CPU-only.

## Install the ONNX runtime

```bash
python3 -m venv voice/.venv-wake
voice/.venv-wake/bin/python voice/call/install_desk_runtime.py
```

openWakeWord 0.6.0 declares a Linux TFLite dependency without a Python 3.12
wheel. Serena uses ONNX only, so the installer resolves the ONNX dependencies
and installs openWakeWord without the unused TFLite package.

## Export and promote `hey serena`

Follow `voice/call/WAKEWORD_TRAINING.md` for the short Colab workflow. Download
the resulting `.onnx` file, then run:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_model install \
  --model ~/Downloads/hey_serena.onnx \
  --source openwakeword-colab
```

The importer rejects symlinked or malformed inputs, loads the model through the
same production scorer used by the desk client, and only then promotes it. Each
model and its provenance live in one immutable private release. The stable
public paths move together through `.wakeword-current`:

```text
~/.config/serena/models/hey_serena.onnx
~/.config/serena/models/hey_serena.provenance.json
```

The provenance is sealed with a machine-local HMAC key. Verify it at any time:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_model verify
```

## Calibrate on the real desk

First verify model inference and microphone access:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_harness doctor
voice/.venv-wake/bin/python -m voice.call.wakeword_harness devices
```

Start continuous score collection:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_harness collect \
  --environment normal-desk
```

Before each deliberate phrase, open an intent window from another terminal,
say the phrase once, then confirm whether it was actually spoken:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_harness attempt \
  --environment quiet-near

voice/.venv-wake/bin/python -m voice.call.wakeword_harness confirm INTENT_ID \
  --status valid
```

Exercise at least near-field quiet, far-field, and normal media or desk noise.
Inspect the development evidence with:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_harness status
voice/.venv-wake/bin/python -m voice.call.wakeword_harness report
```

## Freeze the production threshold

Once development evidence points to a livable threshold, freeze the exact
model, runtime, microphone, VAD, gate, and optional verifier:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_harness freeze \
  --threshold 0.55 \
  --patience-frames 1 \
  --cooldown-seconds 3 \
  --model-provenance ~/.config/serena/models/hey_serena.provenance.json
```

The frozen configuration can power the desk loop immediately. It does not wait
for a week of recordings or a large phrase-attempt quota. Install and start the
separate passive observation collector at the same time:

```bash
install -Dm644 systemd/serena-wakeword-acceptance.service \
  ~/.config/systemd/user/serena-wakeword-acceptance.service
install -Dm644 systemd/serena-wakeword-acceptance-report.service \
  ~/.config/systemd/user/serena-wakeword-acceptance-report.service
install -Dm644 systemd/serena-wakeword-acceptance-report.timer \
  ~/.config/systemd/user/serena-wakeword-acceptance-report.timer
systemctl --user daemon-reload
systemctl --user enable --now serena-wakeword-acceptance.service
systemctl --user enable --now serena-wakeword-acceptance-report.timer
```

Deliberate observation attempts must be labeled and confirmed against the
observation database. The false-wake verdict stays inconclusive until it spans
seven active days with at least one hour of normal desk audio per day. Twenty
total wake attempts provide a basic miss-rate check; five must cover the normal
desk environment. The frozen configuration must have no more than 5 percent
misses overall, no more than 10 percent misses in the checked environment, no
more than 1 percent dropped microphone frames, and zero false accepts in the
minimum observation window. This is Serena's field-observation policy, not an
openWakeWord training requirement.

After the passive week:

```bash
systemctl --user stop serena-wakeword-acceptance.service
voice/.venv-wake/bin/python -m voice.call.wakeword_harness report \
  --manifest ~/.config/serena/wakeword-acceptance.json \
  --db ~/.local/state/serena/wakeword-acceptance.sqlite3 \
  --output ~/.local/state/serena/wakeword-acceptance-report.json \
  --require-pass
```

That report is the only thing allowed to claim the false-wake objective passed.
