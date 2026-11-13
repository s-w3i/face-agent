"""Local STT has no API key/socket dependency and uses the completed WAV."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from respeaker_stt import Transcriber
from faster_whisper.audio import decode_audio


class TranscriberTest(unittest.TestCase):
    def test_installed_audio_decoder_reads_a_real_recording_format(self):
        # Detect PyAV/Faster Whisper incompatibility without downloading a model.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'microphone.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                wav.writeframes(bytes(16000 * 2))
            audio = decode_audio(str(path), sampling_rate=16000)
            self.assertEqual(audio.shape, (16000,))
            self.assertEqual(float(abs(audio).max()), 0)

    def test_local_cpu_int8_and_completed_transcript(self):
        with patch('respeaker_stt.WhisperModel') as model:
            transcriber = Transcriber()
            model.assert_called_once_with('small', device='cpu', compute_type='int8',
                                          cpu_threads=4, num_workers=1)
            # Faster Whisper returns a lazy generator; consume it before emitting final text.
            model.return_value.transcribe.return_value = (
                iter([SimpleNamespace(text=' one, two '), SimpleNamespace(text='nine.')]), Mock())
            result = transcriber.transcribe(Path('recorded.wav'))
            self.assertEqual(result['text'], 'one, two nine.')
            model.return_value.transcribe.assert_called_once_with('recorded.wav', beam_size=5,
                                                                 language='en', vad_filter=False)

    def test_wake_name_hint_is_only_local(self):
        with patch('respeaker_stt.WhisperModel') as model:
            transcriber = Transcriber(prompt='The robot is named Kuro.')
            model.return_value.transcribe.return_value = (iter([SimpleNamespace(text='Hi Kuro.')]), Mock())
            self.assertEqual(transcriber.transcribe('wake.wav')['text'], 'Hi Kuro.')
            self.assertEqual(model.return_value.transcribe.call_args.kwargs['initial_prompt'], 'The robot is named Kuro.')

    def test_language_detection_and_empty_transcript(self):
        with patch('respeaker_stt.WhisperModel') as model:
            transcriber = Transcriber('base', 'auto')
            model.return_value.transcribe.return_value = (iter([]), Mock())
            self.assertEqual(transcriber.transcribe('silence.wav')['text'], '')
            self.assertIsNone(model.return_value.transcribe.call_args.kwargs['language'])

    def test_model_and_decode_errors_propagate_to_microphone_owner(self):
        with patch('respeaker_stt.WhisperModel', side_effect=RuntimeError('Model missing')):
            with self.assertRaises(RuntimeError): Transcriber()
        with patch('respeaker_stt.WhisperModel') as model:
            transcriber = Transcriber()
            model.return_value.transcribe.side_effect = RuntimeError('Corrupt WAV')
            with self.assertRaises(RuntimeError): transcriber.transcribe('bad.wav')


if __name__ == '__main__':
    unittest.main()
