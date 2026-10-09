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
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from serve import RobotServer
from voice_input import VoiceInput, MicrophoneDisconnected
from voice_errors import RealtimeUnavailable
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
        self.server.actions = [dict(id=state, label=state) for state in ('idle', 'sleeping', 'listening', 'thinking', 'speaking')]

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.thread.join(timeout=2)
        self.server.server_close()

    async def test_idle_timer_waits_for_recording_and_transcription_then_expires(self):
        child = Path(self.directory.name) / 'slow_microphone.py'
        child.write_text('''import json,sys,threading,time
def emit(**value):
    print(json.dumps(value), flush=True)
def utterance():
    time.sleep(.05)
    emit(status='utterance_started', utterance_id=1)
    time.sleep(.2)
    emit(status='utterance_saved', utterance_id=1)
    time.sleep(.2)
    emit(status='transcript_partial', item_id='slow', delta='Hello', utterance_id=1)
    time.sleep(.2)
    emit(status='transcript_final', item_id='slow', text='Hello Kuro', utterance_id=1)
emit(status='listening')
started=False
for line in sys.stdin:
    control=json.loads(line)
    emit(status='microphone_control', **control)
    if not control['paused'] and not started:
        started=True
        threading.Thread(target=utterance, daemon=True).start()
''')
        original = asyncio.create_subprocess_exec
        async def spawn(*command, **options):
            return await original(sys.executable, str(child), **options)
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url) as voice:
                voice.terminal_open = False
                started = time.monotonic()
                self.assertEqual(await voice.read(.15), 'Hello Kuro')
                self.assertGreater(time.monotonic() - started, .5)
                self.assertFalse(voice.utterance_active)
                with self.assertRaises(TimeoutError):
                    await voice.read(.15)

    async def test_global_status_uses_realtime_and_gates_actual_microphone_ipc(self):
        child = Path(self.directory.name) / 'status_microphone.py'
        child.write_text('''import json,sys
print(json.dumps(dict(status='listening')), flush=True)
started=False
for line in sys.stdin:
    control=json.loads(line)
    print(json.dumps(dict(status='microphone_control', **control)), flush=True)
    if not control['paused'] and not started:
        started=True
        print(json.dumps(dict(status='utterance_started', utterance_id=1)), flush=True)
        print(json.dumps(dict(status='transcript_final', item_id='first', text='What time is it?', utterance_id=1)), flush=True)
''')
        original = asyncio.create_subprocess_exec
        async def spawn(*command, **options):
            self.assertEqual(command[command.index('--stt-model') + 1], 'gpt-live-transcribe')
            self.assertNotIn('--stt-prompt', command)
            return await original(sys.executable, str(child), **options)
        self.server.apply_robot_status(dict(status='IDLE', revision=1, source='test'))
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url, status_driven=True) as voice:
                voice.terminal_open = False
                self.assertEqual(self.server.command['state'], 'idle')
                await voice.hold(False)
                self.assertEqual(await asyncio.wait_for(voice.read(), 3), 'What time is it?')
                self.assertEqual(voice.activity.get_nowait(), 'LISTENING')
                for revision, state in enumerate(('THINKING', 'SPEAKING', 'ERROR', 'WORKING', 'DETECTING'), 2):
                    self.server.apply_robot_status(dict(status=state, revision=revision, source='test'))
                    await voice.sync_playback()
                    self.assertFalse(voice.accepting)
                    self.assertTrue(voice.paused)
                self.server.apply_robot_status(dict(status='IDLE', revision=7, source='test'))
                await voice.sync_playback()
                self.assertTrue(voice.accepting)
                self.assertFalse(voice.paused)

    async def test_sleep_wake_transcripts_hidden_and_command_capture_is_one_turn(self):
        child = Path(self.directory.name) / 'mode_microphone.py'
        child.write_text('''import json,sys
awake=sys.argv[1]=='gpt-live-transcribe'
print(json.dumps(dict(status='listening')), flush=True)
started=False
for line in sys.stdin:
    control=json.loads(line)
    print(json.dumps(dict(status='microphone_control', **control)), flush=True)
    if not control['paused'] and not started:
        started=True
        text='What time is it?' if awake else 'Hi Kuro'
        print(json.dumps(dict(status='utterance_started', utterance_id=1)), flush=True)
        print(json.dumps(dict(status='transcript_partial', item_id='one', delta=text, utterance_id=1)), flush=True)
        print(json.dumps(dict(status='transcript_final', item_id='one', text=text, utterance_id=1, paused_after=awake)), flush=True)
''')
        original = asyncio.create_subprocess_exec
        models = []
        async def spawn(*command, **options):
            model = command[command.index('--stt-model') + 1]
            models.append(model)
            self.assertEqual('--single-utterance' in command, model == 'gpt-live-transcribe')
            return await original(sys.executable, str(child), model, **options)
        self.server.apply_robot_status(dict(status='SLEEPING', revision=1, source='test'))
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url, status_driven=True) as voice:
                voice.terminal_open = False
                await voice.follow_status('SLEEPING')
                self.assertEqual(await asyncio.wait_for(voice.read(), 3), 'Hi Kuro')
                self.assertEqual(self.server.listening_text['text'], '')
                self.assertTrue(voice.activity.empty())
                self.assertFalse(voice.awake)
                self.assertTrue(voice.accepting)  # Continue local wake recognition.
                session = self.server.tracking_session()
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(request, self.url, 'listening-text', dict(session=session, text='Hi Kuro', final=True, utterance=1))
                self.server.apply_robot_status(dict(status='LISTENING', revision=2, source='test'))
                await voice.follow_status('LISTENING')
                self.assertEqual(await asyncio.wait_for(voice.read(), 3), 'What time is it?')
                self.assertEqual(self.server.listening_text['text'], 'What time is it?')
                self.assertTrue(voice.turn_complete)
                await voice.sync_playback()
                self.assertTrue(voice.paused)
                self.assertFalse(voice.accepting)  # Cannot start a second utterance.
                self.server.apply_robot_status(dict(status='WORKING', revision=3, source='test'))
                await voice.follow_status('WORKING')
                self.assertEqual(self.server.listening_text['text'], '')
                self.assertFalse(voice.accepting)
                self.server.apply_robot_status(dict(status='SLEEPING', revision=4, source='test'))
                await voice.follow_status('SLEEPING')
                self.assertEqual(await asyncio.wait_for(voice.read(), 3), 'Hi Kuro')
                self.assertEqual(self.server.listening_text['text'], '')
                self.assertEqual(models, ['small', 'gpt-live-transcribe', 'small'])

    async def test_speech_hints_are_session_scoped_and_rejected_during_playback(self):
        with patch('serve.Handler.log_message'):
            await asyncio.to_thread(request, self.url, 'input-mode', dict(mode='voice'))
            await asyncio.to_thread(request, self.url, 'command', dict(state='listening'))
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

    async def test_listening_text_is_session_scoped_and_does_not_interrupt_animation(self):
        with patch('serve.Handler.log_message'):
            self.assertEqual((await asyncio.to_thread(request, self.url, 'status'))['command']['state'], 'sleeping')
            await asyncio.to_thread(request, self.url, 'input-mode', dict(mode='voice'))
            await asyncio.to_thread(request, self.url, 'command', dict(state='idle'))
            status = await asyncio.to_thread(request, self.url, 'status')
            payload = dict(session=status['trackingSession'], text='Hi Kuro', final=False, utterance=1)
            await asyncio.to_thread(request, self.url, 'listening-text', payload)
            updated = await asyncio.to_thread(request, self.url, 'status')
            self.assertEqual(updated['command'], status['command'])
            self.assertEqual(updated['listeningText']['text'], 'Hi Kuro')
            with self.server.changed:
                self.server.change_command('speaking', speech='/api/audio/fake')
                self.server.ack = None
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, self.url, 'listening-text', payload)
            await asyncio.to_thread(request, self.url, 'command', dict(state='sleeping'))
            self.assertEqual((await asyncio.to_thread(request, self.url, 'status'))['listeningText']['text'], '')
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, self.url, 'listening-text', payload)
            await asyncio.to_thread(request, self.url, 'input-mode', dict(mode='voice'))
            payload['session'] = (await asyncio.to_thread(request, self.url, 'status'))['trackingSession']
            for invalid in (dict(text=3), dict(final='yes'), dict(utterance=-1), dict(text='a' * 12001)):
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(request, self.url, 'listening-text', {**payload, **invalid})

    async def test_thinking_clears_input_and_rejects_late_transcripts(self):
        with patch('serve.Handler.log_message'):
            await asyncio.to_thread(request, self.url, 'input-mode', dict(mode='voice'))
            await asyncio.to_thread(request, self.url, 'command', dict(state='listening'))
            session = (await asyncio.to_thread(request, self.url, 'status'))['trackingSession']
            text = dict(session=session, text='Goodbye Kuro.', final=True, utterance=1)
            await asyncio.to_thread(request, self.url, 'listening-text', text)
            await asyncio.to_thread(request, self.url, 'command', dict(state='thinking'))
            thinking = await asyncio.to_thread(request, self.url, 'status')
            self.assertEqual(thinking['listeningText']['text'], '')
            self.assertEqual(thinking['trackingSession'], session)
            with self.assertRaises(ValueError):
                await asyncio.to_thread(request, self.url, 'listening-text', text)
            await asyncio.to_thread(request, self.url, 'command', dict(state='listening'))
            await asyncio.to_thread(request, self.url, 'listening-text', {**text, 'text': 'A new request'})
            self.assertEqual((await asyncio.to_thread(request, self.url, 'status'))['listeningText']['text'], 'A new request')

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
            self.assertIn(command[command.index('--stt-model') + 1], ('small', 'gpt-live-transcribe'))
            self.assertEqual(command[command.index('--language') + 1], 'en')
            if command[command.index('--stt-model') + 1] == 'small':
                self.assertEqual(command[command.index('--stt-prompt') + 1], 'The robot is named Kuro.')
            else:
                self.assertNotIn('--stt-prompt', command)
            return await original(sys.executable, str(child), **options)
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url) as voice:
                self.assertTrue(voice.accepting)
                await voice.publish_text('Recognized words')
                await voice.hold(True)
                self.assertEqual((await voice.api('status'))['listeningText']['text'], '')
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
                await voice.set_awake(True)
                await voice.api('command', dict(state='listening'))
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
                await voice.set_awake(False)
                self.assertFalse(voice.awake)
                self.assertIsNone(voice.failed)
                process = voice.process
            self.assertIsNotNone(process.returncode)
            status = await asyncio.to_thread(request, self.url, 'status')
            self.assertEqual(status['inputMode'], 'words')
            self.assertIsNone(status['speechHint'])

    async def test_remote_start_failure_restores_local_wake_and_next_wake_can_succeed(self):
        child = Path(self.directory.name) / 'remote_microphone.py'
        marker = Path(self.directory.name) / 'remote-attempted'
        child.write_text("""import json,sys
from pathlib import Path
model, marker = sys.argv[1], Path(sys.argv[2])
def emit(value):
    print(json.dumps(value), flush=True)
if model == 'gpt-live-transcribe' and not marker.exists():
    marker.touch()
    emit(dict(status='microphone_error', reconnectable=False, category='realtime_unavailable', message='Realtime connection timed out.'))
    sys.exit(1)
emit(dict(status='listening'))
for line in sys.stdin:
    emit(dict(status='microphone_control', **json.loads(line)))
""")
        original = asyncio.create_subprocess_exec
        models, processes = [], []
        async def spawn(*command, **options):
            model = command[command.index('--stt-model') + 1]
            models.append(model)
            process = await original(sys.executable, str(child), model, str(marker), **options)
            processes.append(process)
            return process
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url) as voice:
                await voice.hold(True)
                with self.assertRaisesRegex(RealtimeUnavailable, 'timed out'):
                    await voice.set_awake(True)
                self.assertFalse(voice.awake)
                self.assertIsNone(voice.failed)
                self.assertIsNone(voice.process.returncode)
                self.assertTrue(voice.paused)
                await voice.hold(False)
                self.assertTrue(voice.accepting)
                await voice.hold(True)
                await voice.set_awake(True)
                self.assertTrue(voice.awake)
                self.assertIsNone(voice.failed)
                # A later network loss must also allow switching back to local STT.
                voice.failed = RealtimeUnavailable('Connection lost')
                await voice.set_awake(False)
                self.assertFalse(voice.awake)
                self.assertIsNone(voice.failed)
        self.assertEqual(models, ['small', 'gpt-live-transcribe', 'small', 'gpt-live-transcribe', 'small'])
        self.assertTrue(all(process.returncode is not None for process in processes))

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

    async def test_realtime_worker_restart_clears_old_input_and_waits_for_global_idle(self):
        child = Path(self.directory.name) / 'recovering_microphone.py'
        marker = Path(self.directory.name) / 'worker-started'
        child.write_text('''import json,sys
from pathlib import Path
marker=Path(sys.argv[1]); first=not marker.exists(); marker.touch()
def emit(**value): print(json.dumps(value), flush=True)
emit(status='listening')
for line in sys.stdin:
    control=json.loads(line); emit(status='microphone_control', **control)
    if not control['paused']:
        if first:
            emit(status='microphone_error', category='realtime_unavailable', retryable=True, message='Audio queue full')
            break
        emit(status='transcript_final', item_id='new', text='A fresh request', utterance_id=1, paused_after=True)
''')
        original = asyncio.create_subprocess_exec
        async def spawn(*command, **options):
            return await original(sys.executable, str(child), str(marker), **options)
        self.server.apply_robot_status(dict(status='IDLE', revision=1, source='test'))
        with patch.dict(os.environ, {'XDG_CACHE_HOME': self.directory.name}), \
                patch('voice_input.asyncio.create_subprocess_exec', side_effect=spawn), \
                patch('serve.Handler.log_message'), redirect_stdout(io.StringIO()):
            async with VoiceInput(self.url, status_driven=True) as voice:
                voice.terminal_open = False
                old = voice.process
                await voice.follow_status('IDLE')
                with self.assertRaisesRegex(RealtimeUnavailable, 'queue full'):
                    await voice.read(2)
                voice.partials['old'] = 'Interrupted words'
                voice.activity.put_nowait('LISTENING')
                voice.inputs.put_nowait(dict(text='Old command'))
                self.server.apply_robot_status(dict(status='ERROR', revision=2, source='test'))
                await voice.restart(awake=True)
                self.assertIsNotNone(old.returncode)
                self.assertNotEqual(old.pid, voice.process.pid)
                self.assertIsNone(voice.failed)
                self.assertTrue(voice.held and voice.paused)
                self.assertFalse(voice.accepting)
                self.assertFalse(voice.partials)
                self.assertTrue(voice.inputs.empty() and voice.activity.empty())
                self.server.apply_robot_status(dict(status='IDLE', revision=3, source='test'))
                await voice.follow_status('IDLE')
                self.assertEqual(await voice.read(2), 'A fresh request')
                new = voice.process
            self.assertIsNotNone(new.returncode)

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
