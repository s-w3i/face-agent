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

    def test_phrase_logprob_is_mean_forced_logprob_and_reuses_shared_prefixes(self):
        vocab, length = 60000, 10
        t, _ = self.transcriber([], vocab=vocab, length=length)
        t.embedding = np.repeat(np.arange(vocab, dtype=np.float32)[:, None], 4, axis=1)  # embedding = token id
        t.prefix = (1,)  # small vocabulary: one forced prefix token
        fed = []
        def decode(b):
            ids = b.input('dec/input_layer2').value[0, :, 0, 0].astype(int)
            fed.append(ids)
            logits = np.zeros((1, length, vocab), np.float32)
            logits[0, :, 5] = np.log(3.0)  # token 5 three times as likely as each other token
            b.slots['dec/conv1'].set_buffer(logits[:, :, :vocab // 2]); b.slots['dec/conv2'].set_buffer(logits[:, :, vocab // 2:])
        t.decoder_run.on_run = decode
        score = t.phrase_logprob(np.zeros(16000, np.float32), [[5, 2], [5, 3, 4]])
        p5, other = np.log(3 / (vocab + 2)), np.log(1 / (vocab + 2))
        self.assertAlmostEqual(score, max((p5 + other) / 2, (p5 + 2 * other) / 3), places=5)
        self.assertEqual(t.encoder_run.runs, 1)
        self.assertEqual(t.decoder_run.runs, 3)  # contexts [S,1], [S,1,5] (shared), [S,1,5,3]
        self.assertEqual(list(fed[0][:2]), [hailo_stt.START, 1])

    def test_long_audio_is_split_into_model_windows(self):
        t, _ = self.transcriber([hailo_stt.END] * 8)
        t.transcribe_audio(np.zeros(16000 * 12, np.float32))  # 12 s -> three 5 s windows
        self.assertEqual(t.encoder_run.runs, 3)

    def test_repetition_penalty_breaks_loops_but_spares_punctuation(self):
        t, _ = self.transcriber([300, 300, 300, 300, hailo_stt.END, 0, 0], length=12)
        text = t.transcribe_audio(np.zeros(16000, np.float32))
        self.assertEqual(text.split()[0], '300')
        self.assertIn('11', text.split())  # 300 penalized (5/1.5) below the ',' runner-up



class SharedSessionTest(unittest.TestCase):
    def test_one_session_per_process_replaced_after_failure_and_followed_by_offline_stt(self):
        from unittest.mock import patch
        made = []
        class Fake:
            def __init__(self, model, language):
                made.append(self); self.closed = False
            def transcribe(self, path):
                return {'text': str(len(made))}
            def close(self):
                self.closed = True
        hailo_stt.close_shared()
        with patch.object(hailo_stt, 'Transcriber', Fake):
            first = hailo_stt.shared('base', 'en')
            self.assertIs(hailo_stt.shared('base', 'en'), first)
            offline = hailo_stt.SharedTranscriber('base', 'en')
            self.assertEqual(offline.transcribe('x.wav'), {'text': '1'})
            second = hailo_stt.replace_shared(first, delay=0)
            self.assertTrue(first.closed)
            self.assertIsNot(second, first)
            self.assertEqual(offline.transcribe('x.wav'), {'text': '2'})  # Follows the new session.
            offline.close()
            self.assertFalse(second.closed)
            hailo_stt.close_shared()
            self.assertTrue(second.closed)

if __name__ == '__main__':
    unittest.main()
