import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from human_tracker import PersonTracker
from track_human import LatestFrame, RobotLink, api, decode, tracker_lock
from serve import RobotServer
from urllib.error import HTTPError
import setup_tracking


def face(identity, x=100, y=70):
    return np.array([x, y, 60, 90, x + 18, y + 25, x + 42, y + 25, x + 30, y + 45,
                     x + 20, y + 65, x + 40, y + 65, identity], dtype=np.float32)


class FakeVision:
    def __init__(self):
        self.faces = [face(1)]; self.detections = self.recognitions = 0
        self.regions = []

    def detect(self, image, region=None):
        self.detections += 1; self.regions.append(region)
        return [row.copy() for row in self.faces]

    def feature(self, image, row, scale):
        self.recognitions += 1
        return {1: np.array([1., 0.]), 2: np.array([0., 1.]), 3: np.array([.99, .1])}[int(row[-1])]


class HumanTrackerCheck(unittest.TestCase):
    def setUp(self):
        self.vision = FakeVision(); self.tracker = PersonTracker(self.vision)
        self.image = np.random.default_rng(4).integers(0, 255, (240, 320, 3), dtype=np.uint8)
        self.tracker.set_session('wake-one', True)

    def acquire(self):
        for now in (0, .34, .68):
            packet = self.tracker.process(self.image, now)
        self.assertTrue(packet['detected']); self.assertEqual(self.tracker.state, 'tracking')

    def test_only_one_tracker_per_robot_service_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {'XDG_CACHE_HOME': cache}):
            first = tracker_lock('http://127.0.0.1:5174')
            with first:
                self.assertIsNone(tracker_lock('http://localhost:5174/'))
                self.assertIsNone(tracker_lock('http://[::1]:5174'))
                with tracker_lock('http://127.0.0.1:5175'):
                    pass
            with tracker_lock('http://localhost:5174'):
                pass

    def test_first_person_stays_locked_and_identity_never_expires(self):
        self.acquire(); original = self.tracker.identity.copy()
        self.tracker.lost(); self.vision.faces = [face(2)]
        for now in (100, 101.1, 10000, 10001.1):
            self.assertFalse(self.tracker.process(self.image, now)['detected'])
        np.testing.assert_array_equal(self.tracker.identity, original)
        self.assertEqual(self.tracker.state, 'waiting-for-locked-person')
        self.vision.faces = [face(2, 230), face(1, 30)]
        self.assertFalse(self.tracker.process(self.image, 10002.2)['detected'])
        packet = self.tracker.process(self.image, 10003.3)
        self.assertTrue(packet['detected']); self.assertAlmostEqual(packet['x'], 60 / 320)
        np.testing.assert_array_equal(self.tracker.identity, original)

    def test_sleep_and_even_unobserved_sleep_wake_reset_the_lock(self):
        self.acquire()
        self.tracker.set_session('sleep-two', False)
        self.assertIsNone(self.tracker.identity)
        count = self.vision.detections
        self.assertFalse(self.tracker.process(self.image, 1)['detected'])
        self.assertEqual(self.vision.detections, count)
        self.vision.faces = [face(2)]
        self.tracker.set_session('wake-three', True)
        for now in (2, 2.34, 2.68): packet = self.tracker.process(self.image, now)
        self.assertTrue(packet['detected'])
        np.testing.assert_array_equal(self.tracker.identity, [0, 1])
        self.tracker.set_session('wake-five', True)
        self.assertIsNone(self.tracker.identity)

    def test_crossing_ambiguity_and_similar_faces_pause_without_reassigning(self):
        self.acquire(); original = self.tracker.identity.copy()
        self.vision.faces = [face(1, 100), face(2, 110)]
        with patch.object(self.tracker, 'flow', return_value=True):
            self.assertFalse(self.tracker.process(self.image, 1.1)['detected'])
        self.vision.faces = [face(1, 40), face(3, 230)]
        self.assertFalse(self.tracker.process(self.image, 2.2)['detected'])
        self.vision.faces = [face(2, 100)]
        self.assertFalse(self.tracker.process(self.image, 3.3)['detected'])
        np.testing.assert_array_equal(self.tracker.identity, original)

    def test_flow_tracks_motion_and_rejects_blank_occlusion(self):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        self.tracker.seed(gray, face(1))
        moved = cv2.warpAffine(gray, np.float32([[1, 0, 5], [0, 1, 3]]), (320, 240))
        self.assertTrue(self.tracker.flow(moved))
        np.testing.assert_allclose(self.tracker.face[:2], [105, 73], atol=.3)
        self.assertFalse(self.tracker.flow(np.zeros_like(gray)))

    def test_visible_tracking_checks_roi_and_limits_identity_calls(self):
        self.acquire(); start_calls = self.vision.recognitions
        with patch.object(self.tracker, 'flow', return_value=True):
            for step in range(1, 91):
                self.assertTrue(self.tracker.process(self.image, .68 + step / 15)['detected'])
        self.assertLessEqual(self.vision.recognitions - start_calls, 3)
        self.assertTrue(all(region is not None for region in self.vision.regions[3:]))
        self.tracker.stale(20)
        self.assertIsNone(self.tracker.face); self.assertIsNotNone(self.tracker.identity)

    def test_mirror_and_acquisition_cannot_change_person_mid_confirmation(self):
        self.tracker.mirror = True
        self.tracker.process(self.image, 0)
        self.vision.faces = [face(2)]
        self.assertFalse(self.tracker.process(self.image, .34)['detected'])
        self.assertIsNone(self.tracker.identity)
        for now in (1, 1.34, 1.68): packet = self.tracker.process(self.image, now)
        self.assertTrue(packet['detected'])
        self.assertAlmostEqual(packet['x'], 1 - 130 / 320, places=3)

    def test_horizontal_mirroring_reverses_x_only_and_keeps_identity(self):
        original = PersonTracker(FakeVision(), mirror=False)
        mirrored = PersonTracker(FakeVision(), mirror=True)
        for tracker in (original, mirrored):
            tracker.set_session('same-person', True)
        for now in (0, .34, .68):
            first = original.process(self.image, now)
            second = mirrored.process(self.image, now)
        self.assertTrue(first['detected']); self.assertTrue(second['detected'])
        self.assertLess(first['x'], .5); self.assertGreater(second['x'], .5)
        self.assertAlmostEqual(first['x'] + second['x'], 1)
        self.assertEqual(first['y'], second['y'])
        np.testing.assert_array_equal(original.identity, mirrored.identity)

    def test_crowd_identity_work_is_bounded(self):
        self.acquire(); self.tracker.lost()
        self.vision.faces = [face(2, x) for x in (0, 70, 140, 210)]
        count = self.vision.recognitions
        self.assertFalse(self.tracker.process(self.image, 2)['detected'])
        self.assertEqual(self.vision.recognitions, count)

    def test_voice_selects_left_speaker_instead_of_camera_centre_and_keeps_lock(self):
        self.vision.faces = [face(1, 130), face(2, 20)]
        self.tracker.set_camera_info(640, 400, 320)
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=28.8, ageMs=0))
        self.acquire()
        np.testing.assert_array_equal(self.tracker.identity, [0, 1])
        self.tracker.lost()
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=0, ageMs=0))
        self.vision.faces = [face(1, 130)]
        self.assertFalse(self.tracker.process(self.image, 2)['detected'])
        np.testing.assert_array_equal(self.tracker.identity, [0, 1])

    def test_voice_requires_current_unambiguous_front_hint_and_camera_intrinsics(self):
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=0, ageMs=0))
        self.assertIsNone(self.tracker.speech_face([face(1, 130)], 320))
        self.tracker.set_camera_info(320, 200, 160)
        for hint in (None, dict(session='old', doaDeg=0, ageMs=0),
                     dict(session='wake-one', doaDeg=0, ageMs=2001),
                     dict(session='wake-one', doaDeg=180, ageMs=0)):
            self.tracker.set_input('voice', hint)
            self.assertIsNone(self.tracker.speech_face([face(1, 130)], 320))
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=0, ageMs=0))
        self.assertIsNone(self.tracker.speech_face([face(1, 120), face(2, 140)], 320))
        self.assertIsNotNone(self.tracker.speech_face([face(1, 130)], 320))

    def test_mic_offset_wraparound_and_clockwise_convention(self):
        self.tracker.set_camera_info(320, 200, 160)
        rows = [face(1, 130), face(2, 20)]
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=18.8, ageMs=0), forward=350)
        self.assertEqual(self.tracker.speech_face(rows, 320)[-1], 2)
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=331.2, ageMs=0), clockwise=True)
        self.assertEqual(self.tracker.speech_face(rows, 320)[-1], 2)
        self.tracker.set_input('words', dict(session='wake-one', doaDeg=28.8, ageMs=0))
        self.acquire()
        np.testing.assert_array_equal(self.tracker.identity, [1, 0])

    def test_new_voice_turn_can_confirm_another_speaker_without_switching_on_one_hint(self):
        self.tracker.set_camera_info(320, 200, 160)
        self.vision.faces = [face(1, 130), face(2, 20)]
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=0, ageMs=0, utterance=1))
        self.acquire()
        original = self.tracker.identity.copy()
        self.tracker.set_input('voice', dict(session='wake-one', doaDeg=28.8, ageMs=0, utterance=2))
        with patch.object(self.tracker, 'flow', return_value=True):
            for now in (1.02, 1.36):
                self.assertTrue(self.tracker.process(self.image, now)['detected'])
                np.testing.assert_array_equal(self.tracker.identity, original)
            self.assertTrue(self.tracker.process(self.image, 1.70)['detected'])
        np.testing.assert_array_equal(self.tracker.identity, [0, 1])
        self.assertEqual(self.tracker.voice_turn, 2)

    def test_latest_frame_drops_backlog_and_raw_rgb_handles_stride(self):
        slot = LatestFrame()
        for message in range(20): slot.receive(message)
        self.assertEqual(slot.take()[:2], (20, 19)); self.assertIsNone(slot.take())
        message = SimpleNamespace(encoding='rgb8', width=1, height=2, step=4,
                                  data=bytes([10, 20, 30, 0, 40, 50, 60, 0]))
        np.testing.assert_array_equal(decode(message, False), [[[30, 20, 10]], [[60, 50, 40]]])
        message.encoding = '16UC1'
        with self.assertRaises(ValueError): decode(message, False)
        with self.assertRaises(ValueError): decode(SimpleNamespace(data=b'bad JPEG'), True)

    def test_failed_model_download_preserves_existing_file(self):
        data = b'valid test model'
        model = ('test', 'test.onnx', hashlib.sha256(data).hexdigest(), len(data))
        with tempfile.TemporaryDirectory() as directory, patch.object(setup_tracking, 'MODELS', (model,)):
            path = Path(directory) / 'test.onnx'; path.write_bytes(b'old model')
            with patch.object(setup_tracking, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(ValueError): setup_tracking.ensure_models(directory)
            self.assertEqual(path.read_bytes(), b'old model')
            self.assertEqual(list(Path(directory).iterdir()), [path])
            with patch.object(setup_tracking, 'urlopen', return_value=io.BytesIO(data)):
                self.assertEqual(setup_tracking.ensure_models(directory), [path])
            self.assertEqual(path.read_bytes(), data)

    def test_robot_link_sleep_wake_guard_and_camera_stall(self):
        with tempfile.TemporaryDirectory() as directory, patch('serve.Handler.log_message'):
            root = Path(directory); config = root / 'robot-config.json'
            config.write_text((Path(__file__).resolve().parents[1] / 'robot-config.default.json').read_text())
            server = RobotServer(('127.0.0.1', 0), config_file=config, static_root=root / 'web',
                                 key_file=root / 'private/key')
            service = threading.Thread(target=server.serve_forever, daemon=True); service.start()
            url = f'http://127.0.0.1:{server.server_port}'
            link = RobotLink(url)
            def wait_for(predicate):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if predicate(): return
                    time.sleep(.02)
                self.fail('Timed out waiting for robot link.')
            try:
                api(url, 'actions', [dict(id=state, label=state) for state in ('idle', 'sleeping', 'speaking', 'listening', 'wave')])
                original = api(url, 'status')['trackingSession']
                for state in ('listening', 'speaking', 'wave'):
                    api(url, 'command', dict(state=state))
                    self.assertEqual(api(url, 'status')['trackingSession'], original)
                link.thread.start()
                wait_for(lambda: link.snapshot()[0] is not None)
                link.publish(original, dict(detected=True, x=.2, y=.3))
                wait_for(lambda: api(url, 'gaze')['detected'])
                # A camera stall stops heartbeat targets without clearing person identity.
                wait_for(lambda: not api(url, 'gaze')['detected'])
                api(url, 'command', dict(state='sleeping'))
                sleeping = api(url, 'status')
                self.assertFalse(sleeping['awake']); self.assertNotEqual(sleeping['trackingSession'], original)
                with self.assertRaises(HTTPError) as error:
                    api(url, 'gaze', dict(detected=True, x=.2, y=.3, session=sleeping['trackingSession']))
                self.assertEqual(error.exception.code, 409)
                # Do both transitions inside a single 200ms status poll window.
                api(url, 'command', dict(state='idle'))
                waking = api(url, 'status')
                self.assertTrue(waking['awake']); self.assertNotEqual(waking['trackingSession'], original)
                with self.assertRaises(HTTPError):
                    api(url, 'gaze', dict(detected=True, x=.2, y=.3, session=original))
                wait_for(lambda: link.snapshot()[0]['trackingSession'] == waking['trackingSession'])
                self.assertFalse(api(url, 'gaze')['detected'])
                sequence = waking['command']['sequence']
                link.publish(waking['trackingSession'], dict(detected=True, x=.8, y=.6))
                wait_for(lambda: api(url, 'gaze')['detected'])
                self.assertEqual(api(url, 'status')['command']['sequence'], sequence)
                link.close()
                self.assertFalse(api(url, 'gaze')['detected']); self.assertFalse(link.thread.is_alive())
                self.assertNotIn('identity', json.dumps(api(url, 'status')))
            finally:
                link.close(); server.shutdown(); service.join(timeout=2); server.server_close()


if __name__ == '__main__':
    unittest.main()
