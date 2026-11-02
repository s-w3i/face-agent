"""Check rolling-window wake detection with a scripted scorer and VAD (no Hailo, no audio)."""
from pathlib import Path
import sys
import time
import unittest

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_bus as ab
import rolling_wake as rw

BLOCK_BYTES = rw.BLOCK * 2


class ScriptedVad:
    """Speech for the 20 ms blocks whose index is in `voiced` (all blocks if None)."""

    def __init__(self, voiced=None):
        self.voiced = voiced; self.calls = 0

    def is_speech(self, frame, rate):
        index = self.calls
        self.calls += 1
        return self.voiced is None or index in self.voiced


class ScriptedScorer:
    def __init__(self, scores=(), default=0.0):
        self.scores = list(scores); self.default = default; self.windows = []

    def __call__(self, audio):
        self.windows.append(audio)
        return self.scores.pop(0) if self.scores else self.default


def feed_seconds(detector, seconds, t0=0.0, chunk_blocks=1):
    hits = []
    blocks = round(seconds * rw.RATE / rw.BLOCK)
    for index in range(0, blocks, chunk_blocks):
        hits += detector.feed(bytes(BLOCK_BYTES * chunk_blocks), t0 + index * rw.BLOCK / rw.RATE)
    return hits


class DetectorTest(unittest.TestCase):
    def test_scores_full_windows_every_hop_while_speech(self):
        scorer = ScriptedScorer()
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad())
        feed_seconds(detector, 6.0)
        # Full at 3.0 s, then every 1.0 s: 3, 4, 5, 6.
        self.assertEqual(len(scorer.windows), 4)
        self.assertTrue(all(len(w) == 3 * rw.RATE for w in scorer.windows))

    def test_default_hop_keeps_every_short_phrase_whole_in_one_window(self):
        scorer = ScriptedScorer()
        feed_seconds(rw.RollingWakeDetector(scorer, .5, vad=ScriptedVad()), 9.0)
        self.assertEqual(len(scorer.windows), 5)  # 3.0, 4.5, 6.0, 7.5, 9.0
        self.assertGreaterEqual(rw.WINDOW - rw.HOP, 1.5)

    def test_lagging_thread_skips_windows_but_keeps_the_audio(self):
        scorer = ScriptedScorer(default=.9)
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad())
        for i in range(300):  # 6 s, all while lagging
            self.assertEqual(detector.feed(bytes(BLOCK_BYTES), i * .02, may_score=False), [])
        self.assertEqual((scorer.windows, detector.skipped), ([], 4))
        hits = detector.feed(bytes(BLOCK_BYTES * 50), 6.0)  # caught up: the next window is scored
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(scorer.windows[0]), 3 * rw.RATE)

    def test_quiet_room_costs_nothing(self):
        scorer = ScriptedScorer(default=.9)
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad(voiced=set()))
        self.assertEqual(feed_seconds(detector, 10.0), [])
        self.assertEqual(scorer.windows, [])

    def test_never_waits_for_silence(self):
        """Continuous noise (always 'speech') still gets windows scored, unlike VAD segments."""
        scorer = ScriptedScorer([.1, .1, .1, .1, .1, .1, .9])
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad())
        hits = feed_seconds(detector, 12.0)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0]['continue_from'], 9.0, places=2)

    def test_hit_carries_onset_and_refractory_blocks_the_same_phrase(self):
        # Speech from 4.0 s (block 200) onwards.
        scorer = ScriptedScorer(default=.9)
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad(voiced=set(range(200, 1000))))
        hits = feed_seconds(detector, 9.0)
        # Window ending 5.0 s has 1.0 s of speech: hit. 6.0 s is refractory; 7.0 s hits again.
        self.assertEqual([round(h['continue_from'], 2) for h in hits], [5.0, 7.0, 9.0])
        self.assertAlmostEqual(hits[0]['t'], 4.0 - rw.ONSET_PAD, places=2)
        self.assertEqual(hits[0]['status'], 'wake_detected')
        self.assertEqual(detector.detections, 3)

    def test_below_min_voiced_is_not_scored(self):
        scorer = ScriptedScorer(default=.9)
        # 0.2 s of speech (10 blocks) is under MIN_VOICED.
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad(voiced=set(range(150, 160))))
        self.assertEqual(feed_seconds(detector, 6.0), [])

    def test_uneven_chunks_keep_time(self):
        scorer = ScriptedScorer(default=.9)
        detector = rw.RollingWakeDetector(scorer, .5, hop=1.0, vad=ScriptedVad())
        hits = []
        t = 100.0
        for samples in [480, 1000, 320, 7] * 200:
            hits += detector.feed(bytes(samples * 2), t)
            t += samples / rw.RATE
        self.assertTrue(hits)
        self.assertAlmostEqual(hits[0]['continue_from'], 103.0, places=2)


class FakeHailo:
    def __init__(self, score):
        self.score = score; self.phrases = None

    def phrase_tokens(self, text):
        return [ord(c) for c in text]

    def phrase_logprob(self, audio, phrases):
        self.phrases = phrases
        return self.score


class VerifyingScorer(ScriptedScorer):
    """Scripted Hailo scores plus a scripted CPU check."""

    def __init__(self, scores, heard=()):
        super().__init__(scores, default=-9.0)
        self.heard = list(heard); self.checked = []

    def verify(self, audio):
        self.checked.append(audio)
        return self.heard.pop(0) if self.heard else False


class VerificationTest(unittest.TestCase):
    def detector(self, scorer):
        return rw.RollingWakeDetector(scorer, -3.3, hop=1.0, verify_floor=-6.0, vad=ScriptedVad())

    def test_confident_window_wakes_without_a_cpu_check(self):
        scorer = VerifyingScorer([-7.0, -2.0])
        hits = feed_seconds(self.detector(scorer), 5.0)
        self.assertEqual([(h['score'], h['verified']) for h in hits], [(-2.0, False)])
        self.assertEqual(scorer.checked, [])

    def test_only_the_peak_of_a_borderline_run_is_checked_one_hop_later(self):
        # Windows at 3, 4, 5, 6 s: rising to -4.0 at 5 s, then falling.
        scorer = VerifyingScorer([-5.5, -4.5, -4.0, -5.0, -9.0], heard=[True])
        detector = self.detector(scorer)
        hits = feed_seconds(detector, 7.0)
        self.assertEqual(len(scorer.checked), 1)
        self.assertIs(scorer.checked[0], scorer.windows[2])  # the -4.0 window
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]['score'], hits[0]['verified']), (-4.0, True))
        self.assertAlmostEqual(hits[0]['continue_from'], 5.0, places=2)  # the peak window, not the check time
        self.assertEqual(detector.verifications, 1)

    def test_a_rejected_check_does_not_wake_and_hopeless_windows_cost_nothing(self):
        scorer = VerifyingScorer([-4.0, -7.0, -8.0, -7.5], heard=[False])
        self.assertEqual(feed_seconds(self.detector(scorer), 7.0), [])
        self.assertEqual(len(scorer.checked), 1)

    def test_silence_after_a_candidate_still_checks_it(self):
        scorer = VerifyingScorer([-4.0], heard=[True])
        # Speech only in the first 3 s, then the window has too little speech to score.
        detector = rw.RollingWakeDetector(scorer, -3.3, hop=1.0, verify_floor=-6.0, vad=ScriptedVad(voiced=set(range(150))))
        hits = feed_seconds(detector, 7.0)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]['verified'])

    def test_scorer_without_verify_never_checks(self):
        scorer = ScriptedScorer([-4.0, -4.0, -4.0], default=-4.0)
        self.assertEqual(feed_seconds(rw.RollingWakeDetector(scorer, -3.3, hop=1.0, vad=ScriptedVad()), 6.0), [])


class ScorerTest(unittest.TestCase):
    def test_verify_uses_the_loose_text_match(self):
        scorer = rw.HailoPhraseScorer('Kuro', transcriber=FakeHailo(-4.0), verifier=lambda audio: 'High Kuro.')
        self.assertEqual(scorer(np.zeros(16000, np.float32)), -4.0)
        self.assertTrue(scorer.verify(np.zeros(16000, np.float32)))
        self.assertEqual(scorer.last_text, 'High Kuro.')
        scorer = rw.HailoPhraseScorer('Kuro', transcriber=FakeHailo(-4.0), verifier=lambda audio: 'I grew up.')
        self.assertFalse(scorer.verify(np.zeros(16000, np.float32)))
        self.assertIsNone(rw.HailoPhraseScorer('Kuro', transcriber=FakeHailo(-4.0), verifier=None).verify)

    def test_hailo_failure_reconnects_the_shared_session_once(self):
        import hailo_stt
        from unittest.mock import patch
        class Broken(FakeHailo):
            def phrase_logprob(self, audio, phrases):
                raise RuntimeError('HAILO_RPC_FAILED')
        broken, fresh = Broken(0), FakeHailo(-1.0)
        with patch.object(hailo_stt, 'shared', return_value=broken), \
                patch.object(hailo_stt, 'replace_shared', return_value=fresh) as replace:
            scorer = rw.HailoPhraseScorer('Kuro', verifier=None)
            self.assertEqual(scorer(np.zeros(16000, np.float32)), -1.0)
        replace.assert_called_once_with(broken)
        self.assertEqual(scorer.reconnects, 1)
        # An injected transcriber (tests, eval) is not shared: errors propagate.
        with self.assertRaises(RuntimeError):
            rw.HailoPhraseScorer('Kuro', transcriber=Broken(0), verifier=None)(np.zeros(16000, np.float32))

    def test_phrases_follow_the_robot_name(self):
        scorer = rw.HailoPhraseScorer('  Dots ', transcriber=FakeHailo(-9.0), verifier=None)
        self.assertEqual(scorer.phrases, [[ord(c) for c in ' Hi, Dots'], [ord(c) for c in ' Hey Dots']])

    def test_loose_text_match(self):
        for text in ('Hi Kuro.', 'Hi, Kuro!', 'High Kuro.', 'I, Kuro.', 'Aye Kuro.', 'Aikuro', 'so, hey kuro what time'):
            self.assertTrue(rw.heard_phrase(text, 'Kuro'), text)
        for text in ('I grow.', 'Hi, hero.', 'Thank you, Kuro.', 'Hi Kuroko', 'Kuro is here', 'Hi Cora'):
            self.assertFalse(rw.heard_phrase(text, 'Kuro'), text)


class ThreadTest(unittest.TestCase):
    def test_thread_reports_hits_and_closes_scorer(self):
        bus = ab.AudioBus()
        closed = []
        class Closing(ScriptedScorer):
            def close(self):
                closed.append(True)
        events = []
        thread = rw.RollingWakeThread(bus, .5, lambda: Closing(default=.9), on_detect=events.append,
                                      vad=ScriptedVad())
        thread.start()
        self.assertTrue(thread.ready.wait(1))
        t0 = time.monotonic() - 3.2  # Captured just now: the thread is not lagging.
        for i in range(160):
            bus.publish(bytes(BLOCK_BYTES), t0 + i * .02, i + 1)
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(.01)
        thread.stop()
        self.assertEqual(len(events), 1)
        self.assertTrue(thread.wake_word_detected.is_set())
        self.assertEqual(thread.stats()['detections'], 1)
        self.assertEqual(closed, [True])
        self.assertEqual(bus.subscribers, ())

    def test_thread_skips_windows_for_old_audio(self):
        bus = ab.AudioBus()
        scorer = ScriptedScorer(default=.9)
        thread = rw.RollingWakeThread(bus, .5, lambda: scorer, vad=ScriptedVad())
        thread.start(); self.assertTrue(thread.ready.wait(1))
        t0 = time.monotonic() - 60  # A minute behind, e.g. after a long stall
        for i in range(160):
            bus.publish(bytes(BLOCK_BYTES), t0 + i * .02, i + 1)
        deadline = time.monotonic() + 1
        while thread.stats()['skipped'] < 1 and time.monotonic() < deadline:
            time.sleep(.01)
        thread.stop()
        self.assertEqual(scorer.windows, [])
        self.assertEqual(thread.stats()['skipped'], 1)

    def test_scorer_load_error_is_kept_for_the_worker(self):
        bus = ab.AudioBus()
        def broken():
            raise RuntimeError('no hailo')
        thread = rw.RollingWakeThread(bus, .5, broken)
        thread.start(); thread.join(1)
        self.assertIsInstance(thread.error, RuntimeError)
        self.assertEqual(bus.subscribers, ())


if __name__ == '__main__':
    unittest.main()
