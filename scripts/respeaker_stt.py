"""Realtime OpenAI transcription of locally endpointed channel-0 utterances."""
import asyncio
import base64
from collections import deque
import json
import os
from pathlib import Path
import time

import numpy as np
import soxr
from websockets.asyncio.client import connect

from respeaker import emit

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
        raise RuntimeError('Set OPENAI_API_KEY or save the key in the studio Voice settings; use --no-stt for recording only.')
    return key


def session_config(model, delay, language):
    transcription = {'model': model}
    if model == MODEL:
        transcription['delay'] = delay
        if language:
            transcription['languages'] = [language]
    elif language:
        transcription['languages'] = [language]
    return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
        'format': {'type': 'audio/pcm', 'rate': 24000},
        'transcription': transcription, 'turn_detection': None}}}}


class Transcriber:
    def __init__(self, socket):
        self.socket = socket
        self.queue = asyncio.Queue(maxsize=256)
        self.unassigned = deque()
        self.items = {}
        self.completed = deque()
        self.turns = []
        self.active = None
        self.buffer = bytearray()
        self.sender = asyncio.create_task(self.send_loop())
        self.receiver = asyncio.create_task(self.receive_loop())

    @classmethod
    async def open(cls, model=MODEL, delay='low', language=None):
        try:
            for attempt in range(3):
                try:
                    socket = await connect('wss://api.openai.com/v1/realtime?intent=transcription',
                                           additional_headers={'Authorization': 'Bearer ' + load_key()},
                                           open_timeout=15, close_timeout=2, max_size=2 * 1024 * 1024,
                                           compression=None)
                    break
                except (OSError, asyncio.TimeoutError):
                    if attempt == 2:
                        raise RuntimeError('Cannot connect to OpenAI after three attempts; check internet access or use --no-stt.') from None
                    emit({'status': 'transcription_connect_retry', 'attempt': attempt + 2})
                    await asyncio.sleep(attempt + 1)
            await socket.send(json.dumps(session_config(model, delay, language)))
            deadline = time.monotonic() + 15
            while True:
                event = json.loads(await asyncio.wait_for(socket.recv(), max(.001, deadline-time.monotonic())))
                if event['type'] == 'error':
                    raise RuntimeError('OpenAI rejected transcription configuration: ' + event['error'].get('code', 'unknown'))
                if event['type'] in ('session.updated', 'transcription_session.updated'):
                    emit({'status': 'transcription_ready', 'model': model, 'delay': delay})
                    return cls(socket)
        except BaseException:
            if 'socket' in locals():
                await socket.close()
            raise

    def check(self):
        # ponytail: stop on mid-session failure; add reconnect/replay before unattended deployments.
        for task in (self.sender, self.receiver):
            if task.done():
                raise RuntimeError('OpenAI transcription connection stopped; local audio was retained.') from task.exception()

    def enqueue(self, kind, value):
        self.check()
        try:
            self.queue.put_nowait((kind, value))
        except asyncio.QueueFull:
            raise RuntimeError('Transcription cannot keep up with capture; stopping instead of dropping audio.')

    def begin(self, track_id, doa_deg=None, utterance_id=None):
        self.active = {'track_id': track_id, 'started': time.monotonic(),
                       'done': asyncio.get_running_loop().create_future(), 'wav': None, 'doa_deg': doa_deg,
                       'utterance_id': utterance_id, 'discarded': False}
        self.turns.append(self.active)
        self.unassigned.append(self.active)
        self.enqueue('begin', None)

    def append(self, pcm):
        self.buffer.extend(pcm)
        if len(self.buffer) >= 1280:  # 40 ms at 16 kHz; pre-roll goes in the first append.
            self.enqueue('audio', bytes(self.buffer))
            self.buffer.clear()

    def commit(self, wav, doa_deg=None):
        if self.active is None:
            return
        self.active['wav'] = str(wav) if wav is not None else None
        self.active['doa_deg'] = doa_deg
        self.active['ended'] = time.monotonic()
        if self.buffer:
            self.enqueue('audio', bytes(self.buffer))
            self.buffer.clear()
        self.enqueue('commit', None)
        self.active = None

    def discard(self):
        for turn in self.turns:
            turn['discarded'] = True
        self.buffer.clear()
        self.commit(None)

    async def send_loop(self):
        resampler = None
        async def send_audio(data, last=False):
            output = resampler.resample_chunk(np.frombuffer(data, dtype='<i2'), last=last)
            if output.size:
                await self.socket.send(json.dumps({'type': 'input_audio_buffer.append',
                    'audio': base64.b64encode(output.astype('<i2', copy=False).tobytes()).decode()}))
        while True:
            kind, value = await self.queue.get()
            try:
                if kind == 'begin':
                    resampler = soxr.ResampleStream(16000, 24000, 1, dtype='int16', quality='HQ')
                elif kind == 'audio':
                    await send_audio(value)
                else:
                    await send_audio(b'', last=True)
                    await self.socket.send(json.dumps({'type': 'input_audio_buffer.commit'}))
            finally:
                self.queue.task_done()

    async def receive_loop(self):
        async for message in self.socket:
            event = json.loads(message)
            kind = event.get('type', '')
            if kind == 'error':
                raise RuntimeError('OpenAI transcription error: ' + event.get('error', {}).get('code', 'unknown'))
            if kind not in ('input_audio_buffer.committed',
                            'conversation.item.input_audio_transcription.delta',
                            'conversation.item.input_audio_transcription.completed',
                            'conversation.item.input_audio_transcription.failed'):
                continue
            ident = event['item_id']
            if ident not in self.items:
                if not self.unassigned:
                    raise RuntimeError('Unexpected transcription item; cannot match it to recorded audio.')
                self.items[ident] = self.unassigned.popleft()
            turn = self.items[ident]
            if kind.endswith('.delta') and not turn['discarded']:
                emit({'status': 'transcript_partial', 'item_id': ident, 'track_id': turn['track_id'],
                      'delta': event['delta'], 'elapsed_ms': round((time.monotonic()-turn['started'])*1000)})
            elif kind.endswith('.completed'):
                if not turn['discarded']:
                    emit({'status': 'transcript_final', 'item_id': ident, 'track_id': turn['track_id'],
                          'wav': turn['wav'], 'text': event['transcript'], 'doa_deg': turn['doa_deg'], 'utterance_id': turn['utterance_id'],
                          'final_after_commit_ms': round((time.monotonic()-turn.get('ended', turn['started']))*1000)})
                if not turn['done'].done():
                    turn['done'].set_result(None)
                self.turns.remove(turn)
                self.completed.append(ident)
                if len(self.completed) > 128:
                    del self.items[self.completed.popleft()]
            elif kind.endswith('.failed'):
                raise RuntimeError('OpenAI failed to transcribe an utterance: ' + event.get('error', {}).get('code', 'unknown'))

    async def close(self):
        try:
            self.check()
            await asyncio.wait_for(self.queue.join(), 10)
            if self.turns:
                await asyncio.wait_for(asyncio.gather(*(t['done'] for t in self.turns)), 15)
        except (RuntimeError, asyncio.TimeoutError):
            emit({'status': 'transcription_incomplete', 'message': 'Some final transcripts were unavailable; local WAV files remain.'})
        finally:
            self.sender.cancel()
            self.receiver.cancel()
            await asyncio.gather(self.sender, self.receiver, return_exceptions=True)
            await self.socket.close()
