# Phase 5: thread supervision, dropout recovery and metrics

## Goal
Make the multi-threaded worker fail loudly and recover automatically. No thread may die silently, a stalled stream must be detected and reopened, and every failure must reach the parent's existing recovery path with the right category. Add metrics to tune and prove stability, then pass a 1 h soak.

## Prerequisites
- Phases 1–4 are done.

## Scope
- **In:**
  - a supervisor for T1, T2, T3 and the STT executor;
  - heartbeats and watchdogs;
  - the overflow policy;
  - USB dropout escalation;
  - clean shutdown;
  - metrics logging;
  - fault-injection tests and the soak.
- **Out:** new features, and changing the parent's backoff schedule (`VOICE_RETRY_DELAYS` in `chat.py`).

## Design
- **`ThreadSupervisor`** (in `scripts/audio_bus.py` or a new `scripts/worker_threads.py`):
  - Every worker thread runs `target` inside `try/except BaseException`, which puts `(name, exc, traceback)` on `errors: queue.Queue`.
  - The asyncio main loop polls `errors` every 100 ms (or uses `call_soon_threadsafe`) and maps them:

    | Source | Error | Mapped to |
    | --- | --- | --- |
    | T1 | `DeviceUnavailable` or `USBError` errno 19 | `microphone_error`, `reconnectable: true` (existing parent → `ensure_microphone`) |
    | T1 | `sd.PortAudioError` after 3 reopen attempts | `microphone_error`, `reconnectable: true` |
    | T2 | model exception | log it and restart T2 up to 3 times per 5 min. After that, emit `microphone_error` with a new `category: "wake_engine"`. The parent logs it and falls back to `--wake-engine whisper` on the next restart. |
    | T3 | any exception | discard the current utterance (`utterance_discarded`), restart T3. Repeated failure → `microphone_error` |
    | Realtime | `RealtimeUnavailable` | unchanged (existing category) |

- **Heartbeats:**
  - Each thread updates `last_beat`. A supervisor timer flags a thread stuck for more than 2 s, or more than 5 s for STT.
  - A stuck T1 → reopen the stream. A stuck T2 or T3 → log it with the stack trace (`sys._current_frames()`) and escalate as above.
- **Overflow policy (explicit):**
  - The wake subscription drops oldest and counts it, and the count is logged. A sustained drop rate above 1% for 10 s logs a warning.
  - The command subscription: any drop or `input_overflow` during an utterance → discard it, which matches today's "refuse incomplete speech".
- **Shutdown:**
  - A single `stop: threading.Event` is checked by every loop. `Subscription.close()` unblocks `get()`.
  - Join with a 2 s timeout per thread, and log any thread that fails to join. The worker exits within 3 s of stdin closing. `VoiceInput.stop_microphone` waits 30 s, then kills.
- **Metrics:**
  - Every 60 s the worker emits `{"status":"metrics", ...}`, written to the existing `~/.cache/face-agent/microphone-chat.log` (the parent already sends stderr there; route metrics to the log, not to stdout events the parent must parse, or have the parent log them).
  - Fields: frames, overflows, reopens, per-subscription depth/dropped, wake detections, max wake score, CPU time per thread (`time.thread_time()` per thread), and RSS.
  - Log rotation already exists through the stack supervisor (`*.log` in `~/.cache/face-agent`).
- **Thread priorities (optional, measure first):**
  - `os.nice(-5)` needs privileges, so skip it.
  - Instead, check `onnxruntime` thread settings: set `intra_op_num_threads=1` for openWakeWord, so it doesn't fight T1 or the tracker for the 4 cores.

## Files to touch
- `scripts/audio_bus.py` (or new `scripts/worker_threads.py`): the supervisor and heartbeats.
- `scripts/respeaker.py`: wire the supervisor, the error mapping and metrics into `listen()` / `main()`.
- `scripts/wake_word.py`: `intra_op_num_threads`, heartbeat.
- `scripts/voice_input.py`: handle `category: wake_engine` (log it and set the next start to the whisper engine), and log metrics.
- `README.md`: the recovery paragraph in "Conversation and sleep behavior" (new failure types), and where the metrics are.
- Tests: new `test/test_worker_threads.py`, plus updates to `test/test_voice_recovery.py`.

## Tests to add (fault injection)
- The fake stream stops calling back → reopen. Three failures → `microphone_error reconnectable` emitted once.
- The fake stream raises `PortAudioError` in the callback context → same path.
- The fake wake model raises once → T2 restarts, and the next detection still works. Raising 4 times → `wake_engine` error.
- T3 raises mid-utterance → `utterance_discarded`, T3 restarts, and the next utterance works.
- Shutdown with all threads busy → the worker exits in under 3 s, and no thread is left alive.
- A burst of slow consumers → the wake drop counter increases, and no exception occurs.
- The parent: a `wake_engine` error → the next `start_microphone` passes `--wake-engine whisper`.

## Acceptance checks
- [ ] Full suite green, including the new fault-injection tests.
- [ ] Manual: unplug and replug the ReSpeaker during IDLE, during SPEAKING and during SLEEPING. Each one recovers to the correct state within the documented backoff, with no manual action.
- [ ] Manual: `kill -STOP` the worker for 3 s, then `kill -CONT` → the gate timeout or recovery path recovers, and the robot doesn't get stuck.
- [ ] **1 h soak** with the full stack (`./start-stack.sh`) and a scripted mix: 20 wakes, 10 barge-ins, 30 commands and 2 USB replugs. Result: 0 unrecovered failures, 0 leaked threads, worker RSS growth under 20 MB, and no sustained wake drops. Attach the metrics summary under "Results".

## Rollback
The supervisor is internal, so there's no flag. Revert the phase commit if needed. The phase 1–4 flags still allow the legacy paths.

## Do not
- Don't swallow exceptions without emitting or logging them.
- Don't add retries in the parent that duplicate `recover_voice` / `ensure_microphone`.
- Don't send metrics to the display or to OpenAI.

## Results
_(Fill in.)_

## Open issues
_(Fill in.)_
