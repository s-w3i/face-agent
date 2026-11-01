# Phase 3: T3 command recorder, cancel tokens, and one long-lived worker

## Goal
Keep **one** `respeaker.py` worker alive across sleep↔awake. Switching modes opens or closes the Realtime socket on request instead of killing and respawning the process. Command capture moves into its own **thread** (T3), controlled by **generation-scoped cancel tokens**. The wake thread now runs continuously in both modes.

## Prerequisites
- Phases 1 and 2 are done. The wake thread is working in SLEEPING.

## Scope
- **In:** a new `mode` control, `CommandRecorder` thread, `CancelToken`, `VoiceInput` mode switching, wake → capture with pre-roll (fixes the phase 2 regression), and tests.
- **Out:** barge-in during THINKING/SPEAKING (phase 4), the supervisor and metrics (phase 5).

## Design

### Cancel tokens (replace the single global cancel Event)
```python
class CancelToken:                       # scripts/audio_bus.py or scripts/cancel.py
    generation: int
    _event: threading.Event
    cancel(reason) / cancelled -> bool / reason
class CancelScope:                       # owns "the current" token
    new() -> CancelToken                 # cancels the previous token, returns a fresh one
    cancel_current(reason)
```
- This generalizes `Controls.generation`. `Controls.apply()` calls `scope.cancel_current('control')` wherever it bumps `generation` today.
- T2's hit calls `scope.cancel_current('wake')` **before** the controller creates a new token for the wake-initiated capture. That way the new capture can't be cancelled by its own trigger.
- Every long operation (recording, `realtime.finish()`, local STT) takes a token and checks it. A result whose token is cancelled is dropped, which matches the existing "late transcript suppressed" behavior.

### IPC (backward compatible)
- Existing: `{"paused": bool, "sequence": n}`. Unchanged.
- New: `{"mode": "sleep"|"awake", "sequence": n}`.
  - **awake:** open the Realtime socket (`RealtimeTranscriber.open`), then ack.
  - **sleep:** close the socket, then ack.
  - The wake thread keeps running in both.
  - The ack is `{"status":"microphone_control","mode":…, "sequence": n}`, sent after the socket is open or closed. On a Realtime open failure, emit the existing `microphone_error` with `category: realtime_unavailable` and **stay in sleep mode**.
- New: `{"capture": "start", "from_t": float, "sequence": n}`. Start a command capture now, with audio from monotonic time `from_t` (taken from the ring). Used after a wake hit. Ack it the same way.
- Keep the worker CLI flag `--stt-model` as the *awake* model. Add `--initial-mode sleep|awake`.

### CommandRecorder (T3)
- A thread that waits for a trigger:
  - the hardware VAD (`ReSpeakerControl.is_voice()` polled every 50 ms while awake, not paused, not stopping); or
  - a `capture start` control.
- For each capture it:
  1. takes `token = scope.new()`;
  2. subscribes to the bus and prepends pre-roll (`from_t` or 0.3 s);
  3. runs `SpeechEndpoint` + webrtcvad, with direction sampling through `ReSpeakerControl`;
  4. forwards PCM to the Realtime sender with `loop.call_soon_threadsafe(realtime.append, data)`;
  5. on endpoint, hands `(event, token)` to the asyncio side for `realtime.finish()`, or to the STT executor thread when the local fallback is used.
- Emit the same events as today: `utterance_started`, `speech_direction`, `utterance_discarded`, `utterance_saved`, `transcript_final`.
- **Move** the logic of `record_utterance()` into it rather than rewriting it. Keep the reason codes (`silence`, `maximum_duration`, `time_limit`).

### Parent (`scripts/voice_input.py`)
- `set_awake(awake)`: replace stop/start of the process with a sequence-acked `mode` control. Keep the existing hold/held/guard choreography around it.
  - On `RealtimeUnavailable`, the worker stays in sleep mode. Raise as today (the existing callers handle it).
- `restart(awake=…)`: stays a full process restart, used **only** by recovery (`chat.recover_voice`, `ensure_microphone`).
- `start_microphone()`: start the worker with `--initial-mode`.
- `receive()`: in sleep mode, after a `wake_detected`, the parent forwards the wake as today (phase 2). Once awake, `chat.wake_up()` asks for `capture start` with the wake `t` + ~0.6 s, after the cached ack finishes (see the next point).
  - **Speech straight after "Hi Kuro":** after the cached ack, start capture from the ring at `t_wake_end`, so a command spoken over the ack is captured. With hardware AEC (phase 0), the ack audio is suppressed.
  - If the owner prefers it, simply start capture after the ack instead. Decide by testing, and record the choice.

### `scripts/chat.py`
- `wake_up()`: `voice.set_awake(True)` is now fast, a socket open with no respawn. Keep the cached ack. After `IDLE`, call `voice.start_capture(from_t)` when the wake came from openWakeWord.

## Files to touch
- `scripts/respeaker.py`: `Controls.apply` (new message types), `listen()` (single long-lived loop, mode state, thread startup and shutdown), and `record_utterance` → moved into the `CommandRecorder` thread.
- `scripts/audio_bus.py` (or a new `scripts/cancel.py`): `CancelToken`, `CancelScope`.
- `scripts/voice_input.py`: `set_awake`, `start_microphone`, `receive`, new `start_capture`.
- `scripts/chat.py`: `wake_up()` (both loops).
- Tests: `test/test_respeaker.py`, `test/test_respeaker_realtime.py`, `test/test_voice_input.py`, `test/test_voice_recovery.py`, `test/test_fast_wake.py`.

## Tasks
1. `CancelToken` / `CancelScope`, with unit tests.
2. Worker `mode` control and long-lived loop (keep the old respawn path behind `--respawn-on-wake`, default off).
3. Move the recorder into a thread, bridged to asyncio. Make `emit()` thread-safe if phase 2 didn't.
4. Parent `set_awake` via the control. Add `start_capture`.
5. `wake_up()` captures the follow-on command.
6. Full suite, then the hardware checks.

## Tests to add or update
- A mode switch sends no new `create_subprocess_exec` call (assert the spawn mock was called once).
- A Realtime open failure on `mode: awake` → `microphone_error realtime_unavailable` with the worker still alive and in sleep mode, and the wake thread still running. This matches the existing "remote start failure restores local wake" test.
- A token cancelled mid-recording → `utterance_discarded`, and no `transcript_final`.
- A token cancelled during `realtime.finish()` → the late transcript is suppressed.
- `capture start` with `from_t` → the PCM starts at the ring offset.
- Recovery (`test_voice_recovery.py`) still performs a full restart after a worker crash.

## Acceptance checks
- [ ] Full suite green.
- [ ] Sleep → "Wake command input ready." median improves on BASELINE by ≥ the old worker startup time. Measure 10 trials and record them.
- [ ] "Hi Kuro, what time is it?" spoken in one breath gets an answer to the question (the follow-on command is captured).
- [ ] 50 sleep↔awake cycles with no leaked threads (`threading.enumerate()` count is stable, logged) and no growth in the worker RSS beyond 10 MB.
- [ ] IDLE hardware-VAD commands behave exactly as before: 0.8 s endpoint, 10 s cap, partials shown on the display.

## Rollback
`--respawn-on-wake`, which restores the phase 2 behavior.

## Do not
- Don't implement barge-in. In THINKING/SPEAKING, a wake hit is still only logged.
- Don't change the ROS status transitions in `wake_up()` / `turn()`.
- Don't use a single global `threading.Event` for cancellation.
- Don't block the PortAudio callback or the asyncio loop on STT.

## Results
Done 2026-11-02 on branch `voice/phase-3-single-worker`. There's still no 3.5 mm jack and no trained `hi_kuro` model, so openWakeWord checks used `hey_jarvis` (temporary `startup.env` lines, removed afterwards).

**Design decisions and deviations**
- **Follow-on command without AEC.** The plan's "capture from the wake time across the cached ack" would record Kuro's own "Yes? I am here…" (there's no AEC yet) and send it to Realtime as user speech. Instead:
  - After an openWakeWord hit the worker checks whether you keep talking: 160 ms of speech means yes, 400 ms of silence means no, so a comma pause doesn't end the check.
  - If you do keep talking, `wake_detected` carries `continued: true`. Chat then **skips the spoken ack**, switches to awake and sends `{"capture":"start","from_t": wake_t + 0.05}`. The recording comes from the ring buffer, now 8 s, so nothing plays over you and nothing is lost.
  - A plain "Hi Kuro" still gets the cached ack. This works the same with or without the jack.
- **A Realtime failure on `mode: awake`** comes back as an error in the mode **ack** (`error.category = realtime_unavailable`), not as a `microphone_error` event. The existing parent treats `microphone_error` as worker death, and here the worker stays alive in sleep mode with T2 running. The parent raises `RealtimeUnavailable` from `set_awake`, so the callers' handling is unchanged.
- **The Whisper wake engine** also uses the single worker: Whisper `small` is loaded once and kept, about 390 MB RSS.
- The old path stays behind `DOTS_VOICE_RESPAWN=1` / chat `--respawn-on-wake` (`respeaker.py` without `--initial-mode` = the old `listen()`). `DOTS_VOICE_LEGACY_STREAM=1` implies it.
- A capture request replaces a hardware-VAD recording that started less than 1.5 s earlier, because it's the same speech and more.

**Checks**
- [x] Full suite green: 182 tests. The only errors are the cv2-only tracker files, which pass in the tracking venv.
  - New `test_cancel.py` and `test_respeaker_worker.py` (11 tests, on a real AudioProducer with a fake stream): mode switch, Realtime failure keeps sleep + T2, VAD capture → Realtime → paused_after, pause mid-recording discards, cancel during `finish()` suppresses, capture from `from_t`, capture refused while asleep, continued/plain wake, comma pause, capture replaces a fresh VAD recording.
  - In `test_voice_input.py`: a single spawn across 4 mode switches, a Realtime failure keeps the same process, a continued wake → `start_capture`. The respawn-protocol tests are pinned to `DOTS_VOICE_RESPAWN=1`.
  - `test_fast_wake.py`: a continued wake skips the ack (transitions `['IDLE']`) and requests the capture at t + 0.05.
  - The recovery tests still do a full restart.
- [x] Sleep → awake switch with no respawn, `scripts/voice_mode_cycles.sh` (IDLE set via `/robot/set_status` → "Realtime transcription ready."; each figure includes about 1 s of `ros2` CLI startup):

  | Path | Median | Max | Worker |
  | --- | --- | --- | --- |
  | Old respawn (`DOTS_VOICE_RESPAWN=1`, 10 cycles) | **6.06 s** | 6.36 s | a new PID every cycle |
  | Long-lived (50 cycles) | **1.92 s** | 2.16 s | one PID |

  That's about 4.1 s saved per switch, more than the old worker startup. Falling asleep no longer leaves a deaf gap while Whisper reloads.
  - "Wake command input ready." after a *plain* wake: 12434 ms (Whisper, 10 trials) versus 13289 ms at BASELINE. It's capped by the 3 s cached ack, which the respawn already overlapped.
  - After a *continued* openWakeWord wake (no ack): **0.7–1.1 s** after the sentence ends.
- [x] "Hey Jarvis, what time is it?" in one breath: **3/3 answered** (2 trials, plus 1 diagnostic).
  - A local Whisper transcription of the saved follow-on capture shows it starts exactly at "What" (0.00 s), with no wake-phrase tail and no clipping.
  - Plain "Hey Jarvis.": 6/6 got the ack and were not treated as continued.
  - With the Whisper engine, "Hi Kuro, what time is it?" already worked through the transcript remainder; that's unchanged.
- [x] 50 sleep↔awake cycles: **threads constant at 15**, one PID, **RSS +2.5 MB** (389.8 → 392.3 MB).
- [x] IDLE hardware-VAD commands: 0.8 s endpoint, 10 s cap, partials, single utterance, all unchanged (worker tests plus the live runs above).

**Bugs found and fixed during the live checks**
- A continued wake waited on the skipped ack's playback, so `wait_for_speech(None)` returned False and the robot went to ERROR. Fixed; the test's playback stub now behaves like the real helper.
- `chat.microphone_action()` never returned its task's result, so the follow-on capture logged "not accepted" even though it had been accepted. Fixed and asserted.
- **Found by accident, older than this work:** `test_chat.ChatCheck.test_sleep_wake_three_second_inactivity_and_closing_message` ran `chat.main()` against the **live** service (`DOTS_URL` default :5173) and switched it to words mode. That is the "Voice input paused while the robot service is unavailable" state seen at the start of this work, and after every suite run. `ChatCheck` now points `DOTS_URL` at a closed port; a full suite run leaves the live robot in voice mode.

**OpenAI use in this phase:** 1 TTS clip (cached) and 4 conversation turns (3 one-breath trials plus 1 that errored before replying), plus the Realtime sockets that waking opens.

## Open issues
- The continuation check delays a *plain* openWakeWord wake by about 0.3–0.4 s: detection log 720–870 ms with `hey_jarvis`, versus 432 ms in phase 2. Tunable with `--wake-continuation` (0 disables) and the `speech_continues` silence window.
- In SPEAKING the hardware-VAD capture is gated by the pause, as before. Barge-in is phase 4.
- `voice_mode_cycles.sh` timings include the `ros2` CLI startup. Only the difference between the two paths is meaningful.
