"""Retry voice failures without replaying commands or overwriting external status."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import recover_voice, status_chat_loop
from ros_status import Status
from voice_errors import RealtimeUnavailable


class Authority:
    def __init__(self, state='IDLE'):
        self.source = 'test-agent'
        self.current = Status(state, 1, 'test-node')
        self.events = asyncio.Queue()
        self.transitions = []

    def external(self, state):
        self.current = Status(state, self.current.revision + 1, 'test-node')
        self.events.put_nowait(self.current)

    async def set(self, state, expected_revision=0):
        if expected_revision and expected_revision != self.current.revision:
            return False
        self.current = Status(state, self.current.revision + 1, self.source)
        self.transitions.append(state)
        self.events.put_nowait(self.current)
        return True


class RecoveryCheck(unittest.IsolatedAsyncioTestCase):
    async def test_retries_until_ready_then_clears_only_own_error(self):
        status = Authority('LISTENING')
        voice = Mock(restart=AsyncMock(side_effect=[RealtimeUnavailable('Offline'), RealtimeUnavailable('Offline'), None]))
        with patch('chat.asyncio.sleep', new_callable=AsyncMock) as sleep, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertTrue(await recover_voice(status, voice, RealtimeUnavailable('Queue full')))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        self.assertEqual(status.transitions, ['ERROR', 'IDLE'])
        self.assertEqual(voice.restart.await_count, 3)
        self.assertTrue(all(call.kwargs['awake'] for call in voice.restart.call_args_list))

    async def test_sleep_recovery_uses_local_wake_mode(self):
        status = Authority('SLEEPING')
        voice = Mock(restart=AsyncMock())
        with patch('chat.VOICE_RETRY_DELAYS', (0,)), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertTrue(await recover_voice(status, voice, RealtimeUnavailable('Disconnected')))
        voice.restart.assert_awaited_once_with(awake=False)
        self.assertEqual(status.current.status, 'SLEEPING')

    async def test_long_outage_caps_backoff_and_recovers_when_network_returns(self):
        status = Authority()
        voice = Mock(restart=AsyncMock(side_effect=[RealtimeUnavailable('Offline')] * 7 + [None]))
        with patch('chat.asyncio.sleep', new_callable=AsyncMock) as sleep, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertTrue(await recover_voice(status, voice, RealtimeUnavailable('Offline')))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 15, 30, 30, 30])

    async def test_external_change_during_reconnect_is_never_cleared(self):
        for target in ('ERROR', 'WORKING', 'SLEEPING'):
            status = Authority()
            async def restart(**kwargs):
                status.external(target)
            voice = Mock(restart=AsyncMock(side_effect=restart))
            with patch('chat.VOICE_RETRY_DELAYS', (0,)), redirect_stderr(io.StringIO()):
                self.assertFalse(await recover_voice(status, voice, RealtimeUnavailable('Offline')))
            self.assertEqual(status.current.status, target)
            self.assertEqual(status.current.source, 'test-node')
            self.assertEqual(status.transitions, ['ERROR'])

    async def test_external_change_during_backoff_stops_retry(self):
        status = Authority()
        async def wait(delay):
            status.external('WORKING')
        voice = Mock(restart=AsyncMock())
        with patch('chat.asyncio.sleep', side_effect=wait), redirect_stderr(io.StringIO()):
            self.assertFalse(await recover_voice(status, voice, RealtimeUnavailable('Offline')))
        voice.restart.assert_not_awaited()

    async def test_permanent_error_and_existing_external_error_are_not_retried(self):
        for initial, error in [('IDLE', RealtimeUnavailable('Invalid key', retryable=False)),
                               ('ERROR', RealtimeUnavailable('Offline'))]:
            status = Authority(initial)
            voice = Mock(restart=AsyncMock())
            with redirect_stderr(io.StringIO()):
                self.assertFalse(await recover_voice(status, voice, error))
            voice.restart.assert_not_awaited()
            self.assertEqual(status.current.status, 'ERROR')

    async def test_status_loop_recovers_without_replaying_failed_utterance(self):
        status = Authority()
        voice = Mock(read=AsyncMock(side_effect=[RealtimeUnavailable('Queue full'), 'A new request', '/quit']),
                     follow_status=AsyncMock(), hold=AsyncMock(), restart=AsyncMock(), activity=asyncio.Queue())
        bot = Mock(reply=AsyncMock(return_value='Hello'), sleep_requested=False)
        with patch('chat.VOICE_RETRY_DELAYS', (0,)), patch('chat.Chatbot', return_value=bot), \
                patch('chat.robot_name', return_value='Kuro'), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            result = await status_chat_loop(SimpleNamespace(url='unused', model=None, speak=False), status, voice=voice)
        self.assertEqual(result, 0)
        voice.restart.assert_awaited_once_with(awake=True)
        self.assertEqual([call.args[0] for call in bot.reply.call_args_list], ['A new request'])
        self.assertEqual(status.transitions[:2], ['ERROR', 'IDLE'])

    async def test_fatal_error_waits_for_external_idle_before_new_worker_attempt(self):
        status = Authority()
        voice = Mock(read=AsyncMock(side_effect=[RealtimeUnavailable('Invalid key', retryable=False), '/quit']),
                     follow_status=AsyncMock(), hold=AsyncMock(), restart=AsyncMock(), activity=asyncio.Queue())
        with patch('chat.Chatbot'), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            task = asyncio.create_task(status_chat_loop(SimpleNamespace(url='unused', model=None, speak=False), status, voice=voice))
            try:
                async with asyncio.timeout(2):
                    while status.current.status != 'ERROR':
                        await asyncio.sleep(.01)
                await asyncio.sleep(.05)
                voice.restart.assert_not_awaited()
                self.assertEqual(voice.read.await_count, 1)
                status.external('IDLE')
                self.assertEqual(await asyncio.wait_for(task, 2), 0)
                voice.restart.assert_awaited_once_with(awake=True)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_external_status_cancels_inflight_recovery_and_keeps_input_paused(self):
        status = Authority()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def restart(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        voice = Mock(read=AsyncMock(side_effect=RealtimeUnavailable('Offline')),
                     follow_status=AsyncMock(), hold=AsyncMock(), restart=AsyncMock(side_effect=restart), activity=asyncio.Queue())
        with patch('chat.VOICE_RETRY_DELAYS', (0,)), patch('chat.Chatbot'), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            task = asyncio.create_task(status_chat_loop(SimpleNamespace(url='unused', model=None, speak=False), status, voice=voice))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                status.external('WORKING')
                await asyncio.wait_for(cancelled.wait(), 2)
                await asyncio.sleep(.05)
                self.assertEqual(status.current.status, 'WORKING')
                self.assertEqual(voice.read.await_count, 1)
                self.assertEqual(voice.restart.await_count, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
