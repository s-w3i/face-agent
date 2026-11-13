"""Speaker-independent wake detection: score rolling windows of the audio bus.

The sleeping Whisper path waited for the end of an utterance, so in a noisy room
(TV, other people) a segment ran to its 10 s limit, transcription then left the
microphone deaf for about as long again, and the phrase had to open the transcript.
Here the last WINDOW seconds are scored every HOP seconds while there is speech, so
nothing waits for silence and nothing is missed while a window is being scored.

The scorer is any callable taking float32 PCM (16 kHz) and returning a score that a
threshold is compared against. HailoPhraseScorer asks Whisper base on the Hailo-8L how
likely the window is to say "Hi, <name>" (forced decoding, mean token log-probability):
speaker-independent and untrained, unlike openWakeWord's synthetic-voice models. Hits
look like openWakeWord's, so the worker and chat.py treat both engines the same.
"""
from collections import deque
import os
import re
import threading
import time

import numpy as np
import webrtcvad

RATE = 16000
BLOCK = 320          # 20 ms, the bus block and a WebRTC VAD frame size
WINDOW = 3.0         # seconds scored per window: "Hi Kuro" plus slack on both sides
HOP = 1.5            # seconds between windows; a phrase of up to WINDOW - HOP lands whole in one
MAX_LAG = 2.0        # seconds behind real time after which windows are skipped (never audio)
NICE = 10            # the wake thread and the CPU check's threads yield to audio capture and display
VERIFY_GRACE = 30.0  # stall allowance during one CPU check: ~2 s normally, far longer at nice +10 on a loaded Pi
MIN_VOICED = .3      # seconds of speech a window needs before it is scored
REFRACTORY = 2.0     # seconds after a hit in which the same phrase cannot fire again
ONSET_PAD = .2       # seconds kept before the first speech of a hit window
# Mean log-probability of the phrase tokens ("Hi, Kuro" / "Hey Kuro"). Windows at or above
# DEFAULT_THRESHOLD wake at once; windows between VERIFY_FLOOR and it wake only if CPU Whisper
# small (prompted with the name) also hears the phrase. Measured results: docs/wake-word.md.
DEFAULT_THRESHOLD = -3.3
VERIFY_FLOOR = -5.5  # Over a loud TV: 25 of 30 real "Hi Kuro"s, 2% of TV-only windows; none caught below it.


class RollingWakeDetector:
    """Window bookkeeping and the hit rule; no threads or hardware, so it is easy to test.

    A window at or above `threshold` wakes at once. If the scorer has `verify(audio)`, a
    window between `verify_floor` and `threshold` becomes a candidate instead: one phrase
    spans about three overlapping windows, so only the best of a run of candidates is
    verified, one hop after its peak, which saves two thirds of the CPU checks.
    """

    def __init__(self, scorer, threshold, window=WINDOW, hop=HOP, min_voiced=MIN_VOICED,
                 refractory=REFRACTORY, vad_mode=2, vad=None, verify_floor=VERIFY_FLOOR):
        self.scorer = scorer; self.threshold = threshold; self.verify_floor = verify_floor
        self.verify = getattr(scorer, 'verify', None)
        self.window = window; self.hop = hop; self.min_voiced = min_voiced; self.refractory = refractory
        self.vad = vad or webrtcvad.Vad(vad_mode)
        self.blocks = deque(maxlen=round(window * RATE / BLOCK))  # (pcm bytes, t_start, voiced)
        self.pending = bytearray()
        self.pending_t = None
        self.blocks_seen = 0      # Audio clock: windows and refractory count blocks, not capture times.
        self.next_eval = round(window * RATE / BLOCK)
        self.last_hit = None
        self.candidate = None     # Best borderline window of the current run, awaiting its peak
        self.windows = self.detections = self.verifications = self.skipped = 0
        self.may_score = True
        self.score_seconds = self.verify_seconds = 0.0
        self.scores = deque()  # (t, score) for the last 10 s

    def feed(self, pcm, t_start, may_score=True):
        """Add PCM captured from monotonic `t_start`; return a list of hit dicts.

        `may_score=False` keeps the audio and the clock but skips any window due now:
        the thread uses it to catch up after a slow window instead of dropping audio.
        """
        self.may_score = may_score
        if not self.pending:
            self.pending_t = t_start
        self.pending += pcm
        hits = []
        while len(self.pending) >= BLOCK * 2:
            frame = bytes(self.pending[:BLOCK * 2])
            del self.pending[:BLOCK * 2]
            t = self.pending_t
            self.pending_t += BLOCK / RATE
            self.blocks.append((frame, t, self.vad.is_speech(frame, RATE)))
            self.blocks_seen += 1
            hit = self._maybe_score(t + BLOCK / RATE)
            if hit:
                hits.append(hit)
        return hits

    def _maybe_score(self, t_end):
        if self.blocks_seen < self.next_eval:
            return None
        self.next_eval = self.blocks_seen + round(self.hop * RATE / BLOCK)
        if not self.may_score:
            self.skipped += 1
            return None
        if self.last_hit is not None and self.blocks_seen - self.last_hit < round(self.refractory * RATE / BLOCK):
            self.candidate = None
            return None
        voiced = [block for block in self.blocks if block[2]]
        if len(voiced) * BLOCK / RATE < self.min_voiced:
            return self._verify_candidate()
        audio = np.frombuffer(b''.join(block[0] for block in self.blocks), '<i2').astype(np.float32) / 32768
        started = time.monotonic()
        score = float(self.scorer(audio))
        self.score_seconds += time.monotonic() - started
        self.windows += 1
        self.scores.append((t_end, score))
        while self.scores and self.scores[0][0] < t_end - 10:
            self.scores.popleft()
        # t: where a follow-on command capture starts. It includes the phrase itself
        # ("Hi Kuro, what time is it?"), which the agent prompt already handles.
        # continue_from: the window end, where "still speaking" is checked.
        window = dict(score=score, audio=audio, t=max(self.blocks[0][1], voiced[0][1] - ONSET_PAD), continue_from=t_end)
        if score >= self.threshold:
            self.candidate = None
            return self._hit(window)
        if self.verify is not None and score >= self.verify_floor and \
                (self.candidate is None or score > self.candidate['score']):
            self.candidate = window  # Still rising: verify the peak one hop later.
            return None
        return self._verify_candidate()

    def _verify_candidate(self):
        candidate, self.candidate = self.candidate, None
        if candidate is None:
            return None
        self.verifications += 1
        started = time.monotonic()
        heard = self.verify(candidate['audio'])
        self.verify_seconds += time.monotonic() - started
        return self._hit(candidate, verified=True) if heard else None

    def _hit(self, window, verified=False):
        self.detections += 1
        self.last_hit = self.blocks_seen
        return dict(status='wake_detected', score=round(window['score'], 3), t=window['t'],
                    continue_from=window['continue_from'], model='rolling', verified=verified)

    def max_score(self):
        return max((score for _, score in self.scores), default=0.0)


def available():
    """(True, '') when the Hailo runtime and Whisper base HEFs are installed, else (False, reason)."""
    import importlib.util
    if importlib.util.find_spec('hailo_platform') is None:
        return False, 'hailo_platform is not installed'
    import hailo_stt
    for part in ('encoder', 'decoder'):
        if next(hailo_stt.MODEL_DIR.glob(f'base-whisper-{part}-*_h8l.hef'), None) is None:
            return False, f'no Whisper base {part} HEF in {hailo_stt.MODEL_DIR}'
    return True, ''


def wake_phrases(name):
    """Spoken forms scored per window; the robot name comes from the studio, so a rename just works."""
    name = ' '.join(str(name).split()) or 'Kuro'
    # Measured on the owner's 73 clips: " Hi, Kuro" alone wins on 61; " Hey ..." never false-woke.
    return [f' Hi, {name}', f' Hey {name}']


def heard_phrase(text, name):
    """Loose text match for the CPU check: Whisper writes "Hi Kuro" as "High Kuro", "I, Kuro", "Aikuro"."""
    name = re.escape(' '.join(str(name).split()))
    return bool(re.search(r'\b(?:hi|hey|high|hai|aye|ai|i)\W*' + name + r'\b|\bai\s?' + name + r'\b', text, re.IGNORECASE))


class HailoPhraseScorer:
    """Mean forced log-probability of "Hi, <name>" / "Hey <name>" from Whisper base on the Hailo-8L.

    `verify(audio)` re-checks borderline windows with `verifier` (audio -> transcript; CPU
    Whisper small by default). That recovers accents and noisy rooms the Hailo score is
    unsure about without accepting the near misses it is also unsure about.
    """

    def __init__(self, name, model='base', transcriber=None, verifier='load'):
        self.shared = transcriber is None
        if transcriber is None:
            import hailo_stt
            transcriber = hailo_stt.shared(model)
        self.transcriber = transcriber
        self.reconnects = 0
        self.name = ' '.join(str(name).split()) or 'Kuro'
        self.phrases = [transcriber.phrase_tokens(text) for text in wake_phrases(self.name)]
        self.verifier = cpu_verifier(self.name) if verifier == 'load' else verifier
        if self.verifier is None:
            self.verify = None
        self.last_text = None

    def __call__(self, audio):
        try:
            return self.transcriber.phrase_logprob(audio, self.phrases)
        except Exception:
            if not self.shared:
                raise
            # hailort.service restarted (it can crash when another client disconnects):
            # reconnect once like the face tracker; a second failure restarts the thread.
            import hailo_stt
            self.transcriber = hailo_stt.replace_shared(self.transcriber)
            self.reconnects += 1
            return self.transcriber.phrase_logprob(audio, self.phrases)

    def verify(self, audio):
        self.last_text = self.verifier(audio)
        return heard_phrase(self.last_text, self.name)


def cpu_verifier(name, model='small', context_frames=1000, cpu_threads=3):
    """Whisper small on the CPU, prompted with the name, for windows the Hailo score is unsure of.

    Base-size models miss most "Hi Kuro"s over a loud TV; small hears about two thirds.
    faster-whisper pads every input to 30 s (about 6 s per window on the Pi 5), so the
    encoder gets a 10 s context instead: 1.5 s per window, 19 of 30 TV clips vs 21 at 30 s.
    """
    import ctranslate2
    from faster_whisper import WhisperModel
    whisper = WhisperModel(model, device='cpu', compute_type='int8', cpu_threads=cpu_threads)
    tokenizer = whisper.hf_tokenizer
    special = tokenizer.token_to_id
    language = [] if model.endswith('.en') else [special('<|en|>'), special('<|transcribe|>')]
    prompt = ([special('<|startofprev|>')] + tokenizer.encode(f' The robot is named {name}.', add_special_tokens=False).ids
              + [special('<|startoftranscript|>')] + language + [special('<|notimestamps|>')])

    def transcribe(audio):
        features = whisper.feature_extractor(audio, padding=0)
        features = np.pad(features, ((0, 0), (0, max(0, context_frames - features.shape[-1]))))[:, :context_frames]
        encoded = whisper.model.encode(ctranslate2.StorageView.from_array(
            np.ascontiguousarray(features[None].astype(np.float32))), to_cpu=False)
        result = whisper.model.generate(encoded, [prompt], beam_size=1, max_length=40)
        return tokenizer.decode(result[0].sequences_ids[0]).strip()
    return transcribe


class RollingWakeThread(threading.Thread):
    """Same interface as wake_word.WakeWordThread, so the worker can run either."""

    def __init__(self, bus, threshold, scorer_factory, on_detect=None, stop=None, detected=None, **detector):
        super().__init__(name='wake-word', daemon=False)
        self.bus = bus; self.threshold = threshold; self.scorer_factory = scorer_factory
        self.detector_options = detector
        self.on_detect = on_detect or (lambda event: None)
        self.stop_event = stop or threading.Event()
        self.wake_word_detected = detected or threading.Event()
        self.ready = threading.Event()
        from worker_threads import Heartbeat
        # A window takes up to ~1 s on the Hailo-8L; a scorer stuck longer than this is a stall.
        self.stall_limit = 5.0
        self.heartbeat = Heartbeat('wake-word', self.stall_limit)
        self.heartbeat.paused = True  # The model load is not a stall.
        self.error = None
        self.detector = None
        self.scorer = None
        self.sub = None
        self.cpu_seconds = 0.0

    def run(self):
        # Room for ~10 s of audio: a slow window must not drop speech (drop-oldest queue).
        self.sub = self.bus.subscribe('wake', maxsize=500)
        try:
            try:
                # Per-thread nice on Linux; CTranslate2's worker threads, created while loading
                # the CPU check below, inherit it.
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
            except (AttributeError, OSError):
                pass
            self.scorer = self.scorer_factory()
            self.detector = RollingWakeDetector(self.scorer, self.threshold, **self.detector_options)
            if self.detector.verify is not None:
                self.detector.verify = self._allow_slow(self.detector.verify)
            self.ready.set()
            self.heartbeat.paused = False
            while not self.stop_event.is_set():
                self.heartbeat.beat()
                block = self.sub.get(.2)
                if block is None:
                    continue
                started = time.thread_time()
                hits = self.detector.feed(block.pcm, block.t, may_score=time.monotonic() - block.t < MAX_LAG)
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
            close = getattr(self.scorer, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    pass

    def _allow_slow(self, verify):
        """A slow CPU check under load is not a stall; restarting the worker would only drop audio."""
        def checked(audio):
            self.heartbeat.limit = VERIFY_GRACE
            try:
                return verify(audio)
            finally:
                self.heartbeat.limit = self.stall_limit
                self.heartbeat.beat()
        return checked

    def stop(self, timeout=1.0):
        self.stop_event.set()
        if self.sub:
            self.sub.close()
        self.join(timeout)

    def stats(self):
        detector = self.detector
        return dict(frames=detector.windows if detector else 0,
                    max_score_last_10s=round(detector.max_score(), 3) if detector else 0.0,
                    detections=detector.detections if detector else 0,
                    dropped=self.sub.dropped if self.sub else 0,
                    cpu_seconds=round(self.cpu_seconds, 3),
                    score_seconds=round(detector.score_seconds, 3) if detector else 0.0,
                    verifications=detector.verifications if detector else 0,
                    verify_seconds=round(detector.verify_seconds, 3) if detector else 0.0,
                    skipped=detector.skipped if detector else 0,
                    reconnects=getattr(self.scorer, 'reconnects', 0))
