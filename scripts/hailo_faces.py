"""Face detection (SCRFD 2.5g) and identity embedding (ArcFace MobileFaceNet) on the Hailo-8L.

Drop-in for human_tracker.Vision: `detect()` returns YuNet-format rows
[x, y, w, h, 5 landmark (x, y) pairs, score] and `feature()` an L2-normalized
embedding compared by cosine. The decode follows the public SCRFD anchor scheme
(InsightFace, MIT): per stride 8/16/32, two anchors per grid cell, boxes as
distances and landmarks as offsets from the cell centre in stride units. It is
written here from that description; nothing is imported from hailo-apps.

The VDevice joins the shared group through hailort.service, so other processes
(e.g. an offline STT model) can use the 8L at the same time.
"""
import os
from pathlib import Path
import time

import cv2
import numpy as np

MODEL_DIR = Path('/usr/local/hailo/resources/models/hailo8l')
DETECTOR_HEF = Path(os.environ.get('DOTS_HAILO_FACE_DETECTOR', MODEL_DIR / 'scrfd_2.5g.hef'))
RECOGNIZER_HEF = Path(os.environ.get('DOTS_HAILO_FACE_RECOGNIZER', MODEL_DIR / 'arcface_mobilefacenet.hef'))
INPUT_SIZE = 640
STRIDES = (8, 16, 32)
ANCHORS_PER_CELL = 2
# HEF output layers per stride: (box distances, scores, landmark offsets)
OUTPUTS = {8: ('conv43', 'conv42', 'conv44'), 16: ('conv50', 'conv49', 'conv51'), 32: ('conv56', 'conv55', 'conv57')}
# Standard ArcFace 112x112 destination for eyes, nose tip and mouth corners.
ARCFACE_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                             [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)


def anchor_centers(size=INPUT_SIZE):
    """{stride: (cells*2, 2) array of (x, y) pixel centres}, row-major like the output tensors."""
    centers = {}
    for stride in STRIDES:
        cells = size // stride
        ys, xs = np.mgrid[:cells, :cells]
        grid = np.stack([xs, ys], axis=-1).reshape(-1, 2).astype(np.float32) * stride
        centers[stride] = np.repeat(grid, ANCHORS_PER_CELL, axis=0)
    return centers


CENTERS = anchor_centers()


def nms(boxes, scores, iou_threshold):
    order = np.argsort(-scores)
    keep = []
    while order.size:
        best = order[0]
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        x1 = np.maximum(boxes[best, 0], boxes[rest, 0]); y1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[best, 2], boxes[rest, 2]); y2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
        iou = inter / np.maximum(area(boxes[best]) + area(boxes[rest]) - inter, 1e-9)
        order = rest[iou <= iou_threshold]
    return np.array(keep, dtype=int)


def decode(outputs, score_threshold=.5, iou_threshold=.4, prefix='scrfd_2_5g/'):
    """Network outputs (float, HWC per layer name) -> (boxes x1y1x2y2, scores, landmarks Nx5x2) in input pixels."""
    boxes, scores, landmarks = [], [], []
    for stride in STRIDES:
        box_name, score_name, landmark_name = (prefix + name for name in OUTPUTS[stride])
        score = np.asarray(outputs[score_name], np.float32).reshape(-1)
        hits = np.flatnonzero(score >= score_threshold)
        if not hits.size:
            continue
        centers = CENTERS[stride][hits]
        distance = np.asarray(outputs[box_name], np.float32).reshape(-1, 4)[hits] * stride
        offset = np.asarray(outputs[landmark_name], np.float32).reshape(-1, 10)[hits] * stride
        boxes.append(np.concatenate([centers - distance[:, :2], centers + distance[:, 2:]], axis=1))
        landmarks.append(offset.reshape(-1, 5, 2) + centers[:, None, :])
        scores.append(score[hits])
    if not scores:
        return np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros((0, 5, 2), np.float32)
    boxes = np.concatenate(boxes); scores = np.concatenate(scores); landmarks = np.concatenate(landmarks)
    keep = nms(boxes, scores, iou_threshold)
    return boxes[keep], scores[keep], landmarks[keep]


def letterbox(image, size=INPUT_SIZE):
    """Scale to fit, pad right/bottom (so input coords map back with a single division). Returns (RGB, scale)."""
    height, width = image.shape[:2]
    scale = min(size / width, size / height)
    resized = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                         interpolation=cv2.INTER_LINEAR if scale > 1 else cv2.INTER_AREA)
    canvas = np.zeros((size, size, 3), np.uint8)
    canvas[:resized.shape[0], :resized.shape[1]] = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    return canvas, scale


def yunet_rows(boxes, scores, landmarks, scale, offset=(0, 0)):
    """Same row layout as cv2.FaceDetectorYN: x, y, w, h, 10 landmark coords, score."""
    rows = []
    for box, score, points in zip(boxes / scale, scores, landmarks / scale):
        row = np.empty(15, np.float32)
        row[:4] = box[0], box[1], box[2] - box[0], box[3] - box[1]
        row[4:14] = points.reshape(-1)
        row[14] = score
        row[[0, 4, 6, 8, 10, 12]] += offset[0]
        row[[1, 5, 7, 9, 11, 13]] += offset[1]
        rows.append(row)
    return rows


def align(image, points):
    """Five landmarks -> 112x112 crop in the ArcFace template pose (similarity transform)."""
    matrix, _ = cv2.estimateAffinePartial2D(np.asarray(points, np.float32).reshape(5, 2), ARCFACE_TEMPLATE,
                                            method=cv2.LMEDS)
    if matrix is None:
        raise ValueError('Face landmarks are degenerate.')
    return cv2.warpAffine(image, matrix, (112, 112), borderValue=0)


class HailoUnavailable(RuntimeError):
    """The Hailo device or hailort.service failed and a reconnect did not help."""


class HailoFaceBackend:
    """Vision-compatible backend on the Hailo-8L."""

    name = 'hailo'
    RECONNECT_DELAY = 1.5  # systemd restarts a crashed hailort.service in about a second.

    def __init__(self, detector_hef=DETECTOR_HEF, recognizer_hef=RECOGNIZER_HEF, score_threshold=.5):
        for path in (detector_hef, recognizer_hef):
            if not Path(path).is_file():
                raise FileNotFoundError(f'Missing Hailo model {path}')
        self.paths = (detector_hef, recognizer_hef)
        self.score_threshold = score_threshold
        self.detections = self.recognitions = self.reconnects = 0
        self.device = None
        self._contexts = []
        self._open()

    def _open(self):
        from hailo_platform import FormatType, HailoSchedulingAlgorithm, VDevice
        detector_hef, recognizer_hef = self.paths
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        params.group_id = 'SHARED'
        params.multi_process_service = True  # Needs hailort.service; lets other processes share the 8L.
        self.device = VDevice(params)
        self.models = {}
        self._contexts = []
        try:
            for key, path in (('detector', detector_hef), ('recognizer', recognizer_hef)):
                model = self.device.create_infer_model(str(path))
                model.set_batch_size(1)
                for output in model.outputs:
                    output.set_format_type(FormatType.FLOAT32)
                context = model.configure()
                self._contexts.append(context)
                self.models[key] = (model, context.__enter__())
        except BaseException:
            self.close()
            raise

    def _run(self, key, frame):
        """Infer once; after a HailoRT failure, reconnect once and retry before giving up."""
        try:
            return self._infer(key, frame)
        except Exception as error:
            first = error
        self.close()
        time.sleep(self.RECONNECT_DELAY)
        try:
            self._open()
            self.reconnects += 1
            return self._infer(key, frame)
        except Exception as error:
            self.close()
            raise HailoUnavailable(f'{type(first).__name__}: {first}; reconnect failed: {error}') from error

    def _infer(self, key, frame):
        model, configured = self.models[key]
        outputs = {output.name: np.empty(output.shape, np.float32) for output in model.outputs}
        bindings = configured.create_bindings(output_buffers=outputs)
        bindings.input().set_buffer(np.ascontiguousarray(frame))
        configured.run([bindings], 5000)
        return outputs

    def detect(self, image, region=None):
        x, y, w, h = region if region is not None else (0, 0, image.shape[1], image.shape[0])
        crop = image[y:y + h, x:x + w]
        frame, scale = letterbox(crop)
        self.detections += 1
        boxes, scores, landmarks = decode(self._run('detector', frame), self.score_threshold)
        result = []
        for row in yunet_rows(boxes, scores, landmarks, scale, (x, y)):
            # Same acceptance rules as the OpenCV backend.
            if not np.isfinite(row).all() or min(row[2:4]) < 20:
                continue
            if not (x <= row[0] + row[2] / 2 < x + w and y <= row[1] + row[3] / 2 < y + h):
                continue
            result.append(row)
        return result

    def feature(self, image, face, scale):
        points = face[4:14].reshape(5, 2) / scale
        aligned = cv2.cvtColor(align(image, points), cv2.COLOR_BGR2RGB)
        self.recognitions += 1
        embedding = next(iter(self._run('recognizer', aligned).values())).reshape(1, -1)
        return embedding / max(1e-9, float(np.linalg.norm(embedding)))

    def close(self):
        for context in reversed(self._contexts):
            try:
                context.__exit__(None, None, None)
            except Exception:
                pass
        self._contexts = []
        device, self.device = getattr(self, 'device', None), None
        if device is not None:
            try:
                device.release()
            except Exception:
                pass
