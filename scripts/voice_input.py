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


class MicrophoneDisconnected(RuntimeError):
    pass


class VoiceInput:
    def __init__(self, url, model='gpt-live-transcribe', language=None, forward=0, clockwise=False):
        self.url = url; self.model = model; self.language = language
        self.forward = forward; self.clockwise = clockwise
        self.process = self.log = None
        self.reader = self.guard = None
        self.ready = None; self.failed = None; self.closing = False
        self.inputs = asyncio.Queue(maxsize=8)
        self.controls = {}; self.sequence = 0; self.control_lock = asyncio.Lock()
        self.restart_lock = asyncio.Lock()
        self.held = False; self.paused = True; self.accepting = False
        self.hint = None; self.partials = {}; self.terminal_open = True

    async def api(self, path, value=None):
        return await asyncio.to_thread(request, self.url, path, value, timeout=1)

    async def __aenter__(self):
        log_path = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/microphone-chat.log'
        try:
            await self.api('input-mode', dict(mode='voice', micForwardDeg=self.forward, micClockwise=self.clockwise))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open('a')
            print('Voice mode · calibrating background; stay silent until ready.', flush=True)
            try:
                await self.start_microphone()
            except MicrophoneDisconnected:
                await self.ensure_microphone()
            await self.sync_playback()
            self.guard = asyncio.create_task(self.watch_playback())
            print('Voice mode ready. Speak normally; /reset clears history and /quit exits.', flush=True)
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
        self.controls.clear()
        command = [sys.executable, str(root / 'scripts/respeaker.py'), '--diagnostics', '--control-stdin', '--stt-model', self.model]
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
            print('Microphone USB connection lost. Reconnecting and recalibrating; stay silent.', file=sys.stderr, flush=True)
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
                print('Microphone reconnected and calibrated.', file=sys.stderr, flush=True)
                return True
            self.failed = RuntimeError('ReSpeaker is still unavailable after five retries. Check its USB cable or hub, then restart voice chat.')
            raise self.failed

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
                        future.set_result(event['paused'])
                elif status == 'listening':
                    if not self.ready.done():
                        self.ready.set_result(None)
                elif status == 'transcription_incomplete':
                    incomplete = RuntimeError('Microphone transcription failed; local audio remains in test-output/.')
                    self.accepting = False
                elif status == 'microphone_error':
                    if event.get('reconnectable') is True:
                        raise MicrophoneDisconnected('ReSpeaker USB connection lost.')
                    raise incomplete or RuntimeError('Microphone failed. Check ~/.cache/face-agent/microphone-chat.log.')
                elif self.accepting:
                    if status == 'utterance_started':
                        self.direction(event.get('doa_deg'), event.get('utterance_id'))
                    elif event.get('speech_candidate_track_id') is not None:
                        self.direction(event.get('native_doa_deg'), event.get('utterance_id'))
                    elif status == 'transcript_partial' and sys.stdout.isatty():
                        ident = event['item_id']
                        self.partials[ident] = self.partials.get(ident, '') + event['delta']
                        print('\r\x1b[2Kyou> ' + self.partials[ident].strip(), end='', flush=True)
                    elif status == 'transcript_final':
                        self.partials.pop(event['item_id'], None)
                        text = event['text'].strip()
                        if text and len(text) <= 12000:
                            self.inputs.put_nowait(event)
            if not self.closing:
                raise incomplete or RuntimeError('Microphone stopped. Check ~/.cache/face-agent/microphone-chat.log.')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failed = error; self.accepting = False
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
                    # Playback may have started while a replacement mic calibrated.
                    status = await self.api('status')
                    if status['inputMode'] != 'voice':
                        raise ValueError('Another chat selected words mode.')
                    paused = self.held or status['speechPending']
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
                        raise RuntimeError('Microphone did not confirm its playback gate; voice input stopped.') from None
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
        else:
            await self.sync_playback()

    async def publish_hint(self, status):
        if not self.hint or time.monotonic() - self.hint[1] > 1 or status['speechPending']:
            return
        if not status['awake']:
            await self.api('command', dict(state='listening'))
            status = await self.api('status')
        await self.api('speaker', dict(session=status['trackingSession'], doaDeg=self.hint[0], utterance=self.hint[2]))

    async def sync_playback(self):
        try:
            status = await self.api('status')
            if type(status.get('speechPending')) is not bool:
                raise ValueError('Restart the updated robot service before using voice mode.')
            if status.get('inputMode') != 'voice':
                raise ValueError('Another chat selected words mode; restart voice chat to use the microphone.')
            await self.control(self.held or status['speechPending'])
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
            if self.accepting:
                if notices is not None:
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
                    print('you> ' + text, flush=True)
                    return text
            elif timeout is not None:
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
