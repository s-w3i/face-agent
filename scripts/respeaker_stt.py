"""Local Faster Whisper transcription after an utterance has finished."""
import time
from faster_whisper import WhisperModel

MODEL = 'small'


class Transcriber:
    def __init__(self, model=MODEL, language='en', prompt=None):
        self.language = None if language == 'auto' else language
        self.prompt = prompt
        self.model = WhisperModel(model, device='cpu', compute_type='int8',
                                  cpu_threads=4, num_workers=1)

    def transcribe(self, path):
        started = time.monotonic()
        options = dict(beam_size=5, language=self.language, vad_filter=False)
        if self.prompt:
            options['initial_prompt'] = self.prompt
        segments, _ = self.model.transcribe(str(path), **options)
        text = ' '.join(segment.text.strip() for segment in segments).strip()
        return {'text': text, 'stt_seconds': round(time.monotonic() - started, 3)}
