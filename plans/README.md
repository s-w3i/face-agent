# Voice pipeline refactor — phased plans

Turn Kuro's voice input into a continuous, multi-threaded audio pipeline. The wake word stays live while the robot thinks or speaks, so a user can interrupt it (**barge-in**). Use the Hailo-8L AI HAT where it actually helps.

Claude Code executes these plans **one phase at a time**. Each phase file is self-contained: goal, scope, files, tasks, tests, acceptance checks, rollback flag and a "Do not" list. Read this README first, then **only** the phase you are working on.

| Phase | File | Summary |
| --- | --- | --- |
| 0 | [phase-0-baseline-and-hardware.md](phase-0-baseline-and-hardware.md) | Move the speaker to the ReSpeaker jack (hardware AEC) and record baseline metrics |
| 1 | [phase-1-audio-producer-bus.md](phase-1-audio-producer-bus.md) | T1: continuous producer, fan-out `AudioBus` and pre-roll |
| 2 | [phase-2-wake-word-thread.md](phase-2-wake-word-thread.md) | T2: openWakeWord "hi kuro" thread |
| 3 | [phase-3-single-worker-mode-switch.md](phase-3-single-worker-mode-switch.md) | T3: recorder thread and cancel tokens; switch sleep/awake without a respawn |
| 4 | [phase-4-barge-in.md](phase-4-barge-in.md) | The wake word interrupts THINKING/SPEAKING |
| 5 | [phase-5-robustness.md](phase-5-robustness.md) | Thread supervisor, watchdogs, dropout recovery, metrics, soak |
| 6 | [phase-6-hailo-face-tracking.md](phase-6-hailo-face-tracking.md) | Move face detection and ID from the CPU to the Hailo-8L |
| 7 | [phase-7-hailo-whisper-fallback.md](phase-7-hailo-whisper-fallback.md) | Spike, then a Hailo Whisper offline STT fallback |
| 8 | [phase-8-cleanup-docs-soak.md](phase-8-cleanup-docs-soak.md) | Remove the legacy paths, update the docs, run the final soak |

Phases 6 and 7 do not depend on phases 1–5. They can run earlier if you want, but each must still pass its own acceptance checks.

---

## How it works today (before the refactor)

```
chat.py status_chat_loop (asyncio, follows ROS /robot/status)
   │  VoiceInput (voice_input.py) — owns the worker subprocess
   │     stdin : {"paused": bool, "sequence": n}   (acked after stream closes)
   │     stdout: JSON events (utterance_started, transcript_partial/final, microphone_error…)
   ▼
respeaker.py worker (asyncio, single thread)
   poll dev.is_voice() every 50 ms  ──►  open sd.InputStream per utterance
   webrtcvad endpoint (0.8 s) ──► Realtime STT (awake)  or  faster-whisper "small" (SLEEPING)
```

- Wake detection runs only in SLEEPING. It uses CPU Whisper `small`, then the regex `chat.wake_trigger()`.
- Sleep↔awake **kills and respawns** the worker (`VoiceInput.set_awake`).
- In THINKING/SPEAKING/WORKING/… the stream is **closed**, so nothing can hear the user.
- Barge-in is impossible.

## Target architecture

```
                         respeaker.py worker process
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ T1 AudioProducer (PortAudio callback, 16 kHz mono int16, 20 ms blocks)    │
 │        │ non-blocking fan-out, drop-oldest per subscriber                 │
 │        ▼                                                                  │
 │   AudioBus ──► RingBuffer (pre-roll, ~2 s)                                │
 │     ├── Subscription "wake"     ──► T2 WakeWordThread (openWakeWord)      │
 │     │                                 └─ on hit: wake_word_detected.set() │
 │     │                                            CancelToken.cancel()     │
 │     │                                            emit wake_detected       │
 │     └── Subscription "command"  ──► T3 CommandRecorder (webrtcvad endpt)  │
 │                                       └─ call_soon_threadsafe ─┐          │
 │   asyncio loop (main thread): control stdin, Realtime socket ◄─┘          │
 │   STT executor thread: local fallback (Hailo Whisper / CPU Whisper)       │
 │   ReSpeakerControl lock: serializes pyusb is_voice()/direction calls      │
 └───────────────────────────────────────────────────────────────────────────┘
        ▲ stdin controls {paused|mode|capture, sequence}   │ stdout JSON events
        │                                                  ▼
 voice_input.VoiceInput (asyncio) ──► chat.status_chat_loop ──► ROS /robot/set_status
                                     wake_detected in THINKING/SPEAKING ⇒ cancel turn,
                                     status LISTENING (serve.py cuts browser speech)
```

Trigger rules once the work is done:

| Robot status | Wake thread | Command capture starts on |
| --- | --- | --- |
| SLEEPING | live | wake word → `wake_up()` (existing cached ack) |
| IDLE / LISTENING | live (a hit here is ignored, apart from the LED) | hardware VAD (existing behavior), with pre-roll |
| THINKING / SPEAKING | live | wake word ⇒ **barge-in** |
| WORKING / DETECTING / ERROR | live | nothing (navigation is not interrupted) |

## Decisions (agreed with the owner, 2026-11-02)

1. **Wake engine:** openWakeWord with a custom-trained "hi kuro" model. The Whisper engine stays as a fallback (`--wake-engine whisper`), including automatically when the configured name doesn't match the model.
2. **Barge-in trigger:** the wake word only, during THINKING and SPEAKING.
3. **Echo:** the speaker or amplifier moves to the **ReSpeaker 3.5 mm jack**, so the XVF3000's hardware AEC has a playback reference.
4. **Command STT:** keep OpenAI Realtime as the primary engine. Hailo-8L Whisper is an **offline fallback**, added only if the phase 7 spike passes.

## Contradictions found in the original request, and how they're resolved

1. **One `queue.Queue` can't feed both the wake engine and the recorder.** The two consumers would steal chunks from each other. → Use a fan-out `AudioBus`: a bounded queue per subscriber, plus a ring buffer for pre-roll.
2. **"Thread 3 waits for `wake_word_detected`"** conflicts with the awake flow. In IDLE, Kuro takes commands on hardware VAD without a wake word. → Keep the VAD trigger when awake. The wake word wakes the robot and drives barge-in.
3. **A single shared `cancel_current_task` Event is racy.** Something has to clear it, and the wake hit would cancel the capture it just started. → Use **generation-scoped `CancelToken`s**, an extension of the existing `Controls.generation`. Each turn checks its own token.
4. **"Offload STT to a background worker":** awake STT is already a cloud stream (Realtime), and the AI turn runs in `chat.py`'s asyncio loop. → Barge-in cancels the turn through the existing ROS-status cancellation path in `status_chat_loop`.
5. **"Whisper optimised for the Hailo-8L":** Hailo's `hailo-apps` Whisper targets the **Hailo-10H only**, and faster-whisper (CTranslate2) can't use Hailo at all. → On the 8L, only a Whisper tiny/base port is possible (encoder on Hailo, decoder on CPU). A go/no-go spike comes first (phase 7).
6. **"Use `threading`" vs. an asyncio codebase.** → Threads go only into the mic worker (producer, wake, recorder, STT executor). The parent processes stay asyncio, and the two are bridged with `loop.call_soon_threadsafe`. Don't rewrite `chat.py` to use threads.
7. **"The producer never sleeps"** reverses the documented "no idle capture" design. → This is accepted. It also gives pre-roll, which fixes the clipped speech onsets.
8. **A fixed-phrase wake model vs. the configurable character name.** → Renaming the robot needs a new model. Until one exists, Kuro falls back to the Whisper wake engine automatically.
9. **Barge-in needs the mic open during playback.** That only works with AEC, so it depends on phase 0's hardware change.

## Where the AI HAT (Hailo-8L) helps

- **Yes:** face detection and recognition in the tracker (phase 6). The HEFs are already installed: `/usr/local/hailo/resources/models/hailo8l/scrfd_2.5g.hef` and `arcface_mobilefacenet.hef`. Moving this off the CPU frees cores for the audio threads.
- **Maybe:** Whisper tiny/base as the offline STT fallback and as a second-stage wake check (phase 7, after the spike).
- **No:** the openWakeWord inference itself. It's about 1–3% of one CPU core, and compiling it for Hailo would need the x86-only Dataflow Compiler. It isn't worth it.

## Ground rules for every phase (anti-drift)

1. **One phase per branch or commit series.** Branch from the current working branch, e.g. `voice/phase-1-audio-bus`. Commit only when the user asks.
2. **Don't start phase N+1** until phase N's acceptance checks pass and the results are written in the phase file's "Results" section.
3. **The full Python suite stays green.** The JS tests run on any frontend change.
   ```bash
   .venv/bin/python -m unittest discover -s test -p 'test_*.py'
   /usr/bin/python3 -m unittest discover -s test -p test_camera_snapshot.py   # ROS-python tests
   pnpm test   # only if src/ changed
   ```
4. **The ROS status contract doesn't change.** That covers the `/robot/status` topic, the `/robot/set_status` service, revision checks and the state names in `scripts/robot_states.py`.
5. **The IPC stays backward compatible within a phase.** New control or event types are added, and existing ones keep their meaning until phase 8.
6. **Every behavior change sits behind a flag**, and the old path is kept until phase 8.
7. **No new heavy dependencies** beyond those named in a phase. Pin versions in `requirements-respeaker.txt` (voice) or `requirements-tracking.txt` (tracker).
8. **Update the README sections** that a phase touches, in the same phase.
9. **Measure; don't guess.** Each phase lists numeric targets, so record the real numbers.
10. **When reality disagrees with the plan, stop and ask** rather than improvise: a missing library, an unexpected API, or a failed target. Add the finding to the phase file under "Open issues".

## Shared vocabulary

- **AudioBus / Subscription / RingBuffer:** the fan-out from the single capture stream (phase 1).
- **CancelToken:** a per-turn, generation-scoped cancellation object (phase 3). It is *not* a global Event.
- **wake_word_detected:** a `threading.Event` in the worker, set by T2. It is consumed and cleared by the controller that acts on it, never by T2.
- **Mode:** `sleep` (no Realtime socket) or `awake` (Realtime socket open), switched by a control message (phase 3).
- **Barge-in:** a wake hit during THINKING/SPEAKING that cancels the turn and moves to LISTENING (phase 4).
