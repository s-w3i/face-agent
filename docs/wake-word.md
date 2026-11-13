# Wake detection: "Hi Kuro"

While Kuro is asleep, a local engine listens for **"Hi <name>"** (or "Hey <name>"). No sleeping audio is sent to OpenAI. The agent log says which engine is active, for example `Wake engine: rolling (Hailo Whisper phrase scoring in rolling windows).`

| Engine | When `auto` picks it | Notes |
| --- | --- | --- |
| `rolling` | Hailo runtime and Whisper base HEFs installed (`scripts/setup_respeaker.sh`) | Default. Works for any speaker and any name, with no training. |
| `oww` | No Hailo Whisper, and an openWakeWord model whose name matches ([training](wake-word-training.md)) | Cheapest. Only as good as its synthetic training voices. |
| `whisper` | Neither of the above | VAD segments plus CPU Whisper small. Slow and unreliable in noise. |

Set `DOTS_WAKE_ENGINE` in `~/.config/face-agent/startup.env` to force one.

## How the rolling engine works

`scripts/rolling_wake.py`, running in the microphone worker:

1. **Windows, not segments.** Every 1.5 s, the last 3 s of audio are checked, so a phrase of up to 1.5 s always lands whole in one window. A window is skipped unless WebRTC VAD finds at least 0.3 s of speech, so a quiet room costs nothing. Nothing waits for a pause. If scoring falls more than 2 s behind, windows are skipped until it catches up, but audio is never dropped.
2. **Phrase scoring on the Hailo-8L.** Whisper base (`scripts/hailo_stt.py`) is asked how likely the window is to say " Hi, Kuro" or " Hey Kuro". The score is the mean log-probability of the phrase's tokens under forced decoding. It doesn't transcribe and then search the text, which Whisper base gets wrong for many accents ("high curo", "thai kuru").
3. **CPU check for borderline windows.**
   - A score of `DOTS_WAKE_ROLLING_THRESHOLD` (default −3.3) or higher wakes at once.
   - A score between `DOTS_WAKE_VERIFY_FLOOR` (default −5.5) and the threshold becomes a candidate. One phrase spans several overlapping windows, so only the best window of a run is checked, one window after its peak. The check is Whisper **small** on the CPU, prompted with "The robot is named Kuro.", with the loose text match ("High Kuro", "I, Kuro" and "Aikuro" count).
   - Small, not base, because base models miss most "Hi Kuro"s over a TV. faster-whisper would pad each 3 s window to 30 s (about 6 s on the Pi 5), so the encoder gets a 10 s context instead: about 1.5–2.5 s per check, at nice +10 on 3 threads.
4. **Capture.** A hit starts just before the speech in the window. If you keep talking ("Hi Kuro, what time is it?"), the acknowledgment is skipped and the command is captured from the 8 s ring buffer, starting at the phrase, so no words are cut off. The agent prompt already handles a greeting followed by a command.

The phrases are built from the character name in the studio, so a rename needs no new model. The engine shares one Hailo session with the offline command transcription (`hailo_stt.shared`) and with the face tracker (through hailort's multi-process service).

**hailort.service 4.23** sometimes segfaults when a client process disconnects (seen with any exit path). systemd restarts it within a second. The face tracker and the wake scorer each reconnect once and carry on; `reconnects` in the metrics counts the scorer's.

**Why not openWakeWord?** A custom model trained on synthetic Piper voices (v1) caught 0 of 24 of the owner's real "Hi Kuro"s. Adding the owner's own recordings to training (v3, v4) reached only 7 of 20 on new recordings and woke on "I grew". Natural, accented "Hi Kuro" sounds like "hi kro", which the synthetic voices never produce. Whisper, trained on 680,000 hours of real speech, hears it fine: Whisper small with the name prompt transcribed 70 of the 73 clips correctly. Also tried and dropped: the sherpa-onnx open-vocabulary keyword spotter (3.3M model; it never detected the phrase), and Moonshine base and tiny (0 of 30 over TV, since they can't be prompted with the name).

## Measured results

Recordings on the robot, all from the owner:
- **73 clean "Hi Kuro" clips:** 23 near the robot, 40 varied in distance, volume and speed, and 10 slow and deliberate.
- **30 clips with the TV on:** 15 at normal volume, 15 loud.
- **Three 10-minute negatives:** TV, loud TV, and a script read aloud that is full of near misses ("Hi Cora", "I grew", "Kyoto", "who wrote").

Each clip is streamed through the real detector (`wake_eval.py evaluate-rolling`), with the TV recording around the TV clips:

| Detector | Clean clips | TV clips | False wakes in 10 min: TV / loud TV / reading |
| --- | --- | --- | --- |
| Old path: VAD segments + Whisper small | – | – | Never waits for silence in noise, so it rarely wakes at all |
| openWakeWord v4 (owner-trained) | 7 of 20 held out | – | 0 / – / 1 |
| First version: Hailo score + CPU tiny.en check (1 s hop) | 72 of 73 | 10 of 30 | 0 / 0 / 1 |
| **Rolling (defaults)** | **70 of 73** | **18 of 30** | **0 / 0 / 1** |
| Ceiling: Whisper small, whole clip, no time limit | 70 of 73 | 22 of 30 | – |

The one false wake is the script's "Hi Cora". CPU checks per minute of audio while asleep: 0.7 with loud TV, 1.0 with TV, 4.2 during continuous conversation, at about 2.5 s each. A quiet room costs nothing.

**Limits:** over a loud TV, about 4 in 10 "Hi Kuro"s are missed. This is mostly the speech model itself: Whisper small, the best that runs on the Pi, hears 22 of 30 even with unlimited time. Speak towards the robot, or say it again. The next levers are the ReSpeaker's noise-suppression settings (needs A/B recordings) and a larger model on faster hardware.

## Evaluate

Stop the service first: recording opens the microphone, and evaluation uses the Hailo.

```bash
systemctl --user stop face-agent.service
.venv/bin/python scripts/wake_eval.py record-positive --count 20     # "Hi Kuro" at each prompt; vary place and voice
.venv/bin/python scripts/wake_eval.py record-negative --minutes 10   # TV or conversation without the phrase
.venv/bin/python scripts/wake_eval.py evaluate-rolling --negatives   # clean and 10/5/0 dB TV noise, then negatives
systemctl --user start face-agent.service
```

`evaluate-rolling` streams each clip through the real detector, with 3 s of room noise before it and 2 s after, at each `--snr` (`clean`, or dB of the first negative recording mixed over the clip). Results are saved in `test-output/wake-eval/rolling-*.json` (gitignored). Recordings from several people make the numbers much more meaningful than one voice.

**Tuning** (in `startup.env`, then restart `face-agent.service`):
- Missed wakes in a quiet room: lower `DOTS_WAKE_ROLLING_THRESHOLD`, for example to −3.5.
- False wakes: raise it, for example to −3.0.
- More misses in noise but less CPU: raise `DOTS_WAKE_VERIFY_FLOOR`, for example to −5.0.
- Fewer misses in noise but more CPU: lower it, for example to −6.0 (about 6 checks a minute with loud TV).

The worker's `metrics` lines in `~/.cache/face-agent/microphone-chat.log` show, under `wake`, the windows scored, the peak score over the last 10 s, detections, CPU checks (`verifications`, `verify_seconds`), time spent scoring, windows skipped while catching up, and Hailo reconnects.
