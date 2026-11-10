"""Complete local pickup audio: no real cloud, browser, microphone or robot."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
import wave
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from serve import RobotServer
from dotsctl import request
ROOT=Path(__file__).resolve().parents[1]

class CachedPickSpeech(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'XDG_CACHE_HOME':self.temp.name});self.env.start()
        self.config=self.root/'config.json'
        self.value=json.loads((ROOT/'robot-config.default.json').read_text())
        self.value['appearance']=dict(name='Kuro',presetId='',signatureAvailable=False,state=base64.b64encode(b'ORBAST1\0test').decode())
        self.config.write_text(json.dumps(self.value))
        audio=io.BytesIO()
        with wave.open(audio,'wb') as output:
            output.setparams((1,2,24000,0,'NONE','not compressed'));output.writeframes(b'\0\0'*480)
        self.audio=audio.getvalue()
        self.server=RobotServer(('127.0.0.1',0),config_file=self.config,static_root=self.root/'web',key_file=self.root/'private/key')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.thread.join(2);self.server.server_close();self.env.stop();self.temp.cleanup()

    def test_first_use_is_complete_wav_and_repeat_never_streams_or_generates(self):
        self.server.apply_robot_status(dict(status='SPEAKING',revision=1,source='test'))
        previous=self.server.command['sequence']
        def generate(value):
            self.assertEqual(self.server.command['sequence'],previous)
            self.assertEqual(value['text'],'I will pick up the can for you.')
            return self.audio
        with patch.object(self.server,'speech',side_effect=generate) as generate,patch('serve.Handler.log_message'):
            command=request(self.url,'pick-announcement',dict(text='I will pick up the can for you.',statusRevision=1))
            self.assertEqual(generate.call_count,1)
        with patch.object(self.server,'speech_events',side_effect=AssertionError('No streaming')),patch.object(self.server,'speech',side_effect=AssertionError('No generation')),patch('serve.Handler.log_message'):
            with urlopen(self.url+command['speech']) as response:
                self.assertEqual(response.headers.get_content_type(),'audio/wav');self.assertEqual(response.read(),self.audio)
            request(self.url,'pick-announcement',dict(text='I will pick up the can for you.',statusRevision=1))
            with self.assertRaises(ValueError):request(self.url,'pick-announcement',dict(text='I will pick up the can for you.',statusRevision=999))

    def test_preparation_is_silent_persists_and_voice_or_model_invalidates(self):
        with patch.object(self.server,'speech',return_value=self.audio) as generate,patch('serve.Handler.log_message'):
            previous=dict(self.server.command)
            request(self.url,'pick-announcement',dict(text='I am picking it up now.',prepare=True))
            self.assertEqual(self.server.command,previous);self.assertEqual(generate.call_count,1)
            other=RobotServer(('127.0.0.1',0),config_file=self.config,static_root=self.root/'web',key_file=self.root/'private/key')
            try:
                with patch.object(other,'speech',side_effect=AssertionError('Cache must survive restart')):
                    self.assertEqual(other.pick_announcement('I am picking it up now.'),self.audio)
            finally:other.server_close()
            self.value['settings']['ttsVoice']='cedar';self.config.write_text(json.dumps(self.value))
            self.server.pick_announcement('I am picking it up now.');self.assertEqual(generate.call_count,2)
            with patch('serve.SPEECH_MODEL','different-model'):
                self.server.pick_announcement('I am picking it up now.');self.assertEqual(generate.call_count,3)

    def test_invalid_or_failed_generation_never_publishes_playback(self):
        previous=dict(self.server.command)
        with patch.object(self.server,'speech',side_effect=ValueError('synthetic speech failure')),patch('serve.Handler.log_message'):
            for value in ({'text':''},{'text':'x','stream':True},{'text':'x','prepare':'yes'},{'text':'I will drop the can here.'}):
                with self.assertRaises(ValueError):request(self.url,'pick-announcement',value)
                self.assertEqual(self.server.command,previous)
                self.assertIsNone(self.server.audio)
if __name__=='__main__':unittest.main()
