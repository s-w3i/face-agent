# Dots motion reference study

Inspected 7 October 2026. The public reference pages are [Introducing dots](https://openai.com/index/introducing-dots/) and [Meet dots](https://learn.chatgpt.com/docs/dots). The pages inspected do not supply downloadable animation curves or a GLB/FBX animation pack.

A stronger reference was available locally in the installed desktop application, `/usr/lib/chatgpt/resources/app.asar`. Its `webview/assets/orbit-character-361454329b588d31/runtime` bundle identifies itself as Orbit Web SDK 0.11.0. This was first inspected separately from the studio. At the user’s subsequent request, an optional original-renderer page now uses a local copy imported from that installation; the imported assets can be committed with the application and are copied into the frontend build.

Observed reference details:

- The `blue_beret` character is a six-lobed flower with oval black eyes, blue fur, and a beret. Its visual contour was also checked in the original local renderer.
- The renderer exposes independent activity commands including Ready, Thinking, Working, and Paused, and finite Wave/Signature reactions. It has gaze tracking over Ready and separate preview deformation controls.
- Compiled vertex shaders include a low-order spine bend, applied to surface positions and corresponding normals. Fur follows that field. The important visual mechanic is a deforming body with attached surface detail, rather than a rigid body being tilted.
- Live Thinking has an attentive gaze and a light-bulb prop. Live Paused closes the eyes and displays sleep marks. These were inspected in a temporary local preview of the original renderer.
- The native binding inspected does not expose an exported speaking/sleeping keyframe sequence. This study therefore does not establish the original voice timing or all voice-specific body morphs.

## Complete exposed action library

The inspected `orbit-enums.d.mts` and `orbit-characters.d.mts` define nine nonempty activities, two reaction kinds, four non-neutral completion outcomes, and normalized pointer input. The installed `frame.mjs` forwards Down/Move/Up/Cancel to native pointer physics in editor mode; activity-bound characters use `setReadyGaze()` over Ready instead. The installed `preset-previews.mjs` scripts gaze cues and requests a Signature every eight seconds. There is no additional named idle-clip catalogue in this binding. The studio exposes these available entry points; this does not establish that every private animation in the installed app is individually selectable.

`src/native-actions.js` supplies the library and a small visible-time idle countdown. Glance and press/drag buttons are scripted inputs to the original renderer, rather than new authored deformation clips. The four outcomes are sent as Stop commands on the corresponding live activity episode. All eight `hereCharacters()` outfits have temporary Signature previews. The studio keeps a separate original appearance snapshot while previewing, prevents customization during that preview, excludes it from local persistence, and restores it when the preview finishes or another action is selected.

Idle random choices include only gaze, gestures, Wave, and the current supported hero's Signature. They never borrow a different outfit. Other statuses, cursor tracking, the tour, Reduced motion, and background visibility suspend random actions. Finite previews are timed from their first completed native frame, rather than from canvas construction; callbacks are cancelled on interruption. Tests verify countdown gating, interval reset on manual changes, nonrepetition, and custom-outfit compatibility.

## Implementation in this studio

`src/motion.js` authors independent procedural poses for the 12 requested motions. `src/deformation.js` applies a shared GPU deformation field: height, width, taper, quadratic bend, curved spine, and traveling contour ripples. An inverse-Jacobian normal adjustment keeps the lighting and instanced fur consistent with the changing surface. Headwear stays rigid and follows a deformed attachment point.

Speaking uses a synthetic five-second phrase with syllables, accents, and pauses. Sleeping compresses into a broad, curled form with closed eyes and slow breathing. Listening stretches upright and bends forward with slow nods. Thinking narrows into a swaying S curve and looks upward. These are authored interpretations of the reference mechanics, not imported OpenAI animation data or an exact motion capture.

The animation timeline pauses playback and evaluates an exact procedural pose at the selected time. Playback blends between statuses; scrubbing intentionally bypasses that blend so poses are easy to compare. No additional render passes, per-frame vertex buffer rebuilds, or CPU hair simulation are introduced.

## Optional original renderer

`original-dots.html` and `src/original.js` load the locally imported runtime through its existing binding. `scripts/import_dots.py` copies the required renderer files and preserves third-party notices from the installed archive. `scripts/serve.py` and `vite.config.js` supply cross-origin isolation headers. These local assets are separate from the independently authored procedural renderer. Their presence in this workspace does not establish a public redistribution license.

## Added motion on original assets

The original mode now includes authored Listening and Speaking motions. `src/native-voice.js` extends the vertex shader at compilation, on the original canvas only. It applies the same deformation field and inverse-Jacobian normals used by the procedural studio after the native world transforms. The surface, fitted eyes/glasses/accessories, instanced fibers, and shadow positions follow the field. Neutral body bounds come from the first entry in the renderer's packed DrawBlock upload. Original runtime files, mesh caches, materials, and appearance bytes remain unchanged.

Listening uses the attentive stretch/lean/nod pose. Speaking uses the demo phrase or a supplied 0–1 speech level, with silence returning the deformation to neutral. These are new motions rather than discovered original voice animation data. The UI provides exact added-pose scrubbing, pause/resume, strength, and fixed speech levels; native Ready idling and blinking continue underneath. Reduced motion selects a gentle static pose.

This extension depends on the inspected internal 0.11.0 shader and DrawBlock layout. Unit checks include the actual copied shader, preserving WebGL overloads, bounds adaptation, neutral silence, and finite surfaces at maximum strength. Live WebGL rendering verifies shader compilation and fitted details. Pi performance still requires measurement.
