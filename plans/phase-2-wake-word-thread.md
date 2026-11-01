# Phase 2: T2 wake-word thread (openWakeWord, "hi kuro")

## Goal
Replace SLEEPING-mode wake detection (CPU Whisper `small` → transcript → `chat.wake_trigger()` regex) with an always-on openWakeWord thread that reads from the `AudioBus`. Waking gets faster and cheaper. The thread also becomes the trigger source that barge-in needs later (phase 4).

## Prerequisites
- Phase 1 is done: `AudioBus`, the producer, and `ReSpeakerControl` are all present.
- A trained model file, from step 1 below.

## Scope
- **In:** the model training procedure, the `openwakeword` dependency, `WakeWordThread`, the `wake_detected` event, using it in SLEEPING, the `--wake-engine` flag, and tests.
- **Out:** barge-in (phase 4), removing the worker respawn (phase 3), and Hailo (phase 7).

## Tasks

### 1. Train the model (off-Pi, one time)
- Use openWakeWord's "automatic model training" notebook (Colab or a GPU PC). It generates synthetic positives with Piper TTS, adds negatives and augmentation, and exports `.onnx`.
- Train the phrase **"hi kuro"**, and include "hey kuro" as an alternative spelling or sample. Use ≥ 20k synthetic positives and the default negative datasets.
- Export `hi_kuro.onnx`. Store it on the Pi at `~/.local/share/face-agent/wake/hi_kuro.onnx`. The path is configurable through `DOTS_WAKE_MODEL_PATH`, and `--wake-model-path` on both `respeaker.py` and `chat.py`.
- Also add a file `hi_kuro.json` next to it: `{"phrase": "hi kuro", "name": "Kuro", "threshold": 0.5}`. The worker uses `name` to check it against the studio character name.
- Record 20 real "Hi Kuro" clips and 10 minutes of normal conversation or TV on the Pi under `test-output/wake-eval/`, to use for threshold tuning.

### 2. Dependency
- Python here is 3.12, and `tflite-runtime` has no 3.12 wheel. Install with:
  `.venv/bin/pip install --no-deps openwakeword==<pin>`, plus any missing runtime dependencies (`scipy`, `tqdm`, `requests`; check `import openwakeword` and `Model(..., inference_framework='onnx')`).
- Download the shared feature models (`melspectrogram.onnx`, `embedding_model.onnx`) once, using `openwakeword.utils.download_models()`. Cache them under `~/.local/share/face-agent/wake/`.
- **If the install fails:** stop and report it under "Open issues". The fallback is to vendor the minimal feature pipeline: melspec ONNX → embedding ONNX → classifier ONNX, using onnxruntime 1.31, which is already in `.venv`.
- Pin the version in `requirements-respeaker.txt`, and update `scripts/setup_respeaker.sh`.

### 3. `WakeWordThread` (new module `scripts/wake_word.py`)
```python
class WakeWordThread(threading.Thread):  # daemon=False; joined on shutdown
    __init__(bus, model_path, threshold=0.5, patience=2, refractory=1.5,
             on_detect: Callable[[dict], None], stop: threading.Event)
    run(): sub = bus.subscribe('wake', maxsize=50)
            accumulate 20 ms blocks → 1280-sample (80 ms) frames → model.predict(frame)
            score ≥ threshold for `patience` consecutive frames and not in refractory
               → wake_word_detected.set(); on_detect({status:'wake_detected', score, t, model})
               → model.reset()  (clear internal buffers so one phrase ≠ multiple hits)
    stats() -> dict(frames, max_score_last_10s, detections, dropped)
```
- `on_detect` must be thread-safe. It hands off to the worker's asyncio loop with `loop.call_soon_threadsafe(...)`, and from there to `emit()`. Make `emit()` itself safe by guarding stdout writes with a lock.
- Use `t` = the monotonic capture time of the frame that triggered the hit. Phase 4 uses it to start capture after the phrase.
- Optional: enable openWakeWord's Silero VAD gate (`vad_threshold`) **only if** false wakes exceed the target.

### 4. Integrate into the worker (`scripts/respeaker.py`)
- New args: `--wake-engine {oww,whisper}` (default `oww` when the model file exists and its name matches the character, otherwise `whisper`), `--wake-model-path`, `--wake-threshold`.
- When `--wake-engine oww` is set, the worker started for SLEEPING **does not load faster-whisper**:
  - Start `WakeWordThread`.
  - Don't run the hardware-VAD → record → STT loop while in wake mode, because the wake thread replaces it.
  - On a hit, emit `wake_detected` (`{"status":"wake_detected","score":…,"t":…}`).
- `whisper` engine: unchanged legacy path.

### 5. Integrate into the parent
- `scripts/voice_input.py` `receive()`: handle `status == 'wake_detected'` while `not self.awake`. Put a sentinel into `self.inputs`, e.g. a dict `{'wake': True, 'score': …}`, or use a dedicated `self.wakes` queue, and log the score. **Don't** publish it as listening text, because wake audio never reaches the display (keep the `publish_text` guard).
- `scripts/voice_input.py` `start_microphone()`: pass `--wake-engine` / `--wake-model-path`. Read the character name through `self.api('config')`, as is done today for `--stt-prompt`. If it differs from the model's `name`, choose `whisper` and print a one-line warning.
- `scripts/chat.py` `status_chat_loop`: in SLEEPING, treat the wake sentinel exactly like a matched `wake_trigger()` with an empty remainder, so it calls the existing `wake_up()` (cached ack, `set_awake(True)`, IDLE).
  - With openWakeWord, a command spoken straight after "Hi Kuro" is **not** captured in this phase. That is a known regression, which phases 3 and 4 fix by starting capture with pre-roll.
  - Document it under "Open issues".
- `scripts/chat.py` legacy `chat_loop`: handle the sentinel the same way, using its `wake_up()`.

## Tests to add or update
- `test/test_wake_word.py` (new): a fake model whose `predict()` returns scripted scores. Check:
  - threshold plus patience;
  - refractory (two hits within 1.5 s → one event);
  - `reset()` called after a hit;
  - `on_detect` payload fields;
  - clean stop and join within 1 s;
  - the drop counter when the consumer is slow.
- `test/test_voice_input.py`: in SLEEPING, a `wake_detected` from the fake child produces a wake input, and nothing is published to listening-text.
- `test/test_fast_wake.py` / `test_chat.py`: the sentinel triggers `wake_up()`, the transitions are `SLEEPING → SPEAKING → IDLE`, and a name mismatch picks the whisper engine.

## Acceptance checks
- [ ] Full suite green.
- [ ] Offline eval script (put it in `scripts/`, or as a test helper) on `test-output/wake-eval/`: ≥ 19/20 true wakes, and ≤ 1 false wake in the 10 min negative set at the chosen threshold. Record the threshold.
- [ ] On the Pi, 10 live wakes: median latency (end of phrase → `Wake phrase detected`) ≤ 300 ms. Compare it with the BASELINE median.
- [ ] Wake-thread CPU ≤ 5% of one core. The SLEEPING worker's CPU is well below BASELINE (no Whisper).
- [ ] 1 h of TV or radio playing in SLEEPING: ≤ 1 false wake.
- [ ] Renaming the character in the studio → the next voice start logs the fallback to whisper and still wakes on "Hi <newname>".

## Rollback
`--wake-engine whisper`, or `DOTS_WAKE_ENGINE=whisper` in `~/.config/face-agent/startup.env`.

## Do not
- Don't send any wake audio or scores to OpenAI or the display.
- Don't remove the Whisper wake path or `wake_trigger()` (phase 8 decides).
- Don't add barge-in behavior yet. A `wake_detected` arriving outside SLEEPING is only logged.
- Don't commit the `.onnx` model to git unless the owner agrees. It lives in `~/.local/share`.

## Results
Done 2026-11-02 on branch `voice/phase-2-wake-word`. The custom **"hi kuro" model doesn't exist yet**: the owner trains it in Colab using [docs/wake-word-training.md](../docs/wake-word-training.md).
- Until then the robot auto-selects Whisper. The agent log shows `Wake engine: whisper (no wake model at …/hi_kuro.onnx)`.
- The openWakeWord path was verified end to end with the pre-trained `hey_jarvis_v0.1` model, forced through `DOTS_WAKE_ENGINE=oww` (temporary `startup.env` lines, since removed).

Dependencies:
- `openwakeword==0.6.0` is installed `--no-deps`. tflite-runtime has no 3.12 wheel; ONNX is used.
- Its package `__init__` also imports scikit-learn and scipy. Pinned in `requirements-respeaker.txt`: `scipy>=1.13,<1.17`, `scikit-learn>=1.5,<1.8`, `tqdm`, `requests` (owner chose the official package over vendoring). An unpinned install would pull numpy 2.x; the pins keep numpy 1.26.4.
- Shared feature models are in `~/.local/share/face-agent/wake/`. `setup_respeaker.sh` installs all of it.

Checks:
- [x] Full suite green: 162 tests in `.venv`. The only errors are the cv2-only tracker files, which pass in the tracking venv.
  - The phase 0 `test_global_status` failure is fixed: the test now expects the fast-wake path.
  - New: `test/test_wake_word.py` (threshold/patience, refractory, reset, payload, stop/join < 1 s, drop counter, engine choice, offline replay = live rule).
  - New worker tests: the oww engine never loads Whisper or polls hardware VAD, and a hit while paused is only logged.
  - New parent tests: `WakeWord` sentinel, no display text, rename → Whisper with a log line.
  - New chat tests: sentinel → `SLEEPING → SPEAKING → IDLE` with no agent turn; a hit while awake is ignored.
- [ ] Offline eval ≥ 19/20 true wakes and ≤ 1 false wake per 10 min. **Pending the owner:** the trained model, plus `scripts/wake_eval.py record-positive` / `record-negative` / `evaluate`.
- [~] Live wake latency, 10 synthetic "Hey Jarvis." trials through the speaker (`test-output/bench/phase2-oww-jarvis.json`):

  | Metric | openWakeWord (hey_jarvis) | BASELINE (Whisper small) |
  | --- | --- | --- |
  | Detection rate | 10/10 | 9/10 |
  | End of phrase → `Wake phrase detected` | **432 ms** median, p90 488 | 9979 ms, p90 10488 |
  | End of phrase → ack audible | **630 ms**, p90 679 | 10262 ms |
  | End of phrase → `Wake command input ready.` | **3693 ms**, p90 3726 | 13289 ms |

  This is 23× faster, but the target was ≤ 300 ms. About 160 ms of it is `patience=2` (two 80 ms frames) plus the score rising after the phrase ends. Re-measure with the real hi_kuro model. `patience=1` would save 80 ms, at some risk of false wakes.
  - Commands after the wake transcribed 10/10 as "What time is it?" (phase 1 pre-roll).
- [~] CPU:
  - The wake thread costs **9.7% of one core** in the live worker at idle clocks (6.8% at full clock in an isolated benchmark).
  - The worker with openWakeWord averages 11.4% in a quiet room, or 23.6% including model loads at each respawn.
  - The Whisper engine uses 1.3% when the room is quiet but **118% while it transcribes each utterance** (any TV or conversation).
  - The ≤ 5% target is not met; see open issues.
- [ ] 1 h of TV/radio while SLEEPING, ≤ 1 false wake. **Pending the owner** (needs the trained model).
- [x] Rename → the next voice start logs `Wake engine: whisper (wake model hi_kuro.onnx was trained for "Kuro", but the robot is named "Dots")`. This is unit-tested with a real `RobotServer` config change and a fake worker. Whisper then wakes on "Hi <newname>" as before.

## Open issues
- With openWakeWord, commands spoken straight after the phrase are still lost, until phase 3.
- **Wake CPU above the 5% target.** The embedding model alone costs 3.8 ms per 80 ms frame.
  - `wake_word.load_model` already shrinks openWakeWord's 10 s raw buffer to 1 s. That removes a per-frame list copy and saves about 40% with identical features, verified against stock scores; any difference is openWakeWord's own random init.
  - Further option (not done): gate inference on a cheap energy/VAD signal and replay the ring's last ~1.5 s into the model when sound starts.
- **Wake latency 432 ms against the 300 ms target**, measured with `hey_jarvis`. Re-check with hi_kuro.
- `voice_bench.py` paid-API use is now opt-in (`--allow-api`), at the owner's request.
