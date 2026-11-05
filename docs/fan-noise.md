# Fan noise and voice capture

After the chassis fans were added (fixed speed, close to the ReSpeaker on the same chassis), follow-up commands only registered when the user shouted. The wake word kept working. Users talk from under 1 m.

## How voice is handled

The ReSpeaker XVF3000 does beamforming, noise suppression and AGC on the chip and outputs channel 0. That channel goes to the 16 kHz `AudioBus`, then the `CommandRecorder`, is resampled to 24 kHz, and is streamed to OpenAI Realtime `gpt-live-transcribe`. **Raw audio is sent, not text.** Wake detection (rolling Hailo Whisper) is fully local.

A command capture starts in one of two ways:
- after the wake word, through an explicit capture request;
- for follow-up turns, when the chip's hardware VAD (`VOICEACTIVITY`, threshold `GAMMAVAD_SR`) fires.

The capture ends after 0.8 s of non-speech, or at the 10 s maximum.

## What was wrong

- `GAMMAVAD_SR` was **15.0** on the chip (factory 1.5), probably a leftover manual `tuning.py` write. The hardware VAD fired on only 3% of normal-speech frames, so only shouting started a follow-up turn.
- At the factory 1.5, fan noise alone fired the VAD 36% of the time.
- **A first attempt (VAD 3 + stronger noise suppression) made things worse:** recordings started on background noise ("Thank you for watching."), and stronger suppression lowered accuracy. Its quiet test was too short, and the agent was in use during it.
- The fans hum at about 875 Hz and 1754 Hz, inside the speech band, so a high-pass filter can't remove them.

## Calibration (2026-11-06, `scripts/calibrate_mic.py`)

The guided run stops the agent and alternates two phases: LED red, 30 s of fans only; then LED green, 5 known sentences. Accuracy is scored with local Whisper against those sentences.

| Setting | Fan false triggers | Caught | Slowest trigger | Accuracy |
|---|---|---|---|---|
| factory NS, VAD 5 | 0 | 5/5 | instant | 97% |
| strong NS, VAD 8 | 0 | 5/5 | instant | 91% |
| **factory NS, VAD 8 (chosen)** | **0** | **5/5** | 0.54 s | **100%** |
| strong NS, VAD 5 | 0 | 5/5 | instant | 94% |

Stronger noise suppression cost accuracy in both rounds that used it, so the factory suppression stays. VAD 8 was chosen for its margin against false triggers.

## What runs now

- **`~/.config/mimo-dots/respeaker-calibration.json`** holds the chosen chip settings (`GAMMAVAD_SR=8`, factory noise suppression), the fan floor (−47.4 dB), the end-of-speech gate (8 dB) and the pre-roll (0.79 s). The worker writes these to the chip at every start (`scripts/respeaker_profile.py`) and logs them to `microphone-chat.log`.
  - Without a calibration file, the defaults are VAD 6 and factory noise suppression, with the gate off.
- **End-of-speech gate** (`SpeechGate`): a frame counts as speech only if WebRTC VAD agrees and it is 8 dB above the noise floor. The floor estimate never exceeds the calibrated fan floor + 3 dB, so the robot's own playback in the buffer can't raise it.
- **Pre-roll** never reaches back past the moment the mic reopened, so the robot's last words don't leak into the user's command.
- **OpenAI transcription**: `delay: high`, a prompt plus keywords (the robot's name, "pick up", "green bottle", "soda can"), and **no** `noise_reduction`. See the next section.

## OpenAI transcription settings

After calibration, the saved clips were clean (local Whisper heard every command), but OpenAI still returned cut-off or empty text. Replaying the same 10 command clips through each setting:

| Setting | Result |
|---|---|
| low delay + `near_field` noise reduction (what ran) | 3 empty, most cut off ("Agree", "Green", "Hey for") |
| low delay, no noise reduction | 0 empty, 2 wrong |
| high delay, no noise reduction | 0 empty, 1 wrong ("green folder") |
| **high delay + prompt + keywords** | **10/10 right** |
| `gpt-transcribe` model | 0 empty, 2 wrong |

`near_field` noise reduction was the main cause. The robot only uses the final transcript after each commit, so a higher delay doesn't slow the live captions it never shows. OpenAI validates `delay` but doesn't echo it back in `session.updated`.

Overrides (in the service environment):

```
DOTS_RESPEAKER_PROFILE=off          # leave the chip alone
DOTS_RESPEAKER_GAMMAVAD_SR=6        # any writable XVF3000 parameter (beats the calibration)
DOTS_ENERGY_GATE_DB=0               # disable the end-of-speech gate
DOTS_STT_DELAY=medium               # minimal|low|medium|high|xhigh (default high)
DOTS_STT_KEYWORDS="Kuro,pick up,red cup"   # replaces the default keyword list
DOTS_STT_NOISE_REDUCTION=near_field # opt-in only: it emptied transcripts here
```

## Re-calibrating

Re-calibrate after any hardware change (fans, mic position, enclosure). Run it in a normal terminal, standing where you usually talk; it takes about 4 minutes and nothing is sent to OpenAI:

```bash
cd ~/face-agent && .venv/bin/python scripts/calibrate_mic.py
```

Red LED means stay quiet. Green means say the sentence on screen, once. Don't say the robot's name. The script restarts the agent at the end, and reports and WAVs go to `test-output/calibration/`.

For lower-level checks, `respeaker.py doctor --noise --label <name> [--set NAME=VALUE]` records one run with temporary chip settings (stop the agent first).
