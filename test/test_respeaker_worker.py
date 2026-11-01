"""Long-lived worker (phase 3): mode controls, T3 recorder, cancel tokens, capture requests.

Runs a real AudioProducer on a fake PortAudio stream, a fake Realtime transcriber
and a scripted wake model: no audio hardware, no network, no OpenAI calls.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_bus as ab
import respeaker as r
import respeaker_realtime
from voice_errors import RealtimeUnavailable


class PacedStream:
    """sounddevice.InputStream stand-in: delivers 20 ms blocks of `value` every 2 ms."""

    value = 0
    period = .002  # 10x faster than real time; tests that check capture times use .02

    def __init__(self, *, callback, finished_callback, blocksize, **kwargs):
        self.callback = callback; self.blocksize = blocksize
        self.closed = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.closed.is_set():
            data = np.full((self.blocksize, 1), PacedStream.value, np.int16)
            self.callback(data, self.blocksize, None, SimpleNamespace(input_overflow=False))
            time.sleep(PacedStream.period)

    def close(self):
        self.closed.set()


def producer_factory(bus, device, rate, block):
    return ab.AudioProducer(bus, device, rate, block, stream_factory=PacedStream, stall_seconds=1)


class FakeRealtime:
    fail = None
    instances = []
    finish_gate = None

    def __init__(self):
        self.turn = None; self.audio = bytearray(); self.closed = False; self.begun = []

    @classmethod
    async def open(cls, emit, model, language):
        if cls.fail:
            raise cls.fail
        instance = cls()
        cls.instances.append(instance)
        return instance

    def begin(self, utterance_id):
        self.turn = utterance_id; self.begun.append(utterance_id)

    def append(self, pcm):
        self.audio.extend(pcm)

    async def finish(self):
        if FakeRealtime.finish_gate:
            await FakeRealtime.finish_gate.wait()
        self.turn = None
        return dict(text='what time is it')

    async def close(self):
        self.closed = True; self.turn = None


def arguments(directory, **changes):
    values = dict(device=None, wav=str(Path(directory) / 'voice.wav'), control_stdin=False, no_led=True,
                  no_stt=False, stt_model='gpt-live-transcribe', language='en', seconds=0, once=False,
                  silence_seconds=.1, sleep_silence_seconds=.1, max_seconds=10, vad_mode=2,
                  preroll_seconds=0, wake_engine='whisper', wake_model_path='hi_kuro.onnx', wake_threshold=.5,
                  wake_continuation=.16, wake_stt_model='small', initial_mode='sleep', single_utterance=True,
                  stt_prompt=None)
    return SimpleNamespace(**{**values, **changes})


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeRealtime.fail = None; FakeRealtime.instances = []; FakeRealtime.finish_gate = None
        PacedStream.value = 0; PacedStream.period = .002
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events = []
        self.voice = False
        self.state = r.Controls()
        self.dev = Mock(direction=30)
        self.dev.is_voice.side_effect = lambda: self.voice
        self.speech = False  # webrtcvad stand-in: voiced while True
        self.patches = [
            patch('respeaker.select_input', return_value=1),
            patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')),
            patch('respeaker.AudioProducer', side_effect=producer_factory),
            patch('respeaker.Controls', return_value=self.state),
            patch('respeaker.emit', side_effect=self.events.append),
            patch.object(respeaker_realtime, 'RealtimeTranscriber', FakeRealtime),
            patch('respeaker_stt.Transcriber'),
            patch('respeaker.webrtcvad.Vad', return_value=Mock(is_speech=lambda data, rate: self.speech)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def start(self, **changes):
        self.task = asyncio.create_task(r.serve(arguments(self.directory.name, **changes), self.dev))
        await self.until(lambda: self.status('listening'))

    async def asyncTearDown(self):
        if hasattr(self, 'task'):
            self.state.stopping = True
            await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 5)

    def status(self, name):
        return [event for event in self.events if event.get('status') == name]

    async def until(self, predicate, timeout=3):
        async with asyncio.timeout(timeout):
            while not predicate():
                if hasattr(self, 'task') and self.task.done():
                    self.task.result()
                await asyncio.sleep(.01)

    async def control(self, sequence, **value):
        await self.state.apply(dict(value, sequence=sequence))
        return self.status('microphone_control')[-1]

    async def test_mode_switch_opens_and_closes_realtime_in_one_process(self):
        await self.start()
        self.assertEqual(self.state.mode, 'sleep')
        ack = await self.control(1, mode='awake')
        self.assertEqual((ack['mode'], ack['active_mode']), ('awake', 'awake'))
        self.assertNotIn('error', ack)
        self.assertEqual(len(FakeRealtime.instances), 1)
        ack = await self.control(2, mode='sleep')
        self.assertEqual(ack['active_mode'], 'sleep')
        self.assertTrue(FakeRealtime.instances[0].closed)
        await self.control(3, mode='awake')
        self.assertEqual(len(FakeRealtime.instances), 2)
        import respeaker_stt
        respeaker_stt.Transcriber.assert_called_once()  # Whisper loaded once, kept across switches.

    async def test_realtime_failure_on_awake_keeps_sleep_mode_and_wake_thread(self):
        model = Mock()
        model.predict.return_value = {'hi_kuro': 0.0}
        with patch('wake_word.load_model', return_value=model):
            await self.start(wake_engine='oww')
            FakeRealtime.fail = RealtimeUnavailable('offline', retryable=True)
            ack = await self.control(1, mode='awake')
            self.assertEqual(ack['active_mode'], 'sleep')
            self.assertEqual(ack['error']['category'], 'realtime_unavailable')
            self.assertTrue(ack['error']['retryable'])
            self.assertFalse(self.task.done())
            self.assertTrue(any(t.name == 'wake-word' and t.is_alive() for t in threading.enumerate()))
            FakeRealtime.fail = None
            ack = await self.control(2, mode='awake')
            self.assertEqual(ack['active_mode'], 'awake')

    async def test_awake_hardware_vad_capture_streams_to_realtime_and_pauses_after(self):
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_started'))
        self.voice = False
        self.speech = False
        await self.until(lambda: self.status('transcript_final'))
        final = self.status('transcript_final')[0]
        self.assertEqual((final['text'], final['backend']), ('what time is it', 'realtime'))
        self.assertTrue(final['paused_after'])
        self.assertTrue(self.state.paused)
        self.assertGreater(len(FakeRealtime.instances[0].audio), 0)

    async def test_pause_mid_recording_discards_without_transcript(self):
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_started'))
        ack = await self.control(2, paused=True)
        self.assertTrue(ack['paused'])
        self.assertFalse(self.state.capturing)  # The ack waited for the recorder to let go.
        await self.until(lambda: self.status('utterance_discarded'))
        await asyncio.sleep(.2)
        self.assertEqual(self.status('transcript_final'), [])
        self.assertTrue(FakeRealtime.instances[0].closed)  # Unfinished remote audio is dropped.

    async def test_cancel_during_finish_suppresses_late_transcript(self):
        FakeRealtime.finish_gate = asyncio.Event()
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_saved'))
        self.voice = False
        token = self.state.scope.current
        await self.control(2, paused=True)
        self.assertTrue(token.cancelled)
        FakeRealtime.finish_gate.set()
        await asyncio.sleep(.2)
        self.assertEqual(self.status('transcript_final'), [])

    async def test_capture_request_starts_at_ring_offset(self):
        PacedStream.period = .02  # Real time, so capture timestamps match sample counts.
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        PacedStream.value = 5
        await asyncio.sleep(.4)
        bus = self.recorder_bus()
        from_t = bus.ring.t_end - .1
        expected, t_start, _ = bus.ring.read_since(from_t)
        PacedStream.value = 9
        self.speech = False
        ack = await self.control(2, capture='start', from_t=from_t)
        self.assertTrue(ack['accepted'])
        await self.until(lambda: self.status('utterance_saved'))
        started = self.status('utterance_started')[0]
        self.assertEqual(started['trigger'], 'capture_request')
        audio = np.frombuffer(bytes(FakeRealtime.instances[0].audio), '<i2')
        head = len(expected) // 2 - 320  # One block of scheduling jitter
        self.assertGreater(head, 1000)
        self.assertTrue((audio[:head] == 5).all())   # Starts at from_t, inside the ring history...
        self.assertTrue((audio[-320:] == 9).all())   # ...and continues with live audio.

    async def test_capture_request_refused_while_paused_or_sleeping(self):
        await self.start()
        ack = await self.control(1, capture='start', from_t=time.monotonic())
        self.assertFalse(ack['accepted'])

    async def test_sleep_wake_hit_reports_continued_speech(self):
        scores = iter([0, 0, .9, .9])
        model = Mock()
        model.predict.side_effect = lambda frame: {'hi_kuro': next(scores, 0.0)}
        with patch('wake_word.load_model', return_value=model):
            self.speech = True  # The user keeps talking after "Hi Kuro".
            await self.start(wake_engine='oww')
            await self.control(1, paused=False)
            self.state.resume_at = 0
            await self.until(lambda: self.status('wake_detected'), timeout=5)
        hit = self.status('wake_detected')[0]
        self.assertTrue(hit['continued'])
        self.assertEqual(hit['mode'], 'sleep')
        self.dev.is_voice.assert_not_called()  # oww replaces the hardware-VAD trigger while asleep.

    async def test_plain_wake_hit_is_not_continued(self):
        scores = iter([0, 0, .9, .9])
        model = Mock()
        model.predict.side_effect = lambda frame: {'hi_kuro': next(scores, 0.0)}
        with patch('wake_word.load_model', return_value=model):
            self.speech = False  # Silence after the phrase.
            await self.start(wake_engine='oww')
            await self.control(1, paused=False)
            self.state.resume_at = 0
            await self.until(lambda: self.status('wake_detected'), timeout=5)
        self.assertFalse(self.status('wake_detected')[0]['continued'])

    async def test_comma_pause_does_not_end_the_continuation_check(self):
        bus = ab.AudioBus()
        bus.ring.write(np.zeros(16000, np.int16), 11.0)  # t=10 .. 11
        pattern = iter([False] * 12 + [True] * 20)  # 240 ms pause, then speech
        vad = Mock(is_speech=lambda frame, rate: next(pattern, True))
        with patch('respeaker.webrtcvad.Vad', return_value=vad):
            self.assertTrue(await r.speech_continues(bus, 10.0))
        quiet = Mock(is_speech=lambda frame, rate: False)
        with patch('respeaker.webrtcvad.Vad', return_value=quiet):
            self.assertFalse(await r.speech_continues(bus, 10.0))
        self.assertFalse(await r.speech_continues(bus, 10.0, min_voiced=0))

    async def test_capture_request_replaces_a_just_started_vad_recording(self):
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True  # The mic reopened while the user is still talking.
        await self.until(lambda: self.status('utterance_started'))
        self.voice = False
        ack = await self.control(2, capture='start', from_t=time.monotonic() - .5)
        self.assertTrue(ack['accepted'])
        await self.until(lambda: len(self.status('utterance_started')) == 2)
        self.assertEqual([e['trigger'] for e in self.status('utterance_started')], ['hardware_vad', 'capture_request'])
        self.assertEqual(len(self.status('utterance_discarded')), 1)
        self.speech = False
        await self.until(lambda: self.status('transcript_final'))
        self.assertEqual(len(self.status('transcript_final')), 1)

    def recorder_bus(self):
        recorder = next(t for t in threading.enumerate() if t.name == 'command-recorder')
        return recorder.producer.bus


if __name__ == '__main__':
    unittest.main()
