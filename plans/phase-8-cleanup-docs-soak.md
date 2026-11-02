# Phase 8: cleanup, documentation and the final soak

## Goal
Remove the legacy code paths that phases 1–7 kept behind flags, make sure the docs describe the new behavior, and prove the finished system against `BASELINE.md`.

## Prerequisites
- Phases 0–5 are done, and phases 6 and 7 are done or explicitly deferred (record which).
- The owner has confirmed which rollback flags may be removed. **Ask before deleting** any of these:
  - `--legacy-stream`
  - `--respawn-on-wake`
  - `--wake-engine whisper`
  - `--no-barge-in`
  - `--tracker-backend opencv`
  - `--offline-stt`

## Recommended keep/remove list (confirm with the owner)
| Flag / path | Recommendation | Reason |
| --- | --- | --- |
| `--legacy-stream` (per-utterance `InputStream`) | remove | Superseded by the bus |
| `--respawn-on-wake` | remove | Superseded by the mode control |
| `--wake-engine whisper` | **keep** | The only option for a renamed robot without a retrained model |
| `--no-barge-in` | keep | Useful in noisy demos |
| `--tracker-backend opencv` | keep | The fallback when the HAT is unavailable |
| `--offline-stt off` | keep | Field configuration |

## Tasks
1. Remove the approved legacy paths and their tests. Search for dead code: `rg -n "legacy_stream|respawn_on_wake" scripts test`.
2. Update `README.md`:
   - In "Conversation and sleep behavior", update the state table: the wake word is live in all states, and barge-in works in THINKING/SPEAKING.
   - In "ReSpeaker robot microphone", cover the continuous stream, pre-roll, the speaker on the ReSpeaker jack, the openWakeWord model location, and training notes.
   - Add the Hailo sections: tracking, offline STT and `hailort.service`.
   - Startup settings: add `DOTS_WAKE_ENGINE`, `DOTS_WAKE_MODEL_PATH`, `DOTS_BARGE_IN`, `DOTS_TRACKER_BACKEND`, `DOTS_OFFLINE_STT`.
   - Remove the statements that are no longer true, such as "no idle capture or pre-roll", "Local Whisper checks for Hi Kuro" (unless the engine is whisper), and "THINKING / SPEAKING: input capture is paused" (now: command capture is paused; the wake word is live).
3. Update `docs/human-tracking.md` for the Hailo backend.
4. Make sure `scripts/setup_respeaker.sh`, `scripts/setup_tracking.py` and `scripts/install_startup.py` install or preserve everything new, and that a fresh install works from the README alone (test it in a clean clone if possible).
5. Run the **final soak**: 2 h with the full stack through `systemctl --user restart face-agent.service`, using the phase 5 scripted mix plus normal use.
6. Fill in `plans/BASELINE.md` with an "After" column: CPU per process per state, wake latency, barge-in latency, sleep→ready time, false wakes per hour, and the counts of unrecovered failures.

## Acceptance checks
- [ ] Full Python and JS suites green.
- [ ] `rg` finds no references to the removed flags.
- [ ] A README-only fresh setup works, or the deviations are documented.
- [ ] 2 h soak: 0 unrecovered failures, ≤ 2 false wakes, and no self-triggered barge-ins.
- [ ] `BASELINE.md` shows Before/After for every metric. Any regression is explained.

## Do not
- Don't remove a flag the owner didn't approve.
- Don't change behavior in this phase. It covers cleanup, docs and measurement only.

## Results
Done 2026-11-02 on branch `voice/phase-8-cleanup` (from phase 7), **apart from the live soak, which is pending the owner**.

**Flags (owner's decision):** removed `--legacy-stream` / `DOTS_VOICE_LEGACY_STREAM` and `--respawn-on-wake` / `DOTS_VOICE_RESPAWN`. Kept `--wake-engine whisper`, `--no-barge-in`, `--tracker-backend opencv`, `--offline-stt off`.

**Removed with them:** the old worker loop `listen()`, `record_utterance`, `wake_loop`, `BusCapture`, `StreamCapture`, and the parent's respawn command line. Replacing a *dead* worker in `set_awake` stays, because recovery needs it.

**Behavior note:** the plan says no behavior change in this phase. The standalone CLI had to move: `respeaker.py` now always runs the long-lived worker, starting awake for Realtime or `--no-stt`, and asleep for a local `--stt-model` or `--wake-engine oww`. The `time_limit` reason is gone: `--seconds` now ends the session after the current recording's normal endpoint.

**Checks**
- [x] `.venv` suite: 219 tests; the only errors are the cv2-only tracker files, which pass in the tracking venv. JS suite not run, no `src/` change.
- [x] No references to the removed flags in `scripts`, `test`, `docs`, `README.md` (the plans keep them as history).
- [~] Fresh setup: `setup_respeaker.sh` and `track.sh` pass `bash -n` and their new steps were each run by hand on this Pi; a clean-clone install wasn't tested.
- [ ] 2 h soak: 0 unrecovered failures, ≤ 2 false wakes, no self-triggered barge-ins. **Pending the owner** (needs hi_kuro and the paid conversation mix).
- [~] `BASELINE.md` has Before/After from each phase's measurements, labelled as such; the pending rows need the jack, hi_kuro and the soak.
- Live: `--no-stt --once` and `--stt-model tiny.en --once` ran on the real mic from this branch, and `--legacy-stream` is rejected.

**Coverage moved, not lost:** the old `CaptureTest` cases now live in `test_respeaker_worker.py`: hardware trigger before capture, pre-roll, maximum length, pause discards, overflow, single utterance, late transcript. The old "remote start failure" test became the mode-ack failure test plus `test_failed_worker_is_replaced_when_switching_modes`.

## Open issues
- Uncommitted fault-reporting work from another session sits in the main working tree on phase 7. Whichever lands second must resolve `respawn`/`faults` on the `VoiceInput` signature and `chat.py` constructor call, and keep `report_wake_engine` in `start_microphone`.
- This branch was tested from a separate git worktree, so the running service hasn't run this code as a stack.
