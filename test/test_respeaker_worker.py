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
    fail_finish = None
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
        if FakeRealtime.fail_finish:
            raise FakeRealtime.fail_finish
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
                  stt_prompt=None, offline_stt='off', wake_name='Kuro', wake_verify_floor=-5.5)
    return SimpleNamespace(**{**values, **changes})


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeRealtime.fail = None; FakeRealtime.fail_finish = None; FakeRealtime.instances = []; FakeRealtime.finish_gate = None
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
        await self.until(lambda: FakeRealtime.instances[0].begun)  # A remote turn is open to be dropped.
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

    async def test_rolling_engine_wakes_from_continuous_noise_without_vad_segments(self):
        scores = iter([-6.0, -5.0])
        names = []
        def scorer(name):
            names.append(name)
            return lambda audio: next(scores, -1.0)  # The phrase, after two windows of noise
        with patch('rolling_wake.HailoPhraseScorer', side_effect=scorer):
            self.speech = True  # TV on: never 0.4 s of silence, which stalled the Whisper path
            await self.start(wake_engine='rolling', wake_threshold=-3.6)
            await self.control(1, paused=False)
            self.state.resume_at = 0
            await self.until(lambda: self.status('wake_detected'), timeout=5)
        hit = self.status('wake_detected')[0]
        self.assertEqual((hit['model'], hit['score'], hit['mode']), ('rolling', -1.0, 'sleep'))
        self.assertLess(hit['t'], hit['continue_from'])  # A follow-on capture starts at the phrase.
        self.assertTrue(hit['continued'])
        self.assertEqual(names, ['Kuro'])
        self.assertEqual(self.status('listening')[0]['wake_engine'], 'rolling')
        self.dev.is_voice.assert_not_called()  # No hardware-VAD segments while asleep.
        self.assertEqual(self.status('utterance_started'), [])

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

    async def test_nothing_records_before_the_hardware_trigger(self):
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        await asyncio.sleep(.2)
        self.assertEqual(self.status('utterance_started'), [])
        self.assertTrue(self.dev.is_voice.called)
        self.assertEqual(self.recorder_bus().subscribers, ())

    async def test_recording_starts_with_preroll(self):
        PacedStream.period = .02
        await self.start(initial_mode='awake', preroll_seconds=.1)
        await self.control(1, paused=False)
        self.state.resume_at = 0
        PacedStream.value = 5
        await asyncio.sleep(.3)
        PacedStream.value = 9
        await asyncio.sleep(.04)
        await self.speak_once()
        await self.until(lambda: self.status('utterance_saved'))
        audio = np.frombuffer(bytes(FakeRealtime.instances[0].audio), '<i2')
        self.assertEqual(int(audio[0]), 5)  # Audio from before the trigger.
        self.assertEqual(int(audio[-1]), 9)

    async def test_continuous_speech_stops_at_the_maximum_length(self):
        await self.start(initial_mode='awake', max_seconds=.5)
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_saved'))
        self.voice = False
        saved = self.status('utterance_saved')[0]
        self.assertEqual(saved['reason'], 'maximum_duration')
        self.assertAlmostEqual(saved['duration_seconds'], .5, delta=.03)

    # ---- phase 5: fault injection -------------------------------------------------

    async def test_stalled_stream_reports_reconnectable_device_error(self):
        class Silent(PacedStream):
            def run(self):
                pass  # Never delivers a frame (USB audio stopped).
        def stalled(bus, device, rate, block):
            producer = ab.AudioProducer(bus, device, rate, block, stream_factory=Silent, stall_seconds=.1)
            producer.REOPEN_DELAYS = (.01, .01, .01)
            return producer
        with patch('respeaker.AudioProducer', side_effect=stalled):
            task = asyncio.create_task(r.serve(arguments(self.directory.name), self.dev))
            with self.assertRaises(r.DeviceUnavailable):
                await asyncio.wait_for(task, 5)

    def wake_model(self, failures):
        state = dict(calls=0)
        def predict(frame):
            state['calls'] += 1
            if state['calls'] in failures:
                raise RuntimeError('onnxruntime failure')
            return {'hi_kuro': .9 if state['calls'] % 40 in (10, 11) else 0.0}
        model = Mock()
        model.predict.side_effect = predict
        return model

    async def test_wake_model_error_restarts_the_thread_and_detection_continues(self):
        with patch('wake_word.load_model', return_value=self.wake_model({3})):
            await self.start(wake_engine='oww')
            await self.control(1, paused=False)
            self.state.resume_at = 0
            await self.until(lambda: self.status('wake_detected'), timeout=5)
        self.assertTrue(any('Wake-word thread failed' in e.get('message', '') for e in self.status('warning')))
        self.assertFalse(self.task.done())

    async def test_repeated_wake_model_errors_become_a_wake_engine_failure(self):
        with patch('wake_word.load_model', return_value=self.wake_model(set(range(1, 1000)))):
            task = asyncio.create_task(r.serve(arguments(self.directory.name, wake_engine='oww'), self.dev))
            with self.assertRaises(r.WorkerFailure) as raised:
                await asyncio.wait_for(task, 5)
        self.assertEqual(raised.exception.category, 'wake_engine')
        self.assertEqual(len([e for e in self.status('warning') if 'restarting it' in e['message']]), 3)

    async def test_recorder_error_mid_utterance_discards_and_recorder_restarts(self):
        test = self
        class GlitchyDev:
            calls = 0
            def is_voice(self):
                return test.voice
            @property
            def direction(self):
                GlitchyDev.calls += 1
                if GlitchyDev.calls == 2:
                    raise ValueError('pyusb glitch')
                return 30
        self.dev = GlitchyDev()
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_discarded'))
        self.assertEqual(self.status('utterance_discarded')[0]['reason'], 'error')
        self.voice = False
        await asyncio.sleep(.1)
        self.voice = True  # The restarted recorder takes the next utterance.
        await self.until(lambda: len(self.status('utterance_started')) == 2)
        self.voice = False
        self.speech = False
        await self.until(lambda: self.status('transcript_final'))
        self.assertTrue(any('Command recorder failed' in e['message'] for e in self.status('warning')))

    async def test_overflow_during_utterance_discards_it_and_keeps_running(self):
        await self.start(initial_mode='awake')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        recorder = next(t for t in threading.enumerate() if t.name == 'command-recorder')
        original = recorder.producer.bus.publish
        def lossy(pcm, t, seq, overflow=False):
            original(pcm, t, seq, overflow or seq % 25 == 0)
        recorder.producer.bus.publish = lossy
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_discarded'))
        discarded = self.status('utterance_discarded')[0]
        self.assertEqual(discarded['reason'], 'overflow')
        self.assertEqual(self.status('transcript_final'), [])
        self.assertFalse(self.task.done())

    async def test_stuck_recorder_is_escalated_with_its_stack(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def hang():
            release.wait(5)  # pyusb control transfer that never returns
            return False
        self.dev.is_voice.side_effect = hang
        from worker_threads import Heartbeat as RealHeartbeat
        with patch('respeaker.Heartbeat', side_effect=lambda name, limit: RealHeartbeat(name, .3)):
            task = asyncio.create_task(r.serve(arguments(self.directory.name, initial_mode='awake'), self.dev))
            await self.until(lambda: self.status('listening'))
            await self.state.apply(dict(paused=False, sequence=1))
            self.state.resume_at = 0
            with self.assertRaises(r.WorkerFailure) as raised:
                await asyncio.wait_for(task, 5)
        self.assertEqual(raised.exception.category, 'stalled')
        warning = next(e for e in self.status('warning') if 'stuck' in e['message'])
        self.assertIn('hang', warning['stack'])

    async def test_whole_process_pause_is_not_reported_as_a_stuck_thread(self):
        from worker_threads import Heartbeat as RealHeartbeat
        with patch('respeaker.Heartbeat', side_effect=lambda name, limit: RealHeartbeat(name, .3)):
            await self.start(initial_mode='awake')
            await self.control(1, paused=False)
            self.state.resume_at = 0
            await asyncio.sleep(.1)
            recorder = next(t for t in threading.enumerate() if t.name == 'command-recorder')
            with recorder.dev.lock:  # The recorder blocks in is_voice()...
                time.sleep(1.2)      # ...while the event loop is blocked too: like SIGSTOP.
            await asyncio.sleep(.5)
        self.assertFalse(self.task.done())
        self.assertFalse([e for e in self.status('warning') if 'stuck' in e['message']])

    async def test_metrics_are_emitted_periodically(self):
        with patch('respeaker.METRICS_SECONDS', .2):
            await self.start(initial_mode='awake')
            await self.until(lambda: self.status('metrics'), timeout=3)
        metrics = self.status('metrics')[0]
        for key in ('frames', 'overflows', 'reopen_count', 'subscriptions', 'thread_cpu_s', 'rss_kb', 'threads'):
            self.assertIn(key, metrics)
        self.assertIn('command-recorder', metrics['thread_cpu_s'])

    async def test_shutdown_with_busy_threads_is_quick_and_leaves_nothing_running(self):
        model = Mock()
        model.predict.return_value = {'hi_kuro': 0.0}
        with patch('wake_word.load_model', return_value=model):
            await self.start(initial_mode='awake', wake_engine='oww')
            await self.control(1, paused=False)
            self.state.resume_at = 0
            self.speech = True
            self.voice = True
            await self.until(lambda: self.status('utterance_started'))
            started = time.monotonic()
            self.state.stopping = True
            await asyncio.wait_for(self.task, 3)
        self.assertLess(time.monotonic() - started, 3)
        names = {t.name for t in threading.enumerate() if t.is_alive()}
        self.assertFalse(names & {'command-recorder', 'wake-word', 'audio-watchdog'})
        self.assertFalse([e for e in self.status('warning') if 'did not stop' in e['message']])

    # ---- phase 7: offline STT fallback ------------------------------------------------

    def offline(self, gate=None):
        engine = Mock()
        def transcribe(path):
            if gate is not None:
                gate.wait(3)
            return dict(text='offline command', stt_seconds=.1)
        engine.transcribe.side_effect = transcribe
        loader = patch('respeaker.load_offline_stt', return_value=engine)
        loader.start()
        self.addCleanup(loader.stop)
        return engine

    async def speak_once(self):
        self.speech = True
        self.voice = True
        await self.until(lambda: self.status('utterance_started'))
        self.voice = False
        self.speech = False

    async def test_realtime_down_on_wake_uses_offline_stt_and_stays_awake(self):
        engine = self.offline()
        await self.start(offline_stt='cpu')
        FakeRealtime.fail = RealtimeUnavailable('network down', retryable=True)
        ack = await self.control(1, mode='awake')
        self.assertNotIn('error', ack)
        self.assertEqual(ack['active_mode'], 'awake')
        self.assertEqual(self.status('stt_fallback')[0]['engine'], 'cpu')
        await self.control(2, paused=False)
        self.state.resume_at = 0
        await self.speak_once()
        await self.until(lambda: self.status('transcript_final'))
        final = self.status('transcript_final')[0]
        self.assertEqual((final['text'], final['backend']), ('offline command', 'cpu'))
        self.assertTrue(final['paused_after'])
        engine.transcribe.assert_called_once()

    async def test_realtime_failure_mid_utterance_transcribes_the_saved_clip_locally(self):
        self.offline()
        await self.start(initial_mode='awake', offline_stt='cpu')
        await self.control(1, paused=False)
        self.state.resume_at = 0
        FakeRealtime.fail_finish = RealtimeUnavailable('socket closed', retryable=True)
        await self.speak_once()
        await self.until(lambda: self.status('transcript_final'))
        final = self.status('transcript_final')[0]
        self.assertEqual(final['backend'], 'cpu')
        self.assertTrue(Path(final['wav']).exists())
        self.assertTrue(self.status('stt_fallback'))
        self.assertTrue(FakeRealtime.instances[0].closed)

    async def test_non_retryable_failure_keeps_the_error_path(self):
        engine = self.offline()
        await self.start(offline_stt='cpu')
        FakeRealtime.fail = RealtimeUnavailable('invalid_api_key', retryable=False)
        ack = await self.control(1, mode='awake')
        self.assertEqual(ack['error']['category'], 'realtime_unavailable')
        self.assertEqual(ack['active_mode'], 'sleep')
        self.assertEqual(self.status('stt_fallback'), [])
        engine.transcribe.assert_not_called()

    async def test_offline_disabled_reports_the_failure_as_before(self):
        await self.start()
        FakeRealtime.fail = RealtimeUnavailable('network down', retryable=True)
        ack = await self.control(1, mode='awake')
        self.assertIn('error', ack)

    async def test_pause_during_offline_stt_suppresses_the_result(self):
        gate = threading.Event()
        self.offline(gate)
        await self.start(offline_stt='cpu')
        FakeRealtime.fail = RealtimeUnavailable('network down', retryable=True)
        await self.control(1, mode='awake')
        await self.control(2, paused=False)
        self.state.resume_at = 0
        await self.speak_once()
        await self.until(lambda: self.status('utterance_saved'))
        await self.control(3, paused=True)
        gate.set()
        await asyncio.sleep(.3)
        self.assertEqual(self.status('transcript_final'), [])

    async def test_realtime_comes_back_and_is_used_again(self):
        self.offline()
        with patch('respeaker.REALTIME_RETRY_DELAYS', (.1,)):
            await self.start(offline_stt='cpu')
            FakeRealtime.fail = RealtimeUnavailable('network down', retryable=True)
            await self.control(1, mode='awake')
            FakeRealtime.fail = None  # The network is back.
            await self.until(lambda: self.status('stt_restored'))
            await self.control(2, paused=False)
            self.state.resume_at = 0
            await self.speak_once()
            await self.until(lambda: self.status('transcript_final'))
        self.assertEqual(self.status('transcript_final')[0]['backend'], 'realtime')

    async def test_offline_engine_loads_only_when_needed(self):
        with patch('respeaker.load_offline_stt') as loader:
            await self.start(initial_mode='awake', offline_stt='hailo')
            await asyncio.sleep(.1)
            loader.assert_not_called()

    async def test_offline_transcription_error_loses_the_utterance_not_the_worker(self):
        engine = self.offline()
        engine.transcribe.side_effect = RuntimeError('HAILO_RPC_FAILED')
        await self.start(offline_stt='cpu')
        FakeRealtime.fail = RealtimeUnavailable('network down', retryable=True)
        await self.control(1, mode='awake')
        await self.control(2, paused=False)
        self.state.resume_at = 0
        await self.speak_once()
        await self.until(lambda: any('Offline transcription failed' in e['message'] for e in self.status('warning')))
        self.assertEqual(self.status('transcript_final'), [])
        self.assertFalse(self.task.done())

    def test_hailo_load_failure_falls_back_to_cpu(self):
        args = SimpleNamespace(offline_stt='hailo', language='en', offline_stt_model=None)
        with patch.dict(sys.modules, {'hailo_stt': None}), patch('respeaker_stt.Transcriber') as cpu, \
                patch('respeaker.emit') as emit:
            engine = r.load_offline_stt(args)
        self.assertIs(engine, cpu.return_value)
        self.assertEqual(engine.backend_name, 'cpu')
        cpu.assert_called_once_with('base.en', 'en', cpu_threads=2, beam_size=1)
        self.assertIn('using the CPU', emit.call_args.args[0]['message'])

    def recorder_bus(self):
        recorder = next(t for t in threading.enumerate() if t.name == 'command-recorder')
        return recorder.producer.bus


if __name__ == '__main__':
    unittest.main()
