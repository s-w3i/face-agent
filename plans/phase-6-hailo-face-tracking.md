# Phase 6: move face tracking to the Hailo-8L

## Goal
Free CPU cores for the always-on audio threads by moving face detection and identity embedding from the CPU (OpenCV YuNet + SFace in `scripts/human_tracker.py`) to the Hailo-8L. The HEFs for this hardware are already installed. This is the most certain AI HAT win in the project.

## Prerequisites
- Phase 0's `BASELINE.md`, for the tracker CPU numbers.
- This phase is independent of phases 1–5 and can run at any point.

## Facts on this Pi (verify again at the start)
- `hailortcli fw-control identify` reports HAILO8L with firmware 4.23.0. HailoRT 4.23.0 and its PCIe driver are installed through apt.
- The Python wheel is at `~/hailo-debs/hailort-4.23.0-cp312-cp312-linux_aarch64.whl`. It is **not** installed in any face-agent venv. `hailo_platform` exists in `~/hailo-apps/venv_hailo_apps` (use it as a reference only).
- HEFs: `/usr/local/hailo/resources/models/hailo8l/scrfd_2.5g.hef` (face detection plus 5 landmarks) and `/usr/local/hailo/resources/models/hailo8l/arcface_mobilefacenet.hef` (112×112 face embedding).
- Reference code: `~/hailo-apps` (face recognition pipeline, SCRFD post-processing). Read it to learn the input layouts and quantization; don't import it.

## Scope
- **In:**
  - a `HailoFaceBackend` behind `--tracker-backend {hailo,opencv}`;
  - the dependency in the tracking venv;
  - SCRFD post-processing;
  - ArcFace alignment;
  - threshold recalibration;
  - sharing the Hailo device;
  - tests and a benchmark.
- **Out:** changing the tracking logic (speaker selection, DOA fusion, identity lock), the camera topics, and gaze output.

## Design
- Find the abstraction point in `scripts/human_tracker.py`. A class builds `cv2.FaceDetectorYN` and `cv2.FaceRecognizerSF` around lines 20–35. Extract a small interface:
  ```python
  class FaceBackend(Protocol):
      def detect(self, bgr: np.ndarray) -> list[Face]   # box, score, 5 landmarks (same format as today)
      def embed(self, bgr: np.ndarray, face: Face) -> np.ndarray  # L2-normalized
      def match(self, a, b) -> float                    # cosine
  ```
  Move the current code into `OpenCVFaceBackend` unchanged, and add `HailoFaceBackend`.
- **HailoFaceBackend:**
  - Use `hailo_platform` `VDevice` with `params.group_id = "SHARED"` and the multi-process service enabled (`multi_process_service=True`), so the tracker and a later STT process (phase 7) can share the 8L.
  - Use the async `InferModel` API (HailoRT 4.23) with two configured models in one VDevice.
  - SCRFD 2.5g: letterbox the frame to the HEF input size (read it from the HEF). Decode the strides 8/16/32 anchors (score, bbox, kps), then NMS. Port the decode logic from the hailo-apps reference or the InsightFace SCRFD reference, and keep it in `scripts/hailo_faces.py`.
  - ArcFace: use the 5-point similarity transform to 112×112 (standard ArcFace template) with `cv2.estimateAffinePartial2D` + `warpAffine`, then L2-normalize the output.
  - Only embed when the identity logic needs it, as today (check the current call frequency), to keep the Hailo queue short.
- **Thresholds:** ArcFace cosine distributions differ from SFace's. `scripts/track_human.py` defaults `--threshold .55` for SFace. Add a per-backend default, and calibrate it with the existing calibration flow (e.g. `~/calibrate_face_threshold.py` / `face_calibration.npz` if they're relevant; check them first). Record the chosen value.
- **Device sharing:** `hailort.service` is already **enabled** on this Pi (checked 2026-11-02). Confirm it is active with `systemctl status hailort.service`, so several processes can use the device. Document the requirement in the README. If the service isn't running, stop and report.
- **Fallback:** if `hailo_platform` can't be imported, the device is busy, or the HEF fails to load, log one line and use `OpenCVFaceBackend`. Tracking must never fail because of Hailo.

## Files to touch
- `scripts/human_tracker.py`: the backend interface; current code moves into `OpenCVFaceBackend`.
- `scripts/hailo_faces.py`: new `HailoFaceBackend`, SCRFD decode, ArcFace align.
- `scripts/track_human.py`: the `--tracker-backend` flag, per-backend threshold default, and passing the choice through.
- `scripts/setup_tracking.py`, `requirements-tracking.txt`, `track.sh`: install the hailort wheel from `~/hailo-debs` into the tracking venv when `hailortcli` is present. Make it optional, with a clear message.
- `scripts/chat.py` `start_tracking()`: pass `DOTS_TRACKER_BACKEND`.
- `README.md`, `docs/human-tracking.md`: the Hailo backend, the service requirement, and the threshold.
- Tests: `test/test_human_tracker.py`, `test/test_tracking_models.py`.

## Tasks
1. Install the wheel into the tracking venv and check that `hailo_platform` imports there. Run a one-shot benchmark script that loads both HEFs and times 100 inferences of each.
2. Refactor into the backend interface, with the OpenCV backend only. Existing tests must pass unchanged.
3. Implement the SCRFD decode and the ArcFace align, plus unit tests on fixed synthetic tensors (no device required).
4. Wire `HailoFaceBackend`, then do the fallback and the flag.
5. Calibrate the threshold. Benchmark against BASELINE.

## Tests
- Unit (no device): the SCRFD decoder on a hand-built output tensor gives the expected boxes and landmarks after NMS. The alignment maps the template landmarks onto themselves. Cosine matching holds for identical and orthogonal vectors.
- Backend selection: an import error → the OpenCV backend plus a single log line.
- Existing `test_human_tracker.py` behavior tests pass with the OpenCV backend and with a fake Hailo backend that returns the same faces.

## Acceptance checks
- [ ] Full suite green.
- [ ] `./track.sh --stats` on the Hailo backend: detection FPS ≥ the BASELINE FPS, and tracker CPU reduced by **≥ 50%** compared with BASELINE. Record both.
- [ ] Identity lock works across 3 people at the calibrated threshold, with no identity swaps in a 5 min session.
- [ ] Running the tracker and the `hailortcli` benchmark at the same time works (proves sharing through the service).
- [ ] With the HAT unavailable (`systemctl stop hailort.service`, or a renamed HEF), tracking falls back to OpenCV and still works.

## Rollback
`--tracker-backend opencv` / `DOTS_TRACKER_BACKEND=opencv`.

## Do not
- Don't change the speaker selection, DOA fusion or gaze logic.
- Don't import from `~/hailo-apps` at runtime. Copy only the minimal post-processing, with attribution if its license requires it.
- Don't make the Hailo backend a hard requirement.

## Results
Done 2026-11-02 on branch `voice/phase-6-hailo-tracking`.

**Facts re-checked on this Pi**
- HAILO8L, firmware 4.23.0, `hailort.service` active.
- The `hailort` 4.23.0 wheel is now installed in the tracking venv; it brings only netaddr, netifaces, future and contextlib2, and numpy 1.26.4 / OpenCV 4.11 are unchanged.
- Both HEFs are compiled for HAILO8L:
  - SCRFD: 640×640×3 input; per stride 8/16/32 it outputs boxes (8 ch = 2 anchors × 4), scores (2) and landmarks (20). The layer mapping matches Hailo's postprocess: boxes `conv43/50/56`, scores `conv42/49/55`, landmarks `conv44/51/57`.
  - ArcFace: 112×112×3 input, 512 output.
- The hailo-apps SCRFD postprocess is LGPL-licensed (the repo itself is MIT). It was read for semantics only. `scripts/hailo_faces.py` implements the public SCRFD anchor decode itself, and nothing is imported from `~/hailo-apps`.
- `~/calibrate_face_threshold.py` / `face_calibration.npz` belong to a different ONNX embedding model, so they don't apply to this HEF.

**Implementation**
- `human_tracker.Vision` is now `OpenCVFaceBackend` (unchanged code, plus `name`/`close`). `scripts/hailo_faces.py` adds `HailoFaceBackend`: letterbox → SCRFD → decode/NMS → YuNet-format rows; 5-point similarity alignment → ArcFace → L2-normalized vector.
- Selection: `--tracker-backend auto|hailo|opencv` / `DOTS_TRACKER_BACKEND` (default `auto`). It reaches chat-started tracking through the inherited environment, so `chat.start_tracking()` needed no change.
- Per-backend threshold: OpenCV .55, Hailo .45.
- `track.sh` installs the matching hailort wheel from `~/hailo-debs` (`HAILO_WHEEL_DIR`) when `hailortcli` is present.
- `cv2.setNumThreads(--threads)` now applies to both backends. On the Hailo path, OpenCV had defaulted to 4 threads.
- **Robustness:**
  - At startup, a missing binding, HEF (`DOTS_HAILO_FACE_DETECTOR/RECOGNIZER`) or service → OpenCV, with one log line.
  - Mid-session, a HailoRT error → close, wait 1.5 s, reconnect and retry once. If that fails, `HailoUnavailable` → the tracker swaps to OpenCV for the session and relocks, since features aren't comparable.

**Checks**
- [x] Full suites green: `.venv` 216 (the only errors are the cv2-only files); tracking venv `test_human_tracker` OK, `test_tracking_models` OK (1 skip), `test_hailo_faces` 12 OK. The Hailo tests need no device:
  - decode of hand-built tensors including NMS;
  - row-major anchor order;
  - letterbox and region mapping;
  - template alignment is the identity;
  - cosine;
  - real `detect()`/`feature()` paths with fake tensors driving a `PersonTracker` lock;
  - fallback with one line;
  - reconnect then success, reconnect then `HailoUnavailable`;
  - mid-session swap.
- Decode validated on Hailo's 9 sample face photos against YuNet: box IoU 0.81–0.96, landmarks within 1.4–5% of face width. Detection doesn't depend on RGB/BGR. ArcFace separates identities best with **RGB** input.
- [~] CPU and FPS. `scripts/tracker_bench.py`, 60 s of `face_recognition.mp4` at 15 fps with the live JPEG decode included; detector and identity call counts identical (83/49):

  | Per frame | OpenCV | Hailo |
  | --- | --- | --- |
  | Total tracker CPU | 8.44 ms | **5.14 ms (−39%)** |
  | Face-model work (total minus the 4.84 ms of decode/resize/gray) | 3.60 ms | **0.30 ms (−92%)** |

  Per call: full-frame detection 20 ms CPU (YuNet) vs 11.5 ms (Hailo, 21 ms wall); identity 67 ms (SFace) vs **3.7 ms** (ArcFace); region re-detection 3.3 ms vs 13 ms.
  - **The −50% total target isn't met:** more than half of the tracker's CPU is decoding and resizing frames, which no face backend touches.
  - Live, awake with nobody in view: about 13% of a core, the same as the BASELINE 13.7%. That load is decode-dominated, and this robot uses the RGB-D profile, which decodes 1280×720 JPEGs.
- [ ] Identity lock across 3 people with no swaps in 5 min at .45. **Pending the owner** (needs people). On the sample photos: same person ≥ .937, different ≤ .040.
- [~] Sharing the 8L between processes works (a concurrent benchmark ran at full speed alongside the tracker), **but HailoRT 4.23's `hailort.service` crashed (SEGV/ABRT, auto-restarted by systemd) four times today**, each time when *another* client process tore down its session.
  - The first run crashed the tracker (uncaught `HAILO_RPC_FAILED`).
  - After adding reconnect/fallback, the same scenario left the tracker running on Hailo (same PID).
- [x] HAT unavailable (`DOTS_HAILO_FACE_DETECTOR=/nonexistent.hef ./track.sh --check`, since stopping `hailort.service` needs root): one line, `"backend": "opencv"`, threshold .55, tracking works.

## Open issues
- **`hailort.service` 4.23 crashes when a sharing client exits.** Check this against the next HailoRT release. Until then, phase 7 should keep a single long-lived Hailo client per process and tolerate service restarts.
- The biggest remaining tracker CPU saving is outside the face backend: decode JPEGs at reduced resolution (`cv2.IMREAD_REDUCED_COLOR_2/4`), since the tracker works at 320 px. That has to be checked against identity accuracy, because features are taken from the full-resolution frame. Not changed (tracking logic out of scope).
- Region re-detection is cheaper on the CPU (YuNet 3.3 ms vs SCRFD 13 ms). A hybrid (CPU for small regions) could save about 3% of a core while a person is tracked.
- UINT8 outputs would save about 1.6 ms of CPU per detection (measured). Not done.
