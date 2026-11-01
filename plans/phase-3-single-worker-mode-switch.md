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
_(Fill in.)_

## Open issues
_(Fill in.)_
