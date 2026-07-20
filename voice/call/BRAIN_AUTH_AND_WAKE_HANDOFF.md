# Brain auth and hey-serena handoff

This is the complete human handoff for the two remaining setup inputs. The PC
side is already staged. Do these in order.

## 1. Pin the resident brain to a setup token

From the Serena repository, ask Claude CLI for the subscription token:

```bash
claude setup-token
```

Copy the value beginning with `sk-ant-oat01-`, then run:

```bash
.venv/bin/python -m core.install_setup_token
```

Paste the token at the hidden prompt and press Enter. The installer writes only
this assignment:

```text
CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-...'
```

to `~/.config/serena/brain.env`, locks the file to mode `0600`, restarts
`serena-brain.service`, and waits for healthy subscription-auth evidence. It
never prints the token. Do not paste the token into a chat, Persona.md, or a
shell command.

The brain is already live through Claude's existing credential store. This
step makes the unattended systemd credential explicit and restart-stable.

## 2. Export `hey_serena.onnx` in Colab

Open the official notebook directly in Colab:

<https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb>

Select `Runtime`, `Change runtime type`, then `T4 GPU`.

In the `Modify values in the config` cell, use this exact baseline:

```python
config["target_phrase"] = ["hey serena"]
config["model_name"] = "hey_serena"
config["n_samples"] = 5000
config["n_samples_val"] = 1000
config["steps"] = 10000
config["target_accuracy"] = 0.6
config["target_recall"] = 0.25

config["background_paths"] = ["./audioset_16k", "./fma"]
config["false_positive_validation_data_path"] = "validation_set_features.npy"
config["feature_data_files"] = {
    "ACAV100M_sample": "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
}
```

Run the notebook from the top through `Step 3: Train model`. Serena uses ONNX,
so the optional ONNX-to-TFLite conversion cell is not needed. If current Colab
rejects an old TensorFlow or `onnx_tf` package used only by that optional cell,
skip those TFLite-only installs and continue with the ONNX training path.

When training finishes, download:

```text
my_custom_model/hey_serena.onnx
```

Save it as `~/Downloads/hey_serena.onnx` on the Serena laptop. Do not download
the `.tflite` file.

## 3. One command makes it live

From the Serena repository:

```bash
voice/.venv-wake/bin/python -m voice.call.activate_wakeword
```

That command validates the ONNX through the production scorer, promotes it to
an immutable signed release, runs the microphone doctor, freezes the current
desk microphone at threshold `0.55`, installs the current units, and starts:

- the `hey serena` desk loop
- passive false-wake observation
- the daily observation report timer
- the existing bridge and dot overlay

The production paths are:

```text
~/.config/serena/models/hey_serena.onnx
~/.config/serena/models/hey_serena.provenance.json
~/.config/serena/wakeword-acceptance.json
```

The desk loop is usable immediately. The later real-room observation report is
the separate proof of the long-term false-wake rate. It is not a training gate.

If Colab saves the ONNX under another path, pass it explicitly:

```bash
voice/.venv-wake/bin/python -m voice.call.activate_wakeword \
  --model /path/to/hey_serena.onnx
```

Do not use `--allow-existing-observation` on the first activation.
