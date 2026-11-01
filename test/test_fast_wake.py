"""Cached acknowledgment playback needs no cloud generation or navigation call."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import status_chat_loop
from dotsctl import request
from serve import RobotServer
from ros_status import Status
from voice_input import WakeWord

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

    async def run_inputs(self, inputs, initial='SLEEPING', speak=True):
        status = Authority()
        status.current = Status(initial, 1, 'startup')
        voice = Mock(read=AsyncMock(side_effect=inputs), hold=AsyncMock(), set_awake=AsyncMock(),
                     follow_status=AsyncMock(), activity=asyncio.Queue(), respawn=False,
                     start_capture=AsyncMock(return_value=True))
        def api(url, action, value=None):
            if action == 'status':
                return dict(robotStatus=dict(revision=status.current.revision))
            return dict(displays=1, generation='g', sequence=1)
        bot = Mock(reply=AsyncMock(return_value='Hello.'), sleep_requested=False)
        output = io.StringIO()
        async def playback(speech, url):
            return speech is not None  # Like the real helper: no clip means nothing played.
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.request', side_effect=api), patch('chat.wait_for_speech', side_effect=playback), \
                redirect_stdout(output):
            from types import SimpleNamespace
            self.assertEqual(await asyncio.wait_for(status_chat_loop(
                SimpleNamespace(url='local', model=None, speak=speak), status, voice=voice), 3), 0)
        return status, voice, bot, output.getvalue()

    async def test_openwakeword_hit_uses_the_cached_wake_path(self):
        status, voice, bot, output = await self.run_inputs([WakeWord(.9, 10.0), '/quit'])
        self.assertEqual(status.transitions, ['SPEAKING', 'IDLE'])
        voice.set_awake.assert_awaited_once_with(True)
        bot.reply.assert_not_awaited()
        self.assertIn('Wake phrase detected; Kuro is awake.', output)
        self.assertIn('Wake command input ready.', output)

    async def test_continued_wake_skips_spoken_ack_and_captures_from_ring(self):
        status, voice, bot, output = await self.run_inputs([WakeWord(.9, 10.0, continued=True), '/quit'])
        self.assertEqual(status.transitions, ['IDLE'])  # No SPEAKING: nothing plays over the user.
        voice.set_awake.assert_awaited_once_with(True)
        voice.start_capture.assert_awaited_once()
        self.assertAlmostEqual(voice.start_capture.call_args.args[0], 10.05)
        voice.hold.assert_any_await(False)
        self.assertIn('Capturing the rest of the sentence.', output)
        self.assertNotIn('not accepted', output)

    async def test_plain_wake_never_requests_a_capture(self):
        _, voice, _, _ = await self.run_inputs([WakeWord(.9, 10.0), '/quit'])
        voice.start_capture.assert_not_awaited()

    async def test_openwakeword_hit_while_awake_is_ignored(self):
        status, voice, bot, _ = await self.run_inputs([WakeWord(.9, 10.0), '/quit'], initial='IDLE')
        self.assertEqual(status.transitions, [])
        bot.reply.assert_not_awaited()
        voice.set_awake.assert_not_awaited()



class BargeInTest(unittest.IsolatedAsyncioTestCase):
    """Wake word during THINKING/SPEAKING (phase 4). No network: request/speech are fakes."""

    async def run_turn(self, *, during='THINKING', speaking_flag=False, barge_flag=True, initial='IDLE',
                       external=None):
        status = Authority()
        status.current = Status(initial, 1, 'startup')
        inputs = asyncio.Queue()
        inputs.put_nowait('Tell me a long story.')
        async def read(timeout=None, notices=None):
            return await inputs.get()
        voice = Mock(read=AsyncMock(side_effect=read), hold=AsyncMock(), set_awake=AsyncMock(),
                     follow_status=AsyncMock(), activity=asyncio.Queue(), respawn=False,
                     start_capture=AsyncMock(return_value=True), barge_ins=asyncio.Queue(maxsize=1))
        reached = asyncio.Event()
        finish = asyncio.Event()  # Without a barge-in the turn completes normally.
        async def reply(text, motion_update=False):
            if during == 'THINKING':
                reached.set()
                await finish.wait()
            return 'Once upon a time…'
        says = []
        def api(url, action, value=None):
            if action == 'status':
                return dict(robotStatus=dict(revision=status.current.revision))
            says.append(action)
            return dict(displays=1, generation='g', sequence=1)
        async def playback(speech, url):
            if during == 'SPEAKING':
                reached.set()
                await finish.wait()
            return True
        bot = Mock(reply=AsyncMock(side_effect=reply), sleep_requested=False, history=[{'role': 'user', 'content': 'x'}])
        args = SimpleNamespace(url='local', model=None, speak=True, barge_in=barge_flag, barge_in_speaking=speaking_flag)
        output = io.StringIO()
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.request', side_effect=api), patch('chat.wait_for_speech', side_effect=playback), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            task = asyncio.create_task(status_chat_loop(args, status, voice=voice))
            await asyncio.wait_for(reached.wait(), 2)
            if external:
                # Another node (e.g. navigation) publishes at the same moment.
                status.current = Status(external, status.current.revision + 1, 'navigation')
                status.events.put_nowait(status.current)
                status.transitions.append(external)
            voice.barge_ins.put_nowait(WakeWord(.95, 20.0))
            await asyncio.sleep(.2)
            finish.set()  # A reply that completes after the barge-in must still go nowhere.
            await asyncio.sleep(.2)
            inputs.put_nowait('/quit')
            self.assertEqual(await asyncio.wait_for(task, 3), 0)
        return status, voice, bot, says, output.getvalue()

    async def test_barge_in_during_thinking_cancels_the_turn_and_listens(self):
        status, voice, bot, says, output = await self.run_turn()
        self.assertEqual(status.transitions, ['THINKING', 'LISTENING'])
        self.assertNotIn('say', says)
        self.assertNotIn('ERROR', status.transitions)
        voice.start_capture.assert_awaited_once()
        self.assertAlmostEqual(voice.start_capture.call_args.args[0], 20.05)
        self.assertEqual(bot.history, [{'role': 'user', 'content': 'x'}])  # Unchanged: the turn never finished.
        self.assertIn('Barge-in during THINKING', output)

    async def test_speaking_is_not_interrupted_without_the_aec_flag(self):
        status, voice, bot, says, output = await self.run_turn(during='SPEAKING')
        self.assertEqual(status.transitions, ['THINKING', 'SPEAKING', 'IDLE'])
        voice.start_capture.assert_not_awaited()
        self.assertIn('no barge-in', output)

    async def test_barge_in_during_speaking_when_enabled(self):
        status, voice, bot, says, _ = await self.run_turn(during='SPEAKING', speaking_flag=True)
        self.assertEqual(status.transitions, ['THINKING', 'SPEAKING', 'LISTENING'])
        self.assertNotIn('ERROR', status.transitions)
        self.assertNotIn('IDLE', status.transitions)
        voice.start_capture.assert_awaited_once()
        self.assertEqual(bot.history[-1]['role'], 'system')  # The interruption is noted for the agent.

    async def test_flag_off_ignores_the_wake_word(self):
        status, voice, _, _, output = await self.run_turn(barge_flag=False)
        self.assertEqual(status.transitions, ['THINKING', 'SPEAKING', 'IDLE'])
        voice.start_capture.assert_not_awaited()

    async def test_wake_word_in_other_states_changes_nothing(self):
        for state in ('IDLE', 'LISTENING', 'WORKING', 'DETECTING'):
            with self.subTest(state=state):
                status = Authority()
                status.current = Status(state, 1, 'startup')
                inputs = asyncio.Queue()
                async def read(timeout=None, notices=None):
                    return await inputs.get()
                voice = Mock(read=AsyncMock(side_effect=read), hold=AsyncMock(), set_awake=AsyncMock(),
                             follow_status=AsyncMock(), activity=asyncio.Queue(), respawn=False,
                             start_capture=AsyncMock(), barge_ins=asyncio.Queue(maxsize=1))
                with patch('chat.Chatbot'), patch('chat.request'), redirect_stdout(io.StringIO()):
                    task = asyncio.create_task(status_chat_loop(
                        SimpleNamespace(url='local', model=None, speak=True), status, voice=voice))
                    await asyncio.sleep(.05)
                    voice.barge_ins.put_nowait(WakeWord(.9, 1.0))
                    await asyncio.sleep(.1)
                    inputs.put_nowait('/quit')
                    self.assertEqual(await asyncio.wait_for(task, 3), 0)
                self.assertEqual(status.transitions, [])
                voice.start_capture.assert_not_awaited()

    async def test_navigation_status_at_the_same_moment_wins(self):
        status, voice, _, _, _ = await self.run_turn(external='WORKING')
        self.assertEqual(status.transitions, ['THINKING', 'WORKING'])  # Navigation is never interrupted.
        self.assertNotIn('LISTENING', status.transitions)
        voice.start_capture.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
