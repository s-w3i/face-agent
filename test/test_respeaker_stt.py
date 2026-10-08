import asyncio
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
import soxr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from respeaker_stt import Transcriber, session_config


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def recv(self):
        return json.dumps(await self.incoming.get())

    async def close(self):
        self.closed = True


class TranscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def event(self, socket, kind, item_id, **fields):
        socket.incoming.put_nowait({'type': kind, 'item_id': item_id, **fields})
        await asyncio.sleep(0)

    async def test_startup_retries_transport_timeout_then_configures_session(self):
        socket = Socket()
        socket.incoming.put_nowait({'type': 'session.created'})
        socket.incoming.put_nowait({'type': 'session.updated'})
        with patch('respeaker_stt.connect', AsyncMock(side_effect=[TimeoutError(), socket])) as connect, \
                patch('respeaker_stt.load_key', return_value='test-key'), \
                patch('respeaker_stt.asyncio.sleep', AsyncMock()), patch('respeaker_stt.emit') as emit:
            transcriber = await Transcriber.open()
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(socket.sent[0], session_config('gpt-live-transcribe', 'low', None))
            self.assertEqual(emit.call_args_list[0].args[0]['status'], 'transcription_connect_retry')
            await transcriber.close()

    async def test_stream_keeps_opening_and_flushes_resampling_tail(self):
        socket = Socket()
        transcriber = Transcriber(socket)
        opening = np.full(6400, 1000, dtype='<i2')
        speech = np.full(1897, 2000, dtype='<i2')
        try:
            with patch('respeaker_stt.emit'):
                transcriber.begin(7)
                transcriber.append(opening.tobytes())
                for chunk in np.array_split(speech, 20):
                    transcriber.append(chunk.tobytes())
                transcriber.commit('/tmp/first.wav')
                await transcriber.queue.join()
                sent = np.frombuffer(b''.join(base64.b64decode(e['audio']) for e in socket.sent
                                             if e['type'] == 'input_audio_buffer.append'), dtype='<i2')
                expected = soxr.resample(np.concatenate((opening, speech)), 16000, 24000)
                self.assertEqual(len(sent), len(expected))
                # Integer conversion dithers independently in the two resamplers.
                np.testing.assert_allclose(sent, expected, atol=2)
                self.assertEqual(socket.sent[-1]['type'], 'input_audio_buffer.commit')
                await self.event(socket, 'conversation.item.input_audio_transcription.completed',
                                 'one', transcript='My name is John')
                await transcriber.close()
                self.assertTrue(socket.closed)
        finally:
            transcriber.sender.cancel()
            transcriber.receiver.cancel()
            await asyncio.gather(transcriber.sender, transcriber.receiver, return_exceptions=True)

    async def test_partial_before_commit_and_out_of_order_final_association(self):
        socket = Socket()
        transcriber = Transcriber(socket)
        try:
            with patch('respeaker_stt.emit') as emit:
                transcriber.begin(11, 359, 1)
                transcriber.append(bytes(12800))
                await transcriber.queue.join()
                await self.event(socket, 'conversation.item.input_audio_transcription.delta', 'one', delta='My ')
                self.assertIsNone(transcriber.items['one']['wav'])
                transcriber.commit('/tmp/one.wav', 359)
                transcriber.begin(22, 30, 2)
                transcriber.append(bytes(12800))
                transcriber.commit('/tmp/two.wav', 30)
                await transcriber.queue.join()
                await self.event(socket, 'input_audio_buffer.committed', 'one')
                await self.event(socket, 'input_audio_buffer.committed', 'two')
                await self.event(socket, 'conversation.item.input_audio_transcription.completed', 'two', transcript='Second')
                await self.event(socket, 'conversation.item.input_audio_transcription.completed', 'one', transcript='First')
                events = [call.args[0] for call in emit.call_args_list]
                self.assertEqual(events[0]['status'], 'transcript_partial')
                self.assertEqual([(e['track_id'], e['wav'], e['text']) for e in events[1:]],
                                 [(22, '/tmp/two.wav', 'Second'), (11, '/tmp/one.wav', 'First')])
                self.assertEqual([(e['doa_deg'], e['utterance_id']) for e in events[1:]], [(30, 2), (359, 1)])
                self.assertFalse(transcriber.turns)
                await transcriber.close()
        finally:
            transcriber.sender.cancel()
            transcriber.receiver.cancel()
            await asyncio.gather(transcriber.sender, transcriber.receiver, return_exceptions=True)

    async def test_failure_stops_upload_without_exposing_server_error_text(self):
        socket = Socket()
        transcriber = Transcriber(socket)
        socket.incoming.put_nowait({'type': 'error', 'error': {'code': 'rate_limit_exceeded',
                                                             'message': 'Sensitive request data'}})
        with self.assertRaisesRegex(RuntimeError, 'rate_limit_exceeded'):
            await transcriber.receiver
        with self.assertRaisesRegex(RuntimeError, 'local audio was retained'):
            transcriber.append(bytes(1280))
        with patch('respeaker_stt.emit') as emit:
            await transcriber.close()
            self.assertEqual(emit.call_args.args[0]['status'], 'transcription_incomplete')
        self.assertTrue(socket.closed)

    async def test_playback_pause_discards_pending_transcripts_and_resumes_fresh_turn(self):
        socket = Socket()
        transcriber = Transcriber(socket)
        try:
            with patch('respeaker_stt.emit') as emit:
                transcriber.begin(1, 20)
                transcriber.append(bytes(12800))
                transcriber.discard()
                await transcriber.queue.join()
                await self.event(socket, 'input_audio_buffer.committed', 'old')
                await self.event(socket, 'conversation.item.input_audio_transcription.delta', 'old', delta='Robot echo')
                await self.event(socket, 'conversation.item.input_audio_transcription.completed', 'old', transcript='Robot echo')
                emit.assert_not_called()
                transcriber.begin(2, 30)
                transcriber.append(bytes(12800))
                transcriber.commit('/tmp/user.wav', 31)
                await transcriber.queue.join()
                await self.event(socket, 'conversation.item.input_audio_transcription.completed', 'new', transcript='My name is John')
                self.assertEqual(emit.call_args.args[0]['text'], 'My name is John')
                self.assertEqual(emit.call_args.args[0]['doa_deg'], 31)
                await transcriber.close()
        finally:
            transcriber.sender.cancel()
            transcriber.receiver.cancel()
            await asyncio.gather(transcriber.sender, transcriber.receiver, return_exceptions=True)

    def test_configuration_uses_client_endpointing_and_language_hints(self):
        config = session_config('gpt-live-transcribe', 'low', 'en')['session']['audio']['input']
        self.assertEqual(config['format'], {'type': 'audio/pcm', 'rate': 24000})
        self.assertIsNone(config['turn_detection'])
        self.assertEqual(config['transcription'], {'model': 'gpt-live-transcribe', 'delay': 'low', 'languages': ['en']})
        self.assertNotIn('delay', session_config('gpt-transcribe', 'low', None)['session']['audio']['input']['transcription'])
