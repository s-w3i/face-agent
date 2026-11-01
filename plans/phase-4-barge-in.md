# Phase 4: barge-in, where "Hi Kuro" interrupts THINKING and SPEAKING

## Goal
While Kuro is THINKING (model call, tools, camera) or SPEAKING (browser TTS), saying "Hi Kuro" should:
1. stop speech playback instantly;
2. cancel the in-flight AI turn so its reply is never spoken or shown;
3. move to LISTENING and capture the user's new request (including words spoken straight after the wake phrase).

## Prerequisites
- Phases 0–3 are done. The hardware AEC is verified (phase 0), so the wake thread works while the robot speaks.

## Scope
- **In:**
  - keeping the wake thread live while command capture is gated;
  - the barge-in handling in `status_chat_loop` and the legacy `chat_loop`;
  - capture after barge-in;
  - the `--barge-in` flag;
  - tests and latency measurement.
- **Out:**
  - interrupting WORKING/DETECTING (navigation and motion keep running; a hit there is only logged);
  - "any speech" barge-in;
  - stop words.

## Existing mechanisms to reuse (don't reinvent)
- `status_chat_loop.watch_status()` (`scripts/chat.py`) cancels `operation` when the status changes from another source.
- Revision-checked `status.set(..., expected_revision=…)` stops stale turns from publishing.
- `serve.RobotServer.apply_robot_status()` (`scripts/serve.py`) changes the display command when the status changes. That **cancels the pending browser speech**, and `/api/say` returns 409 for a stale `statusRevision`.
- `wait_for_speech()` returns False when the command changes. `turn()` turns that into `RuntimeError('Speech playback failed or was interrupted.')`. Barge-in must **not** be reported as an error, as described below.
- `VoiceInput.hold()` / `control(paused)` gate **command capture** only. After phase 3, "paused" no longer stops the bus or the wake thread.

## Design
1. **Worker:**
   - Paused now means "no command capture". T1 and T2 keep running in every state.
   - On a hit, T2 cancels the current token (phase 3) and emits `wake_detected`.
   - In awake mode, `wake_detected` events are emitted even while paused. Today `receive()` drops events when `not self.accepting`, so add an explicit pass-through for this event type.
2. **`VoiceInput`:**
   - Add a `self.barge_ins = asyncio.Queue(maxsize=1)` fed by `wake_detected` while `self.awake`.
   - Drop the event if the queue is full, since one pending barge-in is enough.
3. **`status_chat_loop`:**
   - Add a `watch_barge_in()` task next to `watch_status()` / `watch_voice()`.
   - On an event, read `current = status.current`. If `current.status in ('THINKING', 'SPEAKING')` and the `--barge-in` flag is on:
     1. Set a local flag `barged = current.revision`.
     2. `await status.set('LISTENING', expected_revision=current.revision)`. That publishes the new status, so serve.py cuts the audio, and `watch_status` cancels `operation`. Our own `source` is filtered out of `watch_status`, so **cancel `operation` explicitly** after a successful set.
     3. Make sure the cancelled turn's `except asyncio.CancelledError` path treats `barged` as a deliberate interrupt. It must not raise, publish ERROR or set IDLE.
     4. In the main loop, the next iteration sees LISTENING. `voice.follow_status('LISTENING')` opens capture. Then call `voice.start_capture(from_t=wake_t + phrase_tail)`, so speech straight after "Hi Kuro" is kept and the phrase itself is excluded. Tune `phrase_tail`, starting at 0.
   - If `set` fails (the revision changed, so another node took control), ignore the barge-in.
   - Agent history: `Chatbot.reply()` only updates `self.history` after `Runner.run` completes, so a cancelled THINKING turn leaves the history unchanged.
     - If it was cancelled during SPEAKING, the reply *was* added to the history but only partly heard. Append a short system note, e.g. `{"role":"system","content":"The user interrupted your previous reply."}`, or leave the history as it is. Decide, test it, and document it.
4. **Legacy `chat_loop` (`--no-robot-status`):** the equivalent logic uses `server.show_state` / `request(url, 'command', …)` to stop speech. Implement it only if cheap. Otherwise disable barge-in in legacy mode and document that.
5. **Feedback:** there is no spoken ack on barge-in, because it adds latency and the user is already talking. The LISTENING animation, plus `led.set_state(LISTEN)` in the worker, is the feedback.
6. **Flag:** `--barge-in/--no-barge-in` (default on once the acceptance checks pass), and `DOTS_BARGE_IN` in `startup.env`.

## Files to touch
- `scripts/respeaker.py`: emit `wake_detected` while paused in awake mode, and LED feedback.
- `scripts/voice_input.py`: `receive()` pass-through and a `barge_ins` queue.
- `scripts/chat.py`: `status_chat_loop` (`watch_barge_in`, interrupt-aware exception handling in the main loop, and in `turn()` / `run_turn()`), the argparse flag in `main()`, and optionally the legacy `chat_loop`.
- `README.md`: in "Conversation and sleep behavior", document barge-in and the state table ("THINKING / SPEAKING: wake word interrupts").
- Tests: `test/test_chat.py`, `test/test_fast_wake.py` (reuse its `Authority` fake status), `test/test_voice_input.py`, `test/test_global_status.py` if it covers the transitions.

## Tests to add
- **Barge-in during THINKING:** a fake `bot.reply` that blocks. Inject `wake_detected` → the transitions include `THINKING → LISTENING`, `bot.reply` is cancelled, no `say` request is made, and no ERROR.
- **Barge-in during SPEAKING:** a fake `say` + `wait_for_speech` that blocks → `SPEAKING → LISTENING`, no ERROR, no IDLE published by the old turn, and `start_capture` called with `from_t`.
- **Stale reply:** a reply that completes just after the barge-in is never published, because the revision check rejects it.
- **Ignored states:** a wake event in WORKING, DETECTING, IDLE or LISTENING causes no transition.
- **Flag off:** `--no-barge-in` → the event is logged and ignored.
- **Race:** a ROS node sets WORKING at the same moment → whichever set wins, no exception, and the final status is consistent.

## Acceptance checks
- [ ] Full suite green.
- [ ] Ask a long question so Kuro gives a long answer, then say "Hi Kuro, stop. What's the weather?" mid-speech. Audio stops, Kuro listens, and it answers the weather question. Repeat 10 times: ≥ 9 succeed.
- [ ] Latency from the end of "Hi Kuro" to audible silence: median ≤ 400 ms over 10 trials. Measure with a phone recording or log timestamps (`wake_detected` t → serve.py speech-cancel log). Record the numbers.
- [ ] Barge-in during THINKING (e.g. a vision request) cancels it. No stale answer is spoken later.
- [ ] 30 min of normal conversation: **0** self-triggered barge-ins (Kuro interrupting itself). If it fails, tune the threshold for awake mode separately (`--barge-in-threshold`), and re-check the phase 0 AEC.
- [ ] Navigation (WORKING) is never interrupted.

## Rollback
`--no-barge-in` / `DOTS_BARGE_IN=0`.

## Do not
- Don't cancel robot motion or navigation.
- Don't add a spoken ack for barge-in.
- Don't publish ERROR for a deliberate barge-in.
- Don't let a cancelled turn's reply into the display subtitles.
- Don't remove the hold/control gating. It still prevents command capture of the robot's own speech.

## Results
Done 2026-11-02 on branch `voice/phase-4-barge-in`. **There's no 3.5 mm jack yet (no hardware AEC)**, so as the owner decided:
- barge-in during **THINKING** is on by default (`DOTS_BARGE_IN`, chat `--barge-in/--no-barge-in`);
- barge-in during **SPEAKING** is implemented and tested, but off until AEC is verified (`DOTS_BARGE_IN_SPEAKING=1`, `--barge-in-speaking`).

Barge-in needs the openWakeWord engine; Whisper can't listen continuously while awake. **It's inactive until the trained `hi_kuro.onnx` is installed.** It was verified with `hey_jarvis` (temporary `startup.env`, since removed).

**Implementation**
- Worker: awake wake hits are emitted even while capture is paused, and the LED ring shows LISTEN for 1.5 s.
- `VoiceInput.barge_ins`: a 1-slot queue for those hits, cleared on mode switches and restarts.
- `status_chat_loop.watch_barge_in()`:
  - On a hit in an allowed state, it publishes LISTENING with a revision check (the display then cancels pending speech) and cancels the turn.
  - The main loop treats that cancellation as deliberate: no ERROR and no IDLE from the old turn. It then requests a capture from wake t + 0.05 s, so "…stop, what's the weather?" becomes the new request.
  - If the revision changed first (another node), the hit is ignored.
- History:
  - THINKING: unchanged, because `Chatbot.reply()` only stores history after `Runner.run` completes.
  - SPEAKING: `{"role": "system", "content": "The user interrupted your previous reply before it finished playing."}` is appended.
- Legacy `chat_loop` (`--no-robot-status`): no barge-in (documented).

**Checks**
- [x] Full suite green: 189 tests. The only errors are the cv2-only tracker files, which pass in the tracking venv.
  - New `BargeInTest` (7):
    - THINKING → LISTENING with no `say`, no ERROR, history unchanged, capture at t + 0.05;
    - a reply that completes after the barge-in is never spoken;
    - SPEAKING isn't interrupted without the flag (THINKING → SPEAKING → IDLE);
    - with the flag, SPEAKING → LISTENING with no ERROR/IDLE and the interruption note;
    - `--no-barge-in`;
    - IDLE/LISTENING/WORKING/DETECTING change nothing;
    - navigation publishing WORKING at the same moment wins.
  - Parent: an awake hit while paused reaches `barge_ins` and never becomes a command or display text.
- [ ] "Hi Kuro, stop. What's the weather?" during **speech**, ≥ 9/10. **Deferred: needs the jack (AEC) and `DOTS_BARGE_IN_SPEAKING=1`.**
- [~] Latency. The audible-silence target applies to SPEAKING (deferred).
  - In THINKING, end of "Hey Jarvis," → LISTENING published: **median 547 ms** over 2 trials (544/549). Of that, ~530 ms is the openWakeWord detection (`wake_hit_ms`) and ~15 ms is the status publish and cancel.
  - Re-measure with hi_kuro and the jack.
- [x] Barge-in during THINKING cancels it: **2/2** (`voice_bench.py barge`).
  - The interrupted "long dragon story" request was **never spoken or shown** (`stale_replies: []`).
  - The follow-on "What time is it?" was transcribed and answered.
- [ ] 30 min of normal conversation with 0 self-triggered barge-ins. **Pending the owner.**
  - In these runs Kuro's own speech produced no awake wake hits: exactly 2 logged, both from the test clips.
  - SPEAKING hits can't interrupt anything unless the AEC flag is on.
- [x] Navigation (WORKING) is never interrupted: unit-tested, and WORKING isn't in the barge states.

**OpenAI use in this phase:** 1 TTS clip (cached) and 4 turns: 2 interrupted, partly generated, plus 2 answered.

## Open issues
- SPEAKING barge-in and its 400 ms audible-silence target wait for the jack. Then run `respeaker.py doctor --aec`, set `DOTS_BARGE_IN_SPEAKING=1`, and repeat the 10-trial check (a SPEAKING variant of `voice_bench.py barge`, with `--barge-delay` placed inside the reply).
- `phrase_end_to_listening_ms` takes the end of the wake phrase from the first pause in the synthetic clip, so it's approximate (±50 ms).
