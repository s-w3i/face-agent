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
_(Fill in.)_

## Open issues
_(Fill in.)_
