# Phase 7: Hailo-8L Whisper as the offline STT fallback (spike first)

## Goal
When OpenAI Realtime is unreachable, keep taking voice commands with **local** speech-to-text. Use the Hailo-8L if a Whisper port runs acceptably on it. Otherwise use CPU faster-whisper in the same slot. Realtime stays the primary engine.

## Prerequisites
- Phase 3, which provides the STT executor thread and the cancel tokens. Phase 6 is recommended first, because it sets up the HailoRT service and device sharing.

## Important constraint (read first)
- Hailo's `hailo-apps` Whisper (`~/hailo-apps/hailo_apps/python/gen_ai_apps/simple_whisper_chat`) is **Hailo-10H only** (`resolve_hef_path(..., arch=HAILO10H_ARCH)`). It will not run on this **Hailo-8L**.
- Hailo's separate *Application Code Examples* repository has a speech-recognition example with Whisper **tiny/base** HEFs for Hailo-8/8L. In that example the encoder runs on Hailo and the decoder on the CPU, with fixed-length input windows.
- Its availability, HailoRT version compatibility (4.23 here) and accuracy are **unverified**. Hence the spike.

## Part A: spike (time-box: one session)
1. Get the Hailo-8L Whisper example and HEFs for tiny and base. Check that the HEFs were compiled for `hailo8l` and that they are compatible with HailoRT 4.23. Run it in a scratch venv with the hailort 4.23 wheel from `~/hailo-debs/`.
2. Build an eval set from `test-output/respeaker-*.wav` (real robot utterances). Use ≥ 30 clips with reference transcripts taken from Realtime's `transcript_final` in the logs, or typed by hand.
3. Measure each engine: Hailo tiny, Hailo base, CPU faster-whisper `tiny.en` and `base.en` (int8, `cpu_threads=2`), and Realtime as the reference. Record:
   - WER against the reference;
   - latency from the end of the utterance to the text, median and p90;
   - CPU% during transcription;
   - Hailo utilization while the tracker is also running (phase 6).
4. **Go/no-go:**
   - **Go (Hailo)** if Hailo base or tiny has WER within 10 points of `base.en` CPU, a median latency ≤ 1.5 s for 5 s clips, and it runs alongside the tracker through the shared service.
   - **Otherwise go (CPU):** use faster-whisper `base.en` int8.
   - Write the table and the decision under "Results" **before** starting Part B.

## Part B: implementation
- `scripts/hailo_stt.py` (Hailo case): a `Transcriber` with the **same interface** as `scripts/respeaker_stt.py`, i.e. `__init__(model, language, prompt)` and `transcribe(path) -> {'text', 'stt_seconds'}`.
  - The VDevice uses the shared group or service (as in phase 6).
  - Load the model once, in the STT executor thread that owns it.
  - Implement chunking for the example's fixed window length (cap at 10 s, which matches `--max-seconds 10`).
- **Fallback wiring in the worker** (`scripts/respeaker.py`):
  - New flag `--offline-stt {off,hailo,cpu}`. The default is the engine the spike chose.
  - While awake, if `RealtimeTranscriber.open` fails or `realtime.finish()` raises a **retryable** `RealtimeUnavailable`:
    1. emit `{"status":"stt_fallback","engine":…}`;
    2. transcribe the already-saved WAV (`save_clip` output) locally through the STT executor, honoring the cancel token;
    3. emit `transcript_final` with `"backend": "hailo"|"cpu"`.
  - Keep retrying Realtime in the background, with the existing backoff, and switch back automatically.
  - A **non-retryable** `RealtimeUnavailable` (bad key or quota) keeps the existing behavior, which is ERROR plus a message. Don't silently go offline.
- **Parent:** log the fallback, and show the transcript as usual. Because the whole utterance arrives at once, the display shows no partials while in fallback.
  - `chat.py` still needs the OpenAI agent for replies. When fully offline, the turn fails as today. This phase only removes STT as the failure point. Document that.
- **Optional (behind a flag, off by default): second-stage wake verification in SLEEPING.**
  - After an openWakeWord hit, transcribe the last 2 s of the ring with the local engine and require the `wake_trigger()` regex to match.
  - Enable it only if phase 2's false-wake target wasn't met. It adds latency.

## Files to touch
- `scripts/hailo_stt.py` (new; only if Hailo is a go).
- `scripts/respeaker_stt.py`: allow choosing `tiny.en` / `base.en` and the thread count when it is used as the fallback.
- `scripts/respeaker.py`: the `--offline-stt` flag, fallback wiring, and the executor.
- `scripts/voice_input.py`: handle the `stt_fallback` event (logging).
- `requirements-respeaker.txt` / `scripts/setup_respeaker.sh`: the optional hailort wheel in `.venv`.
- `README.md`: an offline STT section, its limits, and the flag.
- Tests: `test/test_respeaker_stt.py`, `test/test_respeaker_realtime.py`, `test/test_voice_recovery.py`.

## Tests
- A Realtime failure mid-utterance (retryable) → `stt_fallback` and a `transcript_final` from a fake local transcriber, with no ERROR status.
- A non-retryable failure → the existing error path, with no fallback.
- A token cancelled during local STT → the result is suppressed.
- `hailo_stt.Transcriber` is unit-tested with a fake device or infer model (encoder output → decoder path), so no hardware is needed.

## Acceptance checks
- [ ] The spike table and decision are recorded.
- [ ] Full suite green.
- [ ] Unplug Ethernet or Wi-Fi while awake, then speak a command. The transcript appears in the terminal or log through the fallback within the target latency. The agent's reply then fails with the existing, clear network error (expected).
- [ ] Reconnect the network → the next command uses Realtime again, without restarting.
- [ ] Tracker (phase 6) and STT share the Hailo with no device-busy errors during a 15 min mixed session.

## Rollback
`--offline-stt off`.

## Do not
- Don't replace Realtime as the primary engine.
- Don't send fallback audio anywhere.
- Don't load Whisper models on the parent (`chat.py`) side.
- Don't start Part B before the spike decision is written down.

## Results
_(Fill in: the spike table and the decision.)_

## Open issues
_(Fill in.)_
