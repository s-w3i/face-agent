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

1. **Windows, not segments.** Every 1 s, the last 3 s of audio are checked. A window is skipped unless WebRTC VAD finds at least 0.3 s of speech, so a quiet room costs nothing. Nothing waits for a pause, and the microphone is never deaf while a window is being scored.
2. **Phrase scoring on the Hailo-8L.** Whisper base (`scripts/hailo_stt.py`) is asked how likely the window is to say " Hi, Kuro" or " Hey Kuro". The score is the mean log-probability of the phrase's tokens under forced decoding. It doesn't transcribe and then search the text, which Whisper base gets wrong for many accents ("high curo", "thai kuru").
3. **CPU check for borderline windows.** A score of `DOTS_WAKE_ROLLING_THRESHOLD` (default −3.3) or higher wakes at once. A score between −4.2 and that threshold wakes only if CPU Whisper tiny.en, prompted with "The robot is named Kuro.", also hears the phrase (loosely: "High Kuro", "I, Kuro" and "Aikuro" count). In a 20-minute test this ran 13 times.
4. **Capture.** A hit starts just before the speech in the window. If you keep talking ("Hi Kuro, what time is it?"), the acknowledgment is skipped and the command is captured from the 8 s ring buffer, starting at the phrase, so no words are cut off. The agent prompt already handles a greeting followed by a command.

The phrases are built from the character name in the studio, so a rename needs no new model. The engine shares one Hailo session with the offline command transcription (`hailo_stt.shared`) and with the face tracker (through hailort's multi-process service).

**Why not openWakeWord?** A custom model trained on synthetic Piper voices (v1) caught 0 of 24 of the owner's real "Hi Kuro"s. Adding the owner's own recordings to training (v3, v4) reached only 7 of 20 on new recordings and woke on "I grew". Natural, accented "Hi Kuro" sounds like "hi kro", which the synthetic voices never produce. Whisper, trained on 680,000 hours of real speech, hears it fine: Whisper small with the name prompt transcribed 70 of the 73 clips correctly. The sherpa-onnx open-vocabulary keyword spotter (3.3M model) never detected the phrase.

## Measured results

The owner's 73 "Hi Kuro" clips (23 near the robot, 40 varied in distance, volume and speed, 10 slow and deliberate), plus two 10-minute negatives recorded on the robot: TV, and a script read aloud that is full of near misses ("Hi Cora", "I grew", "Kyoto", "who wrote"). Scored per clip and per 3 s window:

| Detector | Caught | False wakes: TV / reading |
| --- | --- | --- |
| openWakeWord v4 (owner-trained), 0.3 | 7 / 20 held out | 0 / 1 |
| CPU Whisper tiny.en text match only | 64 / 73 | 0 / 0 (but every window costs ~1.2 s of CPU) |
| Hailo phrase score only, −3.7 | 67 / 73 | 0 / 2 |
| **Rolling: Hailo −3.3, CPU check from −4.2** | **69 / 73** | **0 / 1** |

The streamed end-to-end results, with TV noise mixed over the clips, are in the next section.

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

**Tuning:**
- Missed wakes: lower `DOTS_WAKE_ROLLING_THRESHOLD`, for example to −3.5.
- False wakes: raise it, for example to −3.0.
- Restart `face-agent.service` after changing it.

The worker's `metrics` lines in `~/.cache/face-agent/microphone-chat.log` show the windows scored, the peak score over the last 10 s, detections, CPU checks and the time spent scoring.
