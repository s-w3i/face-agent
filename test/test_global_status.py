"""Real ROS publisher/service, retained state, display mapping and interruption."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ros_status import RosStatus, DisplayStatus
from serve import RobotServer
from chat import status_chat_loop
from dotsctl import request
from voice_input import VoiceInput

ROOT = Path(__file__).resolve().parents[1]


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


def fake_voice(read, activity=None):
    voice = SimpleNamespace(read=read, hold=AsyncMock(), activity=activity or asyncio.Queue(),
                            awake=True, turn_complete=False)
    async def set_awake(awake):
        voice.awake = awake
    voice.set_awake = AsyncMock(side_effect=set_awake)
    async def follow(state):
        await VoiceInput.follow_status(voice, state)
    voice.follow_status = follow
    return voice


@unittest.skipUnless(Path('/opt/ros/jazzy/setup.bash').exists(), 'Needs ROS 2 Jazzy')
class GlobalStatusCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, ROS_DISTRO='jazzy', ROS_DOMAIN_ID='94',
                              ROS_LOCALHOST_ONLY='1', XDG_CACHE_HOME=self.cache.name)
        self.env.start()
        self.node = await asyncio.create_subprocess_exec('bash', str(ROOT / 'robot-status.sh'),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.agent = await RosStatus().__aenter__()
        self.remote = await RosStatus().__aenter__()

    async def asyncTearDown(self):
        await self.agent.close()
        await self.remote.close()
        self.node.terminate()
        await self.node.wait()
        self.env.stop()
        self.cache.cleanup()

    async def test_retained_status_validation_and_conflict(self):
        revision = self.agent.current.revision
        self.assertTrue(await self.remote.set('listening'))
        await until(lambda: self.agent.current.status == 'LISTENING')
        self.assertFalse(await self.agent.set('THINKING', expected_revision=revision))
        self.assertFalse(await self.agent.set('banana'))
        async with RosStatus() as late:
            self.assertEqual(late.current.status, 'LISTENING')
            self.assertEqual(late.current.revision, self.remote.current.revision)
        await self.agent.close()
        self.assertTrue(await self.remote.set('ERROR'))  # Authority survives chatbot exit.

    async def test_display_without_agent_and_stale_speech(self):
        config = Path(self.cache.name) / 'robot.json'
        config.write_text((ROOT / 'robot-config.default.json').read_text())
        server = RobotServer(('127.0.0.1', 0), config_file=config,
                             static_root=Path(self.cache.name) / 'web', key_file=Path(self.cache.name) / 'private/key')
        display = DisplayStatus(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            for state, animation in [('LISTENING', 'needs-input'), ('ERROR', 'error'), ('IDLE', 'idle'), ('SLEEPING', 'sleeping'), ('WORKING', 'working'), ('DETECTING', 'searching')]:
                await self.remote.set(state)
                await until(lambda: server.command['state'] == animation)
                self.assertEqual(server.robot_status['status'], state)
            await self.remote.set('SPEAKING')
            await until(lambda: server.robot_status['status'] == 'SPEAKING')
            old = server.robot_status['revision']
            with server.changed:
                server.change_command('speaking', speech='/api/audio/test', statusRevision=old)
                sequence = server.command['sequence']
            # A delayed notification for the audio's own revision preserves it.
            server.apply_robot_status(dict(server.robot_status))
            self.assertEqual(server.command['sequence'], sequence)
            # A new external SPEAKING command takes control even with the same state.
            await self.remote.set('SPEAKING')
            await until(lambda: server.command['sequence'] > sequence)
            self.assertNotIn('speech', server.command)
            await self.remote.set('ERROR')
            await until(lambda: server.command['state'] == 'error')
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, url, 'say', dict(text='Late reply', stream=True, statusRevision=old))
            self.assertEqual(server.command['state'], 'error')
        finally:
            await asyncio.to_thread(display.close)
            await asyncio.to_thread(server.shutdown)
            thread.join(2)
            server.server_close()

    async def test_voice_activity_and_external_error_interrupt_model(self):
        inputs = asyncio.Queue()
        activity = asyncio.Queue()
        async def read(timeout=None, notices=None):
            return await inputs.get()
        voice = fake_voice(read, activity)
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def answer(text, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        bot = Mock(reply=AsyncMock(side_effect=answer), sleep_requested=False)
        args = SimpleNamespace(url='http://unused', model=None, speak=False)
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'):
            task = asyncio.create_task(status_chat_loop(args, self.agent, voice=voice))
            try:
                await until(lambda: voice.hold.await_count > 0)
                self.assertFalse(voice.hold.call_args.args[0])  # IDLE monitors without a wake word.
                activity.put_nowait('LISTENING')
                await until(lambda: self.remote.current.status == 'LISTENING')
                inputs.put_nowait('What time is it?')
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(self.agent.current.status, 'THINKING')
                await self.remote.set('ERROR')
                await asyncio.wait_for(cancelled.wait(), 5)
                await until(lambda: voice.hold.call_args.args[0] is True)
                self.assertEqual(self.agent.current.status, 'ERROR')
                await asyncio.sleep(.1)
                self.assertEqual(self.agent.current.status, 'ERROR')
                # External IDLE resumes input; no Hi Kuro or local STT is involved.
                await self.remote.set('IDLE')
                await until(lambda: voice.hold.call_args.args[0] is False)
                inputs.put_nowait('/quit')
                self.assertEqual(await asyncio.wait_for(task, 5), 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_reply_lifecycle_and_farewell_sleep_without_wake_phrase(self):
        inputs = asyncio.Queue()
        async def read(timeout=None, notices=None):
            return await inputs.get()
        voice = fake_voice(read)
        bot = Mock(reply=AsyncMock(return_value='Hello.'), sleep_requested=False)
        args = SimpleNamespace(url='http://unused', model=None, speak=False)
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'):
            task = asyncio.create_task(status_chat_loop(args, self.agent, voice=voice))
            try:
                inputs.put_nowait('Tell me the time')
                await until(lambda: bot.reply.await_count == 1 and self.agent.current.status == 'IDLE')
                self.assertEqual(bot.reply.call_args.args[0], 'Tell me the time')
                inputs.put_nowait('Goodbye Kuro')
                await until(lambda: self.agent.current.status == 'SLEEPING')
                await until(lambda: not voice.awake and voice.hold.call_args.args[0] is False)
                inputs.put_nowait('/quit')
                self.assertEqual(await asyncio.wait_for(task, 5), 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_inactivity_requests_global_sleep(self):
        count = 0
        async def read(timeout=None, notices=None):
            nonlocal count
            count += 1
            if count == 1:
                self.assertEqual(timeout, 5)
                raise TimeoutError
            self.assertIsNone(timeout)
            return '/quit'
        voice = fake_voice(read)
        args = SimpleNamespace(url='http://unused', model=None, speak=False)
        with patch('chat.Chatbot'):
            self.assertEqual(await status_chat_loop(args, self.agent, voice=voice), 0)
        self.assertEqual(self.agent.current.status, 'SLEEPING')

    async def test_sleep_repeats_local_wake_detection_and_ignores_other_speech(self):
        await self.remote.set('SLEEPING')
        await until(lambda: self.agent.current.status == 'SLEEPING')
        inputs = asyncio.Queue()
        async def read(timeout=None, notices=None):
            return await inputs.get()
        voice = fake_voice(read)
        bot = Mock(reply=AsyncMock(return_value='Hello!'), sleep_requested=False)
        args = SimpleNamespace(url='http://unused', model=None, speak=False)
        with patch('chat.Chatbot', return_value=bot), patch('chat.robot_name', return_value='Kuro'):
            task = asyncio.create_task(status_chat_loop(args, self.agent, voice=voice))
            try:
                await until(lambda: not voice.awake)
                inputs.put_nowait('The television is playing')
                await asyncio.sleep(.1)
                bot.reply.assert_not_awaited()
                self.assertEqual(self.agent.current.status, 'SLEEPING')
                inputs.put_nowait('Hi Kuro')
                await until(lambda: bot.reply.await_count == 1 and self.agent.current.status == 'IDLE')
                self.assertEqual(bot.reply.call_args.args[0], 'Hi Kuro')
                self.assertTrue(voice.awake)
                inputs.put_nowait('/quit')
                self.assertEqual(await asyncio.wait_for(task, 5), 0)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
