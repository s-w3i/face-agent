"""Check streaming audio, turn isolation and failures without API calls."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import socket as sockets
from unittest.mock import AsyncMock, patch
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from respeaker_realtime import RealtimeTranscriber, session_config
from voice_errors import RealtimeUnavailable


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False
    async def send(self, message):
        self.sent.append(json.loads(message))
    async def recv(self):
        return json.dumps(await self.messages.get())
    def __aiter__(self):
        return self
    async def __anext__(self):
        return json.dumps(await self.messages.get())
    async def close(self):
        self.closed = True


class RealtimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_connection_prefers_ipv4_retries_and_has_auto_family_fallback(self):
        socket = Socket(); events = []
        await socket.messages.put(dict(type='session.updated'))
        with patch('respeaker_realtime.load_key', return_value='test-key'), \
                patch('respeaker_realtime.connect', new_callable=AsyncMock,
                      side_effect=[TimeoutError(), OSError('temporary'), socket]) as connect, \
                patch('respeaker_realtime.asyncio.sleep', new_callable=AsyncMock) as sleep:
            transcriber = await RealtimeTranscriber.open(events.append)
            await transcriber.close()
        self.assertEqual([call.kwargs['family'] for call in connect.call_args_list], [sockets.AF_INET, sockets.AF_INET, sockets.AF_UNSPEC])
        self.assertTrue(all(call.kwargs['happy_eyeballs_delay'] == .25 for call in connect.call_args_list))
        self.assertEqual([event['attempt'] for event in events], [1, 2])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertTrue(socket.closed)

    async def test_network_retry_limit_reports_remote_failure_without_private_details(self):
        with patch('respeaker_realtime.load_key', return_value='test-key'), \
                patch('respeaker_realtime.connect', new_callable=AsyncMock,
                      side_effect=TimeoutError('private connection details')) as connect, \
                patch('respeaker_realtime.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(RealtimeUnavailable, '3 connection attempts') as error:
                await RealtimeTranscriber.open(lambda value: None)
        self.assertEqual(connect.await_count, 3)
        self.assertNotIn('private connection details', str(error.exception))

    async def test_configuration_rejection_is_not_retried_and_closes_socket(self):
        socket = Socket()
        await socket.messages.put(dict(type='error', error=dict(code='invalid_api_key', message='private-key')))
        with patch('respeaker_realtime.load_key', return_value='test-key'), \
                patch('respeaker_realtime.connect', new_callable=AsyncMock, return_value=socket) as connect:
            with self.assertRaisesRegex(RealtimeUnavailable, 'invalid_api_key') as error:
                await RealtimeTranscriber.open(lambda value: None)
        self.assertEqual(connect.await_count, 1)
        self.assertTrue(socket.closed)
        self.assertNotIn('private-key', str(error.exception))

    async def test_cancel_during_configuration_closes_socket_without_retry(self):
        socket = Socket()
        with patch('respeaker_realtime.load_key', return_value='test-key'), \
                patch('respeaker_realtime.connect', new_callable=AsyncMock, return_value=socket) as connect:
            opening = asyncio.create_task(RealtimeTranscriber.open(lambda value: None))
            while not socket.sent:
                await asyncio.sleep(0)
            opening.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening
        self.assertTrue(socket.closed)
        self.assertEqual(connect.await_count, 1)

    async def test_manual_endpoint_resamples_all_audio_and_matches_partial_turns(self):
        socket = Socket(); events = []
        transcriber = RealtimeTranscriber(socket, events.append)
        try:
            config = session_config()['session']['audio']['input']
            self.assertIsNone(config['turn_detection'])
            self.assertEqual(config['format']['rate'], 24000)
            self.assertEqual(config['transcription']['model'], 'gpt-live-transcribe')
            transcriber.begin(7)
            audio = (np.sin(np.arange(16000) * .05) * 15000).astype('<i2')
            for offset in range(0, len(audio), 320):
                transcriber.append(audio[offset:offset + 320].tobytes())
            finish = asyncio.create_task(transcriber.finish())
            await asyncio.sleep(.01)
            await transcriber.queue.join()
            self.assertEqual(socket.sent[-1]['type'], 'input_audio_buffer.commit')
            pcm = b''.join(base64.b64decode(item['audio']) for item in socket.sent if item['type'] == 'input_audio_buffer.append')
            self.assertEqual(len(pcm), 24000 * 2)
            for event in (
                dict(type='input_audio_buffer.committed', item_id='turn-7'),
                dict(type='conversation.item.input_audio_transcription.delta', item_id='old', delta='wrong'),
                dict(type='conversation.item.input_audio_transcription.delta', item_id='turn-7', delta='Hi '),
                dict(type='conversation.item.input_audio_transcription.delta', item_id='turn-7', delta='Kuro'),
                dict(type='conversation.item.input_audio_transcription.completed', item_id='turn-7', transcript='Hi Kuro')):
                await socket.messages.put(event)
            result = await finish
            self.assertEqual(result['text'], 'Hi Kuro')
            self.assertEqual([event['delta'] for event in events], ['Hi ', 'Kuro'])
            self.assertTrue(all(event['utterance_id'] == 7 for event in events))
        finally:
            await transcriber.close()
        self.assertTrue(socket.closed)

    async def test_final_transcript_timeout_is_recoverable(self):
        socket = Socket(); transcriber = RealtimeTranscriber(socket, lambda event: None)
        try:
            transcriber.begin(1)
            transcriber.append(bytes(320 * 2))
            with patch('respeaker_realtime.asyncio.wait_for', new_callable=AsyncMock, side_effect=TimeoutError()):
                with self.assertRaisesRegex(RealtimeUnavailable, 'final text'):
                    await transcriber.finish()
            self.assertIsNone(transcriber.turn)
        finally:
            await transcriber.close()

    async def test_remote_error_fails_pending_turn_and_closes_socket(self):
        socket = Socket(); transcriber = RealtimeTranscriber(socket, lambda event: None)
        try:
            transcriber.begin(1)
            transcriber.append(bytes(320 * 2))
            finish = asyncio.create_task(transcriber.finish())
            await asyncio.sleep(.01)
            await socket.messages.put(dict(type='error', error=dict(code='rate_limit_exceeded', message='private upstream data')))
            with self.assertRaisesRegex(RuntimeError, 'rate_limit_exceeded') as error:
                await finish
            self.assertNotIn('private upstream data', str(error.exception))
        finally:
            await transcriber.close()
        self.assertTrue(socket.closed)
