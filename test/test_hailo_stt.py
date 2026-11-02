"""Hailo Whisper fallback without a device: features match Whisper's, the decode loop is greedy."""
from pathlib import Path
import sys
import unittest

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hailo_stt


def mel_filters():
    """Whisper's 80-bin filterbank, as shipped with faster-whisper (no file needed)."""
    from faster_whisper.feature_extractor import FeatureExtractor
    return FeatureExtractor(feature_size=80).mel_filters


class FeatureTest(unittest.TestCase):
    def test_log_mel_matches_faster_whisper(self):
        try:
            from faster_whisper.feature_extractor import FeatureExtractor
        except ImportError:  # pragma: no cover
            self.skipTest('faster-whisper not installed')
        audio = (np.random.default_rng(1).standard_normal(16000 * 2) * .1).astype(np.float32)
        ours = hailo_stt.log_mel(audio, 5, mel_filters())
        theirs = FeatureExtractor(feature_size=80)(np.pad(audio, (0, 16000 * 3)), padding=0)[:, :500]
        self.assertEqual(ours.shape, (80, 500))
        self.assertLess(float(np.abs(ours - theirs).max()), 1e-4)


class Buffer:
    def __init__(self, shape=None):
        self.value = None; self.shape = shape

    def set_buffer(self, value):
        self.value = value

    def get_buffer(self):
        return self.value


class FakeBindings:
    def __init__(self, names):
        self.slots = {name: Buffer() for name in names}

    def input(self, name=None):
        return self.slots[name or 'in']

    def output(self, name=None):
        return self.slots[name or 'out']


class FakeRun:
    def __init__(self, names, on_run):
        self.names = names; self.on_run = on_run; self.runs = 0

    def create_bindings(self):
        return FakeBindings(self.names)

    def run(self, bindings, timeout):
        self.runs += 1
        self.on_run(bindings[0])


class DecodeTest(unittest.TestCase):
    def transcriber(self, script, vocab=60000, length=8):
        """A Transcriber whose decoder emits `script` (token ids) one step at a time."""
        t = object.__new__(hailo_stt.Transcriber)
        t.seconds = 5; t.length = length; t.network = 'dec'; t.prefix = hailo_stt.ENGLISH_PREFIX
        t.outputs = t.logit_outputs = ['dec/conv1', 'dec/conv2']
        t.embedding = np.zeros((vocab, 4), np.float32); t.positions = np.zeros((length, 4), np.float32)
        t.filters = mel_filters()
        half = vocab // 2
        t.encoder = type('M', (), {'output': lambda self: Buffer((1, 250, 4))})()
        t.decoder = type('M', (), {'output': lambda self, name: Buffer((1, length, half))})()
        seen = []
        def encode(b):
            self.assertEqual(b.input().value.shape, (1, 1, 500, 80))  # NHWC log-mel
            seen.clear()  # Each window decodes from step 0.
            b.output().set_buffer(np.ones((1, 250, 4), np.float32))
        def decode(b):
            step = len(seen)
            seen.append(b.input('dec/input_layer2').value.copy())
            logits = np.zeros((1, length, vocab), np.float32)
            position = step + len(t.prefix)  # The decoder reads the logits after the forced prefix.
            logits[0, position, script[step]] = 5
            logits[0, position, 11] = 4.9  # punctuation runner-up, never penalized
            b.slots['dec/conv1'].set_buffer(logits[:, :, :half]); b.slots['dec/conv2'].set_buffer(logits[:, :, half:])
        t.encoder_run = FakeRun(['in', 'out'], encode)
        t.decoder_run = FakeRun(['dec/input_layer1', 'dec/input_layer2', 'dec/conv1', 'dec/conv2'], decode)
        t.tokenizer = type('T', (), {'decode': lambda self, ids, skip_special_tokens=True: ' '.join(map(str, ids))})()
        return t, seen

    def test_greedy_decode_stops_at_end_token_and_feeds_previous_tokens(self):
        t, seen = self.transcriber([200, 300, 400, hailo_stt.END, 7], length=10)
        text = t.transcribe_audio(np.zeros(16000, np.float32))
        self.assertEqual(text, '200 300 400')
        self.assertEqual(t.decoder_run.runs, 4)
        self.assertEqual(seen[0].shape, (1, 10, 1, 4))  # (1, seq, 1, dim) like the HEF input

    def test_long_audio_is_split_into_model_windows(self):
        t, _ = self.transcriber([hailo_stt.END] * 8)
        t.transcribe_audio(np.zeros(16000 * 12, np.float32))  # 12 s -> three 5 s windows
        self.assertEqual(t.encoder_run.runs, 3)

    def test_repetition_penalty_breaks_loops_but_spares_punctuation(self):
        t, _ = self.transcriber([300, 300, 300, 300, hailo_stt.END, 0, 0], length=12)
        text = t.transcribe_audio(np.zeros(16000, np.float32))
        self.assertEqual(text.split()[0], '300')
        self.assertIn('11', text.split())  # 300 penalized (5/1.5) below the ',' runner-up


if __name__ == '__main__':
    unittest.main()
