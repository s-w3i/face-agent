"""Verify conversation continuity and display cleanup without API calls."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import queue
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from chat import Chatbot, current_datetime, current_location, main, robot_name, wake_trigger, read_terminal, spoken_reply, watch_motion, start_after_announcement, start_tracking
from agents import WebSearchTool
from agents.tool_context import ToolContext


class ChatCheck(unittest.TestCase):
    def setUp(self):
        tracking = patch('chat.start_tracking')
        self.tracking = tracking.start()
        self.addCleanup(tracking.stop)

    def test_tracking_starts_for_selected_service_and_can_be_disabled(self):
        for flags in ([], ['--no-track']):
            self.tracking.reset_mock()
            with patch('sys.argv', ['chat.py', '--url', 'http://127.0.0.1:5174', *flags]), patch('chat.load_key'), patch('chat.robot_name', return_value='Shiro'), patch('chat.read_terminal', return_value='/quit'), patch('chat.Chatbot'):
                self.assertEqual(asyncio.run(main()), 0)
            if flags:
                self.tracking.assert_not_called()
            else:
                self.tracking.assert_called_once_with('http://127.0.0.1:5174')
                self.tracking.return_value.__exit__.assert_called_once()

    def test_closing_reply_finishes_before_sleep(self):
        async def check():
            with patch('chat.request', return_value={}):
                bot = Chatbot('http://localhost:5173')
                end = next(tool for tool in bot.agent.tools if getattr(tool, 'name', '') == 'end_conversation')
                await end.on_invoke_tool(ToolContext(context=None, tool_name='end_conversation', tool_call_id='test', tool_arguments='{}'), '{}')
                self.assertTrue(bot.sleep_requested)
            events = []
            with patch('sys.argv', ['chat.py']), patch('chat.load_key'), patch('chat.robot_name', return_value='Shiro'), patch('chat.read_terminal', side_effect=['Hi Shiro', 'thankyou', 'ignored', '/quit']), patch('chat.Chatbot') as factory, patch('chat.request', return_value={'displays': 1}), patch('chat.wait_for_speech', new_callable=AsyncMock, side_effect=lambda *args: events.append('speech ended')):
                instance = factory.return_value
                instance.sleep_requested = False
                async def reply(text, **kwargs):
                    instance.sleep_requested = text == 'thankyou'
                    return 'You’re welcome!' if instance.sleep_requested else 'Hi!'
                instance.reply = AsyncMock(side_effect=reply)
                instance.show_state.side_effect = lambda state: events.append(state)
                self.assertEqual(await main(), 0)
                self.assertEqual([call.args[0] for call in instance.reply.call_args_list], ['Hi Shiro', 'thankyou'])
                self.assertEqual(events[-2:], ['speech ended', 'sleeping'])
        asyncio.run(check())

    def test_motion_released_only_after_successful_speech(self):
        async def check():
            queued = SimpleNamespace(is_error=False, content=[SimpleNamespace(text=json.dumps(dict(state='queued', motion_id=7)))])
            speech = dict(displays=1, sequence=9, generation='voice')
            for error, displays in (('', 1), ('audio failed', 1), ('', 0)):
                server = SimpleNamespace(call_tool=AsyncMock(side_effect=[queued, SimpleNamespace(is_error=False)]))
                waiting = dict(displays=1, command=speech, acknowledgment=None)
                done = dict(displays=displays, command=speech, acknowledgment=dict(sequence=9, generation='voice', error=error) if displays else None)
                with patch('chat.request', side_effect=[waiting, done]), patch('chat.asyncio.sleep', new_callable=AsyncMock) as pause:
                    await start_after_announcement(server, speech, 'http://localhost:5173')
                expected = 'start_queued_motion' if not error and displays else 'stop_robot'
                self.assertEqual(server.call_tool.call_args.args[0], expected)
                pause.assert_awaited_once()
        asyncio.run(check())

    def test_arrival_notices_once_and_no_motion_tools_in_report(self):
        async def check():
            notices = queue.Queue()
            statuses = [dict(state='moving', motion_id=1), dict(state='arrived', motion_id=1, destination='bedroom'), dict(state='arrived', motion_id=1), dict(state='failed', motion_id=2)]
            server = SimpleNamespace(call_tool=AsyncMock(side_effect=[SimpleNamespace(is_error=False, content=[SimpleNamespace(text=json.dumps(status))]) for status in statuses]))
            with patch('chat.asyncio.sleep', new_callable=AsyncMock, side_effect=[None, None, None, asyncio.CancelledError()]):
                with self.assertRaises(asyncio.CancelledError):
                    await watch_motion(server, notices)
            self.assertEqual(notices.qsize(), 2)
            self.assertEqual(notices.get_nowait()['destination'], 'bedroom')
            self.assertEqual(notices.get_nowait()['state'], 'failed')
            result = SimpleNamespace(final_output='I’ve reached the bedroom.', to_input_list=lambda: [])
            with patch('chat.request', return_value={}), patch('chat.Runner.run', new_callable=AsyncMock, return_value=result) as run:
                bot = Chatbot('http://localhost:5173')
                await bot.reply('Runtime arrival event', motion_update=True)
                self.assertEqual(run.call_args.args[0].tools, [])
                self.assertEqual(run.call_args.args[0].mcp_servers, [])
        asyncio.run(check())

    def test_spoken_weather_omits_citations(self):
        answer = 'It’s hazy and about 25°C right now. Thunderstorms are possible. ([timeanddate.com](https://www.timeanddate.com/weather/malaysia/shah-alam?utm_source=openai))'
        self.assertEqual(spoken_reply(answer), 'It’s hazy and about 25 degrees Celsius right now. Thunderstorms are possible.')
        self.assertEqual(spoken_reply('It’s 25°C.\nSources: [Weather](https://example.com/weather)'), 'It’s 25 degrees Celsius.')
        self.assertEqual(spoken_reply('It’s partly sunny.\n\nSources: \n[Weather](https://example.com)\n'), 'It’s partly sunny.')
        self.assertEqual(spoken_reply('It’s -2°F.'), 'It’s -2 degrees Fahrenheit.')

    def test_timeout_starts_after_playback_ack(self):
        speech = dict(sequence=3, generation='session', displays=1)
        statuses = [dict(command=speech, displays=1, acknowledgment=None),
                    dict(command=speech, displays=1, acknowledgment=dict(sequence=3, generation='old')),
                    dict(command=speech, displays=1, acknowledgment=dict(sequence=3, generation='session'))]
        with patch('chat.request', side_effect=statuses), patch('chat.time.monotonic', return_value=100), patch('chat.select.select', return_value=([], [], [])) as ready:
            with self.assertRaises(TimeoutError):
                read_terminal(30, speech=speech, url='http://localhost:5173')
            self.assertEqual([call.args[3] for call in ready.call_args_list], [.25, .25, 30])
        with patch('chat.request', return_value=statuses[0]), patch('chat.select.select', return_value=([sys.stdin], [], [])), patch('builtins.input', return_value='new question'):
            self.assertEqual(read_terminal(30, speech=speech, url='http://localhost:5173'), 'new question')

    def test_sleep_wake_timeout_and_renamed_trigger(self):
        self.assertTrue(wake_trigger('Hi, Shiro! What time is it?', 'Shiro'))
        self.assertTrue(wake_trigger('hi SHIRO', 'Shiro'))
        self.assertFalse(wake_trigger('Hi Shiroko', 'Shiro'))
        self.assertFalse(wake_trigger('What time is it?', 'Shiro'))
        inputs = ['ignored', 'Hi Shiro', 'question', TimeoutError(), 'ignored again', 'Hi Mimo', '/quit']
        with patch('sys.argv', ['chat.py', '--no-speak']), patch('chat.load_key'), patch('chat.robot_name', side_effect=['Shiro', 'Shiro', 'Shiro', 'Mimo', 'Mimo']), patch('chat.read_terminal', side_effect=inputs) as terminal, patch('chat.Chatbot') as bot:
            bot.return_value.reply = AsyncMock(return_value='Hello')
            self.assertEqual(asyncio.run(main()), 0)
            self.assertEqual([call.args[0] for call in bot.return_value.reply.call_args_list], ['Hi Shiro', 'question', 'Hi Mimo'])
            self.assertEqual([call.args[0] for call in bot.return_value.show_state.call_args_list], ['sleeping', 'listening', 'sleeping', 'listening'])
            self.assertEqual([call.args[0] for call in terminal.call_args_list], [None, None, 30, 30, None, None, 30])

    def test_location_lookup_and_failures(self):
        with patch.dict(os.environ, {'DOTS_CITY': ''}):
            with patch('chat.urlopen', return_value=io.BytesIO(json.dumps({'city': 'Kajang', 'region': 'Selangor', 'country_name': 'Malaysia', 'ip': 'private'}).encode())):
                location = current_location()
                self.assertEqual(location['city'], 'Kajang')
                self.assertNotIn('ip', location)
            with patch('chat.urlopen', side_effect=OSError('offline')):
                self.assertIn('error', current_location())
            with patch('chat.urlopen', return_value=io.BytesIO(b'{"error":true}')):
                self.assertIn('error', current_location())
        with patch.dict(os.environ, {'DOTS_CITY': 'Kajang, Malaysia'}), patch('chat.urlopen') as lookup:
            self.assertEqual(current_location()['city'], 'Kajang, Malaysia')
            lookup.assert_not_called()

    def test_name_refresh_clock_and_search(self):
        bot = Chatbot('http://localhost:5173')
        with patch('chat.request', side_effect=[{'appearance': {'name': 'Shiro'}}, {'appearance': {'name': 'Mimo'}}]):
            self.assertIn('"Shiro"', bot.agent.instructions(None, bot.agent))
            self.assertIn('"Mimo"', bot.agent.instructions(None, bot.agent))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'robot.json'
            config.write_text(json.dumps({'appearance': {'name': 'Offline robot'}}))
            with patch.dict(os.environ, {'DOTS_CONFIG': str(config)}), patch('chat.request', side_effect=ValueError('offline')):
                self.assertEqual(robot_name(bot.url), 'Offline robot')
        with patch.dict(os.environ, {'DOTS_TIMEZONE': 'Asia/Kuala_Lumpur'}):
            clock = current_datetime()
            self.assertEqual(clock['timezone'], 'Asia/Kuala_Lumpur')
            self.assertEqual(datetime.fromisoformat(clock['datetime']).utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(datetime.fromisoformat(current_datetime('UTC')['datetime']).utcoffset().total_seconds(), 0)
        search = next(tool for tool in bot.agent.tools if isinstance(tool, WebSearchTool))
        self.assertTrue(search.external_web_access)
        self.assertIn('current_datetime', [tool.name for tool in bot.agent.tools if hasattr(tool, 'name')])

    def test_spoken_replies_default_and_opt_out(self):
        for options, spoken in (([], True), (['--no-speak'], False)):
            with self.subTest(options=options), patch('sys.argv', ['chat.py'] + options), patch('chat.load_key'), patch('chat.robot_name', return_value='Shiro'), patch('chat.read_terminal', side_effect=['Hi Shiro', '/quit']), patch('chat.Chatbot') as bot, patch('chat.request', return_value={}) as speech:
                bot.return_value.reply = AsyncMock(return_value='Hi')
                self.assertEqual(asyncio.run(main()), 0)
                self.assertEqual(speech.called, spoken)
                if spoken:
                    self.assertEqual(speech.call_args.args[1:], ('say', {'text': 'Hi', 'stream': True}))

    def test_conversation_and_display_lifecycle(self):
        async def check():
            history = [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Hi'}]
            result = SimpleNamespace(final_output='Hi', to_input_list=lambda: history.copy())
            with patch('chat.request', return_value={}) as display, patch('chat.Runner.run', new_callable=AsyncMock, return_value=result) as run:
                bot = Chatbot('http://localhost:5173')
                self.assertEqual(bot.agent.model, 'gpt-6-luna')
                self.assertEqual(await bot.reply('Hello'), 'Hi')
                await bot.reply('Remember me?')
                self.assertEqual(run.call_args.args[1], history + [{'role': 'user', 'content': 'Remember me?'}])
                self.assertEqual([call.args[2]['state'] for call in display.call_args_list], ['thinking', 'idle'] * 2)
                run.side_effect = RuntimeError('provider failure')
                with self.assertRaises(RuntimeError):
                    await bot.reply('Retry')
                self.assertEqual(bot.history, history)
                self.assertEqual(display.call_args.args[2]['state'], 'idle')
                bot.history.clear()
                run.side_effect = None
                display.side_effect = ValueError('Display offline')
                await bot.reply('New conversation')
                self.assertEqual(run.call_args.args[1], [{'role': 'user', 'content': 'New conversation'}])
                with self.assertRaises(ValueError):
                    await bot.reply(' ')
        asyncio.run(check())


class TrackingLaunchCheck(unittest.TestCase):
    def test_quiet_child_stops_on_exit_and_chat_failure(self):
        original_popen = subprocess.Popen
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {'XDG_CACHE_HOME': cache, 'OPENAI_API_KEY': 'must-not-be-inherited'}):
                children = []
                def spawn(command, **options):
                    self.assertEqual(command[-2:], ['--url', 'http://127.0.0.1:5174'])
                    self.assertNotIn('OPENAI_API_KEY', options['env'])
                    self.assertEqual(options['stdin'], subprocess.DEVNULL)
                    self.assertTrue(options['start_new_session'])
                    child = original_popen([sys.executable, '-c', 'import sys,time; print("Tracking: debug", flush=True); print("ROS debug", file=sys.stderr, flush=True); time.sleep(60)'], **options)
                    children.append(child)
                    return child
                console = io.StringIO()
                with patch('chat.subprocess.Popen', side_effect=spawn), redirect_stdout(console), redirect_stderr(console):
                    try:
                        with start_tracking('http://127.0.0.1:5174'):
                            log = Path(cache) / 'face-agent/tracking-chat.log'
                            deadline = time.monotonic() + 3
                            while 'ROS debug' not in log.read_text() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertIn('Tracking: debug', log.read_text())
                            if fail:
                                raise ValueError('chat failed')
                    except ValueError:
                        if not fail:
                            raise
                self.assertEqual(console.getvalue(), '')
                self.assertIsNotNone(children[0].poll())

    def test_tracker_failure_has_one_short_error_without_debug_output(self):
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {'XDG_CACHE_HOME': cache}), patch('chat.subprocess.Popen', return_value=Mock(wait=Mock(return_value=1))), patch('chat.stop'), redirect_stderr(io.StringIO()) as console:
            with start_tracking('http://127.0.0.1:5174'):
                deadline = time.monotonic() + 2
                while not console.getvalue() and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertEqual(console.getvalue().count('Eye tracking is unavailable.'), 1)
            self.assertIn('tracking-chat.log', console.getvalue())

    def test_launch_failure_keeps_chat_available(self):
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {'XDG_CACHE_HOME': cache}), patch('chat.subprocess.Popen', side_effect=OSError('private launch detail')), redirect_stderr(io.StringIO()) as console:
            with start_tracking('http://127.0.0.1:5174'):
                continued = True
            self.assertTrue(continued)
            self.assertIn('Eye tracking could not start.', console.getvalue())
            self.assertNotIn('private launch detail', console.getvalue())


if __name__ == '__main__':
    unittest.main()
