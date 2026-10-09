# Shiro human tracking on Raspberry Pi 5

The camera tracker runs locally alongside the existing pre-rendered robot display.
It subscribes to `/head_camera/color/image_raw/compressed` from your Orbbec ROS
driver and sends normalized face centers to `/api/gaze`. No images go to OpenAI.
The voice and chatbot can continue using the API independently.

## Install and run

Use a **64-bit OS** on the Pi 5, with ROS and the Orbbec driver already working.
The tracker uses the same Python version as your ROS installation, in a separate
virtual environment. The pinned OpenCV release has Linux ARM64 binary wheels;
no GPU, MediaPipe, TensorFlow, PyTorch, Node build, or `cv_bridge` is needed.

Install Python venv support if it is missing:

```sh
sudo apt install python3-venv
```

### Ubuntu 24.04 / ROS 2 Jazzy on this Pi

This Pi uses the official ARM64 `ros-jazzy-orbbec-camera` package (2.9.3),
installed under `/opt/ros/jazzy`, with the image transport plugins. A source
workspace is not required for this installation.

With the current USB 2 cable, launch `./camera.sh` from the repository. It sources
Jazzy and publishes `/head_camera/color/image_raw/compressed` and camera
calibration at 640×480 / 15 fps using V4L2. Depth and point clouds are disabled;
the face tracker only needs color images. This profile streamed successfully
on the connected Gemini 335. Earlier simultaneous color/depth configurations
with the projector at level 6 repeatedly disconnected on this USB connection.

For color and point-cloud output above 10 delivered FPS over the current USB 2
connection, use the separate RGB-D launcher:

```sh
bash /home/cutiepie/face-agent/camera-rgbd.sh
```

It uses **1280×720 MJPEG color at 15 fps** and **640×480 Y16 depth at 30 fps**,
with XYZ point clouds on `/head_camera/depth/points` and no cloud decimation.
The final 90-second launcher test delivered 15.43 fps compressed color, 11.90 fps
raw color, 25.08 fps raw depth, and 23.36 fps point clouds, with approximately
288,000 valid points in the final cloud and no USB disconnects. All four streams
averaged above 10 fps. Face tracking uses
`/head_camera/color/image_raw/compressed`; this is not a colored point cloud.
Frame synchronization is disabled, so color and depth run at their own rates.
The larger 848×480 depth mode, configured for 10 fps, delivered only 7.95 fps
point clouds. Configuring 640×480 depth at 15 fps delivered 10.99 fps point clouds;
using its supported 30 fps mode provides more headroom above the 10 fps target.

The important setting for this cable is **`laser_energy_level:=5`** with
`enable_laser:=true`. Level 6 reproduced the USB disconnect loop even at low
resolution and with depth alone. Levels 1, 3 and 5 streamed usable depth; level 5
is retained for the faster profile. Disabling the projector also
worked, with less depth coverage on the tested scene. These results identify a
working configuration, but do not establish whether the underlying fault is in
the cable/power path or camera firmware. The current device does not support
`color_mjpeg_quality`, so that option is omitted.

For maximum resolution at lower FPS, the earlier 90-second high-resolution test
had zero USB disconnects, 689 distinct
compressed color frames (7.99 fps), and 384 XYZ clouds (4.47 fps), with about
908,000 valid points in the final cloud. Rates can fall further when the Pi is
also running tracking, local Whisper, or RViz. These measurements describe this
machine and scene, rather than a guarantee for every USB 2 cable or power supply.
That profile remains available through launcher overrides:

```sh
bash /home/cutiepie/face-agent/camera-rgbd.sh \
  color_width:=1920 color_height:=1080 color_fps:=8 \
  depth_width:=1280 depth_height:=800 depth_fps:=6
```

Stop any previous camera launch with Ctrl+C before starting either launcher.
Only one driver process should own the camera at a time.

Start `./run.sh --robot` in another terminal. For chat with automatic tracking,
run these commands in a third terminal:

```sh
source /opt/ros/jazzy/setup.bash
.venv/bin/python scripts/chat.py --mode words
```

Wake the saved character with `Hi Kuro`. To test tracking without chat, use
`source /opt/ros/jazzy/setup.bash` followed by `./track.sh --stats` while the
robot is awake. Ctrl+C stops each foreground process.

### Other ROS installations / source workspace

Keep the camera workspace in a persistent directory such as `~/orbbec_ws`.
The previous `/tmp/orbbec-ros-check` build disappeared when the temporary directory
was cleared. On a new device, follow the
[Orbbec installation guide](https://orbbec.github.io/OrbbecSDK_ROS2/en/source/camera_devices/2_installation/build_the_package.html)
and build the driver in that persistent workspace. The current computer uses
OrbbecSDK ROS2 release `v2.10.6` under `~/orbbec_ws`.

Launch the camera in its own terminal:

```sh
source /opt/ros/humble/setup.bash
source ~/orbbec_ws/install/setup.bash
ros2 launch orbbec_camera gemini_330_series.launch.py \
  camera_name:=head_camera \
  color_width:=640 color_height:=480 color_fps:=15 \
  depth_width:=640 depth_height:=480 depth_fps:=15 \
  depth_registration:=false
```

In another terminal, from the face-agent folder:

```sh
PORT=5174 DOTS_RENDERER=prerendered ./run.sh --robot
```

In a third terminal, from the same folder:

```sh
source /opt/ros/humble/setup.bash
source ~/orbbec_ws/install/setup.bash
./track.sh --url http://127.0.0.1:5174 --stats
```

The first launch installs two Python packages and downloads approximately 37 MiB
of checksum-verified OpenCV models. Later launches reuse them. They live under
`~/.cache/face-agent/`, outside the repository and web server. Tracking needs no
API key. The Pi still sets up its own voice API key using the existing prompt.
The ROS installation paths must exist on that device; cloning face-agent does
not install ROS or the camera driver.

## Automatic tracking during chat

With the camera and robot display running, start chat from the face-agent folder:

```sh
.venv/bin/python scripts/chat.py --mode words --url http://127.0.0.1:5174
```

Chat starts the tracker automatically in its separate environment. It sources
`/opt/ros/humble/setup.bash` for the tracker, or the selected `ROS_DISTRO` if set;
the camera driver still runs separately. You do not need a separate `./track.sh`
terminal. Tracking output, including first-time setup, goes to
`~/.cache/face-agent/tracking-chat.log` (`XDG_CACHE_HOME` overrides the cache root).
The chat terminal shows no routine tracking messages. A failed tracker produces
one short error pointing to that log, and chat can continue.

Typing `/quit`, closing chat input, or pressing Ctrl+C stops the tracker chat
started and restores the original eyes. A tracker already running for the same
robot service is reused and remains running after chat exits. Use `--no-track`
for chat without camera tracking; keep the manual `./track.sh --stats` command
above for diagnostics. Words mode keeps the same wake-session person lock as a
manual tracker launch.

## Voice input and speaker selection

Complete the one-time [ReSpeaker setup](../README.md#respeaker-robot-microphone),
then start voice chat with the updated robot service and camera running:

```sh
.venv/bin/python scripts/chat.py --mode voice --url http://127.0.0.1:5174
```

Wait while local Whisper loads. After `Voice mode ready`, say **Hi Kuro** to
wake the robot. Wake detection stays local on the Pi. While awake, command audio
streams to GPT Realtime transcription; partial words appear above the character.
Hardware VAD starts recording without idle capture, and 2 seconds of consecutive
audio nonspeech ends it. The existing agent, history and tools receive the final
transcript. The microphone pauses before inference and robot speech, discards
partial input, and resumes after playback plus a 0.5-second clearance.
Kuro sleeps after **3 seconds of inactivity**, starting after its reply finishes.
Recording, the two-second silence endpoint, transcription, Thinking, and Speaking
do not count as inactivity. A standalone goodbye or thank-you
sleeps after the closing reply and returns transcription to local Whisper;
further requests in the same message keep it awake. `/reset` and `/quit` remain
terminal commands. Words mode starts no microphone process.

The chatbot also exposes ROS 2 `/kuro/wake` automatically. From another terminal:

```sh
source /opt/ros/jazzy/setup.bash
ros2 service call /kuro/wake std_srvs/srv/Trigger '{}'
```

This wakes Kuro into Listening without a spoken greeting. The response confirms
success only after the voice backend is ready. An already-awake call resets the
three-second timer; a call during a reply waits for that turn to finish. Use the
same ROS domain/discovery settings as chat. The service exists while chat runs;
`--no-ros-wake` disables it. ROS logs are in `~/.cache/face-agent/ros-wake.log`.

A USB microphone interruption triggers automatic restart attempts while input
remains closed. Chat history survives, but old queued
speech and camera hints are cleared. The tracker gets a fresh session before
new speech can select someone. Wait through microphone restart and robot playback
before speaking again. Recovery cannot repair a faulty USB cable or hub.

The confirmed microphone mounting is native DOA **0° straight ahead at camera
centre, increasing toward camera left**. `--mic-forward-deg` and
`--mic-clockwise` override this alignment. Alignment changes only the camera
association; the microphone's native angle is preserved. Gaze mirroring remains
independent of this physical alignment.

The tracker subscribes to `/head_camera/color/camera_info` alongside compressed
images. It uses the camera's width, horizontal focal length and principal point
to convert each face centre to a bearing. It waits for valid intrinsics instead
of estimating a field of view. A speech hint must belong to the current wake
session and be less than two seconds old. Rear sound is rejected. A candidate
face must be within 20° of the sound and at least 5° closer than any other face,
then pass three successive location and identity confirmations.

Once confirmed, the existing visual tracking follows that person while they
move and while Shiro responds. A new utterance can select a different person
after the same confirmations. Ambiguous hints cannot replace the selected
identity; gaze pauses if the existing visual track is ambiguous. Sleep clears
the target. Hints received during robot playback or from an old session are
rejected. Missing camera calibration leaves voice input usable but cannot
select a person for gaze.

DOA guides attention; it does not isolate that person's channel-0 audio. Nearby
faces at similar bearings, overlapping voices, dominant music vocals and echoes
can prevent a reliable match. Validate speaker selection and transcription in
the robot's actual environment.

## Check tracking setup

Run this check before starting tracking on a new device:

```sh
./track.sh --check
```

It checks ROS imports and actually executes both CPU models, then exits. The Pi
output should show `platform: aarch64`. If your active `python3` does not match
ROS, select its interpreter, for example `TRACKING_PYTHON=/usr/bin/python3`.
Horizontal camera coordinates are mirrored by default for the front-facing robot:
Shiro's eyes should move toward the person in front of the screen. This changes
only gaze X, leaving detection, the person lock, and vertical gaze unchanged.
Use `--no-mirror` if your camera input is already mirrored or its mounting needs
the opposite direction. `./track.sh --check` also reports the selected `mirror` value.

## Words-mode person lock and motion

- Awake: confirm the first visible face across three detections, including
  identity agreement. If several faces first appear together, select the one
  closest to the camera's horizontal center.
- Track: use sparse optical flow at up to 15 fps on a 320-pixel-wide image.
  Check the selected face's surrounding region about three times per second.
  Normally verify its identity only once every two seconds. The original
  Canvas gaze smoother handles the final eye motion at the display's frame rate.
- Crossing, occlusion, failed flow, or camera stall: pause gaze and restore the
  original eyes. Keep the selected person's identity in memory.
- Recovery: scan at one frame per second, checking at most three candidate
  identities. Require a clear identity match on two successive scans before
  resuming. A different nearby person does not become the new customer.
- Sleep: clear the person lock and stop decoding/inference. The next wake starts
  a fresh lock. Listening, speaking, thinking, idle, and body gestures all keep
  the same wake-session lock.

There is **no timed expiry of the person lock**. The one-second gaze heartbeat
timeout only protects against a stopped tracker; it does not end the interaction.
The existing chatbot's `end_conversation` tool already puts Shiro to sleep after
its closing reply, which clears this lock automatically.

The fixed identity reference is never updated from later frames, avoiding gradual
replacement by a bystander. Identity features and camera frames exist only in RAM;
sleep or a tracker restart clears them. Starting the tracker while Shiro is already
awake selects the first confirmed person visible at that time. A service restart
creates a new session. Session tokens reject delayed results from earlier sessions,
including a sleep/wake transition that happens between status polls.

For lightweight operation, stop camera preview/RViz on the Pi when unnecessary.
The compressed topic lets the Orbbec driver forward MJPEG instead of doing an
extra raw-image conversion. This tracker uses color only; depth is not needed
for gaze and unregistered depth cannot be sampled at the same color coordinates.

## Test with the display

Keep the camera and tracker running. In another terminal:

```sh
python3 scripts/dotsctl.py --url http://127.0.0.1:5174
```

1. Enter `sleeping`, then `idle`. Let one person approach first; another can
   approach after the terminal reports `Tracking: tracking`.
2. Move the first person's face left/right/up/down. Test `listening`, `speaking
   0.7`, `wave`, `drag`, `thinking`, and `idle`. Gaze should remain attached during
   the body motions. `say Hi, I am Shiro` tests actual voice and subtitles.
3. Have the first person leave while the other remains. Shiro should return to
   its original eyes and report `waiting-for-locked-person`.
4. Have the first person return. Tracking should resume after two clear recovery
   checks, even after a long absence.
5. Cross paths or cover the first face. Ambiguous tracking should pause, then
   recover the same person when the scene becomes clear.
6. Enter `sleeping` and `idle` again. A different first person can now be selected.

Turn off the display's **Test gaze** simulation during camera tests: both write
the same gaze input. The studio's **Human eye tracking** setting must be enabled.
No person and sleeping keep Shiro's original eyes and animation library.
Press Ctrl+C in the tracker terminal to stop it and restore the original eyes.
Close the robot window to stop its app service; the separately launched camera
is controlled by its own terminal.

The `--stats` output shows processed fps, mean processing time, model call counts,
and lock state. `--duration 30` runs a bounded 30-second test. Evaluate these with
the robot display and voice running together on the actual Pi.

## Validation and limits

The implementation has tests for optical-flow motion and occlusion, identity
locking without an expiry, refusing bystanders, recovery, ambiguous crossings,
sleep/wake resets, stale camera updates, ROI-only checks, bounded recognition
work, RGB image stride, interrupted model downloads, microphone/camera angle
alignment, ambiguous speech hints and confirmed speaker changes. Run them with:

```sh
~/.cache/face-agent/tracking-venv/bin/python -m unittest discover -s test -p test_human_tracker.py
```

The real YuNet/SFace models have also been exercised locally with two different
sample faces, including refusing the other face and recovering the original.
The live Orbbec compressed topic and the displayed gaze during idle, listening,
speaking, wave, drag, and sleep/wake have been checked on the current x86 computer.
This is not a measurement of Raspberry Pi 5 performance. ARM64 wheel availability
is verified; actual combined Pi camera/display/voice throughput still needs a
hardware test. The optional real-model motion/identity regression uses public
OpenCV `lena.jpg` and `messi5.jpg` fixtures in `test-output/tracking/`; it skips
when those images or the cached models are absent.

Face similarity is probabilistic. The default cosine threshold `.55` is more
conservative than OpenCV's example threshold `.363`; validate it using your
customers' distances, lighting, camera mounting, and crossing scenarios. Adjust
with `--threshold` only after testing. Side profiles, masks, small faces, similar
faces, and heavy occlusion can pause tracking. Recovery with more than three
detected faces deliberately waits for a clearer scene to bound CPU use. This is
an attention tracker, not an access-control identity system.

Models and implementation references:
[YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet),
[SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface),
[OpenCV optical flow](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html),
[Orbbec topics](https://orbbec.github.io/OrbbecSDK_ROS2/en/source/camera_devices/4_application_guide/topics.html).
The models are provided under their upstream Apache 2.0 licenses.
