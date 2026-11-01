"""Cached acknowledgment playback needs no cloud generation or navigation call."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import status_chat_loop
from dotsctl import request
from serve import RobotServer
from ros_status import Status

ROOT = Path(__file__).resolve().parents[1]


class CachedWakeTest(unittest.TestCase):
    def test_persistent_voice_cache_and_revision_checked_playback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CACHE_HOME': directory}):
            root = Path(directory)
            config = root/'robot-config.json'
            config.write_text((ROOT/'robot-config.default.json').read_text())
            server = RobotServer(('127.0.0.1', 0), config_file=config, static_root=root/'web', key_file=root/'private/key')
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f'http://127.0.0.1:{server.server_port}'
            try:
                with patch.object(server, 'speech', return_value=b'cached wav') as generate, patch('serve.Handler.log_message'):
                    previous = dict(server.command)
                    self.assertEqual(request(url, 'wake-ack', {'prepare': True}), {'ready': True})
                    self.assertEqual(server.command, previous)  # Preparation never plays audio.
                    generate.assert_called_once()
                    generate.reset_mock()
                    request(url, 'wake-ack', {'prepare': True})
                    generate.assert_not_called()
                    server.apply_robot_status(dict(status='SPEAKING', revision=1, source='test'))
                    command = request(url, 'wake-ack', {'statusRevision': 1})
                    self.assertEqual(command['text'], 'Yes? I am here, what can I help you?')
                    self.assertEqual(server.audio[1], b'cached wav')
                    generate.assert_not_called()
                    server.apply_robot_status(dict(status='WORKING', revision=2, source='external'))
                    with self.assertRaisesRegex(ValueError, 'newer robot status'):
                        request(url, 'wake-ack', {'statusRevision': 1})
                # A new server instance loads the persisted clip without credentials.
                other = RobotServer(('127.0.0.1', 0), config_file=config, static_root=root/'otherweb', key_file=root/'other/key')
                try:
                    with patch.object(other, 'speech', side_effect=AssertionError('No network allowed')):
                        self.assertEqual(other.wake_ack(), b'cached wav')
                        value=json.loads(config.read_text())
                        value['settings']['ttsVoice']='cedar' if value['settings'].get('ttsVoice') != 'cedar' else 'marin'
                        config.write_text(json.dumps(value))
                        with self.assertRaisesRegex(ValueError, 'not cached'):
                            other.wake_ack()
                finally:
                    other.server_close()
            finally:
                server.shutdown(); worker.join(2); server.server_close()


class Authority:
    def __init__(self):
        self.source = 'agent'
        self.current = Status('SLEEPING', 1, 'startup')
        self.events = asyncio.Queue()
        self.transitions = []

    async def set(self, state, expected_revision=0):
        if expected_revision and self.current.revision != expected_revision:
            return False
        self.current = Status(state, self.current.revision+1, self.source)
        self.events.put_nowait(self.current)
        self.transitions.append(state)
        return True


class WakeLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_cached_yes_precedes_connection_and_bare_greeting_skips_agent(self):
        status = Authority()
        events = []
        voice = Mock(read=AsyncMock(side_effect=['Hi Kuro!', '/quit']), hold=AsyncMock(),
                     follow_status=AsyncMock(), activity=asyncio.Queue())
        connected = asyncio.Event()
        async def connect(awake):
            self.assertTrue(awake)
            events.append('connecting')
            connected.set()
        voice.set_awake = AsyncMock(side_effect=connect)
        def api(url, action, value=None):
            if action == 'status':
                return dict(robotStatus=dict(revision=status.current.revision))
            self.assertEqual(action, 'wake-ack')
            events.append('cached audio')
            return dict(displays=1, generation='g', sequence=1)
        async def playback(*args):
            await asyncio.wait_for(connected.wait(), 1)
            self.assertEqual(status.current.status, 'SPEAKING')
            events.append('playback finished')
            return True
        bot = Mock(reply=AsyncMock())
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.request', side_effect=api), patch('chat.wait_for_speech', side_effect=playback), \
                redirect_stdout(io.StringIO()):
            from types import SimpleNamespace
            self.assertEqual(await asyncio.wait_for(status_chat_loop(
                SimpleNamespace(url='local', model=None, speak=True), status, voice=voice), 3), 0)
        bot.reply.assert_not_awaited()
        self.assertEqual(events, ['cached audio', 'connecting', 'playback finished'])
        self.assertEqual(status.transitions, ['SPEAKING', 'IDLE'])
        voice.hold.assert_any_await(True)


if __name__ == '__main__':
    unittest.main()
