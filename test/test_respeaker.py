"""Check hardware-triggered capture, audio endpoints and playback controls offline."""
import asyncio
from contextlib import redirect_stderr
import io
import math
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_bus as ab
import respeaker as r


def arguments(path, **changes):
    values = dict(device=None, wav=str(path), control_stdin=False, no_led=True,
                  no_stt=True, stt_model='small', language='en', seconds=0,
                  once=True, silence_seconds=0.8, max_seconds=10, vad_mode=2)
    return SimpleNamespace(**{**values, **changes})


class EndpointTest(unittest.TestCase):
    def test_point_eight_seconds_of_audio_and_speech_resets_timer(self):
        endpoint = r.SpeechEndpoint()
        for _ in range(39):
            self.assertFalse(endpoint.update(False, 320))
        self.assertTrue(endpoint.update(False, 320))
        self.assertFalse(endpoint.update(True, 320))
        self.assertEqual(endpoint.silent_samples, 0)
        for _ in range(39):
            self.assertFalse(endpoint.update(False, 320))
        self.assertTrue(endpoint.update(False, 320))

    def test_input_skips_output_only_device_and_rejects_unrelated_default(self):
        with patch('respeaker.sd.query_devices', return_value=[
                dict(name='ReSpeaker playback', max_input_channels=0),
                dict(name='ReSpeaker USB', max_input_channels=6)]), \
                patch('respeaker.sd.check_input_settings') as check:
            self.assertEqual(r.select_input(), 1)
            check.assert_called_once_with(device=1, samplerate=16000, channels=1, dtype='int16')
        with patch('respeaker.sd.query_devices', return_value=[]), \
                patch('respeaker.subprocess.run', return_value=SimpleNamespace(stdout='laptop-mic')):
            with self.assertRaises(r.DeviceUnavailable):
                r.select_input()

    def test_pipewire_default_is_allowed_only_for_respeaker(self):
        with patch('respeaker.sd.query_devices', return_value=[]), \
                patch('respeaker.subprocess.run', return_value=SimpleNamespace(stdout='respeaker_channel0')), \
                patch('respeaker.sd.default.device', (7, 0)), \
                patch('respeaker.sd.check_input_settings'):
            self.assertEqual(r.select_input(), 7)

    def test_saved_wav_is_valid_and_never_overwrites_another_clip(self):
        with tempfile.TemporaryDirectory() as directory, patch('respeaker.emit'):
            output = Path(directory) / 'voice.wav'
            event = dict(pcm=bytes(16000 * 2 * 3), doa_deg=30, utterance_id=1,
                         reason='silence', silence_seconds=2)
            path = r.save_clip(output, 1, event)
            with wave.open(str(path)) as wav:
                self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getsampwidth()), (1, 16000, 2))
                self.assertEqual(wav.getnframes(), 48000)
            second = r.save_clip(output, 2, event)
            self.assertNotEqual(path, second)
            with self.assertRaises(FileExistsError):
                r.save_clip(output, 1, event)

    def test_doctor_aec_records_through_selected_input_and_sink(self):
        import respeaker_aec
        args = SimpleNamespace(command='doctor', aec=True, compare=False, device=None, sink='jieli',
                               aec_seconds=4, playback=None, label=None)
        with patch('respeaker.select_input', return_value=2), \
                patch.object(respeaker_aec, 'measure', return_value=dict(echo_suppression_db=3)) as measure, \
                patch('respeaker.emit') as emit:
            r.run(args, Mock())
        measure.assert_called_once_with(r.sd, 2, r.RATE, sink='jieli', seconds=4, clip=None, label=None)
        emit.assert_called_once_with(dict(echo_suppression_db=3))
        with patch.object(respeaker_aec, 'compare', return_value=dict(runs={})) as compare, \
                patch('respeaker.emit') as emit:
            r.run(SimpleNamespace(**(vars(args) | dict(aec=False, compare=True))), Mock())
        compare.assert_called_once_with()

    def test_usb_disconnect_is_retryable_but_permissions_are_not(self):
        for error, retryable in ((r.USBError('No device', errno=19), True),
                                 (r.USBError('Access denied', errno=13), False),
                                 (RuntimeError('Bad configuration'), False)):
            device = Mock()
            with self.subTest(error=error), patch('sys.argv', ['respeaker.py', '--diagnostics']), \
                    patch('respeaker.find', return_value=device), patch('respeaker.run', side_effect=error), \
                    patch('respeaker.emit') as emit, patch('respeaker.DIAGNOSTICS'), redirect_stderr(io.StringIO()):
                self.assertEqual(r.main(), 1)
                self.assertEqual(emit.call_args.args[0], dict(status='microphone_error', reconnectable=retryable))
                device.close.assert_called_once()
        with patch('sys.argv', ['respeaker.py']), patch('respeaker.find', return_value=None), \
                patch('respeaker.emit') as emit, redirect_stderr(io.StringIO()):
            self.assertEqual(r.main(), 1)
            self.assertTrue(emit.call_args.args[0]['reconnectable'])


class FeedSubscription:
    """Bus subscription that always has the next 20 ms block, like a blocking stream.read."""

    dropped = 0

    def __init__(self, producer):
        self.producer = producer

    def get(self, timeout=None):
        producer = self.producer
        if producer.before_read:
            producer.before_read()
        producer.reads += 1
        producer.seq += 1 + (producer.reads in producer.gap_at)
        return ab.Block(producer.value.tobytes(), 0.0, producer.seq, producer.reads in producer.overflow_at)

    def close(self):
        pass


class FakeProducer:
    """AudioProducer stand-in: no PortAudio, counts command subscriptions and reads."""

    def __init__(self, *args, **kwargs):
        self.bus = ab.AudioBus()
        self.bus.subscribe = self.subscribe
        self.bus.unsubscribe = self.unsubscribe
        self.failed = None
        self.started = self.stopped = False
        self.reads = self.seq = self.subscriptions = 0
        self.subscribed = False
        self.overflow_at = set(); self.gap_at = set()
        self.before_read = None
        self.value = np.zeros(320, np.int16)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def stats(self):
        return dict(frames=self.reads * 320, overflows=len(self.overflow_at), reopen_count=0)

    def subscribe(self, name, maxsize=100):
        self.subscriptions += 1; self.subscribed = True
        return FeedSubscription(self)

    def unsubscribe(self, sub):
        self.subscribed = False


def record(producer, state=None, args=None, realtime=None, dev=None):
    return r.record_utterance(args or arguments('unused.wav'), dev or Mock(direction=20), producer,
                              state or r.Controls(), None, 1, math.inf, None, realtime)


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_speech_stops_at_ten_seconds(self):
        producer = FakeProducer()
        with patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit'):
            vad.return_value.is_speech.return_value = True
            event = await record(producer)
        self.assertEqual(event['reason'], 'maximum_duration')
        self.assertEqual(len(event['pcm']) / (r.RATE * 2), 10)
        self.assertEqual(producer.reads, 500)
        self.assertEqual(event['silence_seconds'], 0)
        self.assertFalse(producer.subscribed)

    async def test_idle_waits_for_hardware_before_subscribing_to_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            args = arguments(Path(directory) / 'voice.wav')
            dev = Mock(direction=42)
            trigger_count = 0
            producer = FakeProducer()
            with patch('respeaker.select_input', return_value=1), \
                    patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                    patch('respeaker.AudioProducer', return_value=producer) as make, \
                    patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit') as emit:
                def trigger():
                    nonlocal trigger_count
                    trigger_count += 1
                    # The stream runs continuously, but nothing records before the trigger.
                    self.assertTrue(producer.started)
                    self.assertEqual(producer.subscriptions, 0)
                    return trigger_count == 3
                dev.is_voice.side_effect = trigger
                vad.return_value.is_speech.return_value = False
                await r.listen(args, dev)
                self.assertEqual(trigger_count, 3)
                self.assertEqual(make.call_args.args[1:], (1, r.RATE, r.FRAME_SAMPLES))
                self.assertEqual(producer.subscriptions, 1)
                self.assertEqual(producer.reads, 40)
                self.assertTrue(producer.stopped)
                vad.assert_called_once_with(2)
                saved = [call.args[0] for call in emit.call_args_list if call.args[0]['status'] == 'utterance_saved'][0]
                self.assertEqual(saved['silence_seconds'], 0.8)
                self.assertEqual(saved['duration_seconds'], 0.8)

    async def test_recording_starts_with_preroll_from_before_the_trigger(self):
        producer = FakeProducer()
        before = np.arange(1, 4801, dtype=np.int16)  # 0.3 s that the hardware VAD fired during
        producer.bus.ring.write(np.arange(-3200, 0, dtype=np.int16), 1.0, 10)
        producer.bus.ring.write(before, 1.3, 25)
        producer.seq = 25
        producer.value = np.full(320, 7, np.int16)
        realtime = Mock()
        with patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit'):
            vad.return_value.is_speech.return_value = False
            event = await record(producer, realtime=realtime)
        recorded = np.frombuffer(event['pcm'], '<i2')
        self.assertEqual(recorded[:4800].tolist(), before.tolist())
        self.assertTrue((recorded[4800:] == 7).all())
        self.assertEqual(len(recorded), 4800 + 40 * 320)
        self.assertEqual(realtime.append.call_args_list[0].args[0], before.tobytes())

    async def test_preroll_length_follows_argument(self):
        producer = FakeProducer()
        producer.bus.ring.write(np.ones(32000, np.int16), 2.0, 100)
        producer.seq = 100
        with patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit'):
            vad.return_value.is_speech.return_value = False
            event = await record(producer, args=arguments('unused.wav', preroll_seconds=0))
        self.assertEqual(len(event['pcm']), 40 * 640)

    async def test_pause_acknowledged_only_after_recorder_unsubscribes_and_clip_discarded(self):
        state = r.Controls()
        entered = threading.Event()
        release = threading.Event()
        producer = FakeProducer()
        def wait_for_release():
            entered.set()
            if not release.wait(2): raise RuntimeError('Test capture timed out')
        producer.before_read = wait_for_release
        with patch('respeaker.emit') as emit:
            task = asyncio.create_task(record(producer, state))
            await asyncio.to_thread(entered.wait, 2)
            pause = asyncio.create_task(state.apply(dict(paused=True, sequence=1)))
            await asyncio.sleep(.01)
            self.assertFalse(pause.done())
            self.assertTrue(producer.subscribed)
            release.set()
            self.assertIsNone(await task)
            await pause
            self.assertFalse(producer.subscribed)
            self.assertFalse(producer.stopped)  # The shared stream stays open while paused.
            self.assertTrue(state.capture_closed.is_set())
            self.assertEqual(emit.call_args.args[0], dict(status='microphone_control', paused=True, sequence=1))

    async def test_overflow_drop_and_disconnect_never_return_partial_audio(self):
        for result in ('overflow', 'dropped', 'disconnect'):
            with self.subTest(result=result):
                producer = FakeProducer()
                if result == 'overflow':
                    producer.overflow_at = {3}
                elif result == 'dropped':
                    producer.gap_at = {3}
                else:
                    def fail():
                        if producer.reads == 2:
                            producer.failed = r.DeviceUnavailable('ReSpeaker audio stream stopped.')
                    producer.before_read = fail
                state = r.Controls()
                with patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit'):
                    vad.return_value.is_speech.return_value = True
                    with self.assertRaises(r.DeviceUnavailable if result == 'disconnect' else RuntimeError) as raised:
                        await record(producer, state)
                if result != 'disconnect':
                    self.assertEqual(str(raised.exception), r.OVERFLOW_MESSAGE)
                self.assertTrue(state.capture_closed.is_set())
                self.assertFalse(producer.subscribed)

    async def test_stream_failure_while_idle_is_reported_as_device_unavailable(self):
        producer = FakeProducer()
        dev = Mock()
        def silent():
            producer.failed = r.DeviceUnavailable('ReSpeaker audio stream stopped.')
            return False
        dev.is_voice.side_effect = silent
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.AudioProducer', return_value=producer), patch('respeaker.emit'):
            with self.assertRaises(r.DeviceUnavailable):
                await r.listen(arguments(Path(directory) / 'voice.wav', once=False), dev)
        self.assertTrue(producer.stopped)

    async def test_legacy_stream_opens_per_utterance_and_refuses_overflow(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = (np.zeros((320, 1), dtype=np.int16), False)
        with patch('respeaker.sd.InputStream', return_value=stream) as capture, \
                patch('respeaker.webrtcvad.Vad') as vad, patch('respeaker.emit'):
            vad.return_value.is_speech.return_value = True
            event = await record(1)
            capture.assert_called_once_with(samplerate=16000, channels=1, dtype='int16', device=1, blocksize=320)
            self.assertEqual(stream.read.call_count, 500)
            self.assertEqual(len(event['pcm']) / (r.RATE * 2), 10)
            stream.read.return_value = (np.zeros((320, 1), dtype=np.int16), True)
            with self.assertRaises(RuntimeError):
                await record(1)
            stream.read.side_effect = r.sd.PortAudioError('Lost input')
            with self.assertRaises(r.DeviceUnavailable):
                await record(1)

    async def test_legacy_flag_never_starts_the_continuous_stream(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.AudioProducer') as make, \
                patch('respeaker.record_utterance', return_value=None) as recorder, patch('respeaker.emit'):
            dev = Mock(); dev.is_voice.return_value = True
            await r.listen(arguments(Path(directory) / 'voice.wav', legacy_stream=True, seconds=.2, once=False), dev)
        make.assert_not_called()
        self.assertEqual(recorder.call_args.args[2], 1)

    async def run_wake(self, scores, state=None, **changes):
        import wake_word
        model = Mock()
        model.predict.side_effect = lambda frame, it=iter(scores): {'hi_kuro': next(it, 0.0)}
        events = []
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.AudioProducer', FakeProducer), \
                patch.object(wake_word, 'load_model', return_value=model) as load, \
                patch('respeaker_stt.Transcriber') as whisper, \
                patch('respeaker.Controls', return_value=state or r.Controls()), \
                patch('respeaker.emit', side_effect=events.append):
            dev = Mock()
            await r.listen(arguments(Path(directory) / 'voice.wav', wake_engine='oww', no_stt=False,
                                     wake_model_path='hi_kuro.onnx', wake_threshold=.5, **changes), dev)
        load.assert_called_once_with('hi_kuro.onnx')
        whisper.assert_not_called()  # The sleeping worker no longer loads Whisper.
        dev.is_voice.assert_not_called()
        return events

    async def test_wake_word_engine_emits_detection_without_whisper(self):
        events = await self.run_wake([0, 0, .9, .9])
        statuses = [e['status'] for e in events]
        self.assertEqual(statuses[0], 'listening')
        self.assertEqual(events[0]['trigger'], 'wake_word')
        hit = next(e for e in events if e['status'] == 'wake_detected')
        self.assertEqual((hit['score'], hit['model']), (.9, 'hi_kuro'))
        self.assertIn('wake_stats', statuses)
        self.assertNotIn('utterance_started', statuses)

    async def test_wake_word_while_paused_is_only_logged(self):
        events = await self.run_wake([0, .9, .9], state=r.Controls(paused=True), once=False, seconds=.5)
        statuses = [e['status'] for e in events]
        self.assertNotIn('wake_detected', statuses)
        self.assertIn('wake_ignored', statuses)

    async def test_single_utterance_pauses_before_final_and_cannot_trigger_again(self):
        state = r.Controls()
        dev = Mock(direction=10)
        dev.is_voice.return_value = True
        event = dict(pcm=bytes(640), doa_deg=10, utterance_id=1, reason='silence',
                     silence_seconds=2, generation=0)
        finals = []
        def emit(value):
            if value['status'] == 'transcript_final':
                finals.append(value)
                self.assertTrue(state.paused)
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.Controls', return_value=state), \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.AudioProducer', FakeProducer), \
                patch('respeaker.record_utterance', return_value=event) as record_mock, \
                patch('respeaker_stt.Transcriber') as transcriber, patch('respeaker.emit', side_effect=emit):
            transcriber.return_value.transcribe.return_value = dict(text='Hello')
            await r.listen(arguments(Path(directory) / 'voice.wav', no_stt=False,
                                     once=False, single_utterance=True, seconds=.2), dev)
            record_mock.assert_awaited_once()
            self.assertTrue(finals[0]['paused_after'])

    async def test_pause_during_stt_suppresses_late_transcript(self):
        state = r.Controls()
        dev = Mock()
        dev.is_voice.return_value = True
        event = dict(pcm=bytes(640), doa_deg=10, utterance_id=1, reason='silence',
                     silence_seconds=2, generation=0)
        def transcribe(_):
            state.paused = True
            state.generation += 1
            return dict(text='Robot echo', stt_seconds=1)
        with tempfile.TemporaryDirectory() as directory, \
                patch('respeaker.Controls', return_value=state), \
                patch('respeaker.select_input', return_value=1), \
                patch('respeaker.sd.query_devices', return_value=dict(name='ReSpeaker')), \
                patch('respeaker.AudioProducer', FakeProducer), \
                patch('respeaker.record_utterance', return_value=event), \
                patch('respeaker_stt.Transcriber') as transcriber, patch('respeaker.emit') as emit:
            transcriber.return_value.transcribe.side_effect = transcribe
            await r.listen(arguments(Path(directory) / 'voice.wav', no_stt=False), dev)
            statuses = [call.args[0]['status'] for call in emit.call_args_list]
            self.assertIn('utterance_saved', statuses)
            self.assertNotIn('transcript_final', statuses)


if __name__ == '__main__':
    unittest.main()
