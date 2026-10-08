import base64
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import wave
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from serve import RobotServer
from dotsctl import execute
import serve


class RealtimeClip:
    def __init__(self, pcm, events=None):
        self.sent = []; self.closed = False; self.close_code = None
        self.output = events or [
            dict(type='response.output_audio.delta', delta=base64.b64encode(pcm[:100]).decode()),
            dict(type='response.output_audio.delta', delta=base64.b64encode(pcm[100:]).decode()),
            dict(type='response.done', response=dict(status='completed'))]
        self.events = iter(events or [dict(type='session.created'), dict(type='session.updated')])
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.closed = True; self.close_code = 1000
    def send(self, message):
        value = json.loads(message); self.sent.append(value)
        if value['type'] == 'response.create': self.events = iter(self.output)
    def recv(self, timeout): return json.dumps(next(self.events))


class RobotServiceCheck(unittest.TestCase):
    def test_private_credentials_and_openai_speech(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            root = Path(directory); web = root / 'web'; web.mkdir()
            key_file = root / 'private' / 'openai-api-key'
            server = RobotServer(('127.0.0.1', 0), config_file=root / 'robot-config.json', static_root=web, key_file=key_file)
            config = dict(version=1, renderer='original-dots', resourceRevision='a' * 64,
                          appearance=dict(name='Otto', presetId='', signatureAvailable=False, state=base64.b64encode(b'ORBAST1\0test').decode()),
                          settings=dict(quality=1, reducedMotion=False, autoIdle=True, idleInterval=12, motionStrength=1,
                                        speechSource='demo', speechLevel=.5, background='light', ttsVoice='cedar'))
            serve.atomic_save(server.config_file, config)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def api(route, value=None, headers=None):
                request = Request(base + '/api/' + route, data=json.dumps(value).encode() if value is not None else None,
                                  headers=headers or {'Content-Type': 'application/json'})
                with urlopen(request, timeout=3) as response:
                    data = response.read()
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    if response.headers.get_content_type() == 'application/x-ndjson':
                        return [json.loads(line) for line in data.splitlines()]
                    return data if response.headers.get_content_type() == 'audio/wav' else json.loads(data)
            dummy_key = 'sk-test-only-' + 'x' * 40
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed')); wav.writeframes(b'\0\0' * 240)
            audio = output.getvalue()
            try:
                self.assertFalse(api('voice')['configured'])
                with self.assertRaises(HTTPError): api('speech', {})
                self.assertFalse(key_file.exists())
                with self.assertRaises(HTTPError): api('key', {'apiKey': dummy_key}, {'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
                with self.assertRaises(HTTPError): api('key', {'apiKey': dummy_key}, {'Content-Type': 'text/plain'})
                with self.assertRaises(HTTPError): api('key', {'apiKey': dummy_key}, {'Content-Type': 'application/json', 'Host': 'untrusted.example'})
                with self.assertRaises(HTTPError): api('key', {'apiKey': 'invalid'})
                self.assertEqual(api('key', {'apiKey': dummy_key}), {'configured': True})
                self.assertEqual(key_file.read_text(), dummy_key)
                self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(key_file.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(list(key_file.parent.iterdir()), [key_file])
                self.assertTrue(api('voice')['configured'])
                # Another device can use the same character config without inheriting credentials.
                other = RobotServer(('127.0.0.1', 0), config_file=server.config_file, static_root=web,
                                    key_file=root / 'other-device' / 'openai-api-key')
                try:
                    self.assertEqual(other.api_key(), '')
                    other.save_key('sk-other-device-' + 'y' * 40)
                    self.assertNotEqual(other.api_key(), server.api_key())
                    self.assertEqual(server.api_key(), dummy_key)
                finally: other.server_close()
                for route in ('voice', 'config', 'status'):
                    self.assertNotIn(dummy_key, json.dumps(api(route)))
                bad = dict(config, apiKey=dummy_key)
                with self.assertRaises(HTTPError): api('config', bad)
                bad = json.loads(json.dumps(config)); bad['settings']['ttsVoice'] = 'unknown'
                with self.assertRaises(HTTPError): api('config', bad)
                self.assertEqual(json.loads(server.config_file.read_text()), config)
                connections = []
                def connect_clip(*args, **kwargs):
                    socket = RealtimeClip(audio[44:]); connections.append(socket); return socket
                with patch.object(serve, 'connect', side_effect=connect_clip) as upstream:
                    self.assertEqual(api('speech', {}), audio)
                    self.assertEqual(upstream.call_args.args[0], 'wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini')
                    self.assertEqual(upstream.call_args.kwargs['additional_headers']['Authorization'], 'Bearer ' + dummy_key)
                    self.assertEqual(api('voice')['model'], serve.SPEECH_MODEL)
                    self.assertNotIn('onyx', api('voice')['voices'])
                    session, response = connections[0].sent
                    self.assertEqual(session['session']['audio']['output'], dict(voice='cedar', format=dict(type='audio/pcm', rate=24000)))
                    self.assertEqual(response['response']['input'][0]['content'][0]['text'], 'Hi, I am Otto.')
                    self.assertEqual(response['response']['output_modalities'], ['audio'])
                    self.assertFalse(connections[0].closed)
                    command = api('say', {'text': '  Robot test  ', 'voice': 'marin'})
                    self.assertEqual(command['state'], 'speaking')
                    self.assertEqual(command['text'], 'Robot test')
                    self.assertEqual(api(command['speech'].removeprefix('/api/')), audio)
                    execute(base, 'say Hello robot')
                    self.assertEqual(api('status')['command']['text'], 'Hello robot')
                    next_clip = api('status')['command']['speech']
                    self.assertNotEqual(next_clip, command['speech'])
                    with self.assertRaises(HTTPError): api(command['speech'].removeprefix('/api/'))
                    self.assertNotIn(dummy_key, json.dumps(api('status')))
                    before = upstream.call_count
                    for payload in ({'text': ''}, {'text': 'x' * 4097}, {'voice': 'invalid'}, {'text': []}):
                        with self.assertRaises(HTTPError): api('speech', payload)
                    self.assertEqual(upstream.call_count, before)
                    # Same voice reuses the session; changing voice opened a new connection.
                    self.assertTrue(connections[0].closed)
                    first = api('speech-stream', {'text': 'Warm speech'})
                    self.assertTrue(first[0]['warm']); self.assertEqual(first[-1]['type'], 'done')
                    self.assertEqual(upstream.call_count, before)
                server.close_speech()
                # The first PCM reaches HTTP callers while the model is still generating.
                socket = RealtimeClip(audio[44:]); release = threading.Event(); waiting = threading.Event()
                original_recv = socket.recv
                def delayed_recv(timeout):
                    event = original_recv(timeout)
                    if json.loads(event)['type'] == 'response.done':
                        waiting.set(); release.wait(2)
                    return event
                socket.recv = delayed_recv
                with patch.object(serve, 'connect', return_value=socket):
                    with urlopen(Request(base + '/api/speech-stream', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=3) as response:
                        try:
                            first = json.loads(response.readline())
                            self.assertEqual(first['type'], 'audio')
                            self.assertTrue(waiting.wait(1)); self.assertTrue(server.speech_lock.locked())
                            self.assertFalse(first['warm'])
                        finally: release.set()
                        self.assertEqual(json.loads(response.readlines()[-1])['type'], 'done')
                server.close_speech()
                failure = serve.InvalidStatus(SimpleNamespace(status_code=401, body=dummy_key))
                with patch.object(serve, 'connect', side_effect=failure):
                    try: api('speech', {})
                    except HTTPError as error:
                        message = error.read().decode()
                        self.assertIn('rejected the API key', message); self.assertNotIn(dummy_key, message)
                    else: self.fail('An invalid upstream key should produce a safe error.')
                self.assertFalse(server.speech_lock.locked())
                for events in ([dict(type='error', error=dict(code='invalid_api_key', message=dummy_key))],
                               [dict(type='error', error=dict(code='model_not_found', message=dummy_key))],
                               [dict(type='response.done', response=dict(status='incomplete'))]):
                    socket = RealtimeClip(b'', events)
                    with patch.object(serve, 'connect', return_value=socket):
                        try: api('speech', {})
                        except HTTPError as error: self.assertNotIn(dummy_key, error.read().decode())
                        else: self.fail('Failed Realtime output must not play partial audio.')
                    self.assertTrue(socket.closed); self.assertFalse(server.speech_lock.locked())
                # Partial streams report a safe error and release the reusable session.
                socket = RealtimeClip(b'', [dict(type='response.output_audio.delta', delta=base64.b64encode(b'\0\0').decode()),
                    dict(type='error', error=dict(code='invalid_api_key', message=dummy_key))])
                with patch.object(serve, 'connect', return_value=socket):
                    partial = api('speech-stream', {})
                    self.assertEqual([event['type'] for event in partial], ['audio', 'error'])
                    self.assertNotIn(dummy_key, json.dumps(partial))
                    self.assertTrue(socket.closed); self.assertFalse(server.speech_lock.locked())
                legacy = json.loads(json.dumps(config)); legacy['settings']['ttsVoice'] = 'onyx'
                server.config_file.write_text(json.dumps(legacy))
                self.assertEqual(api('config')['settings']['ttsVoice'], 'onyx')
                with patch.object(serve, 'connect') as upstream:
                    with self.assertRaises(HTTPError): api('speech', {})
                    upstream.assert_not_called()
                api('config', config)
                api('actions', [dict(id='idle', label='Ready'), dict(id='sleeping', label='Sleeping'), dict(id='speaking', label='Speaking')])
                # A newer state stops a robot stream that is still being generated.
                socket = RealtimeClip(audio[44:]); release = threading.Event(); waiting = threading.Event()
                original_recv = socket.recv; chunks = 0
                def pause_second_chunk(timeout):
                    nonlocal chunks
                    event = original_recv(timeout)
                    if json.loads(event)['type'] == 'response.output_audio.delta':
                        chunks += 1
                        if chunks == 2: waiting.set(); release.wait(2)
                    return event
                socket.recv = pause_second_chunk
                with patch.object(serve, 'connect', return_value=socket):
                    command = api('say', {'stream': True})
                    self.assertEqual(command['text'], 'Hi, I am Otto.')
                    self.assertTrue(waiting.wait(1))
                    try: api('command', dict(state='idle'))
                    finally: release.set()
                    output = api(command['speech'].removeprefix('/api/'))
                    self.assertEqual([event['type'] for event in output], ['audio', 'error'])
                    self.assertTrue(socket.closed); self.assertFalse(server.speech_lock.locked())
                    self.assertEqual(api('status')['command']['state'], 'idle')
                def interrupted(*args, **kwargs):
                    api('command', dict(state='sleeping'))
                    return RealtimeClip(audio[44:])
                with patch.object(serve, 'connect', side_effect=interrupted):
                    with self.assertRaises(HTTPError): api('say', {})
                    self.assertEqual(api('status')['command']['state'], 'sleeping')
                with self.assertRaises(HTTPError): urlopen(base + '/../private/openai-api-key')
                with patch.dict(os.environ, {'OPENAI_API_KEY': dummy_key + '-environment'}):
                    self.assertEqual(server.api_key(), dummy_key + '-environment')
                    self.assertEqual(api('voice')['source'], 'environment')
            finally:
                server.shutdown(); server.server_close(); worker.join()

    def test_shared_file_commands_validation_and_live_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'robot-config.json'
            server = RobotServer(('127.0.0.1', 0), config_file=path, static_root=directory)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def api(route, value=None, origin=None):
                headers = {'Content-Type': 'application/json'}
                if origin: headers['Origin'] = origin
                with urlopen(Request(base + '/api/' + route, data=json.dumps(value).encode() if value is not None else None, headers=headers), timeout=3) as response:
                    self.assertEqual(response.headers['Cross-Origin-Embedder-Policy'], 'require-corp')
                    return json.load(response)
            config = dict(version=1, renderer='original-dots', resourceRevision='a' * 64,
                          appearance=dict(name='Test dot', presetId='', signatureAvailable=False,
                                          state=base64.b64encode(b'ORBAST1\0test').decode()),
                          settings=dict(quality=1, reducedMotion=False, autoIdle=True, idleInterval=12,
                                        motionStrength=1, speechSource='demo', speechLevel=.5, background='light'))
            stream = None
            try:
                api('config', config)
                self.assertEqual(api('config'), json.loads(path.read_text()))
                before = path.read_bytes()
                bad = json.loads(json.dumps(config)); bad['settings']['idleInterval'] = -1
                with self.assertRaises(HTTPError): api('config', bad)
                bad = json.loads(json.dumps(config)); bad['version'] = True
                with self.assertRaises(HTTPError): api('config', bad)
                with self.assertRaises(HTTPError): api('config', config, origin='https://untrusted.example')
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(Path(directory).glob('robot-config.json.*')), [])
                api('actions', [dict(id='idle', label='Idle / Ready'), dict(id='sleeping', label='Sleeping'), dict(id='speaking', label='Speaking'), dict(id='signature:blue_beret', label='Felipe signature')])
                stream = urlopen(base + '/api/events', timeout=3)
                def event(kind):
                    while True:
                        line = stream.readline().decode().strip()
                        if line == 'event: ' + kind:
                            return json.loads(stream.readline().decode().removeprefix('data: '))
                self.assertEqual(event('config'), config)
                self.assertEqual(event('command')['state'], 'idle')
                self.assertEqual(event('gaze')['sequence'], 0)
                execute(base, 'sleeping')
                self.assertEqual(event('command')['state'], 'sleeping')
                execute(base, 'signature:blue_beret')
                self.assertEqual(event('command')['state'], 'signature:blue_beret')
                execute(base, 'Felipe signature')
                self.assertEqual(event('command')['state'], 'signature:blue_beret')
                execute(base, 'speaking 0.8')
                self.assertEqual(event('command')['level'], .8)
                command = api('status')['command']
                api('ack', dict(sequence=command['sequence'], state='speaking', error=''))
                self.assertEqual(api('status')['acknowledgment']['state'], 'speaking')
                target = api('gaze', dict(detected=True, x=.2, y=.8))
                self.assertEqual(event('gaze')['x'], .2)
                self.assertEqual(target['y'], .8)
                self.assertEqual(api('status')['command'], command)
                self.assertEqual(api('status')['acknowledgment']['state'], 'speaking')
                for bad in (dict(detected=True, x=-1, y=.5), dict(detected=True, x=True, y=.5), dict(detected='yes'), dict(detected=True, x=.5, y=float('nan'))):
                    with self.assertRaises(HTTPError): api('gaze', bad)
                api('gaze', dict(detected=False)); self.assertFalse(event('gaze')['detected'])
                with self.assertRaises(HTTPError): api('command', dict(state='speaking', level=2))
                with self.assertRaises(HTTPError): api('command', dict(state='sleeping', level=.5))
                with self.assertRaises(HTTPError): api('command', dict(state='unknown'))
                self.assertEqual(api('status')['command'], command)
                config['settings']['background'] = 'dark'; api('config', config)
                self.assertEqual(event('config')['settings']['background'], 'dark')
                # Direct edits to the same file are picked up without a browser reload.
                config['settings']['background'] = 'light'; path.write_text(json.dumps(config))
                self.assertEqual(event('config')['settings']['background'], 'light')
                self.assertEqual(path.read_bytes(), json.dumps(config).encode())
            finally:
                if stream: stream.close()
                server.shutdown(); server.server_close(); server_thread.join()


if __name__ == '__main__':
    unittest.main()
