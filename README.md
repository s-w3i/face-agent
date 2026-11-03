# Face agent · Kuro

Kuro is a robot companion with a browser face, ReSpeaker voice input, OpenAI conversation and speech, and ROS 2 control. An Orbbec camera can track the person speaking, move the eyes, and provide visual grounding when a question needs it. The Raspberry Pi can play a pre-rendered character while keeping speech and eye tracking live.

This guide describes the `pi5` branch on Ubuntu 24.04 with ROS 2 Jazzy. The built web app and an example animation pack are included; Node.js is only needed when changing the frontend.

## 1. Install and configure

Use a graphical Linux session for the robot window. Install ROS 2 Jazzy first, with its apt repository configured. Voice conversations need a ReSpeaker USB microphone, speakers, internet access, and an OpenAI API key with API billing enabled. Camera tracking is optional.

Clone into your home directory, or use your existing checkout:

```bash
cd ~
git clone --branch pi5 https://github.com/s-w3i/face-agent.git
cd face-agent
```

Install the application dependencies and build its ROS message/service types:

```bash
sudo apt update
sudo apt install -y python3-venv build-essential cmake \
  ros-jazzy-ament-cmake ros-jazzy-rosidl-default-generators
source /opt/ros/jazzy/setup.bash
bash scripts/setup_respeaker.sh
bash scripts/setup_robot_status.sh
```

The setup creates `.venv/`. Wake detection uses Whisper base on the Hailo-8L when its model files are installed (`scripts/setup_respeaker.sh` downloads them); otherwise local Whisper on the CPU handles it and downloads its model on first use, so the first voice launch can take longer. See [docs/wake-word.md](docs/wake-word.md).

Install Chromium if needed:

```bash
sudo snap install chromium
```

Alternatively, set `DOTS_BROWSER=/absolute/path/to/chromium` when launching. The browser must run in your desktop session.

Start the local service:

```bash
./run.sh
```

Open [the character studio](http://127.0.0.1:5173/original-dots.html). Set **Character name** to **Kuro**, choose a **Speaking voice**, and use **Voice → Set up API key**. Test the speaker with **Play sample**. Appearance, name, voice, and display settings save automatically to `robot-config.json`.

The key is stored separately at `~/.config/mimo-dots/openai-api-key`, outside the repository, with private file permissions. You can instead supply `OPENAI_API_KEY` to both the service and chatbot terminals. Each computer needs its own key setup; copying character files does not copy the key.

Stop this setup server with **Ctrl+C** before starting the robot window below.

## 2. Run the robot and chatbot

Run each component in its own terminal. Begin each terminal in the repository:

```bash
cd ~/face-agent
source /opt/ros/jazzy/setup.bash
```

**Terminal 1 — robot display:**

```bash
DOTS_RENDERER=prerendered ./run.sh --robot
```

This opens a fullscreen Chromium window with automatic audio playback. The pre-rendered mode uses the bundled animation pack. Use `./run.sh --robot` to follow the renderer selected in the saved configuration. Set `DOTS_FULLSCREEN=0` for a normal window.

**Terminal 2 — camera, if tracking is wanted:**

```bash
./camera.sh
```

See [camera and eye tracking](#camera-and-eye-tracking) for driver installation and the point-cloud profile. Skip this terminal if you are running without a camera.

**Terminal 3 — voice conversation:**

```bash
.venv/bin/python scripts/chat.py --mode voice
```

The chatbot starts the eye tracker automatically. To run voice without a camera:

```bash
.venv/bin/python scripts/chat.py --mode voice --no-track
```

For typed input, use:

```bash
.venv/bin/python scripts/chat.py --mode words --no-track
```

Replies speak through the browser and appear as subtitles. Add `--no-speak` for text-only replies. `/reset` clears conversation history; `/quit` exits. `--help` lists all chatbot options. Keep the service and a display open for speech playback.

### Conversation and sleep behavior

A newly started status manager begins in **IDLE**. If a manager is already running, its current state is reused.

- **IDLE:** monitors the ReSpeaker hardware voice flag. Speech starts **LISTENING** and opens recording.
- **LISTENING:** records one utterance, ending after **2 seconds of continuous nonspeech**, then enters **THINKING**. A conversation or another ROS node can also request LISTENING directly when an answer is needed.
- **THINKING / SPEAKING:** command capture is paused, but the wake word stays live. **Barge-in:** saying “Hi Kuro” while Kuro is **thinking** cancels that turn (its answer is never spoken or shown) and moves to LISTENING. Anything said straight after the phrase (“Hi Kuro, stop. What's the weather?”) is kept as the new request. There's no spoken acknowledgment; the LISTENING animation and the LED ring are the feedback. Interrupting **speech** as well needs the speaker on the ReSpeaker jack (hardware echo cancellation, see “Speaker and echo cancellation”), so it's off until `DOTS_BARGE_IN_SPEAKING=1` (chat `--barge-in-speaking`). After an interrupted spoken reply, the agent is told that the user interrupted it. `DOTS_BARGE_IN=0` (`--no-barge-in`) disables barge-in entirely. It needs a streaming wake engine (rolling or openWakeWord) and isn't available in legacy `--no-robot-status` mode. After the reply finishes, Kuro returns to IDLE.
- **SLEEPING:** the **rolling** wake engine listens for **“Hi Kuro”** (or “Hey Kuro”) on the continuous microphone stream: every second, while there is speech, Whisper base on the Hailo-8L scores how likely the last 3 seconds are to contain the phrase, with a CPU Whisper tiny.en check for borderline windows ([docs/wake-word.md](docs/wake-word.md)). It works for any speaker and any character name without training, and it keeps listening through TV and conversation. Without the Hailo model files, a matching [openWakeWord](docs/wake-word-training.md) model is used, else local Whisper checks VAD segments for “Hi <name>” (`Wake engine: …` in the agent log says which one is active). Other speech is ignored. Wake detection never appears on the display, and sleeping audio is not sent to OpenAI. A command spoken in the same breath (“Hi Kuro, what time is it?”) is answered with any engine.
- **WORKING / DETECTING / ERROR:** show their corresponding animation and pause microphone capture. The wake word is heard but never interrupts navigation or motion.

Kuro sleeps after **5 seconds of inactivity** while command input is available. Recording, transcription, thinking, and speech playback do not count toward that timeout. A goodbye or thank-you without a further request produces a closing reply and then sleeps.

Conversation input appears centered above the character; smaller reply subtitles appear below. Input text clears on entering THINKING or any other non-input state. Awake commands use GPT Realtime transcription; wake detection stays local.

Temporary Realtime disconnections, transcription timeouts, audio queue overflow, an unexpected voice worker exit, or a stalled playback gate automatically restart the microphone/transcription worker. The agent shows ERROR with input paused while reconnecting, then returns to IDLE only after the replacement worker is ready. Retries wait 1, 2, 4, 8, 15, then 30 seconds between attempts, continuing at 30 seconds while unavailable. Conversation history stays intact; repeat the interrupted utterance, which is never automatically replayed. Recovery stops if another ROS node changes the status, including publishing its own ERROR. Rejected credentials, quota, or configuration need correction; after fixing them, set IDLE to retry. Other unrelated errors remain visible as ERROR. The corner chip and `./doctor.sh` name the cause in each case (see [Diagnosing problems](#diagnosing-problems)), and update it while retrying, for example from **No internet** to **OpenAI unreachable**.

Inside the microphone worker, every thread is supervised:
- Audio that overflows or drops during an utterance discards only that utterance.
- If the command recorder raises, it discards its utterance and restarts, up to 3 times a minute.
- If the wake-word thread raises, it restarts up to 3 times in 5 minutes. After that the worker reports a `wake_engine` failure, and the replacement worker uses Whisper wake detection for the rest of the session.
- A thread that stops responding for 2 seconds is logged with its stack, and the worker is replaced through the reconnect path above.
- A stalled audio stream is reopened three times before it counts as a USB disconnect.

Once a minute the worker writes a `metrics` line to `~/.cache/face-agent/microphone-chat.log`: frames, overflows, stream reopens, per-consumer queue depth and drops, wake detections and peak score, CPU seconds per thread, RSS and thread count. Warnings are logged there too, for example a wake detector that drops more than 1% of audio over 10 seconds. None of this is sent to the display or to OpenAI.

To start asleep, run the manager **before** the display or chatbot in another terminal:

```bash
./robot-status.sh --initial-status SLEEPING
```

That option only sets the initial state of a new manager. Use the service below to change an existing manager.

### Ask questions that need vision

Vision is enabled by default in both voice and typed chat. Start the ROS camera, then speak naturally:

- “Hi Kuro, is my outfit suitable for a casual lunch out?”
- “What am I holding?”
- “What is this?” while showing an object.
- “Can you read what this says?”

The agent decides from the request and conversation whether it needs a camera image. No separate vision mode or trigger is needed. Ordinary greetings, general knowledge, and questions about pasted code/errors do not require camera capture. Visual reasoning stays in THINKING with microphone capture paused, and the answer uses the normal voice/subtitles.

Kuro requests a fresh color frame from `/head_camera/color/image_raw/compressed`, then reasons over the actual image with the conversation model. Small objects can get one focused second look; each turn allows at most two captures. If the view is unclear or the camera is unavailable, Kuro should say so and ask for a clearer view. Hold objects near enough to see their shape/details; step back to show your full outfit. A single camera view cannot reveal parts outside the frame or reliably identify which person you mean in a crowd.

The chatbot starts a persistent ROS camera subscriber at launch, retaining only the latest compressed frame in memory. Requested snapshots reuse that subscriber, avoiding Python startup and ROS discovery on every visual question. It encodes images only on request and closes its helper when the chatbot exits. Disconnected helpers restart on the next request; cancelled requests discard their helper so a late response cannot enter another turn. Camera failures do not prevent ordinary conversation.

Only requested snapshots are sent to OpenAI, using the existing API key. The camera helper limits them to 1280 pixels on the longest side and rejects frames older than one second. Images are not saved to disk or retained in the next turn's conversation history; text observations/replies remain available for follow-up. This is on-demand image input, not continuous video upload. Visual requests add image API usage. Eye tracking itself continues to process images locally.

A typical visual answer uses two model calls: one to request the camera tool, then one to answer from the image. A focused second look adds another call. The persistent subscriber speeds up local capture; it does not remove these model calls. Vision logs include the snapshot duration to distinguish camera delay from model/network delay. The diagnostic command below uses a separate one-shot subscriber, so its startup time does not represent warm chatbot snapshots.

Use `--no-vision` to disable image access, or set `DOTS_VISION=0` in the startup settings. `--no-track` disables eye tracking independently and leaves vision available. For another ROS color topic:

```bash
.venv/bin/python scripts/chat.py --mode voice \
  --camera-topic /head_camera/color/image_raw/compressed
```

`DOTS_CAMERA_TOPIC` supplies the same setting, including through `~/.config/face-agent/startup.env`. Vision needs the system `python3-pil` package, installed by `scripts/setup_respeaker.sh`. For a local capture check without sending an image to OpenAI:

```bash
source /opt/ros/jazzy/setup.bash
/usr/bin/python3 scripts/camera_snapshot.py --check
```

This prints timestamp, size, and freshness only. Use a vision-capable model if overriding `--model`; the default `gpt-6-luna` accepts image input. See the [OpenAI image input guide](https://developers.openai.com/api/docs/guides/images-vision) for API behavior.

### Stop and restart

Use `/quit` or **Ctrl+C** in the chatbot terminal to stop voice input and its owned tracker. Close the robot window or press **Ctrl+C** in its launcher terminal. Stop the camera with **Ctrl+C** in its terminal.

The shared status manager survives display/chat exits. If started explicitly with `./robot-status.sh`, stop it in that terminal with **Ctrl+C**. Otherwise, restarting the display/chat reuses it; set IDLE through the service to resume an existing sleeping or busy session.

## Start the full stack automatically at boot

After completing the one-time setup above, install desktop startup **as your normal user, without sudo**:

```bash
cd ~/face-agent
python3 scripts/install_startup.py
scripts/desktop_start.sh
```

This installs the `face-agent.service` user service and a desktop autostart entry. It launches the ROS status manager, camera, fullscreen face display, and voice chatbot with eye tracking. The chatbot waits until the display and ROS status are ready. Failed components restart automatically; closing the face window while the service is running opens it again.

A fresh manager starts in SLEEPING, waiting for “Hi Kuro” or a ROS status command. An existing status manager keeps its state. The startup camera profile is `camera-rgbd.sh`, providing color, depth, and an XYZ point cloud. To run the same stack manually in the foreground, use `./start-stack.sh`; **Ctrl+C** stops its components. Stop any separately launched display/camera/chat first.

The display requires a logged-in desktop. Enable **Automatic Login** for your account in Ubuntu **Settings → System → Users** to launch after power-on without signing in. On this Pi's GDM desktop, the alternative helper is:

```bash
scripts/enable_autologin.sh
```

Run the helper as your desktop user, without sudo. It uses Ubuntu AccountsService and may show an administrator authentication dialog. The change applies on the next boot without restarting the current session. Automatic login opens the desktop without a password after power-on.

Startup settings are in `~/.config/face-agent/startup.env`:

```bash
DOTS_RENDERER=prerendered
DOTS_FULLSCREEN=1
DOTS_CAMERA_PROFILE=rgbd  # Use color for the lower-bandwidth camera.sh profile.
# DOTS_CAMERA_USB_RESET=1  # 0 disables the automatic USB reset when the Orbbec gets stuck after a USB drop
DOTS_INITIAL_STATUS=SLEEPING
# PORT=5173
# ROS_DOMAIN_ID=0
# DOTS_WAKE_ENGINE=auto            # auto | rolling | oww | whisper
# DOTS_WAKE_ROLLING_THRESHOLD=-3.3 # rolling: lower wakes more easily (and falsely)
# DOTS_WAKE_VERIFY_FLOOR=-5.5      # rolling: higher (e.g. -5.0) saves CPU in noisy rooms, misses more
# DOTS_WAKE_MODEL_PATH=~/.local/share/face-agent/wake/hi_kuro.onnx
# DOTS_BARGE_IN=1                  # 0 disables wake-word barge-in
# DOTS_BARGE_IN_SPEAKING=0         # 1 only with the speaker on the ReSpeaker jack
# DOTS_TRACKER_BACKEND=auto        # auto | hailo | opencv
# DOTS_OFFLINE_STT=hailo           # hailo | cpu | off
```

Edit the file, then restart the service. The installer preserves existing startup settings. An API key supplied only in the installer's environment is saved to the application's private key file so it is available after reboot; it is never placed in the service or desktop entry.

```bash
systemctl --user restart face-agent.service
systemctl --user status face-agent.service
journalctl --user -u face-agent.service -f

# Stop the stack for manual testing:
systemctl --user stop face-agent.service
# Disable startup and stop the stack; keep settings and API key:
python3 scripts/install_startup.py --remove
```

Component logs are `~/.cache/face-agent/stack-{status,camera,display,agent}.log`. Every `*.log` in that directory is rotated by the stack supervisor (checked every minute and at startup): a file over `DOTS_LOG_MAX_MB` (default 10) keeps its newest `DOTS_LOG_MAX_MB` as `<name>.log.1`, older backups shift up to `DOTS_LOG_KEEP` (default 3), and the live file is truncated in place. Set these in the startup settings. Routine HTTP access logs are off in stack mode; set `DOTS_ACCESS_LOG=1` in the startup settings to enable them. Re-run the installer if you move the repository. Startup does not install missing ROS or microphone dependencies; complete setup first.

## Diagnosing problems

When something goes wrong, a small chip in the top-right corner of the face names it: **No internet**, **Mic disconnected**, **API key rejected**, **OpenAI unreachable** and so on. Blocking problems (Kuro shows ERROR) appear in a muted red. Problems Kuro works around (offline transcription, tracking on the CPU, camera missing) appear as a fainter grey chip while the face carries on normally. Tap the chip for the message, the fix and how long it has lasted. The chip disappears once the problem clears. If Kuro is in ERROR without a reported cause, the chip says **Error · run ./doctor.sh**; if the face loses the local service for more than 5 seconds, it says **Service offline**.

On the robot, run:

```bash
./doctor.sh            # active faults, live checks, the last 24 h of faults, recent log warnings
./doctor.sh --detail   # also tracebacks and component log tails
./doctor.sh -f         # follow new fault events live
./doctor.sh --since 2h --json
```

The live checks cover internet vs. OpenAI reachability (TCP only; no API request or key is sent), the ReSpeaker USB device and its permissions, the default PipeWire microphone, the Orbbec camera, Hailo-8L and `hailort.service`, `face-agent.service`, the ROS status manager, the API key file, the navigation robot from `DOTS_NAV_MCP_URL`, and disk space. The exit status is 1 while any check fails or a blocking fault is active.

Faults are recorded in `~/.cache/face-agent/faults.log`, one JSON object per line: `raise`, `update` (new message) and `clear` events with the code, component, message, fix and, for errors, the traceback. Repeats during a retry loop are counted rather than logged again, and the log is rotated with the other component logs. The display service keeps the live fault table; every component reports to it and writes the file directly only while the display service is down. The display itself never receives tracebacks. The service checks the network every 30 seconds (`DOTS_NET_CHECK_SECONDS`, `0` disables), so an outage is named even while Kuro sleeps. Another ROS node's ERROR shows as **Error set externally** with that node's `source`.

| Code | Severity | Chip | Fix |
| --- | --- | --- | --- |
| `openai_auth` | error | API key rejected | Update the key in the studio (Voice → Set up API key), then set IDLE or restart face-agent.service. |
| `openai_quota` | error | OpenAI quota used up | Add credit or raise the limit in OpenAI billing, then set IDLE or restart face-agent.service. |
| `internet_down` | error | No internet | Check the Wi-Fi/Ethernet link and the router. Kuro retries by itself. |
| `mic_disconnected` | error | Mic disconnected | Replug the ReSpeaker USB cable (try another port or hub). Kuro reconnects by itself. |
| `mic_permission` | error | Mic access denied | USB permission denied: add the udev rule and the plugdev group (README → ReSpeaker), then replug. |
| `openai_unreachable` | error | OpenAI unreachable | The internet works but api.openai.com does not answer. Check status.openai.com, DNS or a firewall. Kuro retries by itself. |
| `ros_status_lost` | error | ROS status lost | The ROS status bridge stopped. Check stack-status.log and ROS_DOMAIN_ID, then restart face-agent.service. |
| `mic_worker_failed` | error | Microphone failed | The microphone worker failed. See microphone-chat.log; Kuro restarts it by itself. |
| `realtime_unavailable` | error | Transcription down | OpenAI Realtime transcription failed although the network works. Kuro reconnects by itself. |
| `openai_rate_limit` | error | OpenAI rate limited | Too many requests. Wait a minute; Kuro retries by itself. |
| `openai_timeout` | error | OpenAI timed out | A slow network or a slow OpenAI response. If it keeps happening, check the connection. |
| `openai_error` | error | OpenAI error | OpenAI returned an error. Run ./doctor.sh --detail for the message. |
| `speech_failed` | error | Speech failed | The spoken reply could not be generated or played. Run ./doctor.sh --detail. |
| `external_error` | error | Error set externally | Another ROS node set ERROR. Fix that node, then set IDLE. |
| `agent_error` | error | Agent error | An unexpected error. Run ./doctor.sh --detail for the traceback. |
| `component_restarting` | degraded | Restarting | A stack component exited and is restarting. Run ./doctor.sh --detail for its last log lines. |
| `stt_offline` | degraded | Offline speech | OpenAI Realtime is unreachable; commands are transcribed locally until it reconnects. |
| `camera_unavailable` | degraded | Camera unavailable | Vision questions cannot see. Check the Orbbec USB cable and stack-camera.log. |
| `tracking_unavailable` | degraded | Eye tracking off | The eye tracker stopped; chat continues. See tracking-chat.log, then restart face-agent.service. |
| `tracker_cpu_fallback` | degraded | Tracking on CPU | The Hailo-8L is unavailable (check hailort.service). Restart face-agent.service to retry it. |
| `wake_whisper_fallback` | degraded | Wake word: Whisper | Neither the rolling Hailo engine nor openWakeWord is in use, so Whisper listens for the wake phrase (more CPU, slower). |
| `wake_model_missing` | info | No wake model | Whisper listens for the wake phrase: no Hailo Whisper files and no openWakeWord model (docs/wake-word.md). |

## ROS 2 robot status

The display and agent independently subscribe to `/robot/status`. Other nodes change the state through `/robot/set_status`, so status control works even when the chatbot is closed. The first subscriber automatically starts one shared manager if none is available.

Source ROS and the custom interfaces in a new terminal:

```bash
cd ~/face-agent
source /opt/ros/jazzy/setup.bash
source ros2/install/local_setup.bash

# Read the latest state, including when subscribing after a change:
ros2 topic echo /robot/status robot_status_interfaces/msg/RobotStatus \
  --qos-durability transient_local
```

Call the service from a separate terminal with the same setup:

```bash
# Wake/resume voice monitoring without a spoken wake phrase:
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: IDLE}"

# Ask for input directly:
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: LISTENING}"

# Other examples:
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: WORKING}"
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: DETECTING}"
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: ERROR}"
ros2 service call /robot/set_status robot_status_interfaces/srv/SetRobotStatus "{status: SLEEPING}"
```

| Status | Display animation | Voice input |
| --- | --- | --- |
| `IDLE` | Idle / Ready | Waits for speech without recording |
| `LISTENING` | Needs input | Records the current utterance |
| `THINKING` | Thinking | Paused |
| `SPEAKING` | Speaking, driven by audio | Paused |
| `WORKING` | Working | Paused |
| `DETECTING` | Searching | Paused |
| `ERROR` | Error | Paused |
| `SLEEPING` | Sleeping | Local wake detection only |

The topic type is `robot_status_interfaces/msg/RobotStatus`, containing `status`, `revision`, and `source`. It uses reliable, transient-local QoS with depth one. The service type is `robot_status_interfaces/srv/SetRobotStatus`:

- Request: `status`, optional `source`, and `expected_revision` (`0` means unconditional).
- Response: `success`, `message`, and `current` status.

Call the service instead of publishing competing status messages. The agent uses revision checks so a cancelled reply cannot overwrite a newer external command. Nodes must share `ROS_DOMAIN_ID` and compatible discovery settings. Build and source the interface package on other computers that use these types.

Legacy deployments can opt out with chatbot `--no-robot-status` and display `DOTS_ROBOT_STATUS=0`. The old `/kuro/wake` service belongs to that compatibility mode; the shared status service above is the default interface.

## Camera and eye tracking

The launchers target an Orbbec Gemini 330-series camera. Install the driver and compressed-image support once, with the ROS apt repository configured:

```bash
sudo apt install -y ros-jazzy-orbbec-camera ros-jazzy-image-transport-plugins
```

Choose **one** camera launcher:

| Command | Color | Depth / point cloud | Purpose |
| --- | --- | --- | --- |
| `./camera.sh` | 640 × 480 MJPG, 15 FPS | Off | Lower-bandwidth face tracking |
| `./camera-rgbd.sh` | 1280 × 720 MJPG, 15 FPS | 640 × 480 depth at 30 FPS; XYZ cloud | Image plus point cloud |

Both profiles support the current USB 2 connection. The RGB-D profile disables frame sync and colored point clouds and uses laser energy level 5, which was stable in testing on this Pi. These are requested rates; delivered FPS depends on the cable, hub, USB power, scene, and processing load. Only one process can own the camera.

**AI HAT (Hailo-8L):** face detection and identity features run on the Hailo-8L when it's available (`DOTS_TRACKER_BACKEND=auto`, the default), and on the CPU with OpenCV otherwise (`DOTS_TRACKER_BACKEND=opencv`). Sharing the chip needs `hailort.service` to be active. If the accelerator or its service fails while tracking, the tracker reconnects once and then continues on the CPU for the rest of the session. See [docs/human-tracking.md](docs/human-tracking.md#hailo-8l-face-backend-ai-hat) for the identity threshold and measurements.

Launch arguments can override the profile, for example:

```bash
./camera.sh color_width:=640 color_height:=480 color_fps:=15
```

Useful ROS topics:

```bash
ros2 topic list
ros2 topic hz /head_camera/color/image_raw/compressed
ros2 topic hz /head_camera/depth/points
```

The cloud topic is available with `camera-rgbd.sh`. Tracking uses `/head_camera/color/image_raw/compressed` and camera intrinsics from `/head_camera/color/camera_info`.

Prepare/check the tracker before starting chat:

```bash
source /opt/ros/jazzy/setup.bash
./track.sh --check
```

This creates a separate tracking environment and downloads its local face models on first use. For standalone tracking diagnostics with the display and camera running, use `./track.sh --stats`. The chatbot normally manages the tracker for you.

Voice mode combines face detections with ReSpeaker direction-of-arrival hints to select a speaker, then keeps a visual identity lock. Typed mode acquires a person near the camera center. Tracking is active while awake; missing targets ease the eyes back to the original animation. Tracking images are processed locally; the separate on-demand vision tool sends snapshots to OpenAI only for visual requests.

The pre-rendered body **does not need full 3D rendering for live gaze**. The included pack has eye positions; the player moves small eye patches over the baked body. Customized looks with obscured eyes may need verification in **Test gaze**. For another microphone mounting, use `--mic-forward-deg ANGLE` and `--mic-clockwise` as appropriate. This robot uses native 0° at camera center, increasing toward camera left.

## ReSpeaker robot microphone

Test the microphone separately with the chatbot stopped:

```bash
# USB/VAD/direction diagnostics; does not record:
.venv/bin/python scripts/respeaker.py doctor --diagnostics

# Record and transcribe one utterance through Realtime:
.venv/bin/python scripts/respeaker.py --once --diagnostics

# Record and transcribe locally instead:
.venv/bin/python scripts/respeaker.py --stt-model small --once --diagnostics

# Record one utterance without transcription:
.venv/bin/python scripts/respeaker.py --no-stt --once --diagnostics
```

The worker keeps **one continuous 16 kHz capture stream** open and fans it out in memory to its consumers. The last 8 seconds stay in an in-memory ring buffer that is never written to disk or sent anywhere unless a recording is triggered. The hardware voice flag is polled every 50 ms. When it fires, the recording starts with 0.3 seconds of pre-roll from that ring (`--preroll-seconds`), so the first syllable isn't clipped. Overflowing or dropped audio during an utterance discards it rather than transcribing incomplete speech. If the stream stops delivering audio for 0.5 s, it is reopened up to three times before the worker reports a reconnectable microphone error.

Voice chat runs **one long-lived microphone worker**. Falling asleep or waking switches its mode with a control message: waking opens the Realtime socket, and sleeping closes it. The worker isn't restarted, and a Whisper wake model, when used, stays loaded (about 400 MB RSS). Recording runs in its own thread, and every recording or transcription holds a cancel token, so a pause, mode switch or newer trigger drops only older work. If Realtime can't connect on waking, the worker stays asleep with wake detection running and the usual recovery retries. Capture uses 16 kHz mono PCM. WebRTC VAD uses mode 2, checks 20 ms frames and stops after 0.8 seconds of consecutive nonspeech; new speech resets that countdown. Utterances are limited to 10 seconds. Voice chat uses `small` locally with a 0.4-second silence timeout for wake detection. Awake command recording uses 0.8 seconds of silence. Both modes retain the 10-second recording cap and VAD mode 2. Standalone capture can override them with `--silence-seconds`, `--max-seconds`, and `--vad-mode` (0–3).

Standalone options include `--silence-seconds`, `--max-seconds`, `--device INDEX_OR_NAME`, and `--no-led`. Unique WAV files are saved under ignored `test-output/`, including the trailing silence. Local Whisper needs its model downloaded once; Realtime needs the configured API key and internet access. Playback pauses end the active recording (the recorder releases the stream) before speech output resumes; the shared capture stream itself stays open.

### Speaker and echo cancellation

The XVF3000 removes the robot's own voice from the processed microphone channel (hardware AEC) only when playback goes through the **ReSpeaker's 3.5 mm output**; that playback is its echo reference. Connect a powered speaker or small amplifier to that jack (line/headphone level), then make it the default speaker:

```bash
scripts/audio_output.sh status       # current and persisted default sink
scripts/audio_output.sh respeaker    # ReSpeaker jack (needed for barge-in while speaking)
scripts/audio_output.sh jieli        # back to the separate USB speaker
```

Chromium plays through the PipeWire default sink. `pactl set-default-sink` is persisted by WirePlumber in `~/.local/state/wireplumber/default-nodes`, so the choice survives a reboot. Verify the echo cancellation with the stack stopped:

```bash
systemctl --user stop face-agent.service
.venv/bin/python scripts/respeaker.py doctor --aec --sink jieli       # reference run
.venv/bin/python scripts/respeaker.py doctor --aec --sink respeaker   # after connecting the jack
.venv/bin/python scripts/respeaker.py doctor --aec --compare
systemctl --user start face-agent.service
```

Each run records 3 s of quiet and 10 s of looped speech playback on all six channels (firmware: 0 processed, 1–4 raw microphones, 5 playback reference) and saves the WAV and JSON under `test-output/aec/`. `echo_suppression_db` is how far channel 0 sits below the raw microphones during playback; `aec_reference_present` shows whether the XVF3000 actually receives the playback. With the jack, expect channel 0 to drop by at least 15 dB compared with the Jieli run. Talking over the playback should stay clearly audible in channel 0.

If USB access is denied, add the device rule and your account to `plugdev`:

```bash
sudo groupadd -f plugdev
sudo usermod -aG plugdev "$USER"
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", GROUP="plugdev", MODE="0660"' \
  | sudo tee /etc/udev/rules.d/70-respeaker.rules
sudo udevadm control --reload-rules
```

Replug the microphone and log out/in for new group membership. If PipeWire/PulseAudio owns the hardware, make the ReSpeaker the default input. Check `pactl get-default-source`; an unrelated default source can record the wrong microphone.

### Offline speech-to-text

Awake commands normally use OpenAI Realtime. If Realtime is unreachable with a temporary error (no network, timeouts), commands are transcribed locally instead, and Realtime is retried in the background; the next command after it reconnects uses Realtime again. The agent log shows `transcribing commands locally with hailo` and `Realtime transcription reconnected.`.

`DOTS_OFFLINE_STT` in `~/.config/face-agent/startup.env` (chat worker `--offline-stt`) chooses the engine:

- `hailo` (default): Whisper base on the Hailo-8L, about 1.3 s for a short command with almost no CPU. `scripts/setup_respeaker.sh` installs the models (about 310 MB) in `~/.local/share/face-agent/whisper-hailo8l/`; `DOTS_HAILO_WHISPER_DIR` overrides the folder. It needs `hailort.service`.
- `cpu`: faster-whisper `base.en` with 2 threads. This is also what `hailo` falls back to if the models or the accelerator aren't available.
- `off`: no fallback; a Realtime failure shows ERROR and recovers as before.

The engine loads on first need, so the first offline command takes about 2 seconds longer, and audio stays on the robot. Partial text isn't shown while offline; the whole transcript arrives at once. Local accuracy is lower than Realtime's. Rejected credentials or quota are not treated as offline: they still show ERROR. Replies still need the OpenAI agent, so while fully offline the reply fails with the usual network error; this fallback removes speech-to-text as the point of failure.

## Customize or pre-render on another computer

Open [the original character studio](http://127.0.0.1:5173/original-dots.html) to change appearance, name, voice, background, motion, and renderer settings. `/robot.html` is the clean robot display; `/` is the separate procedural character playground.

For a new animation pack:

1. Run the project on the stronger computer and customize the character in the studio.
2. Under **Raspberry Pi playback**, click **Bake saved character** and keep the tab visible until complete.
3. Use **Test pre-rendered display** and **Test gaze** to check the result. For older packs, **Prepare eye tracking** adds eye positions without re-rendering the body.
4. Copy `public/prerendered/` and `robot-config.json` to the same locations in the Pi checkout.
5. Restart with `DOTS_RENDERER=prerendered ./run.sh --robot`.

No frontend build is needed after copying the pack. Re-bake after changing appearance, motion strength, reduced motion, or background. Name, voice, subtitles, and gaze remain live settings. Keep the API key device-local, and install environments/build ROS interfaces separately on each machine.

See [pre-rendered playback details](docs/prerendered.md) for pack format and gaze limitations, [tracking details](docs/human-tracking.md) for model/camera internals, and [motion reference](docs/dots-motion-reference.md) for renderer observations. The runtime commands and status behavior in this README describe the current `pi5` branch.

## Real robot navigation over LAN MCP

The face agent can connect to the robot navigation server from the Pi without
installing ROS navigation packages or copying the map/waypoint XML to the Pi.
The MCP link uses the private LAN; existing cloud chat/voice services retain their
normal connectivity requirements.

Actual robot navigation is the saved default on this Pi. These settings in
`~/.config/face-agent/startup.env` persist across stack relaunches and logins:

```bash
DOTS_NAV_MCP_URL=http://192.168.10.10:8000/mcp
DOTS_NAV_MCP_MOCK=0
```

After changing these settings, restart the desktop service:

```bash
systemctl --user restart face-agent.service
```

For a manual chat session use `--navigation-mcp-url http://192.168.10.10:8000/mcp`
(or its alias `--mcp-url`). `DOTS_NAV_MCP_URL` supplies the default. Explicit
`--turtlesim` selects the simulator instead of the configured real robot.

Check the same connection without a cloud request, camera, microphone, or motion:

```bash
cd ~/face-agent
.venv/bin/python scripts/chat.py --navigation-mcp-url http://192.168.10.10:8000/mcp --check-navigation-mcp
```

Navigation tools connect on demand; startup does not contact the remote server. Requests such as “Go to
the kitchen” use `navigate_to_region` and the names loaded by `wp_map_tools` on
the robot. The current names are `kitchen`, `bedroom`, and `living room`.
Navigation tools wait for Nav2 to finish, and the agent only reports arrival
when the navigation result confirms success. Simulator queue/status/announcement
hooks are disabled for this connection. Movement requests are not automatically
retried after connection failures.

Keep the robot MCP server, Nav2, localization, and waypoint bridge running at
`192.168.10.10:8000`. The robot pose must be localized before requesting movement.
Read startup diagnostics in `~/.cache/face-agent/stack-agent.log`.

After the local voice worker is ready, the voice agent
says “Kuro is ready. Say Hi Kuro to wake me up.” once per start. The microphone
is paused through playback, then local wake listening resumes. Wait for this
announcement before saying “Hi Kuro”; the face display can appear earlier.
Realtime command transcription connects after waking. A fixed “Yes? I am here, what can I help you?” WAV is generated once
for the selected voice before the startup-ready announcement and stored under
`~/.cache/face-agent/wake-ack/`. Waking replays this local clip while Realtime
connects, with the microphone held closed through playback and connection. A
bare “Hi Kuro” skips the generated greeting; a command after the wake phrase
still reaches the agent. With a streaming wake engine, the worker checks whether you keep talking right after the phrase.
If you do (“Hi Kuro, what time is it?”), the spoken acknowledgment is skipped and the command is captured
from the in-memory ring buffer (from the phrase with the rolling engine, just after it with openWakeWord), so no robot audio plays over it. Cached acknowledgment playback needs no network.
Changing the speech voice requires restarting the agent to prepare its clip.
`DOTS_WAKE_ENGINE` (`auto`, `rolling`, `oww` or `whisper`) and `DOTS_WAKE_MODEL_PATH` choose the wake engine and model;
see [docs/wake-word.md](docs/wake-word.md).
`DOTS_WAKE_MODEL` or `--wake-model` overrides the default `small` Whisper wake model. `--no-speak` also disables
the startup announcement. An external ROS status change takes precedence.

Startup readiness audio is also persisted under
`~/.cache/face-agent/startup-ready/`, keyed by the selected voice and robot name.
Both sleeping and already-awake startup phrases are prepared once; subsequent
starts play the local WAV without a cloud speech connection. Changing the voice
or robot name requires one online preparation before offline playback is available.
The microphone remains paused through the announcement, then wake listening resumes.

## Voice tests with the robot stationary

Use the standalone mock endpoint `http://192.168.10.10:8001/mcp` while the real
robot setup is incomplete. This server reads the same waypoint XML but imports
no ROS code and has no publishers, Nav2 actions, or physical robot connection.
Its three tools simulate pose, named navigation, and cancellation.

To temporarily select mock mode, replace the actual-mode settings in
`~/.config/face-agent/startup.env` with these values and restart the service:

```bash
DOTS_NAV_MCP_URL=http://192.168.10.10:8001/mcp
DOTS_NAV_MCP_MOCK=1
```

Mock mode probes the endpoint before attaching its tools and refuses a real
server. It never falls back to the real MCP URL. Stop the real MCP endpoint
when running this stationary test. Keep voice mode running normally, say “Hi Kuro”, then test:

- “Go to the kitchen.”
- “Go to the bedroom.”
- “Go to the living room.”

The voice reply should clearly describe a completed mock test with the robot
staying still. All reported navigation poses and arrivals are simulated.

Read-only check on the Pi:

```bash
.venv/bin/python scripts/chat.py --navigation-mcp-url http://192.168.10.10:8001/mcp --navigation-mcp-mock --check-navigation-mcp
```

Start the mock server on the robot computer:

```bash
/home/robot10/MCP-for-Robocup-Malaysia-2026/.venv/bin/python -m robot_mcp.mock_region_nav_server --host 192.168.10.10 --port 8001 --waypoints /home/robot10/maps/waypoints.xml
```

When the physical setup is complete, explicitly restore port 8000, set
`DOTS_NAV_MCP_MOCK=0`, restart the real MCP server, and restart the face-agent
service to enable physical navigation again.

## Optional turtlesim control

Keep the face display running. Install the optional dependencies:

```bash
source /opt/ros/jazzy/setup.bash
sudo apt install -y ros-jazzy-turtlesim
.venv/bin/python -m pip install -r requirements-turtlesim.txt
```

Start the simulator in one terminal:

```bash
source /opt/ros/jazzy/setup.bash
ros2 run turtlesim turtlesim_node
```

In another terminal, start the chatbot with its local MCP controller:

```bash
cd ~/face-agent
source /opt/ros/jazzy/setup.bash
.venv/bin/python scripts/chat.py --mode voice --turtlesim --no-track
```

Use `--mode words` for typed commands. Try “Go to the kitchen”, “Turn left ninety degrees”, “Where is the turtle?”, or “Stop the turtle”. Destinations are configured in `turtlesim-waypoints.json`; restart chat after editing them.

Movement starts after the departure announcement finishes successfully. Failed or interrupted speech cancels queued movement; explicit `--no-speak` releases it after the printed reply. This is a straight-line simulator controller without obstacle avoidance. All ROS processes must use the same domain, and Python must match the ROS installation.

## Troubleshooting and configuration

| Symptom | Check |
| --- | --- |
| `Install Chromium to use --robot` | Install Chromium or set `DOTS_BROWSER` to its executable; launch from a graphical session. |
| No voice or subtitles | Keep `/robot.html` open; test **Play sample**, API key, speaker output, and internet. A regular browser tab may need an initial click to enable audio. |
| No microphone response | Check `/robot/status`: IDLE waits for speech, SLEEPING waits for “Hi Kuro”, busy/error states pause input. Run the standalone microphone diagnostics. |
| `Microphone failed` | Read `~/.cache/face-agent/microphone-chat.log`; check USB permissions, device connection, and default audio source. Stop other microphone tests before chat. |
| ROS interface import failure | Source Jazzy, run `bash scripts/setup_robot_status.sh`, and source `ros2/install/local_setup.bash` again. |
| Camera disconnects or no eyes moving | Run only one camera launcher; try `camera.sh`, then `./track.sh --check`. Confirm the compressed image and camera-info topics exist. |
| Kuro cannot see an object or outfit | Run `camera_snapshot.py --check` with ROS sourced. Confirm the color topic and `python3-pil`; move the object closer or step back to show the outfit. Vision messages appear in `stack-agent.log`. |
| Display is slow | Use pre-rendered playback; bake customized looks on a stronger computer. |
| Port 5173 is already in use | Stop the old launcher or choose another port, and point chat/tracking at that service. |

For another port:

```bash
# Display terminal:
PORT=5174 DOTS_RENDERER=prerendered ./run.sh --robot
# Chat terminal:
.venv/bin/python scripts/chat.py --mode voice --no-track --url http://127.0.0.1:5174
```

Useful settings: `DOTS_CONFIG` selects the character configuration file; `DOTS_URL` supplies the chatbot service URL; `--model` or `OPENAI_CHAT_MODEL` selects the conversation model; `--wake-model` selects local Whisper; `--language en` or `--language auto` controls recognition language. `DOTS_TIMEZONE` defaults to `Asia/Kuala_Lumpur`. Set `DOTS_CITY` for a fixed weather location; otherwise location lookup may use the connection's public IP through ipapi.co.

Logs are under `~/.cache/face-agent/`: `microphone-chat.log`, `tracking-chat.log`, `robot-status.log`, and `robot-status-bridge.log`. History stays in chatbot process memory and is sent to OpenAI during conversation; SDK trace uploads are disabled.

## Update and develop

To update a clean checkout, stop the application components, then:

```bash
cd ~/face-agent
git switch pi5
git pull --ff-only origin pi5
.venv/bin/python -m pip install -r requirements.txt -r requirements-respeaker.txt
bash scripts/setup_robot_status.sh
```

Restart using the commands above. Local configuration, API keys, virtual environments, and recordings are not committed.

Frontend development needs Node.js **22.12+** and pnpm:

```bash
pnpm install --frozen-lockfile
pnpm build
pnpm test
```

Commit the regenerated `dist/` after frontend changes so `run.sh` serves the updated app. For hot reload, run `PORT=5174 ./run.sh` in one terminal and `pnpm dev` in another; Vite serves port 5173 and proxies API/pack requests to port 5174. Point chat at the Python service with `--url http://127.0.0.1:5174`.

Focused Python checks:

```bash
.venv/bin/python -m unittest discover -s test -p test_chat.py
.venv/bin/python -m unittest discover -s test -p test_voice_input.py
.venv/bin/python -m unittest discover -s test -p test_voice_recovery.py
.venv/bin/python -m unittest discover -s test -p 'test_respeaker*.py'
.venv/bin/python -m unittest discover -s test -p test_robot_service.py
.venv/bin/python -m unittest discover -s test -p test_camera_vision.py
# Requires built ROS interfaces and ROS 2 Jazzy:
.venv/bin/python -m unittest discover -s test -p test_global_status.py
# ROS/Pillow snapshot tests use the system interpreter, in an isolated ROS domain:
source /opt/ros/jazzy/setup.bash
/usr/bin/python3 -m unittest discover -s test -p test_camera_snapshot.py
```

Bundled runtime notices are in [public/local-dots/THIRD_PARTY_NOTICES.txt](public/local-dots/THIRD_PARTY_NOTICES.txt). Font licenses are in [public/fonts/](public/fonts/).


### Boot readiness

The desktop autostart helper imports the graphical-session environment before
starting `face-agent.service`. The service waits for DISPLAY or WAYLAND_DISPLAY
instead of failing before graphical login. The face, camera, microphone and
cached ready announcement start independently of the remote navigation server.

Navigation tool definitions are stored locally in
`scripts/navigation_tool_manifest.json`. Tool discovery and ordinary conversation
make no network request to the navigation MCP. Each actual navigation-tool call
opens a connection, validates the remote tools (and mock identity when enabled),
calls once, then closes it. An unavailable server returns a tool error while chat
continues; a later explicit request tries again. A lost in-flight request is
reported as unconfirmed and movement is never automatically replayed.
`--check-navigation-mcp` remains an explicit read-only connectivity check. Update
the local tool manifest if the remote navigation tool contract changes.

On the robot computer, `region-nav-mcp.service` is enabled and the robot10 user
manager has lingering enabled so its endpoint can start before desktop login.
The face agent does not depend on this service being available.
