"""Stream triggered utterances to the OpenAI Realtime transcription API."""
import asyncio
import base64
import json
import os
import re
import socket
from pathlib import Path

import numpy as np
import soxr
from websockets.asyncio.client import connect, process_exception
from websockets.exceptions import ConnectionClosed
from voice_errors import RealtimeUnavailable

MODEL = 'gpt-live-transcribe'


def load_key():
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'mimo-dots/openai-api-key'
        try:
            key = path.read_text().strip()
        except FileNotFoundError:
            pass
    if not key:
        raise RuntimeError('Configure the OpenAI API key in the robot Voice settings.')
    return key


NOISE_REDUCTION = ('near_field', 'far_field')
DELAYS = ('minimal', 'low', 'medium', 'high', 'xhigh')
# Replaying the owner's saved commands (2026-11-06, docs/fan-noise.md): near_field noise
# reduction cut or emptied 7 of 10 transcripts; delay "high" plus a prompt and keywords got
# all 10 right. We only use the final text after commit, so a higher delay costs nothing.
DEFAULT_DELAY = 'high'
DEFAULT_KEYWORDS = ('pick up', 'green bottle', 'soda can')


def noise_reduction(environ=os.environ):
    """OpenAI-side input noise reduction: off unless DOTS_STT_NOISE_REDUCTION asks for it."""
    value = environ.get('DOTS_STT_NOISE_REDUCTION', 'off').strip().lower()
    if value in ('', 'off', 'none', '0'):
        return None
    if value not in NOISE_REDUCTION:
        raise RuntimeError(f'DOTS_STT_NOISE_REDUCTION must be near_field, far_field or off, not {value!r}.')
    return value


def stt_delay(environ=os.environ):
    value = environ.get('DOTS_STT_DELAY', DEFAULT_DELAY).strip().lower()
    if value not in DELAYS:
        raise RuntimeError(f'DOTS_STT_DELAY must be one of {", ".join(DELAYS)}, not {value!r}.')
    return value


def hints_for(name, environ=os.environ):
    """Prompt and keywords for gpt-live-transcribe: the robot's name and its usual commands."""
    name = (name or 'Kuro').strip()
    raw = environ.get('DOTS_STT_KEYWORDS')
    keywords = [k.strip() for k in raw.split(',')] if raw is not None else [name, *DEFAULT_KEYWORDS]
    keywords = [re.sub(r'[<>\r\n]', ' ', k).strip() for k in keywords]
    return dict(prompt=f'Short commands and questions spoken to a home robot named {name}, '
                       'e.g. pick up the green bottle.',
                keywords=[k for k in dict.fromkeys(keywords) if k])


def session_config(model=MODEL, language='en', reduction=None, delay=None, hints=None):
    transcription = dict(model=model, delay=delay or stt_delay())
    if language and language != 'auto':
        transcription['languages'] = [language]
    for key in ('prompt', 'keywords'):
        if hints and hints.get(key):
            transcription[key] = hints[key]
    audio_input = {'format': {'type': 'audio/pcm', 'rate': 24000},
                   'transcription': transcription, 'turn_detection': None}
    reduction = noise_reduction() if reduction is None else reduction
    if reduction:
        audio_input['noise_reduction'] = {'type': reduction}
    return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': audio_input}}}


class RealtimeTranscriber:
    def __init__(self, socket, emit):
        self.socket = socket
        self.emit = emit
        self.queue = asyncio.Queue(maxsize=256)
        self.turn = None
        self.sender = asyncio.create_task(self.send_loop())
        self.receiver = asyncio.create_task(self.receive_loop())
        self.sender.add_done_callback(self.failed)
        self.receiver.add_done_callback(self.failed)

    @classmethod
    async def open(cls, emit, model=MODEL, language='en', hints=None):
        key = load_key()
        # Some IPv6 routes connect TCP but stall during TLS on this Pi.
        # Prefer IPv4, retaining an automatic-family fallback for IPv6-only networks.
        families = (socket.AF_INET, socket.AF_INET, socket.AF_UNSPEC)
        for attempt, family in enumerate(families, 1):
            try:
                return await cls.open_once(emit, model, language, key, family, hints)
            except RealtimeUnavailable:
                raise
            except Exception as error:
                retryable = process_exception(error) is None or isinstance(error, ConnectionClosed)
                status = getattr(getattr(error, 'response', None), 'status_code', None)
                reason = f'HTTP {status}' if type(status) is int else type(error).__name__
                if not retryable:
                    raise RealtimeUnavailable(f'Realtime transcription connection rejected ({reason}). Check the API key and model access.', retryable=False) from None
                if attempt == len(families):
                    raise RealtimeUnavailable(f'Realtime transcription unavailable after {attempt} connection attempts ({reason}). Check the network and try waking Kuro again.') from None
                emit(dict(status='transcription_retry', attempt=attempt, attempts=len(families), reason=reason))
                await asyncio.sleep(attempt)

    @classmethod
    async def open_once(cls, emit, model, language, key, family, hints=None):
        socket = await connect('wss://api.openai.com/v1/realtime?intent=transcription',
                               additional_headers={'Authorization': 'Bearer ' + key},
                               family=family, happy_eyeballs_delay=.25, interleave=1,
                               open_timeout=12, close_timeout=2, max_size=2 * 1024 * 1024,
                               compression=None)
        try:
            await socket.send(json.dumps(session_config(model, language, hints=hints)))
            async with asyncio.timeout(8):
                while True:
                    event = json.loads(await socket.recv())
                    if event.get('type') == 'error':
                        code = event['error'].get('code', 'unknown')
                        raise RealtimeUnavailable('Realtime transcription configuration rejected: ' + code,
                                                  retryable=code in ('server_error', 'rate_limit_exceeded'))
                    if event.get('type') in ('session.updated', 'transcription_session.updated'):
                        return cls(socket, emit)
        except BaseException:
            try:
                await socket.close()
            except Exception:
                pass
            raise

    def failed(self, task):
        if task.cancelled():
            return
        error = task.exception()
        if not isinstance(error, RealtimeUnavailable):
            error = RealtimeUnavailable('Realtime transcription connection was interrupted. Try waking Kuro again.')
        if self.turn and not self.turn['done'].done():
            self.turn['done'].set_exception(error)

    def enqueue(self, kind, value=None):
        for task in (self.sender, self.receiver):
            if task.done():
                if not task.cancelled() and isinstance(task.exception(), RealtimeUnavailable):
                    raise task.exception()
                raise RealtimeUnavailable('Realtime transcription connection stopped. Try waking Kuro again.') from None
        try:
            self.queue.put_nowait((kind, value))
        except asyncio.QueueFull:
            raise RealtimeUnavailable('Realtime transcription cannot keep up; restarting the connection. '
                                      'The interrupted utterance must be repeated.') from None

    def begin(self, utterance_id):
        if self.turn is not None:
            raise RuntimeError('Previous transcription has not finished.')
        self.turn = dict(utterance_id=utterance_id, item_id=None,
                         done=asyncio.get_running_loop().create_future())
        self.enqueue('begin')

    def append(self, pcm):
        self.enqueue('audio', pcm)

    async def finish(self):
        self.enqueue('commit')
        turn = self.turn
        try:
            return await asyncio.wait_for(turn['done'], 30)
        except TimeoutError:
            raise RealtimeUnavailable('Realtime transcription timed out waiting for the final text. Try waking Kuro again.') from None
        finally:
            self.turn = None

    async def send_loop(self):
        resampler = None
        while True:
            kind, data = await self.queue.get()
            try:
                if kind == 'begin':
                    resampler = soxr.ResampleStream(16000, 24000, 1, dtype='int16', quality='HQ')
                    continue
                output = resampler.resample_chunk(np.frombuffer(data or b'', dtype='<i2'), last=kind == 'commit')
                if output.size:
                    await self.socket.send(json.dumps({'type': 'input_audio_buffer.append',
                        'audio': base64.b64encode(output.astype('<i2', copy=False).tobytes()).decode()}))
                if kind == 'commit':
                    await self.socket.send(json.dumps({'type': 'input_audio_buffer.commit'}))
            finally:
                self.queue.task_done()

    async def receive_loop(self):
        async for message in self.socket:
            event = json.loads(message)
            kind = event.get('type')
            if kind == 'error':
                code = event.get('error', {}).get('code', 'unknown')
                raise RealtimeUnavailable('Realtime transcription failed: ' + code,
                                          retryable=code not in ('invalid_api_key', 'insufficient_quota',
                                                                'model_not_found', 'permission_denied', 'invalid_request_error'))
            if kind not in ('input_audio_buffer.committed', 'conversation.item.input_audio_transcription.delta',
                            'conversation.item.input_audio_transcription.completed', 'conversation.item.input_audio_transcription.failed'):
                continue
            if not self.turn:
                continue
            turn = self.turn
            if turn['item_id'] is None:
                turn['item_id'] = event['item_id']
            if event['item_id'] != turn['item_id']:
                continue
            if kind.endswith('.delta'):
                self.emit(dict(status='transcript_partial', item_id=event['item_id'],
                               utterance_id=turn['utterance_id'], delta=event.get('delta', '')))
            elif kind.endswith('.completed') and not turn['done'].done():
                turn['done'].set_result(dict(text=event['transcript'].strip(), item_id=event['item_id'], backend='realtime'))
            elif kind.endswith('.failed'):
                raise RealtimeUnavailable('Realtime could not transcribe this utterance.')

    async def close(self):
        if self.turn:
            if not self.turn['done'].done():
                self.turn['done'].cancel()
            elif not self.turn['done'].cancelled():
                self.turn['done'].exception()  # Consume failures when capture aborts before finish().
        self.sender.cancel(); self.receiver.cancel()
        await asyncio.gather(self.sender, self.receiver, return_exceptions=True)
        await self.socket.close()
