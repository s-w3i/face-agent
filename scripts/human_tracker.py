"""Small-frame optical flow with a fixed, in-memory identity for each wake session."""
import math

import cv2
import numpy as np


def overlap(first, second):
    ax, ay, aw, ah = first[:4]; bx, by, bw, bh = second[:4]
    area = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return float(area / max(1, aw * ah + bw * bh - area))


def cosine(first, second):
    return float(np.dot(first.ravel(), second.ravel()) /
                 max(1e-9, np.linalg.norm(first) * np.linalg.norm(second)))


class Vision:
    def __init__(self, detector_path, recognizer_path, threads=1):
        cv2.setNumThreads(threads)
        self.detector = cv2.FaceDetectorYN.create(str(detector_path), '', (320, 240), .8, .3, 500,
                                                cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), '',
                                                    cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        self.detections = self.recognitions = 0

    def detect(self, image, region=None):
        x, y, w, h = region if region is not None else (0, 0, image.shape[1], image.shape[0])
        crop = image[y:y + h, x:x + w]
        # YuNet's stride-32 outputs need padding for small, changing face regions.
        width = max(64, math.ceil(w / 32) * 32); height = max(64, math.ceil(h / 32) * 32)
        crop = cv2.copyMakeBorder(crop, 0, height - h, 0, width - w, cv2.BORDER_CONSTANT)
        self.detector.setInputSize((width, height))
        self.detections += 1
        _, rows = self.detector.detect(crop)
        result = []
        for row in rows if rows is not None else []:
            row = row.copy()
            if not np.isfinite(row).all() or min(row[2:4]) < 20:
                continue
            if not (0 <= row[0] + row[2] / 2 < w and 0 <= row[1] + row[3] / 2 < h):
                continue
            row[[0, 4, 6, 8, 10, 12]] += x
            row[[1, 5, 7, 9, 11, 13]] += y
            result.append(row)
        return result

    def feature(self, image, face, scale):
        face = face.copy(); face[:14] /= scale
        aligned = self.recognizer.alignCrop(image, face)
        self.recognitions += 1
        return self.recognizer.feature(aligned).copy()


class PersonTracker:
    def __init__(self, vision, threshold=.55, mirror=False):
        self.vision = vision; self.threshold = threshold; self.mirror = mirror
        self.session = None; self.awake = False
        self.input_mode = 'words'; self.speech_hint = None
        self.mic_forward = 0.; self.mic_clockwise = False
        self.intrinsics = None
        self.reset()

    def reset(self):
        self.identity = None; self.pending = None; self.confirmations = 0
        self.voice_turn = None
        self.face = self.points = self.gray = None
        self.last_frame = self.last_verified = -math.inf
        self.next_detection = 0

    def set_session(self, session, awake):
        if session != self.session or awake != self.awake:
            self.reset()
        self.session = session; self.awake = awake

    def set_input(self, mode, hint=None, forward=0, clockwise=False):
        if mode != self.input_mode:
            self.reset()
        self.input_mode = mode; self.speech_hint = hint
        self.mic_forward = forward; self.mic_clockwise = clockwise

    def set_camera_info(self, width, fx, cx):
        if not all(math.isfinite(value) for value in (width, fx, cx)) or width <= 0 or fx <= 0 or not 0 <= cx < width:
            raise ValueError('Invalid color-camera intrinsics.')
        self.intrinsics = width, fx, cx

    def speech_face(self, faces, width):
        hint = self.speech_hint
        if not self.intrinsics or not hint or hint.get('session') != self.session or not 0 <= hint.get('ageMs', math.inf) < 2000:
            return None
        bearing = (hint['doaDeg'] - self.mic_forward + 180) % 360 - 180
        bearing *= 1 if self.mic_clockwise else -1  # Camera X increases to its right.
        if abs(bearing) >= 90:
            return None  # A rear source cannot select a face in the front camera.
        camera_width, fx, cx = self.intrinsics
        scale = width / camera_width
        ranked = sorted((abs(math.degrees(math.atan((face[0] + face[2] / 2 - cx * scale) / (fx * scale))) - bearing), index)
                        for index, face in enumerate(faces))
        if not ranked or ranked[0][0] > 20 or len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 5:
            return None  # Close angular matches need more evidence than a DOA hint.
        return faces[ranked[0][1]]

    @property
    def state(self):
        if not self.awake:
            return 'sleeping'
        if self.identity is None:
            return 'acquiring'
        return 'tracking' if self.face is not None else 'waiting-for-locked-person'

    def lost(self):
        self.face = self.points = self.gray = None
        self.pending = None; self.confirmations = 0
        # Never replace the wake-session identity when flow or visibility fails.

    def stale(self, now):
        if now - self.last_frame > .5:
            self.lost()

    def region(self, shape):
        x, y, w, h = self.face[:4]
        x0, y0 = max(0, int(x - w * .6)), max(0, int(y - h * .6))
        x1, y1 = min(shape[1], int(x + w * 1.6)), min(shape[0], int(y + h * 1.6))
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)

    def seed(self, gray, face):
        self.face = face.copy(); self.gray = gray
        mask = np.zeros_like(gray)
        x, y, w, h = face[:4]
        x0, y0 = max(0, int(x + w * .15)), max(0, int(y + h * .15))
        x1, y1 = min(gray.shape[1], int(x + w * .85)), min(gray.shape[0], int(y + h * .9))
        mask[y0:y1, x0:x1] = 255
        self.points = cv2.goodFeaturesToTrack(gray, 32, .01, 3, mask=mask, blockSize=3)

    def flow(self, gray):
        if self.points is None or len(self.points) < 8 or self.gray is None:
            return False
        params = dict(winSize=(15, 15), maxLevel=2,
                      criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 12, .03))
        forward, status, _ = cv2.calcOpticalFlowPyrLK(self.gray, gray, self.points, None, **params)
        if forward is None:
            return False
        backward, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray, forward, None, **params)
        if backward is None:
            return False
        old = self.points.reshape(-1, 2); new = forward.reshape(-1, 2)
        good = (status.ravel() == 1) & (back_status.ravel() == 1)
        good &= np.linalg.norm(backward.reshape(-1, 2) - old, axis=1) < 1.2
        good &= np.isfinite(new).all(axis=1)
        if good.sum() < max(8, len(old) * .6):
            return False
        matrix, inliers = cv2.estimateAffinePartial2D(old[good], new[good], method=cv2.RANSAC,
                                                     ransacReprojThreshold=1.5, maxIters=40, confidence=.95)
        if matrix is None or inliers.sum() < max(8, good.sum() * .65):
            return False
        scale = math.hypot(matrix[0, 0], matrix[0, 1])
        x, y, w, h = self.face[:4]
        center = np.array([x + w / 2, y + h / 2])
        moved = matrix[:, :2] @ center + matrix[:, 2]
        if not .85 <= scale <= 1.18 or np.linalg.norm(moved - center) > max(w, h) * .5:
            return False
        if not (0 <= moved[0] < gray.shape[1] and 0 <= moved[1] < gray.shape[0]):
            return False
        self.face[:4] = (moved[0] - w * scale / 2, moved[1] - h * scale / 2, w * scale, h * scale)
        self.points = new[good][inliers.ravel() == 1].reshape(-1, 1, 2)
        self.gray = gray
        return True

    def match(self, image, faces, scale):
        # Bound the costly work in crowds; ambiguity keeps the original eyes.
        if not faces or len(faces) > 3:
            return None
        scores = [(cosine(self.identity, self.vision.feature(image, face, scale)), face) for face in faces]
        scores.sort(key=lambda item: item[0], reverse=True)
        if scores[0][0] < self.threshold or len(scores) > 1 and scores[0][0] - scores[1][0] < .1:
            return None
        target = scores[0][1]
        if any(overlap(target, face) > .15 for _, face in scores[1:]):
            return None
        return target

    def acquire(self, image, small, gray, scale, now):
        if self.pending is not None and self.pending[1] is None:
            self.pending = None; self.confirmations = 0
        faces = self.vision.detect(small)
        self.next_detection = now + 1 / 3
        if not faces:
            self.pending = None; self.confirmations = 0
            return
        if self.input_mode == 'voice':
            face = self.speech_face(faces, small.shape[1])
            if face is None:
                self.pending = None; self.confirmations = 0
                return
            if self.pending is not None and overlap(face, self.pending[0]) < .2:
                self.pending = None; self.confirmations = 0
                return
        elif self.pending is None:
            # First confirmed face wins; simultaneous arrivals use the camera center.
            face = min(faces, key=lambda row: abs(row[0] + row[2] / 2 - small.shape[1] / 2))
        else:
            face = max(faces, key=lambda row: overlap(row, self.pending[0]))
            if overlap(face, self.pending[0]) < .2:
                self.pending = None; self.confirmations = 0
                return
        if any(overlap(face, other) > .15 for other in faces if other is not face):
            self.pending = None; self.confirmations = 0
            return
        feature = self.vision.feature(image, face, scale)
        if self.pending is not None and cosine(self.pending[1], feature) < self.threshold:
            self.pending = None; self.confirmations = 0
            return
        reference = self.pending[1] if self.pending is not None else feature
        self.pending = (face.copy(), reference); self.confirmations += 1
        if self.confirmations >= 3:
            self.identity = reference.copy()
            self.pending = None; self.confirmations = 0
            self.seed(gray, face)
            self.last_verified = now
            if self.input_mode == 'voice':
                self.voice_turn = self.speech_hint.get('utterance', 0)

    def recover(self, image, small, gray, scale, now):
        self.next_detection = now + 1
        face = self.match(image, self.vision.detect(small), scale)
        if face is None:
            self.pending = None; self.confirmations = 0
            return
        if self.pending is not None and overlap(face, self.pending[0]) >= .2:
            self.confirmations += 1
        else:
            self.confirmations = 1
        self.pending = (face.copy(), None)
        if self.confirmations >= 2:
            self.pending = None; self.confirmations = 0
            self.seed(gray, face)
            self.last_verified = now

    def process(self, image, now):
        if not self.awake:
            return dict(detected=False)
        scale = min(1, 320 / image.shape[1])
        small = cv2.resize(image, (round(image.shape[1] * scale), round(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if now - self.last_frame > .35 or self.gray is not None and self.gray.shape != gray.shape:
            # Keep identity, but don't extrapolate across dropped frames or resolution changes.
            if self.face is not None:
                self.lost()
        self.last_frame = now
        voice_selection = (self.input_mode == 'voice' and self.intrinsics and self.speech_hint and
                           self.speech_hint.get('session') == self.session and
                           0 <= self.speech_hint.get('ageMs', math.inf) < 2000 and
                           self.speech_hint.get('utterance', 0) != self.voice_turn and now >= self.next_detection)
        if self.identity is None:
            if now >= self.next_detection:
                self.acquire(image, small, gray, scale, now)
        elif self.face is None:
            if now >= self.next_detection:
                if voice_selection:
                    self.acquire(image, small, gray, scale, now)
                else:
                    self.recover(image, small, gray, scale, now)
        else:
            if not self.flow(gray):
                self.lost(); self.next_detection = now
                return dict(detected=False)
            if now >= self.next_detection:
                self.next_detection = now + 1 / 3
                faces = self.vision.detect(small, self.region(gray.shape))
                nearest = max(faces, key=lambda row: overlap(row, self.face)) if faces else None
                if nearest is None or overlap(nearest, self.face) < .2:
                    self.lost(); return dict(detected=False)
                if len(faces) > 1 or now - self.last_verified >= 2:
                    verified = self.match(image, faces, scale)
                    if verified is None or overlap(verified, self.face) < .2:
                        self.lost(); return dict(detected=False)
                    nearest = verified; self.last_verified = now
                corrected = nearest.copy()
                corrected[:4] = self.face[:4] * .7 + nearest[:4] * .3
                self.seed(gray, corrected)
            if voice_selection:
                # A new spoken turn can replace the lock only after three identity confirmations.
                self.acquire(image, small, gray, scale, now)
        if self.face is None:
            return dict(detected=False)
        x, y, w, h = self.face[:4]
        x = float(np.clip((x + w / 2) / small.shape[1], 0, 1))
        y = float(np.clip((y + h / 2) / small.shape[0], 0, 1))
        return dict(detected=True, x=1 - x if self.mirror else x, y=y)
