"""SCRFD decode, ArcFace alignment and backend selection without a Hailo device."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import cv2  # noqa: F401  (tracking venv)
except ImportError:  # pragma: no cover
    cv2 = None


def synthetic_outputs(faces, prefix='scrfd_2_5g/'):
    """Build SCRFD output tensors (stride 8) holding `faces`: (cx, cy, w, h, score) in 640-input pixels."""
    import hailo_faces as hf
    outputs = {}
    for stride, (box, score, landmark) in hf.OUTPUTS.items():
        cells = hf.INPUT_SIZE // stride
        outputs[prefix + box] = np.zeros((cells, cells, 8), np.float32)
        outputs[prefix + score] = np.zeros((cells, cells, 2), np.float32)
        outputs[prefix + landmark] = np.zeros((cells, cells, 20), np.float32)
    for cx, cy, w, h, value in faces:
        stride = 8
        box, score, landmark = (prefix + name for name in hf.OUTPUTS[stride])
        col, row = round(cx / stride), round(cy / stride)
        ax, ay = col * stride, row * stride
        outputs[score][row, col, 0] = value
        outputs[box][row, col, :4] = np.array([ax - (cx - w / 2), ay - (cy - h / 2), cx + w / 2 - ax, cy + h / 2 - ay]) / stride
        points = np.array([[cx - w * .2, cy - h * .1], [cx + w * .2, cy - h * .1], [cx, cy + h * .05],
                           [cx - w * .15, cy + h * .25], [cx + w * .15, cy + h * .25]])
        outputs[landmark][row, col, :10] = ((points - [ax, ay]) / stride).reshape(-1)
    return outputs


@unittest.skipIf(cv2 is None, 'Needs OpenCV (tracking venv)')
class DecodeTest(unittest.TestCase):
    def test_decode_recovers_boxes_landmarks_and_suppresses_duplicates(self):
        import hailo_faces as hf
        outputs = synthetic_outputs([(200, 160, 80, 100, .9), (204, 160, 80, 100, .7), (480, 320, 60, 60, .8),
                                     (100, 500, 40, 40, .2)])
        boxes, scores, landmarks = hf.decode(outputs, score_threshold=.5, iou_threshold=.4)
        self.assertEqual(len(boxes), 2)  # The overlapping .7 duplicate is suppressed; .2 is below threshold.
        order = np.argsort(-scores)
        np.testing.assert_allclose(boxes[order[0]], [160, 110, 240, 210], atol=1e-4)
        np.testing.assert_allclose(boxes[order[1]], [450, 290, 510, 350], atol=1e-4)
        np.testing.assert_allclose(landmarks[order[0]][2], [200, 165], atol=1e-4)

    def test_anchor_order_matches_row_major_two_anchors_per_cell(self):
        import hailo_faces as hf
        centers = hf.CENTERS[8]
        self.assertEqual(centers.shape, (80 * 80 * 2, 2))
        np.testing.assert_array_equal(centers[2 * (3 * 80 + 5)], [40, 24])  # row 3, col 5
        np.testing.assert_array_equal(centers[2 * (3 * 80 + 5) + 1], [40, 24])

    def test_yunet_rows_map_back_through_letterbox_and_region(self):
        import hailo_faces as hf
        boxes = np.array([[100, 50, 180, 150]], np.float32)
        landmarks = np.full((1, 5, 2), 120, np.float32)
        row = hf.yunet_rows(boxes, np.array([.9], np.float32), landmarks, 2.0, offset=(10, 20))[0]
        np.testing.assert_allclose(row[:4], [60, 45, 40, 50])
        np.testing.assert_allclose(row[4:6], [70, 80])
        self.assertAlmostEqual(float(row[14]), .9, places=6)

    def test_letterbox_keeps_aspect_and_converts_to_rgb(self):
        import hailo_faces as hf
        image = np.zeros((240, 320, 3), np.uint8); image[..., 0] = 255  # pure blue in BGR
        frame, scale = hf.letterbox(image)
        self.assertEqual(frame.shape, (640, 640, 3)); self.assertEqual(scale, 2)
        self.assertEqual(frame[0, 0].tolist(), [0, 0, 255])
        self.assertEqual(frame[600, 0].tolist(), [0, 0, 0])  # padding below the image

    def test_alignment_maps_template_landmarks_onto_themselves(self):
        import hailo_faces as hf
        image = np.random.default_rng(0).integers(0, 255, (112, 112, 3)).astype(np.uint8)
        aligned = hf.align(image, hf.ARCFACE_TEMPLATE)
        self.assertLess(np.abs(aligned.astype(int) - image.astype(int))[8:104, 8:104].mean(), 1)
        with self.assertRaises(ValueError):
            hf.align(image, np.zeros((5, 2)))

    def test_cosine_for_identical_and_orthogonal_embeddings(self):
        from human_tracker import cosine
        a = np.eye(512, dtype=np.float32)[0]; b = np.eye(512, dtype=np.float32)[1]
        self.assertAlmostEqual(cosine(a, a), 1); self.assertAlmostEqual(cosine(a, b), 0)


class FakeHailo:
    """HailoFaceBackend with the device replaced by synthetic tensors."""

    def __new__(cls, faces):
        import hailo_faces as hf
        backend = object.__new__(hf.HailoFaceBackend)
        backend.score_threshold = .5; backend.detections = backend.recognitions = 0
        backend.default_threshold = .45
        def run(key, frame):
            if key == 'detector':
                return synthetic_outputs(backend.faces)
            embedding = np.zeros(512, np.float32); embedding[int(frame.mean()) % 512] = 1  # depends on the crop
            return {'arcface_mobilefacenet/fc1': embedding}
        backend._run = run
        backend.faces = faces
        backend._contexts = []; backend.device = None
        return backend


@unittest.skipIf(cv2 is None, 'Needs OpenCV (tracking venv)')
class BackendTest(unittest.TestCase):
    def test_hailo_backend_feeds_the_tracker_like_opencv(self):
        from human_tracker import PersonTracker
        backend = FakeHailo([(320, 240, 120, 160, .9)])  # in 640-input pixels: small-frame centre
        tracker = PersonTracker(backend, threshold=backend.default_threshold)
        tracker.set_session(1, True)
        image = np.full((240, 320, 3), 90, np.uint8)
        rows = backend.detect(image)
        self.assertEqual(len(rows), 1)
        np.testing.assert_allclose(rows[0][:4], [130, 80, 60, 80], atol=1e-3)  # 640 -> 320 scale
        feature = backend.feature(image, rows[0], 1)
        self.assertAlmostEqual(float(np.linalg.norm(feature)), 1, places=5)
        for step in range(4):
            tracker.process(image, step / 3 + .01)
        self.assertIsNotNone(tracker.identity)  # Three confirmations lock the person, as with SFace.

    def test_region_detection_keeps_faces_inside_the_region(self):
        backend = FakeHailo([(320, 320, 160, 160, .9)])
        image = np.zeros((240, 320, 3), np.uint8)
        rows = backend.detect(image, (40, 40, 160, 160))  # region upscaled 4x into the 640 input
        self.assertEqual(len(rows), 1)
        self.assertTrue(40 <= rows[0][0] + rows[0][2] / 2 < 200)

    def offline_backend(self, failures):
        import hailo_faces as hf
        backend = object.__new__(hf.HailoFaceBackend)
        backend.reconnects = 0; backend.device = None; backend._contexts = []
        backend.RECONNECT_DELAY = 0
        calls = dict(infer=0, open=0)
        def infer(key, frame):
            calls['infer'] += 1
            if calls['infer'] in failures:
                raise RuntimeError('libhailort failed with error: 77 (HAILO_RPC_FAILED)')
            return {'ok': key}
        def reopen():
            calls['open'] += 1
        backend._infer = infer; backend._open = reopen
        return backend, calls

    def test_service_crash_reconnects_once_and_retries(self):
        backend, calls = self.offline_backend({1})
        self.assertEqual(backend._run('detector', None), {'ok': 'detector'})
        self.assertEqual((calls['open'], backend.reconnects), (1, 1))

    def test_failed_reconnect_raises_hailo_unavailable(self):
        import hailo_faces as hf
        backend, calls = self.offline_backend({1, 2})
        with self.assertRaises(hf.HailoUnavailable):
            backend._run('detector', None)

    def test_mid_session_fallback_swaps_backend_and_relocks(self):
        import human_tracker
        from human_tracker import PersonTracker
        tracker = PersonTracker(FakeHailo([(320, 240, 120, 160, .9)]), threshold=.45)
        tracker.identity = np.ones((1, 512), np.float32)
        with patch.object(human_tracker, 'Vision') as vision:
            replacement = human_tracker.fall_back_to_cpu(tracker, ['det', 'rec'], 1)
        self.assertIs(replacement, vision.return_value)
        self.assertIs(tracker.vision, vision.return_value)
        self.assertEqual(tracker.threshold, .55)
        self.assertIsNone(tracker.identity)  # ArcFace and SFace features are not comparable.

    def test_unavailable_hailo_falls_back_to_opencv_with_one_line(self):
        import human_tracker
        lines = []
        with patch.dict(sys.modules, {'hailo_faces': None}), patch.object(human_tracker, 'Vision') as vision:
            backend = human_tracker.make_backend('auto', ['det', 'rec'], 1, log=lines.append)
        self.assertIs(backend, vision.return_value)
        vision.assert_called_once_with('det', 'rec', threads=1)
        self.assertEqual(len(lines), 1)
        self.assertIn('using OpenCV', lines[0])
        with patch.object(human_tracker, 'Vision') as vision:
            human_tracker.make_backend('opencv', ['det', 'rec'], 2, log=lines.append)
        vision.assert_called_once_with('det', 'rec', threads=2)
        self.assertEqual(len(lines), 1)


if __name__ == '__main__':
    unittest.main()
