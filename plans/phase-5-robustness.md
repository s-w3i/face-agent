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
Done 2026-11-02 on branch `voice/phase-5-robustness`.

**Design as built** (`scripts/worker_threads.py`, wired into `respeaker.serve()`)
- Python threads can't be killed, so:
  - a thread that **raised** is restarted within a budget;
  - a thread that's **stuck** (no heartbeat) has its stack logged and is escalated as a reconnectable `microphone_error` with `category: stalled`. The parent's existing reconnect path then replaces the worker. No new parent retry loop was added.

| Source | Failure | Handling |
| --- | --- | --- |
| T1 | stream stall or stop | reopen 3× (0.2/0.5/1 s), then `DeviceUnavailable` → reconnectable (phase 1) |
| T1 | `USBError` errno 19 | reconnectable (unchanged) |
| T2 | model exception | restart up to 3×/5 min, then `category: wake_engine`. The parent sets `wake_engine_failed` and the next worker uses Whisper. |
| T3 | overflow or a dropped block in an utterance | `utterance_discarded` (`reason: overflow`); worker keeps running |
| T3 | other exception | its utterance → `utterance_discarded` (`reason: error`), Realtime turn dropped, T3 restarted (3/min), then `category: recorder` |
| T2/T3 | heartbeat older than 2 s | stack → `warning` in the log, then `category: stalled` |
| Local STT | over 60 s | result dropped with a warning |
| Realtime | `RealtimeUnavailable` | unchanged |

- **Deviation (STT watchdog 60 s, not 5 s):** Whisper `small` takes about 10 s for a wake clip on this Pi (BASELINE), so a 5 s limit would fire on every Whisper wake. Realtime keeps its own 30 s timeout.
- **Process-wide pause rule (found live):** after `SIGSTOP`/`SIGCONT`, all heartbeats are old for the same reason. If the supervisor loop itself was paused for more than 1 s, heartbeats are refreshed instead of judged.
- **Metrics:** every 60 s → `microphone-chat.log`, written by the parent. Fields: frames, overflows, reopens, per-subscription depth and drops, wake stats, CPU seconds per thread (`/proc/self/task`), RSS, thread count. Wake drop rate above 1% over 10 s → `warning`.
- **Shutdown:** stop events, joins with a 2 s timeout, and a `warning` naming any thread that didn't stop.
- **onnxruntime:** openWakeWord already uses `intra_op_num_threads=1` / `ncpu=1` (checked in phase 2).

**Checks**
- [x] Full suite green: 203 tests. The only errors are the cv2-only tracker files, which pass in the tracking venv.
  - `test_worker_threads.py` (5).
  - Fault injection in `test_respeaker_worker.py`:
    - stalled stream → `DeviceUnavailable`;
    - a wake model raising once → restart, and detection continues;
    - always raising → `wake_engine` after 3 restarts;
    - T3 raising mid-utterance → discarded, restarted, and the next utterance is transcribed;
    - overflow mid-utterance → discarded and still running;
    - stuck recorder → `stalled` with its stack;
    - a whole-process pause isn't treated as stuck (this test fails without the fix);
    - periodic metrics;
    - shutdown with busy threads in < 3 s, with no thread left.
  - Parent: `wake_engine` → restart without `--wake-engine`; metrics and warnings land in `microphone-chat.log`.
- [ ] Unplug and replug the ReSpeaker during IDLE, SPEAKING and SLEEPING. **Pending the owner.**
- [x] `kill -STOP` the worker for 3 s, then `kill -CONT`:
  - First run: the robot recovered by itself in about 3 s (`Microphone reconnected.`, SLEEPING with voice), but through a **false** "stuck" escalation.
  - After the pause rule: same worker PID, no warning, still SLEEPING with voice.
- [~] Soak (free, no paid turns): 100 sleep↔awake cycles via `/robot/set_status`, about 38 min.
  - The first worker ran **73 cycles**: threads constant at 15, RSS 390.2 → 391.6 MB.
  - Cycles 73–91 overlapped an unplanned network fault: Realtime connects timed out ("unavailable after 3 connection attempts") while about 400 MB of phase 7 HEFs downloaded. The existing recovery replaced the worker 5 times and the robot returned on its own. After the network recovered, cycles 92–100 were normal and it ended SLEEPING with voice.
  - Worker metrics: 0 stream reopens, 2 input overflows (both in the first minute while Whisper loaded, outside any utterance), no wake drops (Whisper engine, no T2).
  - **0 unrecovered failures.**
  - The plan's full **1 h soak with 20 wakes, 10 barge-ins, 30 commands and 2 USB replugs is pending the owner**, since it needs the hi_kuro model, paid turns and hands on the cable.

## Open issues
- Overflows while Whisper loads (startup) show the callback can be starved for a moment under full CPU load. They're harmless outside an utterance. Inside one, the utterance is now discarded, not the worker.
- Escalated failures (`stalled`, `recorder`) reuse the USB reconnect path. Its log line now names the real cause instead of always "USB connection lost".
