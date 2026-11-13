"""Startup speech persists across restarts and plays with cloud access blocked."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from serve import RobotServer
from dotsctl import request

ROOT = Path(__file__).resolve().parents[1]


class CachedStartupTest(unittest.TestCase):
    def test_prepare_then_restart_and_play_both_phrases_without_cloud(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CACHE_HOME': directory}):
            root = Path(directory)
            config = root/'robot-config.json'
            value = json.loads((ROOT/'robot-config.default.json').read_text())
            value['appearance'] = dict(name='Kuro', presetId='', signatureAvailable=False,
                                       state=base64.b64encode(b'ORBAST1\0test').decode())
            config.write_text(json.dumps(value))
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0\0'*240)
            audio = output.getvalue()
            options = dict(config_file=config, static_root=root/'web', key_file=root/'private/key')
            first = RobotServer(('127.0.0.1', 0), **options)
            try:
                with patch.object(first, 'speech', return_value=audio) as generate:
                    first.startup_ready(sleeping=True, prepare=True)
                    first.startup_ready(sleeping=False, prepare=True)
                self.assertEqual([call.args[0]['text'] for call in generate.call_args_list],
                                 ['Kuro is ready. Say Hi Kuro to wake me up.', 'Kuro is ready.'])
                self.assertEqual(first.command['sequence'], 0)
            finally:
                first.server_close()
            second = RobotServer(('127.0.0.1', 0), **options)
            worker = threading.Thread(target=second.serve_forever, daemon=True)
            worker.start()
            url = f'http://127.0.0.1:{second.server_port}'
            try:
                with patch('serve.connect', side_effect=AssertionError('Cloud access blocked')), \
                        patch.object(second, 'speech', side_effect=AssertionError('Generation forbidden')), \
                        patch.object(second, 'api_key', side_effect=AssertionError('No key required')), \
                        patch('serve.Handler.log_message'):
                    for revision, sleeping in enumerate((True, False), 1):
                        previous = dict(second.command)
                        self.assertEqual(request(url, 'startup-ready', dict(prepare=True, sleeping=sleeping)),
                                         dict(ready=True))
                        self.assertEqual(second.command, previous)
                        second.apply_robot_status(dict(status='SPEAKING', revision=revision, source='test'))
                        clip = request(url, 'startup-ready', dict(statusRevision=revision, sleeping=sleeping))
                        self.assertEqual(clip['text'], 'Kuro is ready. Say Hi Kuro to wake me up.' if sleeping else 'Kuro is ready.')
                        with urlopen(url+clip['speech'], timeout=3) as response:
                            self.assertEqual(response.read(), audio)
                    second.apply_robot_status(dict(status='WORKING', revision=3, source='external'))
                    with self.assertRaisesRegex(ValueError, 'newer robot status'):
                        request(url, 'startup-ready', dict(statusRevision=2))
                    value['appearance']['name'] = 'New name'
                    config.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ValueError, 'not cached'):
                        second.startup_ready()
                    for payload in ({'sleeping': 'yes'}, {'text': 'Uncached arbitrary speech'}):
                        with self.assertRaises(ValueError):
                            request(url, 'startup-ready', payload)
            finally:
                second.shutdown(); worker.join(2); second.server_close()


if __name__ == '__main__':
    unittest.main()
