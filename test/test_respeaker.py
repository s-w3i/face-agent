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
                  once=True, silence_seconds=0.8, max_seconds=10, vad_mode=2)
    return SimpleNamespace(**{**values, **changes})


class EndpointTest(unittest.TestCase):
    def test_point_eight_seconds_of_audio_and_speech_resets_timer(self):
        endpoint = r.SpeechEndpoint()
        for _ in range(39):
            self.assertFalse(endpoint.update(False, 320))
        self.assertTrue(endpoint.update(False, 320))
        self.assertFalse(endpoint.update(True, 320))
        self.assertEqual(endpoint.silent_samples, 0)
        for _ in range(39):
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

    def test_doctor_aec_records_through_selected_input_and_sink(self):
        import respeaker_aec
        args = SimpleNamespace(command='doctor', aec=True, compare=False, device=None, sink='jieli',
                               aec_seconds=4, playback=None, label=None)
        with patch('respeaker.select_input', return_value=2), \
                patch.object(respeaker_aec, 'measure', return_value=dict(echo_suppression_db=3)) as measure, \
                patch('respeaker.emit') as emit:
            r.run(args, Mock())
        measure.assert_called_once_with(r.sd, 2, r.RATE, sink='jieli', seconds=4, clip=None, label=None)
        emit.assert_called_once_with(dict(echo_suppression_db=3))
        with patch.object(respeaker_aec, 'compare', return_value=dict(runs={})) as compare, \
                patch('respeaker.emit') as emit:
            r.run(SimpleNamespace(**(vars(args) | dict(aec=False, compare=True))), Mock())
        compare.assert_called_once_with()

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


if __name__ == '__main__':
    unittest.main()
