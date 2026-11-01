# Phase 1: T1 audio producer and the fan-out AudioBus

## Goal
Replace the per-utterance `sd.InputStream` with **one continuous capture stream** whose audio fans out to independent subscribers. Recording starts with **pre-roll**, so speech onsets are no longer clipped. Everything else (hardware-VAD trigger, endpointing, IPC, Realtime) behaves exactly as before.

## Prerequisites
- Phase 0 is done, with `BASELINE.md` filled in.

## Scope
- **In:** `scripts/audio_bus.py` (new), `respeaker.record_utterance` and `respeaker.listen` refactored to use the bus, plus tests.
- **Out:** wake word, barge-in and mode switching. The stream may now stay open while paused, but nothing consumes it then.

## Design
`scripts/audio_bus.py` (no dependencies beyond numpy and sounddevice):

```python
class RingBuffer:            # fixed-capacity int16 PCM, thread-safe; keeps last N seconds
    write(frames: np.ndarray, t_end: float) / read_last(seconds) -> (bytes, t_start)

class Subscription:          # one per consumer
    queue: queue.Queue(maxsize)           # items: (pcm_bytes, t_capture_monotonic, seq)
    dropped: int                          # drop-oldest counter
    get(timeout) -> item | None; close()

class AudioBus:
    subscribe(name, maxsize=100) -> Subscription  # 100 × 20 ms = 2 s of headroom
    unsubscribe(sub)
    publish(pcm, t, seq)    # called from the PortAudio callback: put_nowait, drop-oldest on Full
    ring: RingBuffer        # ~2 s

class AudioProducer:        # T1
    __init__(bus, device, rate=16000, block=320)
    start() / stop()        # sd.InputStream(callback=..., finished_callback=...)
    callback(indata, frames, time_info, status): copy → bus.publish; count status.input_overflow
    watchdog thread: no frame for 500 ms or finished_callback → reopen (3 tries, backoff 0.2/0.5/1 s)
                     → then set `failed = DeviceUnavailable(...)` for the asyncio side to raise
    stats() -> dict(frames, overflows, reopen_count, last_frame_age_ms)
```

Rules:
- The callback **never blocks** and never takes a lock that another thread holds for long. Copy with `indata[:, 0].copy()` (or `bytes(indata)`), then `put_nowait`. On `queue.Full`, `get_nowait()` the oldest item, put the new one, and increment `dropped`.
- Record the input-overflow flags. The **recorder** decides what an overflow means: as today, it refuses the incomplete utterance (`RuntimeError('Audio capture overflowed…')`).
- Use a monotonic timestamp for each block: `time.monotonic()` at callback time, minus the block duration.
- The `pyusb` calls (`dev.is_voice()`, `dev.direction`) are **not thread-safe** together with each other. Wrap the `Tuning` device in a small `ReSpeakerControl` with a `threading.Lock`, and use it everywhere: in `listen()`, `record_utterance()`, and (later) the wake thread or LED code.

## Files to touch
- `scripts/audio_bus.py`: new, as designed above.
- `scripts/respeaker.py`:
  - `listen()`: create the `AudioBus` and `AudioProducer` once, after `select_input()`, and stop the producer in `finally`.
  - `record_utterance()`:
    - Replace `with sd.InputStream(...)` with `sub = bus.subscribe('command')`.
    - Prepend `bus.ring.read_last(PREROLL_SECONDS)` (default 0.3 s, flag `--preroll-seconds`) to `pcm` and to `realtime.append()`.
    - Read frames with `await asyncio.to_thread(sub.get, 0.5)`.
    - Map the producer's failure to `DeviceUnavailable`.
    - Unsubscribe in `finally`, then `state.capture_closed.set()`.
  - Keep `SpeechEndpoint`, the VAD, the direction sampling, the `reason` logic and `save_clip` unchanged.
  - `Controls.apply()`: `capture_closed` now means "the recorder unsubscribed" (no consumer), so the mic **stream may stay open**. The ack semantics for the parent stay the same.
  - `main()`: add `--legacy-stream` (hidden) to keep the old per-utterance path for rollback. Keep the old code path, selected by the flag.
- `test/test_respeaker.py`: the existing `CaptureTest` cases patch `sd.InputStream` as a per-utterance stream. Adapt them to inject a fake bus or producer, keeping the **same assertions**: a 10 s cap, the hardware trigger before capture, a pause discarding the clip, overflow and disconnect returning no partial audio, single-utterance, and a late transcript suppressed by a pause.
- `test/test_audio_bus.py`: new.
- `README.md` → "ReSpeaker robot microphone": replace "no idle capture or pre-roll" with the new behavior. The stream is continuous, and audio stays in memory only (2 s ring) and is never sent anywhere unless a recording is triggered.

## Tasks
1. Write `audio_bus.py` and its unit tests first (TDD), using a fake stream object that calls the callback from a thread.
2. Add `ReSpeakerControl` (lock wrapper) and switch all `dev.*` calls to it.
3. Refactor `listen()` and `record_utterance()` as described above, keeping `--legacy-stream`.
4. Run the full suite, then run the hardware checks below.

## Tests to add
- Fan-out: 2 subscribers each receive every block, in order (by `seq`).
- Drop-oldest: a slow subscriber with `maxsize=3` keeps the newest 3, and its `dropped` count is correct. The fast subscriber is unaffected.
- The ring buffer returns exactly the last N ms, with correct boundaries at wrap-around.
- Watchdog: a fake stream that stops calling back triggers a reopen, and after 3 failures `failed` becomes `DeviceUnavailable`.
- A callback with `status.input_overflow` → the recorder raises the existing overflow error. No partial transcript is emitted.
- Pre-roll: the recorded PCM starts with the ring contents from before the trigger.

## Acceptance checks
- [ ] Full suite green, plus the new `test_audio_bus.py`.
- [ ] `.venv/bin/python scripts/respeaker.py --no-stt --once --diagnostics` records one utterance. In the WAV, the first syllable is present, with no clipped onset compared with the BASELINE note.
- [ ] `.venv/bin/python scripts/respeaker.py --once --diagnostics` (Realtime) transcribes correctly.
- [ ] 30 min of idle running: `stats()` shows 0 overflows and 0 reopens. The worker's CPU in IDLE is ≤ BASELINE + 3 percentage points of one core.
- [ ] Unplugging the USB mic produces a `microphone_error` with `reconnectable: true` within 2 s (the existing parent recovery takes it from there).

## Rollback
`--legacy-stream`, passed through by `voice_input.start_microphone` if needed.

## Do not
- Don't add the wake word, barge-in, or any new IPC message types.
- Don't change the endpoint, VAD mode or max-seconds defaults.
- Don't persist the ring buffer audio anywhere.
- Don't call `dev.*` without the lock.

## Results
_(Fill in.)_

## Open issues
_(Fill in.)_
