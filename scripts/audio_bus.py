"""One continuous capture stream fanned out to independent consumers, plus pre-roll.

T1 (AudioProducer) runs the PortAudio callback and publishes 20 ms int16 blocks to
an AudioBus. Every consumer has its own bounded Subscription queue (drop-oldest, so
a slow consumer never stalls the callback or other consumers), and a RingBuffer keeps
the last ~2 s in memory for pre-roll. Nothing here persists or transmits audio.
"""
from collections import namedtuple
import queue
import threading
import time

import numpy as np

RATE = 16000
BLOCK = 320  # 20 ms of mono PCM16.

Block = namedtuple('Block', 'pcm t seq overflow')  # t: monotonic capture time of the first sample


class DeviceUnavailable(RuntimeError):
    """The USB microphone or its active audio stream disappeared."""


class RingBuffer:
    """Fixed-capacity int16 history; thread-safe; remembers the newest sequence number."""

    def __init__(self, seconds=2.0, rate=RATE):
        self.rate = rate
        self.data = np.zeros(round(seconds * rate), np.int16)
        self.lock = threading.Lock()
        self.total = 0  # Samples ever written.
        self.t_end = 0.0  # Monotonic time just after the newest sample.
        self.last_seq = 0

    @property
    def capacity(self):
        return self.data.size

    def write(self, frames, t_end, seq=0):
        frames = np.frombuffer(frames, '<i2') if isinstance(frames, (bytes, bytearray, memoryview)) else frames
        frames = frames[-self.capacity:]
        with self.lock:
            start = self.total % self.capacity
            first = min(frames.size, self.capacity - start)
            self.data[start:start + first] = frames[:first]
            self.data[:frames.size - first] = frames[first:]
            self.total += frames.size
            self.t_end = t_end
            self.last_seq = seq

    def _read(self, samples):
        samples = max(0, min(samples, self.total, self.capacity))
        end = self.total % self.capacity
        start = end - samples
        if start >= 0:
            pcm = self.data[start:end]
        else:
            pcm = np.concatenate([self.data[start:], self.data[:end]])
        return pcm.astype('<i2').tobytes(), self.t_end - samples / self.rate, self.last_seq

    def read_last(self, seconds):
        """(pcm bytes, monotonic start time, newest seq) for the last `seconds` of audio."""
        with self.lock:
            return self._read(round(seconds * self.rate))

    def read_since(self, t):
        """Audio captured at or after monotonic time `t`, limited to what the ring still holds."""
        with self.lock:
            return self._read(round((self.t_end - t) * self.rate))


class Subscription:
    """A bounded per-consumer queue; the producer drops this consumer's oldest block when full."""

    _CLOSED = object()

    def __init__(self, name, maxsize=100):
        self.name = name
        self.queue = queue.Queue(maxsize)
        self.dropped = 0
        self.closed = False

    def put(self, block):
        while True:
            try:
                self.queue.put_nowait(block)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def get(self, timeout=None):
        """The next Block, or None on timeout or once closed."""
        if self.closed and self.queue.empty():
            return None
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is self._CLOSED:
            self.closed = True
            return None
        return item

    def close(self):
        self.closed = True
        self.put(self._CLOSED)

    def depth(self):
        return self.queue.qsize()


class AudioBus:
    def __init__(self, ring_seconds=2.0, rate=RATE):
        self.rate = rate
        self.ring = RingBuffer(ring_seconds, rate)
        self._subscribers = ()  # Copy-on-write: publish never takes the lock.
        self._lock = threading.Lock()

    def subscribe(self, name, maxsize=100):
        sub = Subscription(name, maxsize)
        with self._lock:
            self._subscribers = (*self._subscribers, sub)
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            self._subscribers = tuple(s for s in self._subscribers if s is not sub)
        sub.close()

    @property
    def subscribers(self):
        return self._subscribers

    def publish(self, pcm, t, seq, overflow=False):
        """Called from the PortAudio callback: never blocks."""
        block = Block(pcm, t, seq, overflow)
        self.ring.write(pcm, t + len(pcm) / 2 / self.rate, seq)
        for sub in self._subscribers:
            sub.put(block)

    def preroll(self, sub, seconds=None, since=None):
        """Ring audio from before `sub` started, and the newest seq it covers.

        Subscribe first, then call this: blocks with seq <= the returned value may also
        be queued on `sub` and must be skipped by the consumer.
        """
        pcm, t_start, seq = self.ring.read_since(since) if since is not None else self.ring.read_last(seconds or 0)
        return pcm, t_start, seq


class AudioProducer:
    """T1: one continuous InputStream; a watchdog reopens it after a stall or stop."""

    REOPEN_DELAYS = (.2, .5, 1.0)

    def __init__(self, bus, device, rate=RATE, block=BLOCK, stream_factory=None, stall_seconds=.5):
        if stream_factory is None:
            import sounddevice as sd
            stream_factory = sd.InputStream
        self.bus = bus; self.device = device; self.rate = rate; self.block = block
        self.stream_factory = stream_factory
        self.stall_seconds = stall_seconds
        self.stream = None
        self.failed = None
        self.frames = self.overflows = self.reopen_count = 0
        self.seq = 0
        self.last_frame = None
        self._finished = threading.Event()
        self._stop = threading.Event()
        self._watchdog = None
        self._lock = threading.Lock()

    def _open(self):
        self._finished.clear()
        stream = self.stream_factory(samplerate=self.rate, channels=1, dtype='int16', device=self.device,
                                     blocksize=self.block, callback=self.callback,
                                     finished_callback=self._finished.set)
        stream.start()
        self.stream = stream
        self.opened_at = time.monotonic()

    def _close(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def start(self):
        self._open()
        self._watchdog = threading.Thread(target=self._watch, name='audio-watchdog', daemon=True)
        self._watchdog.start()

    def stop(self):
        self._stop.set()
        if self._watchdog:
            self._watchdog.join(2)
        self._close()

    def callback(self, indata, frames, time_info, status):
        now = time.monotonic()
        overflow = bool(getattr(status, 'input_overflow', False))
        if overflow:
            self.overflows += 1
        self.seq += 1
        self.frames += frames
        self.last_frame = now
        self.bus.publish(indata[:, 0].astype('<i2').tobytes(), now - frames / self.rate, self.seq, overflow)

    def _stalled(self):
        reference = self.last_frame if self.last_frame and self.last_frame >= self.opened_at else self.opened_at
        return self._finished.is_set() or time.monotonic() - reference > self.stall_seconds

    def _watch(self):
        attempts = 0
        while not self._stop.wait(.05):
            if not self._stalled():
                if attempts and self.last_frame and time.monotonic() - self.opened_at > 1:
                    attempts = 0  # Frames flowed again after a reopen.
                continue
            if attempts >= len(self.REOPEN_DELAYS):
                self.failed = DeviceUnavailable('ReSpeaker audio stream stopped.')
                self._close()
                return
            delay = self.REOPEN_DELAYS[attempts]
            attempts += 1
            self._close()
            if self._stop.wait(delay):
                return
            self.reopen_count += 1
            try:
                self._open()
            except Exception:
                self.opened_at = time.monotonic()
                self._finished.set()  # Count it as stalled; the next pass tries again.

    def stats(self):
        age = round((time.monotonic() - self.last_frame) * 1000) if self.last_frame else None
        return dict(frames=self.frames, overflows=self.overflows, reopen_count=self.reopen_count,
                    last_frame_age_ms=age,
                    subscriptions={s.name: dict(depth=s.depth(), dropped=s.dropped) for s in self.bus.subscribers})


class ReSpeakerControl:
    """Serialize pyusb control transfers (is_voice/direction/read/write) across threads."""

    def __init__(self, dev):
        self.dev = dev
        self.lock = threading.Lock()

    def is_voice(self):
        with self.lock:
            return self.dev.is_voice()

    @property
    def direction(self):
        with self.lock:
            return self.dev.direction

    def read(self, name):
        with self.lock:
            return self.dev.read(name)

    def write(self, name, value):
        with self.lock:
            return self.dev.write(name, value)

    def __getattr__(self, name):
        return getattr(self.dev, name)
