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

Launch the camera in its own terminal, using your existing installation paths:

```sh
source /opt/ros/humble/setup.bash
source /tmp/orbbec-ros-check/install/setup.bash
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
source /tmp/orbbec-ros-check/install/setup.bash
./track.sh --url http://127.0.0.1:5174 --stats
```

The first launch installs two Python packages and downloads approximately 37 MiB
of checksum-verified OpenCV models. Later launches reuse them. They live under
`~/.cache/face-agent/`, outside the repository and web server. Tracking needs no
API key. The Pi still sets up its own voice API key using the existing prompt.
The ROS installation paths must exist on that device; cloning face-agent does
not install ROS or the camera driver.

Run this check before starting tracking on a new device:

```sh
./track.sh --check
```

It checks ROS imports and actually executes both CPU models, then exits. The Pi
output should show `platform: aarch64`. If your active `python3` does not match
ROS, select its interpreter, for example `TRACKING_PYTHON=/usr/bin/python3`.
Use `--mirror` if horizontal eye movement is reversed for your camera mounting.

## Person lock and motion

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
work, RGB image stride, and interrupted model downloads. Run them with:

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
