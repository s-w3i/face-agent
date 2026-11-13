"""Offline Whisper tiny/base on the Hailo-8L: same interface as respeaker_stt.Transcriber.

Encoder and decoder both run on the accelerator (Hailo's hailo8l HEFs from the
Application Code Examples speech_recognition app); the host does the log-mel
spectrogram (numpy, identical to Whisper's), token/positional embedding lookup and
greedy decoding with a light repetition penalty. One long-lived VDevice session per
process: hailort.service 4.23 has crashed when sharing clients tear sessions down.
"""
import os
import threading
from pathlib import Path
import time
import wave

import numpy as np

RATE, N_FFT, HOP = 16000, 400, 160
MODEL_DIR = Path(os.environ.get('DOTS_HAILO_WHISPER_DIR',
                                Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'face-agent/whisper-hailo8l'))
START, END = 50258, 50257  # <|startoftranscript|>, <|endoftext|>
ENGLISH_PREFIX = (50259, 50359, 50363)  # <|en|><|transcribe|><|notimestamps|>: stops language drift
PUNCTUATION = (11, 13)     # ',' '.': exempt from the repetition penalty


def log_mel(audio, seconds, filters):
    """Whisper's 80-bin log-mel spectrogram, padded/trimmed to `seconds` (numpy only)."""
    samples = seconds * RATE
    audio = np.pad(audio[:samples], (0, max(0, samples - len(audio))))
    padded = np.pad(audio, N_FFT // 2, mode='reflect')
    window = np.hanning(N_FFT + 1)[:-1]
    frames = 1 + (len(padded) - N_FFT) // HOP
    index = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    power = (np.abs(np.fft.rfft(padded[index] * window, axis=1)) ** 2).T[:, :-1]
    spectrum = np.log10(np.clip(filters @ power, 1e-10, None))
    spectrum = np.maximum(spectrum, spectrum.max() - 8.0)
    return ((spectrum + 4.0) / 4.0).astype(np.float32)


def read_wav(path):
    with wave.open(str(path)) as file:
        if file.getframerate() != RATE or file.getnchannels() != 1:
            raise ValueError('Expected 16 kHz mono PCM16.')
        return np.frombuffer(file.readframes(file.getnframes()), '<i2').astype(np.float32) / 32768


def tokenizer_path():
    """The multilingual Whisper vocabulary is shared by tiny/base/small; reuse a cached copy."""
    explicit = MODEL_DIR / 'tokenizer.json'
    if explicit.exists():
        return explicit
    hub = Path(os.environ.get('HF_HOME', Path.home() / '.cache/huggingface')) / 'hub'
    for name in ('base', 'small', 'tiny'):
        found = sorted(hub.glob(f'models--Systran--faster-whisper-{name}/snapshots/*/tokenizer.json'))
        if found:
            return found[0]
    raise FileNotFoundError('No Whisper tokenizer.json; copy one to ' + str(explicit))


class Transcriber:
    def __init__(self, model='base', language='en', prompt=None):
        from hailo_platform import HEF, FormatType, HailoSchedulingAlgorithm, VDevice
        from tokenizers import Tokenizer
        if model not in ('tiny', 'base'):
            raise ValueError('Hailo Whisper supports tiny or base.')
        encoder = next(MODEL_DIR.glob(f'{model}-whisper-encoder-*_h8l.hef'), None)
        decoder = next(MODEL_DIR.glob(f'{model}-whisper-decoder-*_h8l.hef'), None)
        if encoder is None or decoder is None:
            raise FileNotFoundError(f'Missing Hailo Whisper {model} HEFs in {MODEL_DIR}')
        self.embedding = np.load(MODEL_DIR / f'token_embedding_weight_{model}.npy')
        self.positions = np.load(MODEL_DIR / f'onnx_add_input_{model}.npy')
        from faster_whisper.feature_extractor import FeatureExtractor
        self.filters = FeatureExtractor(feature_size=80).mel_filters  # Whisper's own filterbank.
        self.prefix = ENGLISH_PREFIX if language == 'en' else ()
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path()))
        encoder_hef, decoder_hef = HEF(str(encoder)), HEF(str(decoder))
        self.seconds = int(encoder_hef.get_input_vstream_infos()[0].shape[1] / 100)  # 5 s base, 10 s tiny
        self.outputs = decoder_hef.get_sorted_output_names()
        self.logit_outputs = [name for name in self.outputs if 'conv' in name]
        self.network = decoder_hef.get_network_group_names()[0]
        self.length = decoder_hef.get_output_vstream_infos()[0].shape[1]
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        params.multi_process_service = True  # Share the 8L with the face tracker.
        params.group_id = 'SHARED'
        self.device = VDevice(params)
        self._contexts = []
        try:
            self.encoder = self.device.create_infer_model(str(encoder))
            self.decoder = self.device.create_infer_model(str(decoder))
            self.encoder.input().set_format_type(FormatType.FLOAT32)
            self.encoder.output().set_format_type(FormatType.FLOAT32)
            for layer in ('input_layer1', 'input_layer2'):
                self.decoder.input(f'{self.network}/{layer}').set_format_type(FormatType.FLOAT32)
            for name in self.outputs:
                self.decoder.output(name).set_format_type(FormatType.FLOAT32)
            for model_ in (self.encoder, self.decoder):
                context = model_.configure()
                self._contexts.append(context)
            self.encoder_run = self._contexts[0].__enter__()
            self.decoder_run = self._contexts[1].__enter__()
        except BaseException:
            self.close()
            raise

    @property
    def lock(self):
        """Serializes the accelerator between the wake thread and offline transcription."""
        if '_lock' not in self.__dict__:
            self._lock = threading.Lock()
        return self._lock

    def transcribe_audio(self, audio):
        if np.max(np.abs(audio)) < .1:
            audio = audio * 10.0  # +20 dB for quiet input, as in Hailo's example
        chunks = [audio[start:start + self.seconds * RATE] for start in range(0, max(1, len(audio)), self.seconds * RATE)]
        with self.lock:
            return ' '.join(part for part in (self._chunk(chunk) for chunk in chunks) if part).strip()

    def phrase_logprob(self, audio, phrases):
        """Best mean token log-probability of any of `phrases` (token-id lists) for one window.

        Forced decoding: "how likely is it that this audio says 'Hi, Kuro'?" instead of
        transcribing and searching the text, which Whisper base gets wrong for many accents.
        Phrases sharing a prefix reuse its decoder steps.
        """
        if np.max(np.abs(audio)) < .1:
            audio = audio * 10.0
        start = len(self.prefix)
        with self.lock:
            features = self._encode(audio[:self.seconds * RATE])
            cache = {}
            best = -np.inf
            for phrase in phrases:
                ids = np.zeros((1, self.length), np.int64)
                ids[0, 0] = START
                ids[0, 1:1 + len(self.prefix)] = self.prefix
                total = 0.0
                for offset, token in enumerate(phrase[:self.length - 1 - start]):
                    step = start + offset
                    key = tuple(ids[0, :step + 1])
                    if key not in cache:
                        logits = self._logits(features, ids, step, self.decoder_run.create_bindings()).astype(np.float64)
                        top = logits.max()
                        cache[key] = logits - (top + np.log(np.exp(logits - top).sum()))
                    total += cache[key][token]
                    ids[0, step + 1] = token
                best = max(best, total / max(1, len(phrase)))
            return float(best)

    def phrase_tokens(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def _encode(self, audio):
        mel = log_mel(audio, self.seconds, self.filters)
        mel = np.ascontiguousarray(mel.T[None, None])  # NHWC (1, 1, frames, 80)
        bindings = self.encoder_run.create_bindings()
        bindings.input().set_buffer(mel)
        bindings.output().set_buffer(np.zeros(self.encoder.output().shape, np.float32))
        self.encoder_run.run([bindings], 30000)
        return bindings.output().get_buffer()

    def _logits(self, features, ids, step, decode):
        """Next-token logits after position `step` of `ids` (the decoder takes the full sequence)."""
        embedded = (self.embedding[ids] + self.positions)[:, :, None, :]  # (1, seq, 1, dim)
        decode.input(f'{self.network}/input_layer1').set_buffer(features)
        decode.input(f'{self.network}/input_layer2').set_buffer(np.ascontiguousarray(embedded, np.float32))
        for name in self.outputs:
            decode.output(name).set_buffer(np.zeros(self.decoder.output(name).shape, np.float32))
        self.decoder_run.run([decode], 30000)
        return np.concatenate([decode.output(name).get_buffer() for name in self.logit_outputs], axis=2)[0, step]

    def _chunk(self, audio):
        features = self._encode(audio)
        ids = np.zeros((1, self.length), np.int64)
        ids[0, 0] = START
        ids[0, 1:1 + len(self.prefix)] = self.prefix
        tokens = []
        decode = self.decoder_run.create_bindings()
        for step in range(len(self.prefix), self.length - 1):
            logits = self._logits(features, ids, step, decode)
            for token in set(tokens[-8:]):
                if token not in PUNCTUATION:
                    logits[token] /= 1.5
            token = int(np.argmax(logits))
            if token == END:
                break
            tokens.append(token)
            ids[0, step + 1] = token
        return self.tokenizer.decode(tokens, skip_special_tokens=True).strip()

    def transcribe(self, path):
        started = time.monotonic()
        text = self.transcribe_audio(read_wav(path))
        return {'text': text, 'stt_seconds': round(time.monotonic() - started, 3)}

    def close(self):
        for context in reversed(self._contexts):
            try:
                context.__exit__(None, None, None)
            except Exception:
                pass
        self._contexts = []
        device, self.device = getattr(self, 'device', None), None
        if device is not None:
            try:
                device.release()
            except Exception:
                pass


_shared = {}
_shared_lock = threading.Lock()


def shared(model='base', language='en'):
    """The process's one Transcriber (one VDevice session) for `model`.

    The rolling wake detector and the offline command fallback both use the 8L; a
    second session per process, or tearing one down while another client runs, has
    crashed hailort.service 4.23. Calls are serialized by `Transcriber.lock`.
    """
    with _shared_lock:
        key = (model, language)
        if key not in _shared:
            _shared[key] = Transcriber(model, language)
        return _shared[key]


class SharedTranscriber:
    """Offline-STT handle that always uses the current shared session (survives replace_shared)."""

    backend_name = 'hailo'

    def __init__(self, model='base', language='en'):
        self.key = (model, language)
        shared(*self.key)  # Open now: a load failure falls back to the CPU at startup.

    def transcribe(self, path):
        return shared(*self.key).transcribe(path)

    def close(self):
        pass  # close_shared() releases the session after the wake thread has stopped.


def replace_shared(engine, delay=1.0):
    """After a HailoRT failure (e.g. hailort.service restarted): drop `engine`, open a new session."""
    with _shared_lock:
        for key, value in list(_shared.items()):
            if value is engine:
                del _shared[key]
                break
        else:
            key = ('base', 'en')
    try:
        engine.close()
    except Exception:
        pass
    time.sleep(delay)  # Let a restarting hailort.service come back.
    return shared(*key)


def close_shared():
    with _shared_lock:
        engines = list(_shared.values())
        _shared.clear()
    for engine in engines:
        engine.close()
