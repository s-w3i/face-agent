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
_(Fill in.)_

## Open issues
- When the openWakeWord engine is used, commands spoken straight after the phrase are lost until phase 3 or 4.
