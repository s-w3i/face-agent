"""T2: always-on openWakeWord detection on the shared AudioBus.

The model never leaves the worker and neither does the audio: a hit only emits a
small `wake_detected` event (score, capture time). Engine choice falls back to the
local Whisper wake path whenever the trained model is missing or was trained for
a different character name.
"""
from collections import deque
import json
import os
from pathlib import Path
import threading
import time

import numpy as np

RATE = 16000
FRAME_SAMPLES = 1280  # openWakeWord consumes 80 ms frames.
WAKE_DIR = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'face-agent' / 'wake'
DEFAULT_MODEL = WAKE_DIR / 'hi_kuro.onnx'
DEFAULT_THRESHOLD = 0.5


def default_model_path():
    return Path(os.environ.get('DOTS_WAKE_MODEL_PATH') or DEFAULT_MODEL).expanduser()


def metadata(model_path):
    """`<model>.json` beside the model: {"phrase", "name", "threshold"}; {} if absent or invalid."""
    path = Path(model_path).with_suffix('.json')
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def choose_engine(requested, model_path, name):
    """Return (engine, threshold, reason). `requested` is auto, oww or whisper."""
    requested = (requested or 'auto').strip().lower()
    if requested == 'whisper':
        return 'whisper', None, 'whisper requested'
    model_path = Path(model_path).expanduser()
    if not model_path.is_file():
        return 'whisper', None, f'no wake model at {model_path}'
    info = metadata(model_path)
    threshold = info.get('threshold', DEFAULT_THRESHOLD)
    if not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
        threshold = DEFAULT_THRESHOLD
    trained_for = str(info.get('name', '')).strip()
    if requested != 'oww' and trained_for.lower() != str(name).strip().lower():
        return 'whisper', None, (f'wake model {model_path.name} was trained for "{trained_for or "?"}", '
                                 f'but the robot is named "{name}"')
    return 'oww', float(threshold), 'openWakeWord model ' + model_path.name


def load_model(model_path):
    """openWakeWord ONNX model; shared feature models come from the model's directory if present."""
    from openwakeword.model import Model
    model_path = Path(model_path)
    features = {}
    for key, filename in (('melspec_model_path', 'melspectrogram.onnx'), ('embedding_model_path', 'embedding_model.onnx')):
        candidate = model_path.with_name(filename)
        if not candidate.exists():
            candidate = WAKE_DIR / filename
        if candidate.exists():
            features[key] = str(candidate)
    model = Model(wakeword_models=[str(model_path)], inference_framework='onnx', **features)
    # Streaming only reads the last ~1760 samples, but the stock 10 s deque is copied into a
    # list on every frame; a 1 s buffer gives identical features at about 60% of the CPU.
    model.preprocessor.raw_data_buffer = deque(maxlen=RATE)
    return model


class WakeDetector:
    """Threshold + patience + refractory over 80 ms frames; no threads, so it is easy to test."""

    def __init__(self, model, threshold=DEFAULT_THRESHOLD, patience=2, refractory=1.5):
        self.model = model; self.threshold = threshold
        self.patience = patience; self.refractory = refractory
        self.pending = bytearray()
        self.pending_end = None  # Capture time just after the newest pending sample.
        self.streak = 0
        self.last_hit = -float('inf')
        self.frames = self.detections = 0
        self.scores = deque()  # (t, score) for the last 10 s

    def feed(self, pcm, t_start):
        """Add PCM captured from monotonic `t_start`; return a list of hit dicts."""
        hits = []
        self.pending += pcm
        self.pending_end = t_start + len(pcm) / 2 / RATE
        while len(self.pending) >= FRAME_SAMPLES * 2:
            frame = np.frombuffer(bytes(self.pending[:FRAME_SAMPLES * 2]), '<i2')
            del self.pending[:FRAME_SAMPLES * 2]
            t_end = self.pending_end - len(self.pending) / 2 / RATE
            name, score = max(self.model.predict(frame).items(), key=lambda item: item[1])
            score = float(score)
            self.frames += 1
            self.scores.append((t_end, score))
            while self.scores and self.scores[0][0] < t_end - 10:
                self.scores.popleft()
            self.streak = self.streak + 1 if score >= self.threshold else 0
            if self.streak >= self.patience and t_end - self.last_hit >= self.refractory:
                self.detections += 1
                self.last_hit = t_end
                self.streak = 0
                hits.append(dict(status='wake_detected', score=round(score, 3), t=t_end, model=name))
                # One phrase must not produce several hits from the model's internal buffers.
                self.model.reset()
            elif self.streak >= self.patience:
                self.streak = 0  # Still refractory: the same phrase, not a new one.
        return hits

    def max_score(self):
        return max((score for _, score in self.scores), default=0.0)


class WakeWordThread(threading.Thread):
    """Read the 'wake' subscription and call `on_detect(event)` for every hit (thread-safe callback)."""

    def __init__(self, bus, model_path=None, threshold=DEFAULT_THRESHOLD, patience=2, refractory=1.5,
                 on_detect=None, stop=None, model_factory=None, detected=None):
        super().__init__(name='wake-word', daemon=False)
        self.bus = bus; self.model_path = model_path
        self.threshold = threshold; self.patience = patience; self.refractory = refractory
        self.on_detect = on_detect or (lambda event: None)
        self.stop_event = stop or threading.Event()
        self.model_factory = model_factory or (lambda: load_model(self.model_path))
        # Set on each hit; cleared by whoever acts on it, never by this thread.
        self.wake_word_detected = detected or threading.Event()
        self.ready = threading.Event()
        from worker_threads import Heartbeat
        self.heartbeat = Heartbeat('wake-word', 2.0)
        self.heartbeat.paused = True  # The model load is not a stall.
        self.error = None
        self.detector = None
        self.sub = None
        self.cpu_seconds = 0.0

    def run(self):
        self.sub = self.bus.subscribe('wake', maxsize=50)
        try:
            self.detector = WakeDetector(self.model_factory(), self.threshold, self.patience, self.refractory)
            self.ready.set()
            self.heartbeat.paused = False
            while not self.stop_event.is_set():
                self.heartbeat.beat()
                block = self.sub.get(.2)
                if block is None:
                    continue
                started = time.thread_time()
                hits = self.detector.feed(block.pcm, block.t)
                self.cpu_seconds += time.thread_time() - started
                for hit in hits:
                    self.wake_word_detected.set()
                    self.on_detect(hit)
        except BaseException as error:  # Reported to the worker's main loop, never swallowed.
            self.error = error
        finally:
            self.heartbeat.paused = True
            self.ready.set()
            self.bus.unsubscribe(self.sub)

    def stop(self, timeout=1.0):
        self.stop_event.set()
        if self.sub:
            self.sub.close()
        self.join(timeout)

    def stats(self):
        detector = self.detector
        return dict(frames=detector.frames if detector else 0,
                    max_score_last_10s=round(detector.max_score(), 3) if detector else 0.0,
                    detections=detector.detections if detector else 0,
                    dropped=self.sub.dropped if self.sub else 0,
                    cpu_seconds=round(self.cpu_seconds, 3))
