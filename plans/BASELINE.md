# Voice pipeline baseline (before the refactor)

Measured 2026-11-02 on `pi5-wip` @ `400aea9` (Raspberry Pi 5 Model B, 8 GB, kernel 6.8.0-1065-raspi, Hailo-8L fitted but unused), with the full stack running through `face-agent.service`.

**Hardware deviation:** the ReSpeaker 3.5 mm jack isn't wired up yet. The speaker is still the Jieli USB device (`alsa_output.usb-Jieli_Technology_UACDemoV1.0_…`), so the AEC target can't be checked yet. Everything below is measured as the system is today. The AEC rows marked **pending** are filled in by rerunning one command once the jack is connected (see "Redo with the 3.5 mm jack").

## How the numbers were taken

- `scripts/voice_bench.py` (new, read-only):
  - `wake` synthesizes "Hi Kuro." once, in a voice that isn't the robot's (`ash`), through serve.py `/api/speech`. It plays the clip through the default speaker and timestamps the agent log lines as they arrive.
  - It also records the speaker's monitor source (`parec`), so the end of the phrase and the start of the acknowledgment sit on the same clock. CPU is sampled once per second for each process group, tagged with the live `/robot/status`.
  - Raw results: `test-output/bench/baseline-wake.json` (10 trials, each followed by the command "What time is it?") and `test-output/bench/baseline-sleep-quiet.json` (60 s of SLEEPING with nobody speaking).
- `scripts/respeaker.py doctor --aec` (new, see below). Raw results: `test-output/aec/*.json` / `*.wav`.
- Latency precision: about ±30 ms (parec and log-tail polling).

## Wake (SLEEPING → awake), Whisper `small` engine

| Metric | n | Median | p90 | Min | Max |
| --- | --- | --- | --- | --- | --- |
| End of "Hi Kuro" → `Wake phrase detected` log | 9 | **9979 ms** | 10488 ms | 9322 ms | 10488 ms |
| End of "Hi Kuro" → cached ack audible (speaker monitor) | 9 | **10262 ms** | 10678 ms | 9601 ms | 10678 ms |
| End of "Hi Kuro" → `Wake command input ready.` (includes the worker respawn and Realtime connect) | 9 | **13289 ms** | 13714 ms | 12701 ms | 13714 ms |

- Detection rate: **9/10**. The one miss (trial 5) came while the bench still used a 12 s detection timeout, which this ~10 s engine can exceed. The timeout is now 25 s, so that miss may have been a late detection rather than a true miss.
- The wake WAV (about 1.1 s) is saved within ~0.4 s of the end of the phrase. Almost all of the ~10 s is faster-whisper `small` (int8, `beam_size=5`, 4 threads) encoding a padded 30 s window.
- Ack → ready: about 3.0 s (worker kill + respawn + Realtime socket).

## IDLE command capture (onset clipping)

- 9 commands ("What time is it?", played 0.3 s after `Wake command input ready.`).
- In **all 9** saved WAVs, the first 60 ms are already speech: −16.6 to −33.8 dBFS, against a room floor around −45 dBFS. The recording starts after the speech does.
- **2/9 transcripts lost the first word** ("The time is it?"). Smoke-test runs before the baseline also produced "Time is it?".
- The hardware VAD → `sd.InputStream` open path has no pre-roll. This is the clipping phase 1 has to fix.
- Command end → `robot>` reply line: median 6754 ms (5848–9440 ms). This includes the Realtime transcript, the agent model call and the `current_datetime` tool.

## CPU per process (% of one core; mean of 1 s samples)

During the wake + command trials:

| State | chat.py | respeaker.py worker | tracker | serve.py | chromium (all procs) |
| --- | --- | --- | --- | --- | --- |
| SLEEPING, quiet room (n=57) | 1.8 | **1.3** | 4.1 | 3.4 | 25.1 |
| SLEEPING, during wake trials (n=169) | 2.2 | **117.6** | 6.3 | 5.0 | 36.4 |
| IDLE, awake (n=49) | 2.4 | 1.2 | 13.7 | 5.0 | 27.3 |
| LISTENING (n=20) | 4.0 | 3.2 | 13.8 | 5.2 | 34.4 |
| THINKING (n=37) | 5.2 | 0.7 | 13.8 | 4.0 | 34.7 |
| SPEAKING (n=59) | 2.6 | 5.2 | 12.9 | 5.1 | 25.1 |

- The SLEEPING worker averages ~118% while it transcribes each wake candidate, and peaks well above that (4 Whisper threads).
- Any speech in the room (TV, conversation) costs that much CPU for every utterance while asleep.
- The tracker drops to ~4–6% while asleep, because the display stops tracking.

Temperature 44.6–52.9 °C; `get_throttled` = `0x0` throughout (read from sysfs; `vcgencmd` can't open `/dev/vcio` as this user).

## Echo cancellation (phase 0 AEC check)

`.venv/bin/python scripts/respeaker.py doctor --aec --sink <sink>` (stack stopped):
- records 3 s of quiet, then 10 s while looping the cached wake-ack speech through the sink;
- uses all six channels (6-channel firmware confirmed: `analog-surround-41`, `max_input_channels = 6`).

| Run | ch0 rise above its floor | raw mics rise above floor | echo suppression (raw − ch0) | ch5 AEC reference |
| --- | --- | --- | --- | --- |
| Jieli USB speaker (today) | **+20.9 dB** | +9.1 dB | **−11.8 dB** (ch0 *amplifies* the echo; its AGC) | absent (−120 dBFS) |
| ReSpeaker jack, nothing connected (sanity) | −1.2 dB | −0.2 dB | n/a (no acoustic path) | **present** (−28.8 dBFS) |
| ReSpeaker jack + speaker | **pending** | **pending** | **pending** (target: ch0 ≥ 15 dB below the Jieli run) | expected present |

- The firmware's channel layout is confirmed: 0 = processed, 1–4 = raw, 5 = playback reference.
- The XVF3000 receives playback through the ReSpeaker sink (ch5), so AEC is possible once a speaker is on that jack.
- With the Jieli speaker it isn't: the robot hears itself louder on the processed channel than on the raw mics.

### Redo with the 3.5 mm jack

```bash
systemctl --user stop face-agent.service
scripts/audio_output.sh respeaker                                  # persistent default sink
.venv/bin/python scripts/respeaker.py doctor --aec --sink respeaker
.venv/bin/python scripts/respeaker.py doctor --aec --compare        # prints ch0_reduction_db and target_met
systemctl --user start face-agent.service
.venv/bin/python scripts/voice_bench.py wake --trials 10 --command "What time is it?"   # optional: new wake/CPU numbers
```

Then fill in the "pending" row above. Also check by ear: talk over the playback during the `--aec` run, and listen to channel 0 of the saved WAV (`sox test-output/aec/respeaker-*.wav ch0.wav remix 1`).

## XVF3000 tuning

No tuning parameters were changed (AGC, `GAMMAVAD_SR`, …).

## Test suite at baseline

- `.venv`: 140 tests run, 8 skipped, 3 errors, all of which **predate this work**:
  - `test_human_tracker` / `test_tracking_models` can't import `cv2` in `.venv`. They pass in the tracking venv (`~/.cache/face-agent/tracking-venv/bin/python`: 17 OK + 1 skip, and the tracker tests OK).
  - `test_global_status.test_sleep_repeats_local_wake_detection_and_ignores_other_speech` still expects a bare "Hi Kuro" to reach `bot.reply`. Commit `c335a56` (fast wake) changed that to the cached ack. To be fixed in phase 2, which reworks the same path.
- ROS python: `test_camera_snapshot.py`, 8 tests OK.
