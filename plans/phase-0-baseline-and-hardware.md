# Phase 0: hardware echo cancellation and a baseline

## Goal
Make barge-in physically possible, then measure today's system so later phases can prove they improve it. The XVF3000's AEC (acoustic echo cancellation) only removes the robot's voice when playback goes through the ReSpeaker's own 3.5 mm output. Today the speaker is a separate USB device, `alsa_output.usb-Jieli_Technology_UACDemoV1.0_…`.

## Prerequisites
- The current `pi5-wip` branch passes the Python test suite.
- A powered speaker or a small amplifier with a 3.5 mm input. The ReSpeaker jack is line/headphone level.

## Scope
- **In:** audio routing, AEC verification, the baseline measurements, and an optional read-only diagnostic.
- **Out:** any change to the capture, wake or chat logic.

## Files to touch
- `plans/BASELINE.md` (new): the measured numbers.
- Optional: `scripts/respeaker.py`. Add a `doctor --aec` subcommand that records 5 s while playing a test tone and prints the RMS. Reuse `select_input()` and `RATE`.
- `README.md`: in "ReSpeaker robot microphone", document that the speaker must be connected to the ReSpeaker jack.

## Tasks
1. **Route the playback.**
   - Connect the speaker or amp to the ReSpeaker 3.5 mm output.
   - Find the sink with `pactl list short sinks | grep -i respeaker`.
   - Make it the default with `pactl set-default-sink <name>`, and make that persistent (a WirePlumber rule or `~/.config/pipewire`). Document which method you used.
   - Confirm that Chromium (the robot display) plays through it. Use the studio's **Play sample**, or let `./run.sh --robot` speak once.
2. **Confirm the firmware and channels.**
   - The input shows as `analog-surround-41` (6-channel firmware). Channel 0 is the processed (beamformed + AEC) signal, and `respeaker.py` opens `channels=1`, which is channel 0.
   - Verify by recording with `arecord -D <respeaker> -c 6 -r 16000 -f S16_LE -d 5` while speaking, then inspect the channels.
   - If the device turns out to be on the 1-channel firmware, record that. It is also processed, so it's still OK.
3. **Verify the AEC.**
   - Play TTS or music at normal robot volume. Record 10 s of channel 0 with nobody speaking, and compute the RMS in dBFS.
   - Repeat with the speaker on the old Jieli device.
   - Expect channel 0 RMS to be **≥ 15 dB lower** on the ReSpeaker jack.
   - Then talk over the playback and confirm your voice stays clearly audible in the recording.
   - Save the WAVs under `test-output/` (that directory is gitignored).
4. **Record the baseline** in `plans/BASELINE.md`, running the stack as it is today:
   - Per-process CPU% while idle-awake, SLEEPING, THINKING and SPEAKING, via `pidstat -p <pids> 5` or `top -b -d 5`. Processes: chat.py, respeaker.py worker, track_human / human_tracker, serve.py, chromium.
   - Wake latency: the time from the end of "Hi Kuro" to the `Wake phrase detected` log line in `~/.cache/face-agent/stack-agent.log`. Take 10 trials and report the median and p90.
   - Wake → cached ack audible, and wake → "Wake command input ready." (includes the worker respawn).
   - IDLE command: speech onset clipped? Note whether the first syllable is missing in the saved `test-output/respeaker-*.wav` files.
   - Pass/fail counts for the test suite.
   - Temperature and throttling: `vcgencmd measure_temp` and `vcgencmd get_throttled` during the CPU runs.

## Tests
None of the code changes. If you add `doctor --aec`, add a unit test in `test/test_respeaker.py` with a mocked `sd`.

## Acceptance checks
- [ ] Chromium speech plays through the ReSpeaker jack after a reboot, with nothing done by hand.
- [ ] The RMS reduction on channel 0 is ≥ 15 dB, measured and written in `BASELINE.md`.
- [ ] `BASELINE.md` has every metric listed above, including the median and p90 latencies.
- [ ] The full test suite is green.

## Rollback
Switch the default sink back to the Jieli device.

## Do not
- Don't change the capture, wake or chat code in this phase.
- Don't change the XVF3000 tuning parameters (AGC, the `GAMMAVAD_SR` voice-detection threshold) without recording the before and after values in `BASELINE.md`.

## Results
Done 2026-11-02 on branch `voice/phase-0-baseline`, **without the 3.5 mm jack** (owner decision: the hardware isn't available yet). Numbers are in [BASELINE.md](BASELINE.md).

- [ ] Chromium speech plays through the ReSpeaker jack after a reboot. **Deferred until the jack is available.**
  - `scripts/audio_output.sh respeaker` sets and persists the default sink (`pactl set-default-sink`, stored by WirePlumber 0.4 in `~/.local/state/wireplumber/default-nodes`) and moves running streams.
  - Today the persisted default is `auto_null`. The Jieli speaker only plays because WirePlumber falls through to the next remembered sink.
- [ ] ch0 RMS reduction ≥ 15 dB. **Pending the jack.**
  - Added `respeaker.py doctor --aec` / `--compare`.
  - Jieli reference: ch0 rises +20.9 dB above its floor during playback, and the AEC reference channel (ch5) is silent, so there's no AEC.
  - Sanity run through the ReSpeaker sink with nothing connected: ch5 carries the reference (−28.8 dBFS).
  - One command fills in the result (BASELINE.md, "Redo with the 3.5 mm jack").
- [x] BASELINE.md has every metric:
  - CPU per process per state, plus temperature and throttling;
  - wake latency median/p90, wake → ack audible, wake → ready (10 synthetic trials);
  - onset clipping;
  - test suite counts.
  - Wake latency used a synthetic voice played through the speaker, not 10 live human trials. Optional live trials can be added by the owner.
- [x] Full test suite green apart from three errors that predate this phase (see BASELINE.md).

Added: `scripts/voice_bench.py`, `scripts/respeaker_aec.py`, `scripts/audio_output.sh`, `test/test_respeaker_aec.py`, a doctor dispatch test in `test/test_respeaker.py`, and the README "Speaker and echo cancellation" section.

## Open issues
- **AEC unverified until the jack arrives.** Phase 4 therefore enables barge-in for THINKING only by default. SPEAKING barge-in sits behind its own flag (`DOTS_BARGE_IN_SPEAKING`), off until the `--aec` rerun passes.
- Channel 0 has strong AGC: its quiet floor moved between −33 and −46 dBFS across runs. The `--aec` "echo suppression" compares each channel's rise above its own floor for that reason.
- `vcgencmd` can't open `/dev/vcio` for this user. The bench reads temperature and throttling from sysfs instead.
- The pre-existing `test_global_status` failure is tracked for phase 2.
