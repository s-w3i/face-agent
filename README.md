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

The setup creates `.venv/`. Local Whisper downloads its wake-detection model on first use, so the first voice launch can take longer.

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
- **THINKING / SPEAKING:** input capture is paused. After the reply finishes, Kuro returns to IDLE.
- **SLEEPING:** local Whisper checks for **“Hi Kuro”**, using the configured character name. Other speech is ignored. Wake-detection text never appears on the display, and sleeping audio is not sent to OpenAI.
- **WORKING / DETECTING / ERROR:** show their corresponding animation and pause microphone capture.

Kuro sleeps after **5 seconds of inactivity** while command input is available. Recording, transcription, thinking, and speech playback do not count toward that timeout. A goodbye or thank-you without a further request produces a closing reply and then sleeps.

Conversation input appears centered above the character; smaller reply subtitles appear below. Input text clears on entering THINKING or any other non-input state. Awake commands use GPT Realtime transcription; wake detection stays local.

Temporary Realtime disconnections, transcription timeouts, audio queue overflow, an unexpected voice worker exit, or a stalled playback gate automatically restart the microphone/transcription worker. The agent shows ERROR with input paused while reconnecting, then returns to IDLE only after the replacement worker is ready. Retries wait 1, 2, 4, 8, 15, then 30 seconds between attempts, continuing at 30 seconds while unavailable. Conversation history stays intact; repeat the interrupted utterance, which is never automatically replayed. Recovery stops if another ROS node changes the status, including publishing its own ERROR. Rejected credentials, quota, or configuration need correction; after fixing them, set IDLE to retry. Other unrelated errors remain visible as ERROR.

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
DOTS_INITIAL_STATUS=SLEEPING
# PORT=5173
# ROS_DOMAIN_ID=0
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

Component logs are `~/.cache/face-agent/stack-{status,camera,display,agent}.log`. Routine HTTP access logs are off in stack mode; set `DOTS_ACCESS_LOG=1` in the startup settings to enable them. Re-run the installer if you move the repository. Startup does not install missing ROS or microphone dependencies; complete setup first.

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

The hardware voice flag is polled every 50 ms. Recording opens only after that trigger; there is no idle capture or pre-roll, so stream startup can miss the very beginning of speech. Capture uses 16 kHz mono PCM. WebRTC VAD checks 20 ms frames and stops after 2 seconds of consecutive nonspeech; new speech resets that countdown. Utterances are limited to 30 seconds by default.

Standalone options include `--silence-seconds`, `--max-seconds`, `--device INDEX_OR_NAME`, and `--no-led`. Unique WAV files are saved under ignored `test-output/`, including the trailing silence. Local Whisper needs its model downloaded once; Realtime needs the configured API key and internet access. Playback pauses close the microphone stream before speech output resumes.

If USB access is denied, add the device rule and your account to `plugdev`:

```bash
sudo groupadd -f plugdev
sudo usermod -aG plugdev "$USER"
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", GROUP="plugdev", MODE="0660"' \
  | sudo tee /etc/udev/rules.d/70-respeaker.rules
sudo udevadm control --reload-rules
```

Replug the microphone and log out/in for new group membership. If PipeWire/PulseAudio owns the hardware, make the ReSpeaker the default input. Check `pactl get-default-source`; an unrelated default source can record the wrong microphone.

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
