"""Check the fan-out bus, ring buffer pre-roll and producer watchdog offline."""
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_bus as ab


def block(value, samples=320):
    return np.full(samples, value, np.int16).tobytes()


class FakeStream:
    """Calls the PortAudio-style callback from its own thread, like sounddevice does."""

    instances = []

    def __init__(self, *, callback, finished_callback, blocksize, stop_after=None, overflow_at=(), **kwargs):
        self.callback = callback; self.finished_callback = finished_callback
        self.blocksize = blocksize; self.stop_after = stop_after
        self.kwargs = kwargs; self.closed = threading.Event(); self.sent = 0
        self.overflow_at = set(overflow_at)
        FakeStream.instances.append(self)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.closed.is_set() and (self.stop_after is None or self.sent < self.stop_after):
            data = np.full((self.blocksize, 1), self.sent % 1000, np.int16)
            status = SimpleNamespace(input_overflow=self.sent in self.overflow_at)
            self.callback(data, self.blocksize, None, status)
            self.sent += 1
            time.sleep(.002)

    def close(self):
        self.closed.set()


class BusTest(unittest.TestCase):
    def test_every_subscriber_receives_every_block_in_order(self):
        bus = ab.AudioBus()
        first, second = bus.subscribe('wake'), bus.subscribe('command')
        for seq in range(1, 51):
            bus.publish(block(seq), seq * .02, seq)
        for sub in (first, second):
            self.assertEqual([sub.get(0).seq for _ in range(50)], list(range(1, 51)))
            self.assertEqual(sub.dropped, 0)

    def test_slow_subscriber_keeps_newest_blocks_without_affecting_others(self):
        bus = ab.AudioBus()
        slow, fast = bus.subscribe('slow', maxsize=3), bus.subscribe('fast')
        for seq in range(1, 11):
            bus.publish(block(seq), seq * .02, seq)
        self.assertEqual([slow.get(0).seq for _ in range(3)], [8, 9, 10])
        self.assertEqual(slow.dropped, 7)
        self.assertEqual(fast.dropped, 0)
        self.assertEqual(fast.depth(), 10)

    def test_close_unblocks_waiting_consumer(self):
        bus = ab.AudioBus()
        sub = bus.subscribe('command')
        result = []
        thread = threading.Thread(target=lambda: result.append(sub.get(5)))
        thread.start()
        bus.unsubscribe(sub)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])
        bus.publish(block(1), 0, 1)
        self.assertIsNone(sub.get(0))

    def test_preroll_seq_lets_consumer_skip_blocks_already_in_ring(self):
        bus = ab.AudioBus()
        for seq in range(1, 6):
            bus.publish(block(seq), seq * .02, seq)
        sub = bus.subscribe('command')
        bus.publish(block(6), .12, 6)
        pcm, _, last = bus.preroll(sub, seconds=.04)
        self.assertEqual(last, 6)
        self.assertEqual(np.frombuffer(pcm, '<i2')[[0, -1]].tolist(), [5, 6])
        self.assertEqual(sub.get(0).seq, 6)  # Duplicate of the ring tail; consumers skip it.


class RingBufferTest(unittest.TestCase):
    def test_returns_exactly_last_samples_across_wraparound(self):
        ring = ab.RingBuffer(seconds=.05, rate=16000)  # 800 samples
        values = np.arange(2000, dtype=np.int16)
        for start in range(0, 2000, 320):
            chunk = values[start:start + 320]
            ring.write(chunk, (start + chunk.size) / 16000, start // 320 + 1)
        pcm, t_start, seq = ring.read_last(.02)  # 320 samples
        self.assertEqual(np.frombuffer(pcm, '<i2').tolist(), list(range(1680, 2000)))
        self.assertAlmostEqual(t_start, 1680 / 16000)
        self.assertEqual(seq, 7)
        whole, _, _ = ring.read_last(10)
        self.assertEqual(np.frombuffer(whole, '<i2').tolist(), list(range(1200, 2000)))

    def test_read_since_uses_capture_time(self):
        ring = ab.RingBuffer(seconds=1, rate=1000)
        ring.write(np.arange(500, dtype=np.int16), t_end=10.5)
        pcm, t_start, _ = ring.read_since(10.4)
        self.assertEqual(np.frombuffer(pcm, '<i2').tolist(), list(range(400, 500)))
        self.assertAlmostEqual(t_start, 10.4)
        self.assertEqual(ring.read_since(11)[0], b'')

    def test_short_history_returns_what_exists(self):
        ring = ab.RingBuffer(seconds=2)
        self.assertEqual(ring.read_last(.3)[0], b'')
        ring.write(block(3, 160), 1.0)
        self.assertEqual(len(ring.read_last(.3)[0]), 320)


class ProducerTest(unittest.TestCase):
    def setUp(self):
        FakeStream.instances = []

    def producer(self, bus, **stream_options):
        factory = lambda **kwargs: FakeStream(**kwargs, **stream_options)
        producer = ab.AudioProducer(bus, 2, stream_factory=factory, stall_seconds=.1)
        producer.REOPEN_DELAYS = (.01, .01, .01)
        return producer

    def test_callback_publishes_blocks_with_overflow_flag_and_stats(self):
        bus = ab.AudioBus()
        sub = bus.subscribe('command')
        producer = self.producer(bus, stop_after=5, overflow_at={2})
        producer.start()
        try:
            blocks = [sub.get(1) for _ in range(5)]
        finally:
            producer.stop()
        self.assertEqual([b.seq for b in blocks], [1, 2, 3, 4, 5])
        self.assertEqual([b.overflow for b in blocks], [False, False, True, False, False])
        self.assertEqual(producer.overflows, 1)
        self.assertGreaterEqual(producer.stats()['frames'], 5 * 320)
        self.assertIn('command', producer.stats()['subscriptions'])
        self.assertEqual(FakeStream.instances[0].kwargs['channels'], 1)

    def test_stalled_stream_reopens_then_fails_after_three_attempts(self):
        bus = ab.AudioBus()
        producer = self.producer(bus, stop_after=0)  # Never delivers a frame.
        producer.start()
        deadline = time.monotonic() + 3
        while producer.failed is None and time.monotonic() < deadline:
            time.sleep(.01)
        producer.stop()
        self.assertIsInstance(producer.failed, ab.DeviceUnavailable)
        self.assertEqual(producer.reopen_count, 3)
        self.assertEqual(len(FakeStream.instances), 4)

    def test_recovers_when_reopened_stream_delivers_frames(self):
        bus = ab.AudioBus()
        sub = bus.subscribe('wake')
        attempts = []
        def factory(**kwargs):
            attempts.append(1)
            return FakeStream(**kwargs, stop_after=3 if len(attempts) == 1 else None)
        producer = ab.AudioProducer(bus, 2, stream_factory=factory, stall_seconds=.1)
        producer.REOPEN_DELAYS = (.01, .01, .01)
        producer.start()
        try:
            time.sleep(.6)
            self.assertIsNone(producer.failed)
            self.assertEqual(producer.reopen_count, 1)
            self.assertIsNotNone(sub.get(1))
        finally:
            producer.stop()

    def test_open_error_counts_as_failed_attempt(self):
        bus = ab.AudioBus()
        calls = []
        def factory(**kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError('PortAudio: device unavailable')
            return FakeStream(**kwargs, stop_after=0)
        producer = ab.AudioProducer(bus, 2, stream_factory=factory, stall_seconds=.1)
        producer.REOPEN_DELAYS = (.01, .01, .01)
        producer.start()
        deadline = time.monotonic() + 3
        while producer.failed is None and time.monotonic() < deadline:
            time.sleep(.01)
        producer.stop()
        self.assertIsInstance(producer.failed, ab.DeviceUnavailable)


class ControlLockTest(unittest.TestCase):
    def test_usb_calls_are_serialized(self):
        active = []; overlap = []
        class Dev:
            def is_voice(self):
                active.append(1)
                if len(active) > 1:
                    overlap.append(1)
                time.sleep(.005)
                active.pop()
                return True
            @property
            def direction(self):
                return self.is_voice() and 90
        control = ab.ReSpeakerControl(Dev())
        threads = [threading.Thread(target=lambda: [control.is_voice() for _ in range(10)]) for _ in range(3)]
        threads += [threading.Thread(target=lambda: [control.direction for _ in range(10)])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(overlap)
        self.assertEqual(ab.ReSpeakerControl(Mock(version=3)).version, 3)


if __name__ == '__main__':
    unittest.main()
