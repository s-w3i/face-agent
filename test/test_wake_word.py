"""Check the openWakeWord thread logic with scripted scores (no model files needed)."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_bus as ab
import wake_word as ww
import wake_eval

FRAME_BYTES = ww.FRAME_SAMPLES * 2


class ScriptedModel:
    def __init__(self, scores, name='hi_kuro'):
        self.scores = list(scores); self.name = name
        self.calls = 0; self.resets = 0; self.frames = []

    def predict(self, frame):
        self.frames.append(frame)
        score = self.scores[self.calls] if self.calls < len(self.scores) else 0.0
        self.calls += 1
        return {self.name: score}

    def reset(self):
        self.resets += 1


def frames(n):
    return bytes(FRAME_BYTES * n)


class DetectorTest(unittest.TestCase):
    def test_threshold_and_patience_need_consecutive_frames(self):
        model = ScriptedModel([.9, .1, .9, .9, .1])
        detector = ww.WakeDetector(model, threshold=.5, patience=2)
        hits = detector.feed(frames(5), 100.0)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0]['t'], 100.0 + 4 * .08)  # end of the 4th frame
        self.assertEqual(hits[0], dict(status='wake_detected', score=.9, t=hits[0]['t'], model='hi_kuro'))
        self.assertEqual(model.resets, 1)

    def test_refractory_merges_hits_within_one_and_a_half_seconds(self):
        scores = [.9, .9] + [0] * 5 + [.9, .9] + [0] * 15 + [.9, .9]
        detector = ww.WakeDetector(ScriptedModel(scores), threshold=.5, patience=2, refractory=1.5)
        hits = detector.feed(frames(len(scores)), 0.0)
        self.assertEqual([round(h['t'], 2) for h in hits], [.16, 2.08])  # frames 2 and 26
        self.assertEqual(detector.detections, 2)

    def test_twenty_ms_blocks_accumulate_into_eighty_ms_frames(self):
        model = ScriptedModel([0] * 10)
        detector = ww.WakeDetector(model)
        for i in range(20):
            detector.feed(np.full(320, i, np.int16).tobytes(), i * .02)
        self.assertEqual(model.calls, 5)
        self.assertEqual(model.frames[1][:320].tolist(), [4] * 320)
        self.assertEqual(len(model.frames[0]), 1280)

    def test_max_score_window_is_ten_seconds(self):
        detector = ww.WakeDetector(ScriptedModel([.4] + [.1] * 200), threshold=.5)
        detector.feed(frames(1), 0.0)
        self.assertAlmostEqual(detector.max_score(), .4)
        detector.feed(frames(140), .08)  # 11.2 s later
        self.assertAlmostEqual(detector.max_score(), .1)


    def test_offline_eval_replays_the_same_rule(self):
        rng = np.random.default_rng(3)
        for _ in range(20):
            scores = rng.choice([0, .3, .6, .9], size=120).tolist()
            for threshold in (.5, .8):
                detector = ww.WakeDetector(ScriptedModel(scores), threshold=threshold)
                live = len(detector.feed(frames(len(scores)), 0.0))
                self.assertEqual(wake_eval.count_hits(scores, threshold), live)


class ThreadTest(unittest.TestCase):
    def start(self, scores, bus=None, **options):
        bus = bus or ab.AudioBus()
        events = []
        thread = ww.WakeWordThread(bus, model_factory=lambda: ScriptedModel(scores),
                                   on_detect=events.append, **options)
        thread.start()
        self.assertTrue(thread.ready.wait(1))
        return bus, thread, events

    def publish(self, bus, n_blocks, t0=0.0):
        for i in range(n_blocks):
            bus.publish(bytes(640), t0 + i * .02, i + 1)

    def test_hit_sets_event_and_calls_back_with_capture_time(self):
        bus, thread, events = self.start([0, .8, .8])
        self.publish(bus, 12, t0=50.0)
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(.01)
        thread.stop()
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]['t'], 50.24)
        self.assertTrue(thread.wake_word_detected.is_set())  # Cleared by the consumer, not T2.
        self.assertEqual(thread.stats()['detections'], 1)

    def test_stop_joins_within_one_second_and_unsubscribes(self):
        bus, thread, _ = self.start([])
        started = time.monotonic()
        thread.stop()
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(bus.subscribers, ())

    def test_slow_consumer_counts_drops_without_blocking_publisher(self):
        bus = ab.AudioBus()
        gate = threading.Event()
        class Slow(ScriptedModel):
            def predict(self, frame):
                gate.wait(2)
                return super().predict(frame)
        events = []
        thread = ww.WakeWordThread(bus, model_factory=lambda: Slow([]), on_detect=events.append)
        thread.start(); thread.ready.wait(1)
        started = time.monotonic()
        self.publish(bus, 200)
        self.assertLess(time.monotonic() - started, .5)
        gate.set()
        thread.stop(2)
        self.assertGreater(thread.stats()['dropped'], 0)

    def test_model_error_is_kept_for_the_worker(self):
        bus = ab.AudioBus()
        def broken():
            raise RuntimeError('bad model')
        thread = ww.WakeWordThread(bus, model_factory=broken)
        thread.start(); thread.join(1)
        self.assertIsInstance(thread.error, RuntimeError)
        self.assertEqual(bus.subscribers, ())


class NoisyMixTest(unittest.TestCase):
    def test_noise_level_matches_the_requested_snr_around_and_under_the_clip(self):
        rng = np.random.default_rng(1)
        clip = (np.sin(np.arange(16000) / 5) * 8000).astype(np.int16)
        noise = rng.normal(0, 3000, 16000 * 20).astype(np.int16)
        stream, start = wake_eval.noisy(clip, noise, 10, rng)
        self.assertEqual((start, len(stream)), (3.0, 16000 * 6))
        lead = stream[:3 * 16000].astype(np.float64)
        speech = np.sqrt((clip.astype(np.float64) ** 2).mean())
        self.assertAlmostEqual(20 * np.log10(speech / np.sqrt((lead ** 2).mean())), 10, delta=.5)
        silent, _ = wake_eval.noisy(clip, noise, None, rng)
        self.assertFalse(silent[:3 * 16000].any())


class EngineChoiceTest(unittest.TestCase):
    def model(self, directory, **info):
        path = Path(directory) / 'hi_kuro.onnx'
        path.write_bytes(b'onnx')
        if info:
            path.with_suffix('.json').write_text(json.dumps(info))
        return path

    def test_matching_name_selects_openwakeword_with_model_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.model(directory, phrase='hi kuro', name='Kuro', threshold=.42)
            self.assertEqual(ww.choose_engine('auto', path, 'kuro', rolling_available=False)[:2], ('oww', .42))

    def test_renamed_robot_or_missing_model_falls_back_to_whisper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.model(directory, name='Kuro')
            engine, _, reason = ww.choose_engine('auto', path, 'Dots', rolling_available=False)
            self.assertEqual(engine, 'whisper')
            self.assertIn('Dots', reason)
            self.assertEqual(ww.choose_engine('auto', Path(directory) / 'missing.onnx', 'Kuro', rolling_available=False)[0], 'whisper')
            self.assertEqual(ww.choose_engine('whisper', path, 'Kuro')[0], 'whisper')

    def test_forced_engine_ignores_name_but_still_needs_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.model(directory, name='Jarvis', threshold=7)
            self.assertEqual(ww.choose_engine('oww', path, 'Kuro')[:2], ('oww', ww.DEFAULT_THRESHOLD))
            self.assertEqual(ww.choose_engine('oww', Path(directory) / 'none.onnx', 'Kuro')[0], 'whisper')

    def test_auto_prefers_rolling_when_hailo_whisper_is_installed(self):
        import rolling_wake
        with tempfile.TemporaryDirectory() as directory:
            path = self.model(directory, name='Kuro', threshold=.42)
            engine, threshold, _ = ww.choose_engine('auto', path, 'Dots', rolling_available=True)
            self.assertEqual((engine, threshold), ('rolling', rolling_wake.DEFAULT_THRESHOLD))  # Any name works.
            self.assertEqual(ww.choose_engine('rolling', path, 'Kuro', rolling_available=False)[0], 'whisper')
            self.assertEqual(ww.choose_engine('oww', path, 'Kuro', rolling_available=True)[0], 'oww')
            self.assertEqual(ww.choose_engine('whisper', path, 'Kuro', rolling_available=True)[0], 'whisper')


if __name__ == '__main__':
    unittest.main()
