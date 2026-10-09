"""Own the microphone process, playback gate and local speech-direction hints."""
import asyncio
import json
import math
import os
from pathlib import Path
import queue
import select
import sys
import time

from dotsctl import request
from voice_errors import RealtimeUnavailable


class MicrophoneDisconnected(RuntimeError):
    pass


class VoiceInput:
    def __init__(self, url, model='gpt-live-transcribe', language='en', forward=0, clockwise=False, wake_model='small', status_driven=False):
        self.url = url; self.model = model; self.language = language
        self.forward = forward; self.clockwise = clockwise
        self.wake_model = wake_model; self.awake = status_driven
        self.status_driven = status_driven
        self.activity = asyncio.Queue()
        self.process = self.log = None
        self.reader = self.guard = None
        self.ready = None; self.failed = None; self.closing = False; self.replacing = False
        self.inputs = asyncio.Queue(maxsize=8)
        self.controls = {}; self.sequence = 0; self.control_lock = asyncio.Lock()
        self.restart_lock = asyncio.Lock()
        self.held = status_driven; self.paused = True; self.accepting = False
        self.hint = None; self.partials = {}; self.terminal_open = True
        self.utterance_active = False
        self.turn_complete = False

    async def api(self, path, value=None):
        return await asyncio.to_thread(request, self.url, path, value, timeout=1)

    async def __aenter__(self):
        log_path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/microphone-chat.log'
        try:
            await self.api('input-mode', dict(mode='voice', micForwardDeg=self.forward, micClockwise=self.clockwise))
            if not self.status_driven:
                await self.api('command', dict(state='sleeping'))
            else:
                global_status = (await self.api('status')).get('robotStatus') or {}
                self.awake = global_status.get('status') != 'SLEEPING'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open('a')
            print('Voice mode · connecting Realtime; waiting for ReSpeaker.' if self.awake else 'Voice mode · loading local Whisper; waiting for ReSpeaker.', flush=True)
            try:
                await self.start_microphone()
            except MicrophoneDisconnected:
                await self.ensure_microphone()
            await self.sync_playback()
            self.guard = asyncio.create_task(self.watch_playback())
            print('Voice mode ready · follows /robot/status; sleeping wake detection stays local.' if self.status_driven else 'Voice mode ready · local wake detection. /reset clears history; /quit exits.', flush=True)
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *exception):
        await self.close()

    async def start_microphone(self):
        root = Path(__file__).resolve().parents[1]
        self.ready = asyncio.get_running_loop().create_future()
        self.failed = None; self.paused = True; self.accepting = False; self.sequence = 0
        self.utterance_active = False; self.turn_complete = False
        self.controls.clear()
        command = [sys.executable, str(root / 'scripts/respeaker.py'), '--diagnostics', '--control-stdin', '--stt-model', self.model if self.awake else self.wake_model]
        if self.status_driven and self.awake:
            command += ['--single-utterance']
        if not self.awake:
            config = await self.api('config')
            name = config.get('appearance', {}).get('name', 'Kuro')
            command += ['--stt-prompt', f'The robot is named {name}.']
        if self.language:
            command += ['--language', self.language]
        self.process = await asyncio.create_subprocess_exec(*command, cwd=root,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
        self.reader = asyncio.create_task(self.receive())
        await asyncio.wait_for(self.ready, 90)

    async def stop_microphone(self):
        if self.process:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 30)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)

    async def ensure_microphone(self):
        async with self.restart_lock:
            if not self.failed:
                return False
            if not isinstance(self.failed, MicrophoneDisconnected):
                raise self.failed
            print('Microphone USB connection lost. Reconnecting ReSpeaker.', file=sys.stderr, flush=True)
            self.accepting = False; self.hint = None; self.partials.clear()
            while not self.inputs.empty():
                self.inputs.get_nowait()
            for _ in range(5):
                await self.stop_microphone()
                await asyncio.sleep(2)
                try:
                    await self.start_microphone()
                except MicrophoneDisconnected:
                    continue
                # New child utterance IDs and old hints cannot cross a USB reconnection.
                if (await self.api('status'))['inputMode'] != 'voice':
                    raise ValueError('Another chat selected words mode during microphone recovery.')
                await self.api('input-mode', dict(mode='voice', micForwardDeg=self.forward, micClockwise=self.clockwise))
                print('Microphone reconnected.', file=sys.stderr, flush=True)
                return True
            self.failed = RuntimeError('ReSpeaker is still unavailable after five retries. Check its USB cable or hub, then restart voice chat.')
            raise self.failed

    async def restart(self, *, awake):
        """Replace a failed worker with input held closed until global status resumes."""
        self.held = True; self.accepting = False
        guard = self.guard
        if guard:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        async with self.restart_lock:
            self.replacing = True
            try:
                await self.stop_microphone()
                self.awake = awake
                self.hint = None; self.partials.clear()
                for events in (self.inputs, self.activity):
                    while not events.empty():
                        events.get_nowait()
                await self.publish_text('')
                await self.start_microphone()
            except BaseException:
                await self.stop_microphone()
                raise
            finally:
                self.replacing = False
        self.guard = asyncio.create_task(self.watch_playback())

    def direction(self, value, utterance=0):
        self.hint = None
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 360:
            self.hint = value, time.monotonic(), utterance or 0

    async def receive(self):
        incomplete = None
        try:
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                status = event.get('status')
                if status == 'microphone_control':
                    future = self.controls.pop(event['sequence'], None)
                    if future and not future.done():
                        # Accept frames following the resume ACK in this same IPC
                        # batch; waiting for control() to resume can lose the trigger.
                        self.paused = event['paused']
                        self.accepting = not self.paused and not self.held
                        future.set_result(event['paused'])
                elif status == 'listening':
                    if not self.ready.done():
                        self.ready.set_result(None)
                elif status == 'transcription_retry':
                    print(f"Realtime connection attempt {event['attempt']}/{event['attempts']} failed ({event['reason']}); retrying.", file=sys.stderr, flush=True)
                elif status == 'transcription_incomplete':
                    incomplete = RuntimeError('Microphone transcription failed; local audio remains in test-output/.')
                    self.accepting = False
                elif status == 'microphone_error':
                    if event.get('category') == 'realtime_unavailable':
                        raise RealtimeUnavailable(event.get('message', 'Realtime transcription is unavailable.'),
                                                  retryable=event.get('retryable', True) is True)
                    if event.get('reconnectable') is True:
                        raise MicrophoneDisconnected('ReSpeaker USB connection lost.')
                    raise incomplete or RuntimeError('Microphone failed. Check ~/.cache/face-agent/microphone-chat.log.')
                elif self.accepting:
                    if status == 'utterance_started':
                        self.utterance_active = True
                        if self.awake:
                            self.activity.put_nowait('LISTENING')
                        self.partials.clear()
                        await self.publish_text('', utterance=event.get('utterance_id'))
                        self.direction(event.get('doa_deg'), event.get('utterance_id'))
                    elif status == 'speech_direction':
                        self.direction(event.get('native_doa_deg'), event.get('utterance_id'))
                    elif status == 'transcript_partial':
                        ident = event['item_id']
                        self.partials[ident] = self.partials.get(ident, '') + event['delta']
                        await self.publish_text(self.partials[ident], utterance=event.get('utterance_id'))
                        if self.awake and sys.stdout.isatty():
                            print('\r\x1b[2Kyou> ' + self.partials[ident].strip(), end='', flush=True)
                    elif status == 'transcript_final':
                        self.utterance_active = False
                        self.partials.pop(event['item_id'], None)
                        text = event['text'].strip()
                        if len(text) <= 12000 and (text or self.status_driven and self.awake):
                            await self.publish_text(text, final=True, utterance=event.get('utterance_id'))
                            self.inputs.put_nowait(event)
                        if event.get('paused_after'):
                            self.turn_complete = True
                            self.paused = True; self.accepting = False
                    elif status == 'utterance_discarded':
                        self.utterance_active = False
                        if self.awake:
                            self.activity.put_nowait('IDLE')
            if not self.closing and not self.replacing:
                raise incomplete or (RealtimeUnavailable('Microphone worker stopped; restarting voice input.')
                                     if self.awake else MicrophoneDisconnected('Local wake microphone worker stopped.'))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failed = error; self.accepting = False; self.utterance_active = False
            if not self.ready.done():
                self.ready.set_exception(error)
            for future in self.controls.values():
                if not future.done():
                    future.set_exception(error)

    async def control(self, paused):
        async with self.control_lock:
            while True:
                await self.ensure_microphone()
                if not paused and self.paused:
                    # Playback may have started while a replacement mic loaded.
                    status = await self.api('status')
                    if status['inputMode'] != 'voice':
                        raise ValueError('Another chat selected words mode.')
                    paused = self.held or status['speechPending'] or self.status_blocked(status)
                if paused:
                    self.utterance_active = False
                if paused == self.paused:
                    self.accepting = not paused
                    return
                self.accepting = False
                self.sequence += 1
                sequence = self.sequence
                future = asyncio.get_running_loop().create_future()
                self.controls[sequence] = future
                try:
                    self.process.stdin.write((json.dumps(dict(paused=paused, sequence=sequence)) + '\n').encode())
                    await self.process.stdin.drain()
                    try:
                        acknowledged = await asyncio.wait_for(future, 5)
                    except asyncio.TimeoutError:
                        error = RealtimeUnavailable('Microphone playback gate timed out; restarting voice input.')
                        self.failed = error
                        raise error from None
                    if acknowledged != paused:
                        raise RuntimeError('Microphone did not confirm its playback gate.')
                    self.paused = paused; self.accepting = not paused
                    return
                except MicrophoneDisconnected:
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    await self.reader
                    if isinstance(self.failed, MicrophoneDisconnected):
                        continue
                    raise self.failed or RuntimeError('Microphone control connection stopped.')
                finally:
                    self.controls.pop(sequence, None)

    async def hold(self, held):
        self.held = held
        if held:
            await self.control(True)
            while not self.inputs.empty():
                self.inputs.get_nowait()
            self.partials.clear(); self.hint = None
            self.utterance_active = False
            await self.publish_text('')
        else:
            await self.sync_playback()

    async def set_awake(self, awake):
        """Switch STT with capture closed; sleeping audio stays local."""
        if self.awake == awake:
            return
        guard = self.guard
        if guard:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
        held = self.held
        previous = self.awake
        try:
            if not isinstance(self.failed, RealtimeUnavailable):
                await self.control(True)
            self.accepting = False
            if awake:
                print('Connecting to Realtime transcription…', flush=True)
            async with self.restart_lock:
                self.held = True; self.replacing = True
                await self.stop_microphone()
                self.awake = awake
                self.hint = None; self.partials.clear()
                while not self.inputs.empty():
                    self.inputs.get_nowait()
                try:
                    await self.start_microphone()
                except RealtimeUnavailable:
                    # Restore local wake detection if remote startup fails.
                    await self.stop_microphone()
                    self.awake = previous
                    await self.start_microphone()
                    raise
            if awake:
                print('Realtime transcription ready.', flush=True)
            self.held = held
            await self.sync_playback()
        finally:
            self.held = held; self.replacing = False
            if guard:
                self.guard = asyncio.create_task(self.watch_playback())

    async def publish_text(self, text, final=False, utterance=0):
        if not self.awake:
            return  # Wake recognition is local and must never appear on the display.
        try:
            status = await self.api('status')
            await self.api('listening-text', dict(session=status['trackingSession'],
                text=text[:12000], final=final, utterance=utterance or 0))
        except (OSError, ValueError):
            pass

    async def publish_hint(self, status):
        if not self.awake or not status['awake'] or not self.hint or time.monotonic() - self.hint[1] > 1 or status['speechPending']:
            return
        await self.api('speaker', dict(session=status['trackingSession'], doaDeg=self.hint[0], utterance=self.hint[2]))

    def status_blocked(self, status):
        if not self.status_driven:
            return False
        global_status = status.get('robotStatus')
        if not global_status or self.turn_complete:
            return True
        state = global_status['status']
        return not (self.awake and state in ('IDLE', 'LISTENING') or not self.awake and state == 'SLEEPING')

    async def follow_status(self, state):
        """Choose local wake detection or command STT with capture closed."""
        target = False if state == 'SLEEPING' else True if state in ('IDLE', 'LISTENING') else self.awake
        if self.awake != target:
            await self.hold(True)
            await self.set_awake(target)
        self.turn_complete = False
        await self.hold(state not in ('IDLE', 'LISTENING', 'SLEEPING'))

    async def sync_playback(self):
        try:
            status = await self.api('status')
            if type(status.get('speechPending')) is not bool:
                raise ValueError('Restart the updated robot service before using voice mode.')
            if status.get('inputMode') != 'voice':
                raise ValueError('Another chat selected words mode; restart voice chat to use the microphone.')
            await self.control(self.held or status['speechPending'] or self.status_blocked(status))
            if self.accepting:
                await self.publish_hint(status)
        except (OSError, ValueError):
            await self.control(True)  # Unknown playback state keeps microphone input closed.
            raise

    async def watch_playback(self):
        offline = False
        while True:
            try:
                await self.sync_playback()
                offline = False
            except (OSError, ValueError):
                if not offline:
                    print('Voice input paused while the robot service is unavailable.', file=sys.stderr)
                offline = True
            await asyncio.sleep(.1)

    async def read(self, timeout=None, notices=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            if self.failed:
                if await self.ensure_microphone():
                    await self.sync_playback()
                if timeout is not None:
                    deadline = time.monotonic() + timeout
            if self.guard.done():
                await self.guard
                raise RuntimeError('Voice playback monitoring stopped.')
            if self.terminal_open:
                try:
                    readable = select.select([sys.stdin], [], [], 0)[0]
                except (OSError, ValueError):
                    readable = []
                if readable:
                    line = sys.stdin.readline()
                    if not line:
                        self.terminal_open = False
                    elif line.strip() in ('/quit', '/reset'):
                        return line.strip()
            if self.accepting or not self.inputs.empty():
                if notices is not None and self.accepting:
                    try:
                        return notices.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    event = self.inputs.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    self.direction(event.get('doa_deg'), event.get('utterance_id'))
                    try:
                        await self.publish_hint(await self.api('status'))
                    except (ValueError, OSError):
                        self.inputs.put_nowait(event)
                        await self.control(True)
                        continue
                    if sys.stdout.isatty():
                        print('\r\x1b[2K', end='')
                    text = event['text'].strip()
                    if self.awake:
                        print('you> ' + text, flush=True)
                    return text
            if timeout is not None and (not self.accepting or self.utterance_active):
                # Recording, its two-second endpoint and ASR are all activity.
                # Start counting idle time only once input is actually available.
                deadline = time.monotonic() + timeout
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            await asyncio.sleep(.05)

    async def close(self):
        self.closing = True; self.accepting = False
        if self.guard:
            self.guard.cancel()
            await asyncio.gather(self.guard, return_exceptions=True)
        await self.stop_microphone()
        if self.log:
            self.log.close()
        try:
            await self.api('input-mode', dict(mode='words'))
        except (OSError, ValueError):
            pass
