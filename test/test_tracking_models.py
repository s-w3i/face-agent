"""Optional real-model regression; fixtures are public OpenCV sample images, never camera captures."""
import math
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from human_tracker import PersonTracker, Vision
from setup_tracking import model_paths

FIXTURES = ROOT / 'test-output/tracking'
AVAILABLE = all(path.is_file() for path in model_paths()) and all(
    (FIXTURES / name).is_file() for name in ('lena.jpg', 'messi5.jpg'))


@unittest.skipUnless(AVAILABLE, 'Optional: cache tracking models and the two public OpenCV image fixtures first.')
class TrackingModelsCheck(unittest.TestCase):
    def test_real_models_follow_motion_refuse_other_face_and_recover_original(self):
        first = cv2.imread(str(FIXTURES / 'lena.jpg'))
        second = cv2.imread(str(FIXTURES / 'messi5.jpg'))[65:155, 205:280]
        first = cv2.resize(first, (300, 300)); second = cv2.resize(second, (250, 300))
        def scene(x=20, y=80, include_first=True, include_second=False):
            image = np.zeros((480, 640, 3), np.uint8)
            if include_first: image[y:y + 300, x:x + 300] = first
            if include_second: image[80:380, 370:620] = second
            return image
        vision = Vision(*model_paths()); tracker = PersonTracker(vision)
        tracker.set_session('model-test', True)
        for index in range(300):
            now = index / 15
            image = scene(round(160 + math.sin(now * .7) * 120), round(70 + math.sin(now * .4) * 30))
            packet = tracker.process(image, now)
            if index >= 12:
                self.assertTrue(packet['detected'], f'Visible face lost at frame {index}')
        identity = tracker.identity.copy()
        for index in range(300, 360):
            self.assertFalse(tracker.process(scene(include_first=False, include_second=True), index / 15)['detected'])
        recovered = False
        for index in range(360, 421):
            packet = tracker.process(scene(include_second=True), index / 15)
            if packet['detected']:
                recovered = True
                self.assertLess(packet['x'], .5)
                break
        self.assertTrue(recovered)
        np.testing.assert_array_equal(tracker.identity, identity)
        self.assertLess(vision.recognitions, 30)


if __name__ == '__main__':
    unittest.main()
