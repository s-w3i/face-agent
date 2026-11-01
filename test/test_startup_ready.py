"""Check startup speech gates capture and respects external robot control."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import announce_voice_ready
from ros_status import Status


class Authority:
    def __init__(self, state='SLEEPING'):
        self.current = Status(state, 1, 'startup')
        self.transitions = []

    async def set(self, state, expected_revision):
        if self.current.revision != expected_revision:
            return False
        self.current = Status(state, self.current.revision + 1, 'agent')
        self.transitions.append(state)
        return True


class StartupReadyTest(unittest.IsolatedAsyncioTestCase):
    async def test_capture_stays_paused_through_playback_then_returns_to_sleep(self):
        status = Authority()
        events = []
        voice = Mock(hold=AsyncMock(side_effect=lambda paused: events.append(('hold', paused))))
        def request(url, action, value=None):
            if action == 'status':
                return dict(robotStatus=dict(revision=status.current.revision))
            self.assertEqual(events, [('hold', True)])
            self.assertEqual(status.current.status, 'SPEAKING')
            self.assertEqual(action, 'startup-ready')
            self.assertEqual(value, dict(sleeping=True, statusRevision=2))
            events.append(('startup-ready', value['sleeping']))
            return dict(displays=1, generation='test', sequence=1)
        async def playback(speech, url):
            self.assertEqual(status.current.status, 'SPEAKING')
            voice.hold.assert_awaited_once_with(True)
            events.append(('finished', True))
            return True
        with patch('chat.request', side_effect=request), patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.wait_for_speech', side_effect=playback), redirect_stdout(io.StringIO()):
            self.assertTrue(await announce_voice_ready('local', status, voice))
        self.assertEqual(status.transitions, ['SPEAKING', 'SLEEPING'])
        self.assertEqual(events[-1], ('finished', True))

    async def test_external_status_during_playback_is_preserved(self):
        status = Authority()
        voice = Mock(hold=AsyncMock())
        async def playback(*args):
            status.current = Status('WORKING', 3, 'external')
            return False
        with patch('chat.request', return_value={'robotStatus': {'revision': 2}}), \
                patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.wait_for_speech', side_effect=playback), redirect_stderr(io.StringIO()):
            self.assertFalse(await announce_voice_ready('local', status, voice))
        self.assertEqual(status.current.status, 'WORKING')
        self.assertEqual(status.transitions, ['SPEAKING'])

    async def test_failed_playback_restores_sleep_without_claiming_success(self):
        status = Authority()
        voice = Mock(hold=AsyncMock())
        output = io.StringIO()
        with patch('chat.request', return_value={'robotStatus': {'revision': 2}}), \
                patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.wait_for_speech', return_value=False), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertFalse(await announce_voice_ready('local', status, voice))
        self.assertEqual(status.current.status, 'SLEEPING')
        self.assertNotIn('finished', output.getvalue())

    async def test_waits_for_display_revision_before_speaking(self):
        status = Authority()
        voice = Mock(hold=AsyncMock())
        with patch('chat.request', side_effect=[{'robotStatus': {'revision': 1}},
                                               {'robotStatus': {'revision': 2}}, {}]) as request, \
                patch('chat.robot_name', return_value='Kuro'), \
                patch('chat.wait_for_speech', return_value=True), \
                patch('chat.asyncio.sleep', new_callable=AsyncMock) as sleep, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(await announce_voice_ready('local', status, voice))
        self.assertEqual([call.args[1] for call in request.call_args_list], ['status', 'status', 'startup-ready'])
        sleep.assert_awaited_once()

    async def test_busy_status_or_speech_disabled_does_not_interrupt(self):
        for state, spoken in [('WORKING', True), ('ERROR', True), ('SLEEPING', False)]:
            status = Authority(state)
            voice = Mock(hold=AsyncMock())
            with patch('chat.request') as request:
                self.assertFalse(await announce_voice_ready('local', status, voice, spoken=spoken))
            voice.hold.assert_not_awaited()
            request.assert_not_called()
            self.assertEqual(status.transitions, [])


if __name__ == '__main__':
    unittest.main()
