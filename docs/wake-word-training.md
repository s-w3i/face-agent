# Training the "Hi Kuro" wake-word model

Kuro's sleeping wake detection uses [openWakeWord](https://github.com/dscripka/openWakeWord) with a custom model. Training needs a GPU and tens of GB of public data, so it runs **off the Pi**, once, on a Linux PC with an NVIDIA GPU such as an RTX 3080. Until the model is installed, or whenever its name doesn't match the robot's name in the studio, the robot automatically keeps the local Whisper wake detection.

## 1. Train on the GPU PC

**You need:** Linux on x86_64 (on Windows, use WSL2 and keep the work folder on the Linux disk, not `/mnt/c`), a working `nvidia-smi`, `git`, Python 3.10 or [uv](https://docs.astral.sh/uv/), about 45 GB of free disk space, and at least 16 GB of RAM. An SSD is strongly recommended because training reads a 17 GB feature file continuously.

Clone this repository on the PC, or copy over `scripts/train_wake_word.py` and `requirements-wake-training.txt`, then run these from the repository folder:

```bash
python3 scripts/train_wake_word.py setup     # Python 3.10 venv and pinned upstream code, in ~/kuro-wake-training
python3 scripts/train_wake_word.py data      # about 21 GB of downloads; resumes if interrupted
python3 scripts/train_wake_word.py preview   # sample clips, in ~/kuro-wake-training/preview
python3 scripts/train_wake_word.py train     # generate clips, augment, train
```

- **Check the pronunciation first.** Play the WAV files from `preview`. The default phrases are spelled `hi kuuro` and `hey kuuro` because the synthetic voice reads "kuro" as "kyoo-roh" and "kuuro" as "koo-roh". If you want a different pronunciation, pass your own spelling to both commands (the example below is the "kyoo-roh" one):
  ```bash
  python3 scripts/train_wake_word.py preview --phrase "hi kuro" --phrase "hey kuro"
  python3 scripts/train_wake_word.py train   --phrase "hi kuro" --phrase "hey kuro"
  ```
  Words that aren't in the CMU pronouncing dictionary also need an entry in `PRONUNCIATIONS` in `scripts/train_wake_word.py`; `train` stops and says so if one is missing.
- **Defaults:** both greetings wake the robot, with 50000 positive clips, 50000 adversarial clips and 50000 training steps. `--samples` and `--steps` change these.
- **False wakes:** if the evaluation in section 3 shows too many, train again with a higher `--max-negative-weight` (default 1500). The generated clips are reused, so only augmentation and training run again.
- **New phrases:** after changing the phrases, add `--fresh`. It deletes the previous run's clips and features, which belong to the old phrases.
- **Other folder:** add `--work PATH` to every command to keep the data somewhere other than `~/kuro-wake-training`.

Running a step again continues where it stopped: finished downloads and generated clips are kept, but augmentation and training start over. The result is `~/kuro-wake-training/output/hi_kuro.onnx` plus its `hi_kuro.json`. There's no `.tflite`: Python 3.12 on the Pi has no tflite runtime, so Kuro uses ONNX.

## 2. Install on the Pi

From the PC:

```bash
ssh <pi> 'mkdir -p ~/.local/share/face-agent/wake'
scp ~/kuro-wake-training/output/hi_kuro.onnx ~/kuro-wake-training/output/hi_kuro.json <pi>:~/.local/share/face-agent/wake/
```

`hi_kuro.json` contains `{"phrase": "hi kuuro", "name": "Kuro", "threshold": 0.5}`. `phrase` shows the training spelling, which is fine because the robot only matches on `name`.

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
