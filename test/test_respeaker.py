import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from respeaker import distance, level_dbfs, noise_threshold, accepts

class MicrophoneTest(unittest.TestCase):
    def test_usb_disconnect_is_retryable_but_permissions_and_configuration_are_not(self):
        from respeaker import main, USBError
        for error, retryable in ((USBError('No such device', errno=19), True),
                                 (USBError('Access denied', errno=13), False),
                                 (RuntimeError('Bad firmware'), False)):
            device = Mock()
            with self.subTest(error=error), patch('sys.argv', ['respeaker.py', '--diagnostics']), \
                    patch('respeaker.find', return_value=device), patch('respeaker.run', side_effect=error), \
                    patch('respeaker.emit') as emit, patch('respeaker.DIAGNOSTICS'), patch('sys.stderr'):
                self.assertEqual(main(), 1)
                self.assertEqual(emit.call_args.args[0], dict(status='microphone_error', reconnectable=retryable))
                device.close.assert_called_once()
        with patch('sys.argv', ['respeaker.py', '--diagnostics']), patch('respeaker.find', return_value=None), \
                patch('respeaker.emit') as emit, patch('respeaker.DIAGNOSTICS'), patch('sys.stderr'):
            self.assertEqual(main(), 1)
            self.assertTrue(emit.call_args.args[0]['reconnectable'])

    def test_calibration_and_target_gate(self):
        self.assertEqual(level_dbfs(bytes(1600)), -180)
        self.assertAlmostEqual(level_dbfs(b'\x00\x40' * 800), -6.0206, places=3)
        self.assertEqual(distance(359, 1), 2)
        self.assertEqual(noise_threshold([-50] * 96 + [-20] * 4), (-50, -47))
        self.assertTrue(accepts(True, -30, -47, 359, 1, 25))
        self.assertFalse(accepts(True, -30, -47, 90, 1, 25))
        self.assertFalse(accepts(True, -50, -47, 359, 1, 25))
        self.assertFalse(accepts(False, -30, -47, 359, 1, 25))
        self.assertTrue(accepts(True, -30, -47, 90, None, 25))
        with self.assertRaises(RuntimeError):
            noise_threshold([])

class OdasTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_frames_across_boundaries(self):
        import asyncio
        from respeaker_odas import json_frames
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"timeStamp":1,"src":[]}{"time')
        reader.feed_data(b'Stamp":2,"src":[]}')
        reader.feed_eof()
        frames = json_frames(reader)
        self.assertEqual((await anext(frames))['timeStamp'], 1)
        self.assertEqual((await anext(frames))['timeStamp'], 2)
        with self.assertRaises(RuntimeError):
            await anext(frames)

    def test_channel_order_and_configuration(self):
        import struct
        from respeaker_odas import split_channels, make_config, azimuth
        pcm = struct.pack('<8h', 1, 2, 3, 4, 5, 6, 7, 8)
        self.assertEqual([struct.unpack('<2h', c) for c in split_channels(pcm)],
                         [(1, 5), (2, 6), (3, 7), (4, 8)])
        self.assertEqual(azimuth({'x': 0, 'y': -1}), 270)
        config = make_config('test-source', ['front-left', 'front-right'],
                             {'tracks': 12001, 'audio': 12002, 'categories': 12003})
        for port in (12001, 12002, 12003):
            self.assertIn('port = ' + str(port), config)
        self.assertIn('source = "test-source"', config)
        self.assertNotIn('postfiltered.raw', config)

class EndpointTest(unittest.TestCase):
    def test_direction_average_wraps_zero_and_rejects_conflicting_sources(self):
        from respeaker_odas import Utterance
        endpoint = Utterance(); endpoint.track_id = 1
        endpoint.direction(359); endpoint.direction(1)
        self.assertLess(distance(endpoint.finish('shutdown')['doa_deg'], 0), .001)
        endpoint.track_id = 2
        endpoint.direction(0); endpoint.direction(180)
        self.assertIsNone(endpoint.finish('shutdown')['doa_deg'])

    def test_pause_other_speaker_and_slot_change(self):
        from respeaker_odas import Utterance, HOP
        endpoint = Utterance()
        a, b = b'\x01\x00' * HOP, b'\x02\x00' * HOP
        sources = [{'id': 1, 'activity': .8}, {'id': 2, 'activity': .9}]
        for _ in range(12):
            endpoint.feed(sources, [a, b], [0], a)
        self.assertIsNone(endpoint.track_id)  # Brief noise cannot start a clip.
        endpoint.feed(sources, [a, b], [], a)
        for _ in range(25):
            endpoint.feed(sources, [a, b], [0], a)
        self.assertEqual(endpoint.track_id, 1)
        for _ in range(60):
            _, event = endpoint.feed(sources[::-1], [b, a], [0], a)
            self.assertIsNone(event)  # Another speaker cannot reset target silence.
        endpoint.feed(sources[::-1], [b, a], [1], a)  # Target resumes after a short pause.
        for _ in range(124):
            _, event = endpoint.feed(sources[::-1], [b, a], [0], a)
            self.assertIsNone(event)
        _, event = endpoint.feed(sources[::-1], [b, a], [0], a)
        self.assertEqual(event['reason'], 'silence')
        self.assertEqual(event['track_id'], 1)
        self.assertEqual(event['pcm'], a * (38 + 60 + 1 + 25))
        self.assertIsNone(endpoint.track_id)
        for _ in range(25):
            endpoint.feed(sources, [a, b], [1], a)
        self.assertEqual(endpoint.track_id, 2)
        self.assertEqual(endpoint.finish('shutdown')['track_id'], 2)

    def test_maximum_and_lost_source(self):
        from respeaker_odas import Utterance, HOP
        endpoint = Utterance()
        sources = [{'id': 1, 'activity': 1}]
        data = bytes(HOP * 2)
        for _ in range(3750):
            _, event = endpoint.feed(sources, [data], [0], data)
            if event:
                break
        self.assertEqual(event['reason'], 'maximum_duration')
        self.assertEqual(len(event['pcm']), 16000 * 2 * 30)
        for _ in range(25):
            endpoint.feed(sources, [data], [0], data)
        for _ in range(125):
            _, event = endpoint.feed([], [], [], data)
        self.assertEqual(event['reason'], 'silence')

    def test_first_word_before_track_acquisition_and_live_preroll(self):
        from respeaker_odas import Utterance, HOP
        endpoint = Utterance()
        first_word = b'\x10\x00' * HOP
        later_words = b'\x20\x00' * HOP
        sources = [{'id': 7, 'activity': 1}]
        # The microphone already hears "my" while ODAS has no source yet.
        for _ in range(75):
            endpoint.feed([], [], [], first_word)
        # Separation has a track, but classification still needs time.
        for _ in range(25):
            endpoint.feed(sources, [bytes(HOP * 2)], [], first_word)
        for _ in range(25):
            live, event = endpoint.feed(sources, [later_words], [0], later_words)
        expected = first_word * 100 + later_words * 25
        self.assertEqual(live, expected)  # FIFO consumers receive the pre-roll too.
        self.assertEqual(endpoint.finish('shutdown')['pcm'], expected)

    def test_upstream_capture_socket_configuration(self):
        from respeaker_odas import make_config
        config = make_config('source', [],
                             {'input': 12000, 'tracks': 12001, 'audio': 12002, 'categories': 12003})
        self.assertIn('type = "socket"; ip = "127.0.0.1"; port = 12000', config)
        self.assertNotIn('type = "pulseaudio"', config)

    def test_entire_utterance_uses_processed_channel_zero(self):
        from respeaker_odas import Utterance, HOP
        endpoint = Utterance()
        sources = [{'id': 1, 'activity': 1}]
        processed = b'\x31\x00' * HOP
        separated = b'\x72\x00' * HOP
        for _ in range(25):
            endpoint.feed(sources, [separated], [0], processed)
        for _ in range(50):
            live, _ = endpoint.feed(sources, [separated], [0], processed)
            self.assertEqual(live, processed)
        for _ in range(125):
            _, event = endpoint.feed(sources, [separated], [], processed)
        self.assertEqual(event['pcm'], processed * 100)
        self.assertNotIn(separated, event['pcm'])

class ProfileTest(unittest.TestCase):
    def test_quantized_time_readback_and_rejected_gain_mismatch(self):
        import tempfile
        from unittest.mock import patch
        import respeaker
        class Device:
            def __init__(self):
                self.values = {}
                self.bad_gain = False
            def write(self, name, value):
                self.values[name] = value
            def read(self, name):
                if name == 'AGCTIME':
                    return .9838172
                if name == 'AGCONOFF' and self.bad_gain:
                    return 0
                return self.values[name]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'respeaker-backup.json').write_text('{}')
            with patch.object(respeaker, 'ROOT', root):
                device = Device()
                respeaker.apply_profile(device)
                device.bad_gain = True
                with self.assertRaisesRegex(RuntimeError, 'AGCONOFF readback mismatch'):
                    respeaker.apply_profile(device)
