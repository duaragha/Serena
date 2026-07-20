# Exporting the `hey serena` model

Training happens in the official openWakeWord automatic model training Colab.
It generates synthetic positive examples with Piper TTS and runs on a hosted GPU.
The desk PC only performs CPU inference and local validation.

## Colab workflow

1. Open the official notebook directly in Google Colab:
   `https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb`.
2. Select a T4 GPU runtime.
3. In the config-edit cell, use `target_phrase = ["hey serena"]`,
   `model_name = "hey_serena"`, `n_samples = 5000`,
   `n_samples_val = 1000`, `steps = 10000`, `target_accuracy = 0.6`, and
   `target_recall = 0.25`. Keep the notebook's background paths, validation
   feature path, and ACAV feature mapping unchanged.
4. Run the notebook from top to bottom using its synthetic Piper examples and
   included negative-feature workflow through Step 3. The optional TFLite
   conversion is not needed for Serena.
5. Download `my_custom_model/hey_serena.onnx`. Do not rename a TFLite model to
   `.onnx`.

Real recordings are optional evaluation material, not a prerequisite for this
training route. Local CUDA, an NVIDIA desktop GPU, the old 100 GB corpus setup,
and the retired Serena `wakeword_train.py` wrapper are not part of production.

## Import on the desk host

The shortest production path from the Serena repository is:

```bash
voice/.venv-wake/bin/python -m voice.call.activate_wakeword
```

It expects `~/Downloads/hey_serena.onnx`, validates and promotes the model,
freezes the `0.55` baseline on the current microphone, and starts the desk loop
plus passive observation. For import-only work, the lower-level command remains:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_model install \
  --model ~/Downloads/hey_serena.onnx \
  --source openwakeword-colab
```

If the notebook also emitted a small JSON summary, preserve it in the signed
installation provenance:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_model install \
  --model ~/Downloads/hey_serena.onnx \
  --source openwakeword-colab \
  --training-metadata ~/Downloads/hey_serena-training.json
```

The importer copies the untrusted download into a private staging release,
checks its hash, loads and scores it with the production ONNX runtime, writes
signed provenance, seals the files read-only, and atomically switches the
current release. A failed import never replaces the active model.

Then continue with same-day microphone calibration and passive field
observation in `voice/call/WAKEWORD.md`.
