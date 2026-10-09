"""Check hardware-triggered capture, audio endpoints and playback controls offline."""
import asyncio
from contextlib import redirect_stderr
import io
import math
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import respeaker as r


def arguments(path, **changes):
    values = dict(device=None, wav=str(path), control_stdin=False, no_led=True,
                  no_stt=True, stt_model='small', language='en', seconds=0,
                  once=True, silence_seconds=2, max_seconds=30)
    return SimpleNamespace(**{**values, **changes})


class EndpointTest(unittest.TestCase):
    def test_two_seconds_of_audio_and_speech_resets_timer(self):
        endpoint = r.SpeechEndpoint()
        for _ in range(99):
            self.assertFalse(endpoint.update(False, 320))
        self.assertTrue(endpoint.update(False, 320))
        self.assertFalse(endpoint.update(True, 320))
        self.assertEqual(endpoint.silent_samples, 0)
        for _ in range(99):
            self.assertFalse(endpoint.update(False, 320))
        self.assertTrue(endpoint.update(False, 320))

    def test_input_skips_output_only_device_and_rejects_unrelated_default(self):
        with patch('respeaker.sd.query_devices', return_value=[
                dict(name='ReSpeaker playback', max_input_channels=0),
                dict(name='ReSpeaker USB', max_input_channels=6)]), \
                patch('respeaker.sd.check_input_settings') as check:
            self.assertEqual(r.select_input(), 1)
            check.assert_called_once_with(device=1, samplerate=16000, channels=1, dtype='int16')
        with patch('respeaker.sd.query_devices', return_value=[]), \
                patch('respeaker.subprocess.run', return_value=SimpleNamespace(stdout='laptop-mic')):
            with self.assertRaises(r.DeviceUnavailable):
                r.select_input()

    def test_pipewire_default_is_allowed_only_for_respeaker(self):
        with patch('respeaker.sd.query_devices', return_value=[]), \
                patch('respeaker.subprocess.run', return_value=SimpleNamespace(stdout='respeaker_channel0')), \
                patch('respeaker.sd.default.device', (7, 0)), \
                patch('respeaker.sd.check_input_settings'):
            self.assertEqual(r.select_input(), 7)

    def test_saved_wav_is_valid_and_never_overwrites_another_clip(self):
        with tempfile.TemporaryDirectory() as directory, patch('respeaker.emit'):
            output = Path(directory) / 'voice.wav'
            event = dict(pcm=bytes(16000 * 2 * 3), doa_deg=30, utterance_id=1,
                         reason='silence', silence_seconds=2)
            path = r.save_clip(output, 1, event)
            with wave.open(str(path)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getsampwidth()), (1, 16000, 2))
                self.assertEqual(wav.getnframes(), 48000)
            second = r.save_clip(output, 2, event)
            self.assertNotEqual(path, second)
            with self.assertRaises(FileExistsError):
                r.save_clip(output, 1, event)

    def test_usb_disconnect_is_retryable_but_permissions_are_not(self):
        for error, retryable in ((r.USBError('No device', errno=19), True),
                                 (r.USBError('Access denied', errno=13), False),
                                 (RuntimeError('Bad configuration'), False)):
            device = Mock()
            with self.subTest(error=error), patch('sys.argv', ['respeaker.py', '--diagnostics']), \
                    patch('respeaker.find', return_value=device), patch('respeaker.run', side_effect=error), \
                    patch('respeaker.emit') as emit, patch('respeaker.DIAGNOSTICS'), redirect_stderr(io.StringIO()):
                self.assertEqual(r.main(), 1)
                self.assertEqual(emit.call_args.args[0], dict(status='microphone_error', reconnectable=retryable))
                device.close.assert_called_once()
        with patch('sys.argv', ['respeaker.py']), patch('respeaker.find', return_value=None), \
                patch('respeaker.emit') as emit, redirect_stderr(io.StringIO()):
            self.assertEqual(r.main(), 1)
            self.assertTrue(emit.call_args.args[0]['reconnectable'])


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_idle_waits_for_hardware_before_opening_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(Path(directory) / 'voice.wav')
            dev = Mock(direction=42)
            trigger_count = 0
            stream = Mock()
            stream.__enter__ = Mock(return_value=stream)
            stream.__exit__ = Mock(return_value=False)
            stream.read.return_value = (np.zeros((320, 1), dtype=np.int16), False)
            with patch('respeaker.select_input', return_value=1), \
                    patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                    patch('respeaker.sd.InputStream', return_value=stream) as capture, \
                    patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit') as emit:
                def trigger():
                    nonlocal trigger_count
                    trigger_count += 1
                    capture.assert_not_called()
                    return trigger_count == 3
                dev.is_voice.side_effect = trigger
                vad.return_value.is_speech.return_value = False
                await r.listen(args, dev)
                self.assertEqual(trigger_count, 3)
                capture.assert_called_once()
                self.assertEqual(stream.read.call_count, 100)
                saved = [call.args[0] for call in emit.call_args_list if call.args[0]['status'] == 'utterance_saved'][0]
                self.assertEqual(saved['silence_seconds'], 2)
                self.assertEqual(saved['duration_seconds'], 2)

    async def test_pause_acknowledged_only_after_stream_closes_and_clip_discarded(self):
        state = r.Controls()
        entered = threading.Event()
        release = threading.Event()
        closed = []
        class Stream:
            def __enter__(self): return self
            def read(self, samples):
                entered.set()
                if not release.wait(2): raise RuntimeError('Test capture timed out')
                return np.zeros((samples, 1), dtype=np.int16), False
            def __exit__(self, *args): closed.append(True)
        with patch('respeaker.sd.InputStream', return_value=Stream()), patch('respeaker.emit') as emit:
            task = asyncio.create_task(r.record_utterance(arguments('unused.wav'), Mock(direction=20),
                                                         1, state, None, 1, math.inf, None))
            await asyncio.to_thread(entered.wait, 2)
            pause = asyncio.create_task(state.apply(dict(paused=True, sequence=1)))
            await asyncio.sleep(.01)
            self.assertFalse(pause.done())
            self.assertFalse(closed)
            release.set()
            self.assertIsNone(await task)
            await pause
            self.assertTrue(closed)
            self.assertTrue(state.capture_closed.is_set())
            self.assertEqual(emit.call_args.args[0], dict(status='microphone_control', paused=True, sequence=1))

    async def test_overflow_and_disconnect_never_return_partial_audio(self):
        for result in ('overflow', 'disconnect'):
            with self.subTest(result=result):
                stream = Mock()
                stream.__enter__ = Mock(return_value=stream)
                stream.__exit__ = Mock(return_value=False)
                if result == 'overflow':
                    stream.read.return_value = (np.zeros((320, 1), dtype=np.int16), True)
                else:
                    stream.read.side_effect = r.sd.PortAudioError('Lost input')
                state = r.Controls()
                with patch('respeaker.sd.InputStream', return_value=stream), patch('respeaker.emit'):
                    with self.assertRaises(r.DeviceUnavailable if result == 'disconnect' else RuntimeError):
                        await r.record_utterance(arguments('unused.wav'), Mock(direction=20),
                                                 1, state, None, 1, math.inf, None)
                self.assertTrue(state.capture_closed.is_set())

    async def test_single_utterance_pauses_before_final_and_cannot_trigger_again(self):
        state = r.Controls()
        dev = Mock(direction=10)
        dev.is_voice.return_value = True
        event = dict(pcm=bytes(640), doa_deg=10, utterance_id=1, reason='silence',
                     silence_seconds=2, generation=0)
        finals = []
        def emit(value):
            if value['status'] == 'transcript_final':
                finals.append(value)
                self.assertTrue(state.paused)
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.Controls', return_value=state), \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.record_utterance', return_value=event) as record, \
                patch('respeaker_stt.Transcriber') as transcriber, patch('respeaker.emit', side_effect=emit):
            transcriber.return_value.transcribe.return_value = dict(text='Hello')
            await r.listen(arguments(Path(directory) / 'voice.wav', no_stt=False,
                                     once=False, single_utterance=True, seconds=.2), dev)
            record.assert_awaited_once()
            self.assertTrue(finals[0]['paused_after'])

    async def test_pause_during_stt_suppresses_late_transcript(self):
        state = r.Controls()
        dev = Mock()
        dev.is_voice.return_value = True
        event = dict(pcm=bytes(640), doa_deg=10, utterance_id=1, reason='silence',
                     silence_seconds=2, generation=0)
        def transcribe(_):
            state.paused = True
            state.generation += 1
            return dict(text='Robot echo', stt_seconds=1)
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.Controls', return_value=state), \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.record_utterance', return_value=event), \
                patch('respeaker_stt.Transcriber') as transcriber, patch('respeaker.emit') as emit:
            transcriber.return_value.transcribe.side_effect = transcribe
            await r.listen(arguments(Path(directory) / 'voice.wav', no_stt=False), dev)
            statuses = [call.args[0]['status'] for call in emit.call_args_list]
            self.assertIn('utterance_saved', statuses)
            self.assertNotIn('transcript_final', statuses)


if __name__ == '__main__':
    unittest.main()
