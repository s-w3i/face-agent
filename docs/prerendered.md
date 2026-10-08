# Pre-rendered Dots on Raspberry Pi 5

The studio still uses the original 3D renderer for customization. The robot can
instead draw pre-rendered transparent WebP sprite sheets using Canvas 2D, without
loading the native WASM renderer, WebGL, or Three.js. Export and playback require
no new dependencies. Actual Raspberry Pi performance has not been measured.

1. Start the app on your computer with `PORT=5174 ./run.sh` and open
   `http://127.0.0.1:5174/original-dots.html`.
2. Customize your character. In **Raspberry Pi playback**, click **Bake saved
   character**. Keep the page visible. Progress covers every activity, gesture,
   completion, all eight original signature previews, listening, and speaking.
   Your current-look signature is included only when the original outfit supports it.
3. Click **Test pre-rendered display**. All existing `dotsctl.py` state commands
   and `say` commands work there. Finite actions return to idle; the saved random
   idle settings still apply. Choose **Pre-rendered · lightweight** under **Robot
   renderer** to use it whenever `/robot.html` opens.
4. Copy `public/prerendered/` and your `robot-config.json` to the same locations
   in the Pi clone. The bundled example pack is already included in the repository;
   your own newly baked pack must be copied or committed separately. No Node build
   is needed after copying it. Set up your OpenAI API key separately on the Pi.

Run the dedicated robot window on the Pi (or this computer):

```sh
PORT=5174 DOTS_RENDERER=prerendered ./run.sh --robot
```

`DOTS_RENDERER=prerendered` forces baked playback for this launch without changing
the configuration. Without it, the saved **Robot renderer** setting is used.
Chromium and a graphical session are required. The launcher enables automatic
audio playback. A normal browser tab can still require an initial tap.

Test animations from another terminal:

```sh
python3 scripts/dotsctl.py --url http://127.0.0.1:5174
```

Try `states`, `sleeping`, `thinking`, `listening`, `speaking 0`, `speaking 1`,
`wave`, `Iggy signature`, `idle`, or `say Hi, I am Shiro`.

Frames are 512 × 512 at 16 fps, packed into transparent 2048 × 2048 WebP sheets.
The player decodes three ordinary sheets plus the two speaking sheets at most
(approximately 80 MiB of decoded pixels, excluding browser overhead). Loading
a new action can briefly hold the previous pose while its first sheet decodes.
The complete painted bounds are saved per action so props and accessories stay
inside the screen, above the existing centered subtitles.

Speaking uses 21 body poses from silent to full volume. The existing live audio
analyser supplies the volume; a smoothed level selects the pose. Any new TTS
sentence works immediately, with the saved voice and subtitles. This is body
motion synchronized to volume, without phoneme-specific lip animation. Listening
and the other statuses use their baked frame sequences. Looping native activities
are settled before capture; their finite recording repeats and may have a small
seam at the loop boundary.

Re-bake after changing appearance, motion strength, reduced motion, or background
(the native props use the chosen theme). Name, voice, subtitle text, idle interval,
and random-idle enablement remain live settings. An outdated pack is identified
in the studio and robot status. Cancel or failed export preserves the last
complete pack. Packs are published atomically to `public/prerendered/manifest.json`;
old pack folders can be removed after displays using them have stopped.

The pack contains character configuration and image assets only. It contains
neither the OpenAI API key nor recorded speech. The key remains in its existing
private file outside the repository. A fresh device prompts for its own key in
the robot display (or the studio). Saving it on one device does not configure
another device. The default location is `~/.config/mimo-dots/openai-api-key`,
with file permissions `0600` and a `0700` parent directory. `XDG_CONFIG_HOME`
can select another device-local configuration directory; `OPENAI_API_KEY` is
an optional server environment override and is never exported.

## Live eye tracking with the baked body

Shiro's bundled pack includes eye positions for each body frame. The lightweight
player moves the original eye pair using small Canvas 2D image patches, easing
the surrounding fur back to fixed edges. The body, crown, blinks, highlights and
audio-driven speaking poses still come from the saved WebP sheets. No camera
capture or person detector is included yet.

**Human eye tracking** is on by default while awake. A critically damped filter
smooths target changes, with a small dead zone for camera jitter. **Gaze response**
in the studio adjusts its response time; changing it does not require a bake.
When no person is detected, or updates stop for one second, the eyes ease back to
the original pixels and animations. Sleeping always uses the original closed
eyes; a current target is picked up again after waking. Automatic idle look
gestures are skipped while tracking a person, while other body gestures continue.

Open **Test gaze** in the robot display. Move Target X/Y, or enable **Follow
pointer**, to simulate a continuous camera target. Test `idle`, `listening`,
`speaking`, `wave`, `drag`, and `sleeping` using the buttons. **No person · original
eyes** stops the simulated target. Close the panel to leave the display clear.
Simulation continues until you turn it off or close the page.

A future camera tracker should send the person's face center at 10–30 Hz:

```sh
curl -X POST http://127.0.0.1:5174/api/gaze \
  -H 'Content-Type: application/json' \
  -d '{"detected":true,"x":0.25,"y":0.4}'
```

Coordinates are normalized: `(0,0)` is the display's upper left and `(1,1)` its
lower right. Mirror the camera X coordinate before sending if your camera view
requires it. Send `{"detected":false}` when the person disappears. Gaze events are
separate from animation commands, so target updates do not restart movement or
interrupt speech. `GET /api/gaze` includes the latest target and its `ageMs`.
Terminal commands `gaze 0.25 0.4` and `gaze off` are useful for short checks; a
single target expires after one second.

For an older pack, click **Prepare eye tracking** in the studio. This only adds
eye positions to `manifest.json`; it does not re-render or change the body images.
New bakes include the positions automatically. The current eye detector supports
Shiro's pink body and unobstructed dark capsule eyes. Other colors, obscured eyes,
unsupported frames, and the eight other characters' signature previews retain
their original eyes. Verify a customized look in Test gaze after preparing it.
