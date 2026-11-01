# Training the "Hi Kuro" wake-word model

Kuro's sleeping wake detection uses [openWakeWord](https://github.com/dscripka/openWakeWord) with a custom model. Training needs a GPU and several GB of public negative data, so it runs **off the Pi**, once, in Google Colab (about 1 hour on the free GPU). Until the model is installed, or whenever its name doesn't match the robot's name in the studio, the robot automatically keeps the local Whisper wake detection.

## 1. Train in Colab

1. Open the openWakeWord repository, go to its README section **"Training New Models"**, and open the linked Google Colab notebook (the simple automatic training notebook). Choose **Runtime → Change runtime type → GPU**.
2. Settings:
   - **Target phrase:** `hi kuro`. Play the generated test sample first. If the synthetic voice mispronounces "Kuro", try a phonetic spelling such as `hi koo roh` and keep whichever sounds like the way people actually say it.
   - **Number of examples:** at least **20000** (more is better; the default is far lower).
   - **Training steps:** the notebook default (about 10000) is fine for a first model. Raise the **false-activation penalty** if the evaluation below shows false wakes.
   - If the notebook offers extra phrases or spelling variants, add `hey kuro`, so both greetings wake the robot.
3. Run all cells and download the exported **`.onnx`** file. The `.tflite` isn't needed: Python 3.12 on the Pi has no tflite runtime, so Kuro uses ONNX.

## 2. Install on the Pi

```bash
mkdir -p ~/.local/share/face-agent/wake
cp ~/Downloads/hi_kuro.onnx ~/.local/share/face-agent/wake/hi_kuro.onnx      # or scp from the PC
cat > ~/.local/share/face-agent/wake/hi_kuro.json <<'EOF'
{"phrase": "hi kuro", "name": "Kuro", "threshold": 0.5}
EOF
```

- `name` must match the character name in the studio (case doesn't matter). Otherwise the robot falls back to Whisper and logs why.
- The shared feature models (`melspectrogram.onnx`, `embedding_model.onnx`) are already in the same folder; `scripts/setup_respeaker.sh` downloads them.
- A different path can be set with `DOTS_WAKE_MODEL_PATH` in `~/.config/face-agent/startup.env`.
- The model is a local file and isn't committed to git.

## 3. Pick the threshold with real recordings

```bash
systemctl --user stop face-agent.service
.venv/bin/python scripts/wake_eval.py record-positive --count 20     # say "Hi Kuro" at each prompt, from a few places in the room
.venv/bin/python scripts/wake_eval.py record-negative --minutes 10   # normal conversation or TV, no wake phrase
.venv/bin/python scripts/wake_eval.py evaluate
systemctl --user start face-agent.service
```

Choose the lowest threshold whose row passes: **≥ 19/20 true wakes and ≤ 1 false wake per 10 minutes**. Write it into `hi_kuro.json`, then restart the service. The clips stay in `test-output/wake-eval/`, which is gitignored.

## Renaming the robot

A model only knows its own phrase. After a rename in the studio, the next voice start logs `Wake engine: whisper (…trained for "Kuro", but the robot is named "…")`, and "Hi <new name>" keeps working through Whisper. Train a new model for the new name to get fast wake detection back.

## Engine override

`DOTS_WAKE_ENGINE` in `startup.env`:
- `auto` (default): openWakeWord when the model matches the name.
- `oww`: force the model and ignore the name check (useful for testing a pre-trained model such as `hey_jarvis`).
- `whisper`: the original local Whisper wake detection.
