# Mimo character studio

A lightweight, real-time 3D companion playground, visually reconstructed around dots' sculptural forms, saturated colors, flocked surfaces, and minimal faces. The procedural studio generates its geometry and animation locally; the optional original mode loads resources imported from the installed app. Customization reference: https://learn.chatgpt.com/docs/dots

The **Reference looks** buttons recreate four visual combinations: blue six-lobed blob with a black beret, lime round body with raised eyes, yellow teardrop with relaxed eyes and glasses, and pink heart with sunglasses. The model has no limbs, mouth, or cheeks. Each look remains fully customizable.

## Original dots renderer

Open `/original-dots.html`, or use **Original dots renderer** in the studio header. This mode uses the actual geometry, materials, fur, character presets, and native activities from the installed desktop app. It includes 108 original presets, native shape/color/eye/glasses/accessory choices, body depth, Compact/Balanced quality, reduced motion, saved appearance, and export/import of the native look.

The library exposes every animation action in the copied 0.11.0 binding: all nine activity states, Wave, the current outfit's Signature, native gaze in four directions, native press/drag/release interactions, and four completion outcomes (ArtifactReady, PaymentPaid, InputReceived, ThinkingResolved). Completion demos start the associated activity, send its Stop outcome after 2.5 seconds, then return to Idle / Ready. These visual demos make no payment or network request. All eight original hero signatures have their own preview button. Those previews temporarily show the corresponding outfit, restore your appearance after eight seconds or any manual interruption, and leave the saved look intact. Customization is temporarily disabled during an outfit preview. Custom outfits can use Wave; their current-outfit Signature shortcut is disabled.

**Random idle movements** is on by default. During **Idle / Ready**, a movement plays about every 9–15 seconds: a glance, native press/drag interaction, Wave, or the current hero's Signature. It never changes your outfit and avoids consecutive repeats. Use the interval slider or **Try random now** to test it. Automatic actions wait during all other statuses, cursor tracking, and **Play all**, and pause with Reduced motion or a hidden page. Finite actions return to idle; manual selections cancel the pending action. **Play all** previews 30 library entries, including every original signature and both added voice states, then returns to idle. It omits the redundant current-outfit Signature shortcut.

Sleeping calls the native **Paused** activity (closed eyes and sleep marks). Ready includes cursor-following gaze. The inspected binding has no voice-specific commands. The studio now adds **Listening** (attentive stretch, forward lean, acknowledgment nods) and **Speaking** (syllable stretch, taper and contour ripples) directly to the original character's vertex shader. Body, fitted face parts, fur, and shadow geometry share the deformation; the copied runtime files are unchanged. This is a local renderer integration, not a published OpenAI SDK.

Select Listening or Speaking to enable **Body motion time**, **Pause body motion**, and **Motion strength**. Scrubbing freezes the added deformation at an exact time; the original Ready idle/blinks continue underneath. Speaking defaults to a five-second demo phrase. **Speech drive → Fixed speech level** allows comparison from silence (0%, neutral body) to full level (100%). Reduced motion makes the added pose static and gentler. OpenAI speech playback now supplies the actual audio amplitude to `voiceMotionPose()` in `src/native-voice.js`. The demo/fixed controls remain available for silent animation tests. Terminal chatbot input is available through scripts/chat.py; microphone/STT access is not connected yet.

The extension is verified against the copied 0.11.0 renderer. It patches only this canvas's shader compilation and reads the body's bounds from its existing uniform upload. It adds no new render pass, graphics library, or per-frame mesh rebuild. Because this is an internal renderer layout, an app update requires rechecking the shader and uniform layout before importing its resources.

## Terminal chatbot (OpenAI Agents SDK)

Start `./run.sh` or `./run.sh --robot`, then in another terminal run:

```bash
.venv/bin/python scripts/chat.py
```

The chatbot starts asleep and sends `sleeping` to the connected display. At `you>`, submit `Hi {name}` using the current Character name from settings (for example, `Hi Shiro`). The greeting is case-insensitive and may include a command, such as `Hi Shiro, what time is it?`. Input without the wake greeting is silently ignored while asleep, with no model or speech request. Once awake, type ordinary messages. Thirty seconds without a submitted line after spoken playback finishes sends the robot back to sleep; wake it again with the greeting. The countdown starts on the connected display’s playback acknowledgment, rather than when speech is queued. You can submit another command while it is speaking. In text-only mode, or with no connected display, the countdown starts after the printed reply. Terminal input counts when you press Enter. Standalone thanks or farewells (including “thankyou”) get a natural closing reply, then sleep immediately after speech completes. If the same message contains another request, the chatbot handles it and stays awake. Conversation history survives sleep, until `/reset` or exit; `/quit` exits even while asleep. The [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/quickstart?lang=python) runs the chatbot with tools to list and play supported animations. Open the robot display to load those animations. The character shows Thinking while answering and returns to Idle unless the agent chose an animation. Chat works even with the display/service offline.

The chatbot uses `OPENAI_API_KEY` or the private key saved through studio Voice settings. Internet and API billing are required. History stays in process memory and is sent to OpenAI each turn; SDK trace uploads are disabled. The reasoning backend defaults to `gpt-6-luna`. Use `--model MODEL_ID` or `OPENAI_CHAT_MODEL` to override it. `--url` or `DOTS_URL` selects the display service.

Replies print in the terminal and speak by default through the existing speech service, saved voice, subtitles, and body motion. Use `--no-speak` for text-only replies. Speech needs the local service and a display with audio enabled. Input remains text only; later, transcription can feed `Chatbot.reply()` with the same history and tools.

Offline check: `.venv/bin/python -m unittest discover -s test -p test_chat.py`.

The chatbot reads its name from `appearance.name` in the live service's settings on every turn (falling back to `DOTS_CONFIG` or `robot-config.json` if offline). Change **Character name** in the studio and the next turn uses it. Its date/time tool reads the computer clock, defaulting to `Asia/Kuala_Lumpur`; set `DOTS_TIMEZONE` to another IANA timezone if needed. Keep the computer clock synchronized for accurate answers.

OpenAI's hosted web-search tool is enabled with live internet access, using the same API key. The bot can search current facts and weather, with conversational replies; source sections and links are removed before display and speech. Weather prioritizes the city you specify or have established in the conversation. Otherwise, the current_location tool estimates the robot computer’s city through https://ipapi.co/json/ before asking. This sends a request to ipapi.co (which sees the public IP); only city, region, and country are returned to the model, not the IP itself. IP location can be inaccurate, especially with VPNs, and locates the robot computer’s connection rather than GPS. If lookup fails it asks for a city. Set DOTS_CITY="Kajang, Malaysia" to use a fixed city without contacting the location provider. Search adds API usage charges. Try “What is your name?”, “What date and time is it?”, “Search today's technology news”, or “What is the weather in Kuala Lumpur today?”.

## Turtlesim MCP control

The chatbot can connect to `scripts/turtlesim_mcp.py`, a local stdio MCP server using native `rclpy` communication. Movement publishes `geometry_msgs/Twist` on `/turtle1/cmd_vel`, reads `turtlesim/Pose` from `/turtle1/pose`, and runs the `turtlesim/RotateAbsolute` action on `/turtle1/rotate_absolute` for heading commands. It never shells out to `ros2 topic pub` or `ros2 action send_goal`. Turtlesim has no native go-to-position action; room navigation uses a 20 Hz pose-feedback controller over its velocity topic.

With ROS 2 Humble and turtlesim installed, source ROS in the terminal that runs the chatbot, then install the optional dependencies once:

```bash
source /opt/ros/humble/setup.bash
.venv/bin/python -m pip install -r requirements-turtlesim.txt
.venv/bin/python scripts/chat.py --turtlesim
```

Launch your turtlesim separately and keep its `ROS_DOMAIN_ID`, ROS middleware, and discovery settings consistent with the chatbot. The chatbot starts and closes the MCP server automatically. Startup commands above only launch software; robot commands flow through MCP and ROS APIs. Python must match your ROS installation (Humble on this machine uses Python 3.10). Speech still uses the face display service.

Wake with `Hi Shiro` (or your saved character name), then ask “Go to the kitchen”, “Where is the turtle?”, “Stop the turtle”, or “Turn to face north”. The MCP tools are `list_destinations`, `go_to_room`, `robot_status`, `stop_robot`, `rotate_robot`, `move_straight`, and `turn_robot`. Movement tools queue their request without moving the turtle. The chatbot generates and speaks its departure announcement first, waits for the display’s successful playback acknowledgment, then releases the queued motion through a runtime-only MCP tool (hidden from the model). If speech fails, is interrupted, has no connected display, or is not confirmed within 120 seconds, motion is cancelled. In explicit --no-speak mode, the printed announcement releases motion immediately. It then monitors live MCP status to generate a spoken arrival or failure report automatically, once per motion. The report waits until any current spoken reply finishes; input remains available while moving. Completion reports cannot invoke movement tools. Stop before sending a different destination while it is moving. “Go straight” moves one simulation unit; “go straight two meters” moves two units along the current heading. Turtlesim has no real meter scale, so this interface maps a requested meter to one simulation unit. Negative distance in move_straight moves backwards. “Turn left” or “turn right” defaults to ninety degrees; explicit relative turns support one through 180 degrees. Targets beyond the safe canvas boundary are rejected. Sleep does not cancel an active navigation task; stopping or exiting the MCP server does.

Fixed points are in `turtlesim-waypoints.json`:

| Room | x | y |
| --- | --- | --- |
| living room | 2.0 | 2.0 |
| bedroom | 8.5 | 8.5 |
| kitchen | 8.5 | 2.0 |

Edit these coordinates before restarting the chatbot; `TURTLESIM_WAYPOINTS` selects another JSON file. Coordinates must lie between 0.5 and 10.5. Navigation stops within 0.12 units, limits velocity, fails after 60 seconds, and stops publishing motion if pose feedback becomes older than one second. This is a straight-line turtlesim controller; there is no obstacle map or real-world navigation stack.

Run the live integration check with `.venv/bin/python -m unittest discover -s test -p test_turtlesim_mcp.py`. It launches its own headless simulator in a separate ROS domain, exercises MCP room navigation, the rotation action, stopping, and lost-pose handling, then cleans up. It does not require an OpenAI request.

## Robot display and local configuration

The studio now saves **appearance, character name, speaking voice, rendering quality, reduced motion, random idle behavior/interval, voice motion strength/source/level, and robot background** automatically to **`robot-config.json` in the workspace root**. **Save configuration** writes to that same file on demand. The initial migration reads the existing browser appearance once; subsequent loads use the file as the source of truth. Temporary original-outfit previews and the current test animation are not saved as character configuration. Invalid saves leave the previous file intact; writes replace it atomically. To use another location, set `DOTS_CONFIG=/absolute/path/robot-config.json` when starting the service.

### Speaking voice and API key

In the studio, edit **Character name**, choose a **Speaking voice**, then press **Play sample** to hear “Hi, I am {name}.” The original character moves with the actual audio amplitude and returns to Idle / Ready when playback finishes. **Stop** cancels playback; selecting another animation also interrupts speech. The selected voice saves in the same `robot-config.json` as `settings.ttsVoice`.

Speech generation uses **`gpt-realtime-2.1-mini`**, OpenAI's [recommended replacement for the retiring TTS models](https://developers.openai.com/api/docs/deprecations). The server connects to the Realtime API over WebSocket and streams the returned 24 kHz PCM chunks immediately to native browser Web Audio playback. An 80 ms initial playback buffer smooths network jitter. Successful requests reuse the same connection and use minimal reasoning; a voice/key change, expired session, cancellation, or connection failure opens a fresh session. Each request supplies only its own text, without accumulating conversation history. The Voice panel shows measured **First audio** time and whether the connection was warm. This measures the server's request-to-first-audio time; browser scheduling adds about 80 ms, and network/provider load can vary. It requires internet access and OpenAI API billing. The server uses the small Python `websockets` dependency; the browser gains no additional dependencies. Speech generation is isolated in `RobotServer.speech_events()`.

The picker includes the model's [10 supported voices](https://developers.openai.com/api/docs/guides/realtime-conversations): Marin, Cedar, Alloy, Ash, Ballad, Coral, Echo, Sage, Shimmer, and Verse. Marin is the default. Fable, Nova, and Onyx belonged to the previous speech model and are unavailable here. Opening the studio with one of those saved voices selects and saves Marin, with a visible migration notice; you can choose another voice. The existing character appearance and other settings are preserved. Samples and terminal `say` commands play incrementally while generation continues, and the actual playback amplitude drives Speaking.

When no key is available, the page asks for one. Paste it into the password field and choose **Save API key**. You can dismiss this prompt with **Later** and continue using the renderer. **Set up API key / Change API key** reopens it. The key travels only to this local service and to OpenAI for speech generation. The input clears after submission/closing, and the key is never returned by the API or included in character exports, configuration, event streams, logs or browser localStorage.

The server stores the key in `~/.config/mimo-dots/openai-api-key` (or beneath `XDG_CONFIG_HOME`), outside the workspace and web files. The directory is mode `0700` and the file is mode `0600`; writes are atomic. This is a private plaintext file protected by OS permissions, not an encrypted vault: processes running as your OS account or root can read it. Alternatively, provide `OPENAI_API_KEY` through the server environment, which takes precedence over the saved file. Credential setup and billable speech requests accept only local-machine access with a localhost Host header; cross-origin JSON requests are rejected. Configure the Pi's own key locally rather than putting it in `dist/` or `robot-config.json`.

The robot page starts voice automatically and has no Enable voice control. Launch it with `./run.sh --robot` for unattended speech: this starts the service and opens a dedicated Chromium window with audio allowed from startup. Closing that window or pressing Ctrl+C stops both processes. `say` alone speaks the saved greeting; `say Hello, I am your robot` speaks custom text using the saved voice. The audio drives Speaking and returns to idle after it finishes. Audio clips stay in server memory, with only the latest robot clip retained; they are not written to disk.

Start the robot on this machine or a graphical Raspberry Pi:

```sh
PORT=5174 ./run.sh --robot
```

The launcher finds Chromium or Google Chrome in PATH. Set `DOTS_BROWSER=/path/to/browser` to select another Chromium executable; `DOTS_BROWSER_PROFILE` overrides its separate profile at `~/.config/face-agent-chromium`. This machine also supports the official Chrome for Testing binary in `~/.cache/face-agent/chrome/chrome-linux64/chrome`. The browser uses Chrome's [documented autoplay flag](https://developer.chrome.com/blog/autoplay/#developer-switches). Voice still needs the configured OpenAI API key and an audio output device. Plain `./run.sh` runs only the service: opening its URL in an ordinary browser or the Codex preview may still require a tap or key press under that browser's autoplay rules. The latest queued speech plays after that interaction, without a dedicated activation button.

```sh
python3 scripts/dotsctl.py --url http://127.0.0.1:5174 say
python3 scripts/dotsctl.py --url http://127.0.0.1:5174 say "Hello, I am your robot"
```

For robot software, `POST /api/say` with `{"text":"Hello!","stream":true}` starts speech and sends it to the robot display as soon as the first audio chunk is available. An optional `voice` overrides the saved voice for that request. The CLI and studio terminal use this streaming mode. `POST /api/speech-stream` returns newline-delimited JSON audio events (`type: "audio"`, base64 PCM16LE in `pcm`, 24 kHz mono), followed by `done` or a safe `error`. The first audio event includes `firstAudioMs` and `warm`. Audio arrives before generation completes. `POST /api/speech` and `/api/say` without `stream:true` retain the complete WAV behavior for compatibility. `GET /api/voice` returns setup status, model name, and available voices; `POST /api/key` accepts `{"apiKey":"..."}` and returns only a success flag. Never put a key in a URL or browser code. Provider errors are mapped to safe messages rather than returning raw provider response bodies. A mid-stream failure stops playback; audio already heard cannot be recalled.

Open **Robot display** in the header, or `/robot.html`. This page fills the window with the saved character, without the studio controls. It uses the same native renderer and action controller. Configuration changes and animation commands arrive through a local event stream, including direct edits to the same JSON file. Use **F**, double-click the character, or the fullscreen button at the bottom edge to enter browser fullscreen. Move to the bottom edge to reveal the small studio/fullscreen controls. The character automatically fills the available space with a small safety margin. Framing includes moving props, accessories, sleep marks and signatures, and makes room above subtitles. Each action retains its widest observed bounds so breathing does not continually change the zoom. A 64 × 64 alpha sample measures rendered pixels; Compact rendering still caps the backing canvas at 512 pixels and Balanced at 1024 pixels. Pi performance remains unmeasured.

Spoken messages show subtitles centered at the bottom of the robot display. The text appears when audio playback starts and clears when it ends, fails, or is interrupted by another animation or message. Long sentences wrap onto multiple lines within the screen; unusually long words also wrap. Very long messages use a scrollable caption area capped at 38% of the display height. Subtitles use the supplied speech text, with no word-by-word highlighting. Animation-only `speaking` commands have no subtitles. Speech commands include the actual requested `text`, including the saved default greeting, alongside their audio URL.

Start the service and an interactive animation terminal:

```sh
./run.sh
python3 scripts/dotsctl.py
```

For the current preview on port 5174, run:

```sh
python3 scripts/dotsctl.py --url http://127.0.0.1:5174
```

At `dots>` type `listening`, `thinking`, `speaking`, `speaking 0.8`, `sleeping`, `idle` (or `ready`), `wave`, or any library state. `Felipe signature` selects that temporary original preview. `say [text]` plays real speech with the configured voice. `states` lists every supported identifier, `status` shows the file path, display connections, latest command and acknowledgment, and `quit` exits. Looping statuses continue until another command; finite actions return to Idle / Ready. Commands are live session state and do not overwrite the character configuration. A command sent before the display opens is applied when it connects.

One-shot commands are also available:

```sh
python3 scripts/dotsctl.py sleeping
python3 scripts/dotsctl.py speaking 0.8
python3 scripts/dotsctl.py idle
```

The studio's **Animation terminal** panel sends the same commands. For robot software, send JSON to the local service:

```sh
curl -X POST http://127.0.0.1:5173/api/command \
  -H 'Content-Type: application/json' \
  -d '{"state":"listening"}'
```

`GET /api/config` reads the shared file; `PUT /api/config` validates and saves it; `GET /api/status` reports command and display status. Configuration, animation commands, and the terminal use Python's standard library; OpenAI voice generation additionally uses `websockets`. No shell commands from the animation terminal are executed. The display needs a browser with WebGL 2 and shared WASM memory. For Raspberry Pi deployment, commit the bundled `dist/`, `public/local-dots/`, `robot-config.default.json`, `requirements.txt`, `run.sh`, and the service/terminal scripts with the project. Clone the repository and run `./run.sh`. Node.js is needed for development builds, but not for running the bundled build on the Pi. Pi rendering performance remains unmeasured.

Import resources from a local installation, then build:

```sh
pnpm import:dots
pnpm build
./run.sh
```

On this Linux machine the importer defaults to `/usr/lib/chatgpt/resources/app.asar`. For another installation, pass its archive to `python3 scripts/import_dots.py /path/to/app.asar`. Imported files, API declarations, and original third-party notices are in `public/local-dots/`, which can be committed with the application. Vite includes the entire folder in `dist/local-dots/` during a build. The complete imported bundle is approximately 11 MB before compression. A clone containing the bundled `dist/` needs no installed ChatGPT app, additional asset transfer, or frontend build. The launcher installs its Python voice dependency on first use. Device configuration is seeded from `robot-config.default.json`, then saved only to the device's ignored `robot-config.json`; the API key is set up separately on each device. Importing does not establish a redistribution license for the bundled OpenAI assets.

The original renderer requires WebGL 2, shared WASM memory, and cross-origin isolation. Both Vite and `run.sh` supply the required HTTP headers. To try it on the Pi, use this same local build and static server, including `scripts/serve.py`; start in Compact mode. Native Pi performance has not been measured. A procedural-only build can be made without `public/local-dots/`; the original-renderer page then explains how to import the missing resources.

## Open the preview

The production build in `dist/` is included in repository packaging. After committing and pushing the complete project, clone it on the Pi and run:

```sh
git clone https://github.com/s-w3i/face-agent.git
cd face-agent
./run.sh
```

The launcher creates `.venv`, installs the one Python voice dependency if needed, seeds the default character configuration if no device configuration exists, and starts the local service. First launch needs internet for dependency installation. Subsequent launches reuse the environment and preserve your saved settings. `DOTS_CONFIG` still selects another configuration path. There is no Node.js or pnpm requirement on the Pi when using the bundled frontend.

Use Python 3.10 or newer with venv support and a graphical Chromium browser supporting WebGL 2. Raspberry Pi OS requires third-party Python packages to use a [virtual environment](https://www.raspberrypi.com/documentation/computers/os.html#use-python-on-a-raspberry-pi). If Python venv support is missing, install it once with `sudo apt install python3-venv`, then rerun the launcher. Raspberry Pi OS Lite additionally needs a graphical session and suitable browser for displaying the character. Set up the OpenAI API key through the configuration page on the Pi.

Open http://localhost:5173/original-dots.html for configuration and http://localhost:5173/robot.html for the robot face. If another preview is already running, open that URL or use `PORT=5174 ./run.sh` and pass that URL to `dotsctl.py`.

To run on a Raspberry Pi, clone the complete project or copy its deployment files, run the launcher, and open the URL in Chromium with WebGL 2 enabled. Fonts, graphics, and JavaScript are local; robot commands and configuration use local API calls; OpenAI voice playback requires an API key and internet. The studio defaults to a 30 fps target with capped render resolution. Actual Pi rendering performance has not been measured.

## Try the character

- **Animations:** Idle, Listening, Thinking, Speaking, Sleeping, Happy, Curious, Bounce, Spin, Stretch, Wobble, and Wake up. Select any tile; select it again to restart. Speaking is a visual simulation, with no audio input or output.
- **Timeline:** scrub Animation time to pause at an exact pose. The selected animation tile restarts playback; Resume continues from that time.
- **Play all:** runs all 12 motions once, then returns to idle. Selecting any motion stops the tour. Pause/resume and speed controls also work during the tour.
- **Appearance:** five body shapes, any body color, four eye styles, round/square glasses or sunglasses, beret/cap/sprout/halo/headphones, and flocked/matte/gloss finishes. Appearance saves to browser local storage. Version 2 uses a new storage key; earlier saved appearances remain preserved under their original key. Version 1 character exports can still be imported, with pebble mapped to blob.
- **Behavior:** random idle actions with no consecutive repeats, adjustable interval, optional sleep timer, and cursor-following eyes. Any manually selected animation wakes the character. Auto motions only start during idle. Sleep timing is independent of the random-motion switch.
- **Scene:** three backgrounds, lightweight/studio rendering, live performance counters, and character import. Export in the header saves a versioned JSON appearance file. Behavior and scene controls apply to the current session.
- Drag to orbit, scroll to zoom, use Reset view to return to the front. Expand opens a full-screen character view. Reduced-motion preferences start the studio paused with spontaneous motions off.

## Develop and test

Use Node.js 22.12 or newer and pnpm:

```sh
pnpm install --frozen-lockfile
pnpm dev
pnpm test
python3 -m unittest discover -s test -p 'test_*.py'
pnpm build
```

After frontend or imported-resource changes, run `pnpm build` on your development machine and commit the updated `dist/` alongside the source before pushing. The Pi runs that prebuilt version. Update `robot-config.default.json` deliberately when you want a new default for fresh installations; existing device configuration takes precedence. This file contains character settings only and must never contain credentials.

During development, run the Python service on port 5174 alongside Vite on 5173. Vite proxies `/api` to that service; both use the same configuration file. The Python check exercises atomic saves, rejected input preserving the saved file, state commands, speech levels, live event delivery, external file edits and display acknowledgments. The voice check also exercises private file permissions, request validation, provider request fields, safe error messages, CLI speech and transient audio delivery using a mocked OpenAI response.

The procedural browser renderer uses Three.js; the original mode uses the imported native renderer. OpenAI speech uses Python `websockets` on the server. Vite is the development/build tool. `src/character.js` contains the renderer and procedural model. `src/motion.js` defines expressive body poses; `src/deformation.js` applies them to the body, face, and fur on the GPU. `src/fur.js` builds short instanced fibers and a tiny felt texture. `src/model.js` owns validated appearance settings and the animation scheduler. `src/main.js` connects the controls. Tests cover interruption, idle scheduling, sleep/wake, pause/speed, the complete tour, import validation, preset compatibility, distinct status silhouettes, stable deformations, smooth transitions, and timeline seeking.

For the procedural studio, future voice/chat integration can call `director.play('listening')`, `director.play('thinking')`, `director.play('speaking')`, and `director.play('idle')` at the appropriate interaction boundaries. The original mode's added motion accepts a speech level through `voiceMotionPose()`; its UI currently supplies the demo or fixed level. The original robot view now uses the OpenAI voice and server-only credentials described above. The terminal chatbot now drives this display through the local service; microphone input is not wired up yet.

The procedural renderer's soft contact shadow is a tiny generated texture; that renderer has no dynamic shadow maps, downloaded models, large textures, or post-processing passes. Its flocked surface combines a 256px texture with short fiber ribbons in instanced draws; lightweight mode uses half the fiber density. The coat moves with the body without CPU hair simulation. Speaking reshapes the contour in synthetic syllable phrases. Sleeping settles into a low, wide shape; listening bends forward; thinking curves and sways. Hats retain their form while following the deformed body. Lightweight is a starting profile to benchmark on the intended Pi, not a hardware performance guarantee.

Font licenses are included in `public/fonts/` (DM Sans and Manrope, SIL Open Font License).

The motion study and original-renderer observations are documented in [docs/dots-motion-reference.md](docs/dots-motion-reference.md). Procedural animations are independently authored interpretations. Original mode uses the imported local runtime, which is included in repository packaging and copied into the build when present.
