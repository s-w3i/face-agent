"""Local Faster Whisper transcription after an utterance has finished."""
import time
from faster_whisper import WhisperModel

MODEL = 'small'


class Transcriber:
    def __init__(self, model=MODEL, language='en', prompt=None, cpu_threads=4, beam_size=5):
        self.language = None if language == 'auto' else language
        self.prompt = prompt
        self.beam_size = beam_size
        # Also the offline fallback for awake commands (tiny.en/base.en, 2 threads).
        self.model = WhisperModel(model, device='cpu', compute_type='int8',
                                  cpu_threads=cpu_threads, num_workers=1)

    def transcribe(self, path):
        started = time.monotonic()
        options = dict(beam_size=self.beam_size, language=self.language, vad_filter=False)
        if self.prompt:
            options['initial_prompt'] = self.prompt
        segments, _ = self.model.transcribe(str(path), **options)
        text = ' '.join(segment.text.strip() for segment in segments).strip()
        return {'text': text, 'stt_seconds': round(time.monotonic() - started, 3)}
