"""Exercise local HTTP and microphone IPC without an OpenAI request or speaker playback."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from serve import RobotServer
from voice_input import VoiceInput, MicrophoneDisconnected
from dotsctl import request

ROOT = Path(__file__).resolve().parents[1]


class VoiceIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        config = root / 'robot-config.json'
        config.write_text((ROOT / 'robot-config.default.json').read_text())
        self.server = RobotServer(('127.0.0.1', 0), config_file=config, static_root=root / 'web', key_file=root / 'private/key')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.server.actions = [dict(id=state, label=state) for state in ('idle', 'sleeping', 'listening', 'speaking')]

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.thread.join(timeout=2)
        self.server.server_close()

    async def test_speech_hints_are_session_scoped_and_rejected_during_playback(self):
        with patch('serve.Handler.log_message'):
            await asyncio.to_thread(request, self.url, 'input-mode', dict(mode='voice'))
            status = await asyncio.to_thread(request, self.url, 'status')
            hint = dict(session=status['trackingSession'], doaDeg=30)
            await asyncio.to_thread(request, self.url, 'speaker', hint)
            status = await asyncio.to_thread(request, self.url, 'status')
            self.assertEqual(status['speechHint']['doaDeg'], 30)
            self.assertFalse(status['speechPending'])
            with self.server.changed:
                self.server.change_command('speaking', speech='/api/audio/fake')
                self.server.ack = None
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, self.url, 'speaker', hint)
            status = await asyncio.to_thread(request, self.url, 'status')
            self.assertTrue(status['speechPending'])
            await asyncio.to_thread(request, self.url, 'ack', dict(sequence=status['command']['sequence'],
                                   generation=status['command']['generation'], state='speaking'))
            self.assertFalse((await asyncio.to_thread(request, self.url, 'status'))['speechPending'])
            await asyncio.to_thread(request, self.url, 'command', dict(state='sleeping'))
            await asyncio.to_thread(request, self.url, 'command', dict(state='listening'))
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, self.url, 'speaker', hint)
            self.assertIsNone((await asyncio.to_thread(request, self.url, 'status'))['speechHint'])
            for payload in (dict(mode='bad'), dict(mode='voice', micForwardDeg=True),
                            dict(mode='voice', micForwardDeg=float('nan')), dict(mode='voice', micClockwise=1)):
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(request, self.url, 'input-mode', payload)

    async def test_real_child_controls_gate_echo_and_restore_words_mode_on_exit(self):
        child = Path(self.directory.name) / 'fake_microphone.py'
        child.write_text('''import json,sys
def emit(value):
    print(json.dumps(value), flush=True)
emit(dict(status='listening'))
for line in sys.stdin:
    control = json.loads(line)
    emit(dict(status='transcript_final', item_id='late', text='Robot echo'))
    emit(dict(status='microphone_control', **control))
''')
        original = asyncio.create_subprocess_exec
        async def spawn(*command, **options):
            self.assertIn('--diagnostics', command)
            self.assertIn('--control-stdin', command)
            return await original(sys.executable, str(child), **options)
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url) as voice:
                self.assertTrue(voice.accepting)
                await voice.hold(True)
                self.assertTrue(voice.paused)
                self.assertFalse(voice.accepting)
                self.assertTrue(voice.inputs.empty())
                await voice.hold(False)
                self.assertFalse(voice.paused)
                self.assertTrue(voice.accepting)
                self.assertTrue(voice.inputs.empty())
                with self.server.changed:
                    self.server.change_command('speaking', speech='/api/audio/fake')
                    self.server.ack = None
                await voice.sync_playback()
                self.assertTrue(voice.paused)
                with self.server.changed:
                    self.server.ack = dict(generation=self.server.generation, sequence=self.server.command['sequence'])
                await voice.sync_playback()
                self.assertTrue(voice.accepting)
                voice.direction(90, 9)
                voice.inputs.put_nowait(dict(status='transcript_final', item_id='user', text='Hello', doa_deg=30, utterance_id=8))
                voice.terminal_open = False
                self.assertEqual(await voice.read(1), 'Hello')
                hint = (await voice.api('status'))['speechHint']
                self.assertEqual((hint['doaDeg'], hint['utterance']), (30, 8))
                voice.direction(None, 10)
                self.assertIsNone(voice.hint)
                voice.direction(28, 7)
                await voice.publish_hint(await voice.api('status'))
                hint = (await voice.api('status'))['speechHint']
                self.assertEqual((hint['doaDeg'], hint['utterance']), (28, 7))
                with patch.object(voice, 'publish_hint', side_effect=ValueError('Playback started during hint publication')):
                    with self.assertRaises(ValueError):
                        await voice.sync_playback()
                self.assertTrue(voice.paused)
                await voice.hold(True)
                with patch.object(voice, 'api', side_effect=ValueError('offline')):
                    with self.assertRaises(ValueError):
                        await voice.hold(False)
                self.assertTrue(voice.paused)
                process = voice.process
            self.assertIsNotNone(process.returncode)
            status = await asyncio.to_thread(request, self.url, 'status')
            self.assertEqual(status['inputMode'], 'words')
            self.assertIsNone(status['speechHint'])

    async def test_usb_disconnect_during_pause_restarts_closed_and_preserves_playback_gate(self):
        child = Path(self.directory.name) / 'usb_microphone.py'
        child.write_text('''import json,sys,time
generation = int(sys.argv[1])
def emit(value):
    print(json.dumps(value), flush=True)
if generation == 2:
    emit(dict(status='microphone_error', reconnectable=True))
    sys.exit(1)
emit(dict(status='listening'))
for line in sys.stdin:
    control = json.loads(line)
    if generation == 1 and control['paused']:
        emit(dict(status='transcription_incomplete'))
        emit(dict(status='microphone_error', reconnectable=True))
        sys.exit(1)
    emit(dict(status='microphone_control', **control))
    if generation > 2 and not control['paused']:
        time.sleep(.05)
        emit(dict(status='transcript_final', item_id='user', text='A new question', doa_deg=0, utterance_id=1))
''')
        original_spawn = asyncio.create_subprocess_exec
        original_sleep = asyncio.sleep
        processes = []
        async def spawn(*command, **options):
            process = await original_spawn(sys.executable, str(child), str(len(processes) + 1), **options)
            processes.append(process)
            return process
        async def short_retry(seconds):
            await original_sleep(.01 if seconds == 2 else seconds)
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('voice_input.asyncio.sleep', side_effect=short_retry), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            async with VoiceInput(self.url) as voice:
                session = (await voice.api('status'))['trackingSession']
                voice.inputs.put_nowait(dict(text='Stale input'))
                await voice.hold(True)
                self.assertEqual(len(processes), 3)
                self.assertTrue(voice.paused)
                self.assertFalse(voice.accepting)
                self.assertTrue(voice.inputs.empty())
                self.assertNotEqual(session, (await voice.api('status'))['trackingSession'])
                with self.server.changed:
                    self.server.change_command('speaking', speech='/api/audio/fake')
                    self.server.ack = None
                await voice.hold(False)
                self.assertTrue(voice.paused)
                with self.server.changed:
                    self.server.ack = dict(generation=self.server.generation, sequence=self.server.command['sequence'])
                await voice.sync_playback()
                voice.terminal_open = False
                self.assertEqual(await voice.read(2), 'A new question')
                self.assertFalse(voice.paused)
            self.assertTrue(all(process.returncode is not None for process in processes))

    async def test_usb_retry_limit_and_fatal_errors_do_not_loop_forever(self):
        from unittest.mock import AsyncMock
        voice = VoiceInput(self.url)
        voice.failed = RuntimeError('Provider configuration rejected')
        with patch.object(voice, 'start_microphone', new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(RuntimeError, 'configuration rejected'):
                await voice.ensure_microphone()
            start.assert_not_called()
        voice.failed = MicrophoneDisconnected('USB lost')
        with patch.object(voice, 'start_microphone', new_callable=AsyncMock,
                          side_effect=MicrophoneDisconnected('Still missing')) as start, \
                patch.object(voice, 'stop_microphone', new_callable=AsyncMock), \
                patch('voice_input.asyncio.sleep', new_callable=AsyncMock), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'five retries'):
                await voice.ensure_microphone()
            self.assertEqual(start.call_count, 5)
            self.assertFalse(voice.accepting)
