#!/usr/bin/env python3
"""ReSpeaker hardware voice trigger, 0.8-second audio endpoint, Realtime or local STT."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import wave

import sounddevice as sd
import webrtcvad
from usb.core import USBError
from voice_errors import RealtimeUnavailable
from respeaker_vendor.tuning import find, PARAMETERS
from audio_bus import AudioBus, AudioProducer, DeviceUnavailable, ReSpeakerControl
from cancel import CancelScope
from worker_threads import DropRate, Heartbeat, RestartBudget, join_all, rss_kb, stack_of, thread_cpu

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000
FRAME_SAMPLES = 320  # 20 ms of mono PCM16; supported by WebRTC VAD.
STREAMING_WAKE = ('oww', 'rolling')  # Wake engines that read the stream themselves while asleep.
PREROLL_SECONDS = 0.3  # Audio from before the hardware VAD fired; keeps speech onsets.
DIAGNOSTICS = False
OVERFLOW_MESSAGE = 'Audio capture overflowed; refusing to transcribe incomplete speech.'
METRICS_SECONDS = 60
REALTIME_RETRY_DELAYS = (5, 15, 30)  # Background reconnects while commands use offline STT.
OFFLINE_STT_DEFAULT = 'hailo'  # Chosen by the phase 7 spike; falls back to 'cpu' if it can't load (plans/phase-7-hailo-whisper-fallback.md).
LOCAL_STT_TIMEOUT = 60  # Whisper small takes ~10 s on the Pi 5; only a hang gets this far.


class WorkerFailure(RuntimeError):
    """A supervised thread failed in a way only a fresh worker can fix."""

    def __init__(self, message, category, reconnectable=True):
        super().__init__(message)
        self.category = category
        self.reconnectable = reconnectable
EMIT_LOCK = threading.Lock()  # Worker threads and the asyncio loop share stdout.


def emit(value):
    with EMIT_LOCK:
        if DIAGNOSTICS:
            print(json.dumps(value), flush=True)
        elif value.get('status') == 'transcript_final':
            print(value['text'], flush=True)
        elif value.get('status') == 'wake_detected':
            print(f"Wake word detected (score {value.get('score', 0):.2f}).", flush=True)
        elif value.get('status') in ('warning', 'microphone_error'):
            print(value.get('message', 'Microphone failed.'), file=sys.stderr, flush=True)


def select_input(requested=None):
    if requested is not None:
        device = int(requested) if requested.isdecimal() else requested
    else:
        candidates = [i for i, info in enumerate(sd.query_devices())
                      if 'respeaker' in info['name'].lower() and info['max_input_channels'] > 0]
        if len(candidates) > 1:
            raise RuntimeError('Multiple ReSpeaker inputs found; select one with --device.')
        device = candidates[0] if candidates else None
        if device is None:
            # PipeWire may own the hardware, hiding its capture channels from ALSA.
            source = subprocess.run(['pactl', 'get-default-source'], check=True,
                                    capture_output=True, text=True).stdout.strip()
            if 'respeaker' not in source.lower():
                raise DeviceUnavailable('Select ReSpeaker as the system microphone, or pass --device.')
            device = sd.default.device[0]
    sd.check_input_settings(device=device, samplerate=RATE, channels=1, dtype='int16')
    return device


class SpeechEndpoint:
    """Measure consecutive nonspeech samples rather than elapsed USB/CPU time."""
    def __init__(self, seconds=0.8):
        self.limit = round(seconds * RATE)
        self.silent_samples = 0

    def update(self, voiced, samples):
        self.silent_samples = 0 if voiced else self.silent_samples + samples
        return self.silent_samples >= self.limit


class Controls:
    """Sequence-acknowledged stdin controls.

    {"paused": bool, "sequence": n}                  every worker
    {"mode": "sleep"|"awake", "sequence": n}         long-lived worker (serve) only
    {"capture": "start", "from_t": t, "sequence": n}  long-lived worker only
    """

    def __init__(self, paused=False):
        self.paused = paused
        self.sequence = self.generation = 0
        self.stopping = False
        self.resume_at = 0
        self.capture_closed = asyncio.Event()
        self.capture_closed.set()
        self.capturing = False  # Set by the T3 recorder thread around each subscription.
        self.scope = CancelScope()
        self.mode = None
        self.on_mode = self.on_capture = None  # Installed by serve().

    def invalidate(self, reason):
        self.generation += 1  # Invalidate recordings/STT from before this control.
        self.scope.cancel_current(reason)

    async def released(self):
        """The recorder has unsubscribed; the shared stream itself may stay open."""
        await self.capture_closed.wait()
        while self.capturing:
            await asyncio.sleep(.01)

    def _sequence(self, value, keys):
        if (not isinstance(value, dict) or set(value) != keys or type(value['sequence']) is not int
                or value['sequence'] <= self.sequence):
            raise RuntimeError('Invalid microphone capture control.')
        self.sequence = value['sequence']

    async def apply(self, value):
        if isinstance(value, dict) and 'mode' in value and self.on_mode:
            self._sequence(value, {'mode', 'sequence'})
            if value['mode'] not in ('sleep', 'awake'):
                raise RuntimeError('Invalid microphone mode.')
            self.invalidate('mode')
            await self.released()
            error = await self.on_mode(value['mode'])
            ack = {'status': 'microphone_control', **value, 'paused': self.paused, 'active_mode': self.mode}
            if error is not None:
                ack['error'] = dict(category='realtime_unavailable', message=str(error),
                                    retryable=getattr(error, 'retryable', True))
            emit(ack)
            return
        if isinstance(value, dict) and 'capture' in value and self.on_capture:
            self._sequence(value, {'capture', 'from_t', 'sequence'})
            if value['capture'] != 'start' or type(value['from_t']) not in (int, float) or not math.isfinite(value['from_t']):
                raise RuntimeError('Invalid microphone capture request.')
            accepted = await self.on_capture(float(value['from_t']))
            emit({'status': 'microphone_control', **value, 'accepted': accepted, 'paused': self.paused})
            return
        if (not isinstance(value, dict) or set(value) != {'paused', 'sequence'}
                or type(value['paused']) is not bool or type(value['sequence']) is not int
                or value['sequence'] <= self.sequence):
            raise RuntimeError('Invalid microphone capture control.')
        self.paused = value['paused']
        self.sequence = value['sequence']
        self.invalidate('control')
        self.resume_at = time.monotonic() + .5
        if self.paused:
            await self.released()
        # The parent may start speech as soon as it receives this acknowledgment.
        emit({'status': 'microphone_control', **value})


async def read_controls(state):
    reader = asyncio.StreamReader(limit=4096)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while line := await reader.readline():
            await state.apply(json.loads(line))
    finally:
        state.stopping = True
        state.paused = True
        state.generation += 1
        transport.close()


def check_controller(controller):
    if controller and controller.done():
        controller.result()


def save_clip(output, clip_count, event):
    path = output if clip_count == 1 else output.with_name(f'{output.stem}-{clip_count:03d}{output.suffix}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as file, wave.open(file, 'wb') as wav:
        wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes(event['pcm'])
    emit({'status': 'utterance_saved', 'wav': str(path), 'recording_channel': 0,
          **{key: event[key] for key in ('doa_deg', 'utterance_id', 'reason', 'silence_seconds')},
          'duration_seconds': len(event['pcm']) / (RATE * 2)})
    return path


RING_SECONDS = 8.0  # Enough history for a wake-initiated capture after a Realtime connect.
CAPTURE_REQUEST_TTL = 2.0


async def speech_continues(bus, t, min_voiced=.16, silence=.4, start=.1, limit=1.0, vad_mode=2):
    """After a wake hit at capture time `t`: is the user still talking ("Hi Kuro, what time…")?

    Reads the ring as audio arrives: `min_voiced` seconds of speech means yes (decided as
    soon as it is heard); `silence` seconds without speech means a plain wake. A comma
    pause shorter than `silence` therefore does not end the check. 0 disables it.
    """
    if min_voiced <= 0:
        return False
    vad = webrtcvad.Vad(vad_mode)
    frame_bytes = FRAME_SAMPLES * 2
    frame_seconds = FRAME_SAMPLES / RATE
    position = t + start
    voiced = quiet = 0
    deadline = time.monotonic() + limit + 1.0
    while position < t + start + limit and time.monotonic() < deadline:
        pcm, _, _ = bus.ring.read_since(position)
        frames = len(pcm) // frame_bytes
        if not frames:
            await asyncio.sleep(.02)
            continue
        for index in range(frames):
            if vad.is_speech(pcm[index * frame_bytes:(index + 1) * frame_bytes], RATE):
                voiced += 1; quiet = 0
            else:
                quiet += 1
            if voiced * frame_seconds >= min_voiced:
                return True
            if quiet * frame_seconds >= silence:
                return False
        position += frames * frame_seconds
    return False


class CommandRecorder(threading.Thread):
    """T3: wait for hardware VAD or a capture request, record one utterance from the bus.

    PCM and lifecycle items go to the asyncio loop in order through `handoff`
    (call_soon_threadsafe); the loop owns Realtime and STT. Each capture holds its own
    CancelToken, so a control, mode switch or newer trigger cancels only older work.
    """

    def __init__(self, args, dev, producer, state, loop, handoff, led=None):
        super().__init__(name='command-recorder', daemon=True)
        self.args = args; self.dev = dev; self.producer = producer; self.state = state
        self.loop = loop; self.handoff = handoff; self.led = led
        self.armed = threading.Event()
        self.armed.set()
        self.requests = queue.Queue()
        self.stop_event = threading.Event()
        self.error = None
        self.utterance_id = 0
        self.current = None  # (token, trigger, monotonic start) of the capture in progress
        self.current_utterance = None
        self.failed_utterance = None  # Utterance in progress when this thread raised.
        self.heartbeat = Heartbeat('command-recorder', 2.0)

    def post(self, *item):
        self.loop.call_soon_threadsafe(self.handoff.put_nowait, item)

    def listening(self):
        state = self.state
        uses_vad = state.mode == 'awake' or (state.mode == 'sleep' and getattr(self.args, 'wake_engine', 'whisper') not in STREAMING_WAKE)
        return uses_vad and not state.paused and not state.stopping and time.monotonic() >= state.resume_at

    def request(self, from_t):
        """Loop thread: accept a capture start when one can begin now.

        A hardware-VAD recording that started in the last 1.5 s is replaced: it is the
        same speech, and the requested capture also has everything since `from_t`.
        """
        state = self.state
        if state.mode != 'awake' or state.paused or state.stopping:
            return False
        current = self.current
        if state.capturing or not self.armed.is_set():
            if not current or current[1] != 'hardware_vad' or time.monotonic() - current[2] > 1.5:
                return False
            current[0].cancel('capture_request')
        self.requests.put((from_t, time.monotonic()))
        return True

    def run(self):
        try:
            while not self.stop_event.is_set():
                self.heartbeat.beat()
                if not self.armed.wait(.05):
                    continue
                from_t = None
                while not self.requests.empty():
                    value, requested = self.requests.get_nowait()
                    if time.monotonic() - requested <= CAPTURE_REQUEST_TTL:
                        from_t = value
                if from_t is None:
                    if not self.listening() or not self.dev.is_voice():
                        self.stop_event.wait(.05)
                        continue
                self.armed.clear()
                self.capture(from_t)
        except BaseException as error:  # Reported to the worker loop, never swallowed.
            self.error = error
            self.post('error', error)

    def cancelled(self, token, generation):
        state = self.state
        return token.cancelled or state.paused or state.stopping or state.generation != generation

    def capture(self, from_t=None):
        args, state, bus = self.args, self.state, self.producer.bus
        awake = state.mode == 'awake'
        endpoint = SpeechEndpoint(args.silence_seconds if awake else getattr(args, 'sleep_silence_seconds', args.silence_seconds))
        vad = webrtcvad.Vad(args.vad_mode)
        generation = state.generation
        state.capturing = True  # Before re-checking pause, so a pause ack waits for us.
        try:
            token = state.scope.new()
            if self.cancelled(token, generation):
                self.armed.set()
                return
            self.utterance_id += 1
            utterance_id = self.utterance_id
            self.current = (token, 'capture_request' if from_t is not None else 'hardware_vad', time.monotonic())
            self.current_utterance = utterance_id
            sub = bus.subscribe('command')
            try:
                if from_t is not None:
                    preroll, _, last_seq = bus.preroll(sub, since=from_t)
                else:
                    preroll, _, last_seq = bus.preroll(sub, seconds=getattr(args, 'preroll_seconds', PREROLL_SECONDS))
                expected = last_seq + 1 if last_seq else None
                pcm = bytearray()
                direction = self.dev.direction
                next_direction = 0.0
                emit({'status': 'utterance_started', 'utterance_id': utterance_id, 'doa_deg': direction,
                      'trigger': 'capture_request' if from_t is not None else 'hardware_vad'})
                self.post('begin', utterance_id, token)
                if self.led:
                    self.led.set_state(self.led.states.LISTEN)
                if preroll:
                    pcm.extend(preroll)
                    self.post('audio', preroll)
                while True:
                    self.heartbeat.beat()
                    if self.cancelled(token, generation):
                        emit({'status': 'utterance_discarded', 'utterance_id': utterance_id})
                        self.post('discarded', utterance_id)
                        return
                    block = sub.get(.5)
                    if self.producer.failed:
                        raise self.producer.failed
                    if block is None or block.seq <= last_seq:
                        continue
                    if block.overflow or (expected is not None and block.seq != expected):
                        # Incomplete speech is never transcribed; drop this utterance only.
                        emit({'status': 'utterance_discarded', 'utterance_id': utterance_id,
                              'reason': 'overflow', 'message': OVERFLOW_MESSAGE})
                        self.post('discarded', utterance_id)
                        return
                    expected = block.seq + 1
                    if self.cancelled(token, generation):
                        continue  # Reported as discarded at the top of the loop.
                    data = block.pcm
                    pcm.extend(data)
                    self.post('audio', data)
                    voiced = vad.is_speech(data, RATE)
                    finished = endpoint.update(voiced, len(data) // 2)
                    now = time.monotonic()
                    if voiced and now >= next_direction:
                        direction = self.dev.direction
                        emit({'status': 'speech_direction', 'native_doa_deg': direction,
                              'utterance_id': utterance_id})
                        if self.led:
                            self.led.set_listen(direction)
                        next_direction = now + .25
                    reason = 'silence' if finished else None
                    if not reason and len(pcm) >= round(args.max_seconds * RATE) * 2:
                        reason = 'maximum_duration'
                    if reason:
                        self.post('done', {'pcm': bytes(pcm), 'doa_deg': direction, 'utterance_id': utterance_id,
                                           'reason': reason, 'silence_seconds': endpoint.silent_samples / RATE,
                                           'generation': generation}, token)
                        return
            finally:
                bus.unsubscribe(sub)
        except BaseException:
            self.failed_utterance = self.current_utterance
            raise
        finally:
            self.current = None
            self.current_utterance = None
            state.capturing = False

    def stop(self, timeout=2.0):
        self.stop_event.set()
        self.join(timeout)


def load_offline_stt(args):
    """Local STT for awake commands while Realtime is unreachable (phase 7)."""
    model = getattr(args, 'offline_stt_model', None)
    if args.offline_stt == 'hailo':
        try:
            import hailo_stt
            return hailo_stt.SharedTranscriber(model or 'base', args.language)  # Shared with the rolling wake thread.
        except Exception as error:  # Missing HEFs or binding, service down, device busy...
            emit({'status': 'warning', 'message': f'Hailo offline STT unavailable ({error!r}); using the CPU.'})
            model = None
    from respeaker_stt import Transcriber
    engine = Transcriber(model or 'base.en', args.language, cpu_threads=2, beam_size=1)
    engine.backend_name = 'cpu'
    return engine


async def serve(args, dev):
    """The microphone worker: T1 stream, T2 wake word, T3 recorder; sleep/awake switch by control."""
    device = select_input(args.device)
    output = Path(args.wav).resolve()
    if output.exists():
        raise RuntimeError(f'Recording already exists: {output}; choose another --wav path.')
    loop = asyncio.get_running_loop()
    state = Controls(paused=args.control_stdin)
    dev = ReSpeakerControl(dev)
    handoff = asyncio.Queue()
    stt_lock = asyncio.Lock()
    wake_engine = getattr(args, 'wake_engine', 'whisper')
    streaming = wake_engine in STREAMING_WAKE  # A wake thread on the stream instead of VAD segments + Whisper
    led = realtime = transcriber = wake = recorder = controller = None
    backends = {}
    clip_count = 0
    led_hold = 0.0  # Keep barge-in LED feedback visible briefly.
    offline_engine = getattr(args, 'offline_stt', 'off')
    offline_loading = None   # Task loading the local fallback transcriber
    offline_active = False   # Awake, but Realtime is unreachable: commands use local STT
    reconnecting = None      # Background Realtime retry while offline
    producer = AudioProducer(AudioBus(ring_seconds=RING_SECONDS), device, RATE, FRAME_SAMPLES)

    async def open_realtime():
        from respeaker_realtime import RealtimeTranscriber
        emit({'status': 'transcription_loading', 'model': args.stt_model})
        opened = await RealtimeTranscriber.open(emit, args.stt_model, args.language)
        emit({'status': 'transcription_ready', 'model': args.stt_model, 'backend': 'realtime'})
        return opened

    async def offline_transcriber():
        """The local fallback engine, or None if disabled or it failed to load.

        Loaded on first need: normal operation holds no extra Hailo session (hailort.service
        4.23 has crashed when sharing clients tear sessions down) or Whisper memory.
        """
        nonlocal offline_loading
        if offline_engine == 'off' or args.no_stt:
            return None
        if offline_loading is None:
            offline_loading = asyncio.create_task(asyncio.to_thread(load_offline_stt, args))
        try:
            return await offline_loading
        except Exception as error:
            emit({'status': 'warning', 'message': f'Offline STT ({offline_engine}) unavailable: {error!r}'})
            return None

    def offline_name(engine):
        name = getattr(engine, 'backend_name', None)
        return name if isinstance(name, str) else offline_engine

    async def go_offline(error):
        """Retryable Realtime failure while awake: keep taking commands with local STT."""
        nonlocal offline_active, reconnecting
        if not getattr(error, 'retryable', False) or await offline_transcriber() is None:
            return False
        if not offline_active:
            offline_active = True
            emit({'status': 'stt_fallback', 'engine': offline_name(await offline_transcriber()), 'reason': str(error)})
        if reconnecting is None or reconnecting.done():
            reconnecting = asyncio.create_task(reconnect_realtime())
        return True

    async def reconnect_realtime():
        """Keep retrying Realtime in the background and switch back as soon as it answers."""
        nonlocal realtime, offline_active
        attempt = 0
        while offline_active and state.mode == 'awake' and not state.stopping:
            await asyncio.sleep(REALTIME_RETRY_DELAYS[min(attempt, len(REALTIME_RETRY_DELAYS) - 1)])
            attempt += 1
            async with stt_lock:
                if not offline_active or state.mode != 'awake' or realtime is not None:
                    return
                try:
                    realtime = await open_realtime()
                except RealtimeUnavailable as error:
                    if not error.retryable:
                        emit({'status': 'warning', 'message': f'Realtime rejected the connection: {error}'})
                        return
                    continue
                offline_active = False
                emit({'status': 'stt_restored', 'backend': 'realtime'})
                return

    async def set_mode(mode):
        """Open/close Realtime for the mode; on failure keep the previous mode and report it."""
        nonlocal realtime, transcriber, offline_active
        async with stt_lock:
            if mode == 'awake' and realtime is None and not args.no_stt:
                try:
                    realtime = await open_realtime()
                    offline_active = False
                except RealtimeUnavailable as error:
                    if not await go_offline(error):
                        return error
            elif mode == 'sleep':
                offline_active = False  # The next wake tries Realtime again.
                if reconnecting is not None:
                    reconnecting.cancel()
                if realtime is not None:
                    await realtime.close()
                    realtime = None
                if not streaming and transcriber is None and not args.no_stt:
                    from respeaker_stt import Transcriber
                    model = getattr(args, 'wake_stt_model', 'small')
                    emit({'status': 'transcription_loading', 'model': model})
                    # Loaded once and kept: sleep/awake switches never reload Whisper.
                    transcriber = await asyncio.to_thread(Transcriber, model, args.language, getattr(args, 'stt_prompt', None))
                    emit({'status': 'transcription_ready', 'model': model, 'backend': 'local'})
            state.mode = mode
            return None

    async def request_capture(from_t):
        return recorder.request(from_t)

    async def start_wake():
        on_detect = lambda hit: loop.call_soon_threadsafe(handoff.put_nowait, ('wake', hit))
        if wake_engine == 'rolling':
            from rolling_wake import HailoPhraseScorer, RollingWakeThread
            thread = RollingWakeThread(producer.bus, args.wake_threshold, lambda: HailoPhraseScorer(args.wake_name),
                                       on_detect=on_detect, vad_mode=args.vad_mode)
        else:
            from wake_word import WakeWordThread
            thread = WakeWordThread(producer.bus, args.wake_model_path, args.wake_threshold, on_detect=on_detect)
        thread.start()
        await asyncio.to_thread(thread.ready.wait, 60)
        return thread

    wake_budget = RestartBudget(3, 300)
    recorder_budget = RestartBudget(3, 60)
    wake_drops = DropRate(.01, 10)
    next_metrics = time.monotonic() + METRICS_SECONDS
    last_tick = time.monotonic()

    def metrics():
        stats = producer.stats()
        return {'status': 'metrics', 'mode': state.mode, **stats, 'wake': wake.stats() if wake else None,
                'thread_cpu_s': thread_cpu(), 'rss_kb': rss_kb(), 'threads': threading.active_count()}

    def stalled(heartbeat, thread):
        emit({'status': 'warning', 'message': f'{heartbeat.name} thread stuck for more than {heartbeat.limit:g} s.',
              'stack': stack_of(thread)})
        return WorkerFailure(f'Microphone {heartbeat.name} thread stopped responding; restarting the worker.', 'stalled')

    try:
        producer.start()
        if not args.no_led:
            from respeaker_led import LEDController
            led = LEDController(brightness=20)
        error = await set_mode(args.initial_mode)
        if error is not None:
            raise error
        if streaming:
            wake = await start_wake()
            if wake.error:
                raise WorkerFailure(f'Wake-word model failed to load: {wake.error}', 'wake_engine')
        recorder = CommandRecorder(args, dev, producer, state, loop, handoff, led)
        state.on_mode, state.on_capture = set_mode, request_capture
        recorder.start()
        controller = asyncio.create_task(read_controls(state)) if args.control_stdin else None
        emit({'status': 'listening', 'input': sd.query_devices(device, 'input')['name'],
              'trigger': 'wake_word' if streaming else 'hardware_vad', 'mode': state.mode,
              'wake_engine': wake_engine, 'silence_seconds': args.silence_seconds,
              'max_seconds': args.max_seconds, 'vad_mode': args.vad_mode})
        deadline = time.monotonic() + args.seconds if args.seconds else math.inf
        while not state.stopping and time.monotonic() < deadline:
            check_controller(controller)
            if producer.failed:
                raise producer.failed
            now = time.monotonic()
            if now - last_tick > 1.0:
                # The whole process was paused (SIGSTOP, swap, overload): every heartbeat is old
                # for the same reason, so give the threads a fresh window instead of replacing them.
                for thread in (wake, recorder):
                    if thread is not None:
                        thread.heartbeat.beat()
            last_tick = now
            if wake and wake.error:
                if not wake_budget.allow(now):
                    raise WorkerFailure('The wake-word engine failed repeatedly; falling back to Whisper.', 'wake_engine')
                emit({'status': 'warning', 'message': f'Wake-word thread failed ({wake.error!r}); restarting it.'})
                wake = await start_wake()
            if wake and wake.heartbeat.stale(now):
                raise stalled(wake.heartbeat, wake)
            if recorder.is_alive() and recorder.heartbeat.stale(now):
                raise stalled(recorder.heartbeat, recorder)
            if wake and wake.sub and (lost := wake_drops.check(producer.seq, wake.sub.dropped, now)):
                emit({'status': 'warning', 'message': f'Wake detection is falling behind: {lost:.1%} of audio dropped in 10 s.'})
            if now >= next_metrics:
                next_metrics = now + METRICS_SECONDS
                emit(metrics())
            if led and not state.capturing and recorder.armed.is_set() and time.monotonic() >= led_hold:
                led.off() if state.paused else led.set_state(led.states.IDLE)
            try:
                item = await asyncio.wait_for(handoff.get(), .05)
            except TimeoutError:
                continue
            kind = item[0]
            if kind == 'error':
                error = item[1]
                if isinstance(error, (DeviceUnavailable, USBError)):
                    raise error
                # T3 raised: drop its utterance (never transcribe partial audio) and restart it.
                if recorder.failed_utterance is not None:
                    emit({'status': 'utterance_discarded', 'utterance_id': recorder.failed_utterance, 'reason': 'error'})
                    if backends.pop(recorder.failed_utterance, None) == 'realtime' and realtime is not None:
                        async with stt_lock:
                            await realtime.close()
                            realtime = None
                state.capturing = False
                if not recorder_budget.allow(now):
                    raise WorkerFailure(f'The command recorder failed repeatedly ({error!r}).', 'recorder')
                emit({'status': 'warning', 'message': f'Command recorder failed ({error!r}); restarting it.'})
                previous = recorder
                recorder = CommandRecorder(args, dev, producer, state, loop, handoff, led)
                recorder.utterance_id = previous.utterance_id
                recorder.start()
                continue
            if kind == 'begin':
                utterance_id, token = item[1:]
                backend = None
                async with stt_lock:
                    if state.mode == 'awake' and not args.no_stt and not token.cancelled:
                        if realtime is None and not offline_active:
                            try:
                                realtime = await open_realtime()  # Reopened lazily after a discard.
                            except RealtimeUnavailable as error:
                                if not await go_offline(error):
                                    raise
                        if offline_active:
                            backend = 'offline'
                        else:
                            realtime.begin(utterance_id)
                            backend = 'realtime'
                    elif state.mode == 'sleep' and transcriber:
                        backend = 'local'
                backends[utterance_id] = backend
            elif kind == 'audio':
                if realtime is not None and realtime.turn is not None:
                    realtime.append(item[1])
            elif kind == 'discarded':
                if backends.pop(item[1], None) == 'realtime' and realtime is not None:
                    # Drop unfinished remote audio and its context before the next turn.
                    async with stt_lock:
                        await realtime.close()
                        realtime = None
                recorder.armed.set()
            elif kind == 'done':
                event, token = item[1:]
                backend = backends.pop(event['utterance_id'], None)
                clip_count += 1
                path = save_clip(output, clip_count, event)
                result = None
                if backend and led:
                    led.set_state(led.states.THINK)
                if backend == 'realtime':
                    # Hold the lock: a mode switch must not close the socket mid-finish.
                    async with stt_lock:
                        if realtime is not None and realtime.turn is not None:
                            try:
                                result = await realtime.finish()
                            except RealtimeUnavailable as error:
                                if not error.retryable or await offline_transcriber() is None:
                                    raise
                                await realtime.close()
                                realtime = None
                                await go_offline(error)
                                backend = 'offline'  # The saved WAV is transcribed locally below.
                if backend == 'offline':
                    engine = await offline_transcriber()
                    try:
                        result = await asyncio.wait_for(asyncio.to_thread(engine.transcribe, path), LOCAL_STT_TIMEOUT)
                        result['backend'] = offline_name(engine)
                    except TimeoutError:
                        emit({'status': 'warning', 'message': f'Offline transcription took over {LOCAL_STT_TIMEOUT} s; dropped.'})
                        result = None
                    except Exception as error:  # e.g. hailort.service restarted: lose this utterance, not the worker.
                        emit({'status': 'warning', 'message': f'Offline transcription failed: {error!r}'})
                        result = None
                elif backend == 'local':
                    try:
                        result = await asyncio.wait_for(asyncio.to_thread(transcriber.transcribe, path), LOCAL_STT_TIMEOUT)
                    except TimeoutError:
                        emit({'status': 'warning', 'message': f'Local transcription took over {LOCAL_STT_TIMEOUT} s; dropped.'})
                        result = None
                check_controller(controller)
                if (result is not None and not token.cancelled and not state.stopping and not state.paused
                        and state.generation == event['generation']):
                    final_fields = {}
                    if getattr(args, 'single_utterance', False) and backend in ('realtime', 'offline'):
                        state.paused = True
                        final_fields['paused_after'] = True
                    emit({'status': 'transcript_final', 'item_id': f'local-{event["utterance_id"]}',
                          'utterance_id': event['utterance_id'], 'wav': str(path), 'doa_deg': event['doa_deg'],
                          'backend': backend, **result, **final_fields})  # offline: result sets backend=hailo|cpu
                recorder.armed.set()
                if args.once:
                    break
            elif kind == 'wake':
                hit = item[1]
                wake.wake_word_detected.clear()  # Consumed here; T2 only ever sets it.
                if state.mode == 'sleep':
                    if state.paused or time.monotonic() < state.resume_at:
                        # The robot's own playback ("Say Hi Kuro…") or a closed gate.
                        emit({'status': 'wake_ignored', 'reason': 'paused', 'score': hit['score']})
                        continue
                    # Rolling hits start at the phrase; "still speaking" is checked after the window.
                    hit['continued'] = await speech_continues(producer.bus, hit.get('continue_from', hit['t']),
                                                              getattr(args, 'wake_continuation', .16), vad_mode=args.vad_mode)
                    emit({**hit, 'mode': 'sleep'})
                    if args.once:
                        break
                else:
                    # Even while capture is paused (THINKING/SPEAKING): chat decides on barge-in.
                    emit({**hit, 'mode': 'awake', 'paused': state.paused})
                    if led:
                        led.set_state(led.states.LISTEN)
                        led_hold = time.monotonic() + 1.5
    finally:
        for task in (reconnecting, offline_loading):
            if task is not None and not task.done():
                task.cancel()
        if offline_loading is not None and offline_loading.done() and not offline_loading.cancelled() \
                and offline_loading.exception() is None and hasattr(offline_loading.result(), 'close') \
                and getattr(offline_loading.result(), 'backend_name', None) != 'hailo':
            offline_loading.result().close()  # The shared Hailo session closes below, after the wake thread.
        if recorder:
            recorder.stop_event.set()
        if wake:
            wake.stop_event.set()
        stuck = await asyncio.to_thread(join_all, [recorder, wake], 2.0)
        if wake:
            await asyncio.to_thread(wake.stop, .1)
            emit({'status': 'wake_stats', **wake.stats()})
        if realtime:
            await realtime.close()
        if controller:
            controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)
        if led:
            led.off()
        producer.stop()
        emit({'status': 'capture_stats', **producer.stats()})
        if sys.modules.get('hailo_stt') is not None and not stuck:
            sys.modules['hailo_stt'].close_shared()
        stuck += [t.name for t in threading.enumerate() if t.name == 'audio-watchdog' and t.is_alive()]
        if stuck:
            emit({'status': 'warning', 'message': 'Threads did not stop: ' + ', '.join(stuck)})


def run(args, dev):
    if args.command == 'doctor' and (args.aec or args.compare):
        import respeaker_aec
        result = (respeaker_aec.compare() if args.compare else
                  respeaker_aec.measure(sd, select_input(args.device), RATE, sink=args.sink,
                                        seconds=args.aec_seconds, clip=args.playback, label=args.label))
        emit(result)
    elif args.command == 'doctor':
        emit({'control_version_byte': dev.version, 'usb_device_revision': hex(dev.dev.bcdDevice),
              'parameters': {name: dev.read(name) for name in PARAMETERS}})
    else:
        asyncio.run(serve(args, dev))


def main():
    global DIAGNOSTICS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='listen', choices=['listen', 'doctor'])
    parser.add_argument('--device', help='PortAudio input index/name; auto-detect ReSpeaker by default')
    parser.add_argument('--seconds', type=float, default=0, help='Limit session duration; 0 waits indefinitely')
    parser.add_argument('--once', action='store_true', help='Exit after one utterance')
    parser.add_argument('--silence-seconds', type=float, default=0.8)
    parser.add_argument('--max-seconds', type=float, default=10, help='Maximum length of one utterance')
    parser.add_argument('--vad-mode', type=int, choices=range(4), default=2,
                        help='WebRTC VAD aggressiveness: 0 (least) to 3 (most)')
    parser.add_argument('--wav', default=str(ROOT / 'test-output' / f'respeaker-{time.time_ns()}.wav'))
    parser.add_argument('--no-stt', action='store_true', help='Save utterances without transcription')
    parser.add_argument('--no-led', action='store_true', help='Leave the LED ring alone')
    parser.add_argument('--diagnostics', action='store_true', help='Print microphone events as JSON')
    parser.add_argument('--single-utterance', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--control-stdin', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--stt-model', default='gpt-live-transcribe', help='Realtime gpt-live-transcribe, or a local Whisper model/directory')
    parser.add_argument('--stt-prompt', help='Optional local Whisper vocabulary hint (not sent to Realtime).')
    parser.add_argument('--language', default='en', help='Language code, or auto to detect it')
    parser.add_argument('--preroll-seconds', type=float, default=PREROLL_SECONDS,
                        help='Audio kept from before the hardware voice trigger (0-1.5 s)')
    parser.add_argument('--wake-engine', choices=['whisper', 'oww', 'rolling'], default='whisper',
                        help='oww: openWakeWord model on the continuous stream; rolling: Hailo Whisper scores '
                             '"Hi <name>" in rolling windows; whisper: hardware VAD segments + local Whisper')
    parser.add_argument('--wake-name', default='Kuro', help='Robot name for --wake-engine rolling')
    parser.add_argument('--wake-model-path', help='openWakeWord .onnx model (default DOTS_WAKE_MODEL_PATH or '
                        '~/.local/share/face-agent/wake/hi_kuro.onnx)')
    parser.add_argument('--wake-threshold', type=float, help='Wake score threshold (default: model .json or 0.5)')
    parser.add_argument('--initial-mode', choices=['sleep', 'awake'],
                        help='Start in this mode; switch with {"mode": …} controls (default follows --stt-model)')
    parser.add_argument('--sleep-silence-seconds', type=float, default=0.4,
                        help='Endpoint silence in sleep mode with the Whisper wake engine')
    parser.add_argument('--wake-stt-model', default='small', help='Local Whisper model for sleep mode (Whisper wake engine)')
    parser.add_argument('--offline-stt', choices=['off', 'hailo', 'cpu'], default=OFFLINE_STT_DEFAULT,
                        help='Local STT for awake commands while Realtime is unreachable')
    parser.add_argument('--offline-stt-model', help='hailo: tiny|base; cpu: a faster-whisper model (default base.en)')
    parser.add_argument('--wake-continuation', type=float, default=0.16,
                        help='Seconds of speech right after a wake hit that mark "still speaking" (0 disables)')
    aec = parser.add_argument_group('doctor --aec (echo cancellation check; stop the voice stack first)')
    aec.add_argument('--aec', action='store_true', help='Record all channels while playing speech through --sink')
    aec.add_argument('--compare', action='store_true', help='Compare the latest saved --aec runs per label')
    aec.add_argument('--sink', help='Playback sink: respeaker, jieli, hdmi or a full name (default sink if omitted)')
    aec.add_argument('--aec-seconds', type=float, default=10, help='Seconds of playback to record')
    aec.add_argument('--playback', help='WAV to loop (default: cached wake acknowledgment speech)')
    aec.add_argument('--label', help='Run label in test-output/aec (default: sink alias)')
    args = parser.parse_args()
    DIAGNOSTICS = args.diagnostics or args.command != 'listen'
    if (not all(math.isfinite(n) for n in (args.seconds, args.silence_seconds, args.max_seconds))
            or args.seconds < 0 or args.silence_seconds <= 0 or args.max_seconds <= args.silence_seconds):
        parser.error('Seconds must be finite: session >= 0; silence > 0; maximum > silence.')
    if not 0 <= args.preroll_seconds <= 1.5:
        parser.error('--preroll-seconds must be between 0 and 1.5.')
    if args.initial_mode is None:
        # Standalone use: Realtime (or --no-stt) records awake; a local Whisper model or
        # the wake engine runs in sleep mode, with the requested silence timeout.
        local = args.stt_model != 'gpt-live-transcribe' and not args.no_stt
        args.initial_mode = 'sleep' if local or args.wake_engine in STREAMING_WAKE else 'awake'
        if local:
            args.wake_stt_model = args.stt_model
            args.sleep_silence_seconds = args.silence_seconds
    if not 0 < args.sleep_silence_seconds < args.max_seconds:
        parser.error('--sleep-silence-seconds must be positive and below --max-seconds.')
    if args.wake_engine == 'rolling':
        import rolling_wake
        if args.wake_threshold is None:
            args.wake_threshold = rolling_wake.DEFAULT_THRESHOLD
        if not -20 < args.wake_threshold < 0:
            parser.error('--wake-threshold for rolling is a mean log-probability between -20 and 0.')
    if args.wake_engine == 'oww':
        import wake_word
        args.wake_model_path = str(Path(args.wake_model_path).expanduser() if args.wake_model_path
                                   else wake_word.default_model_path())
        if not Path(args.wake_model_path).is_file():
            parser.error(f'No wake model at {args.wake_model_path}.')
        if args.wake_threshold is None:
            args.wake_threshold = wake_word.metadata(args.wake_model_path).get('threshold', wake_word.DEFAULT_THRESHOLD)
        if not 0 < args.wake_threshold < 1:
            parser.error('--wake-threshold must be between 0 and 1.')
    dev = None
    try:
        dev = find()
        if dev is None:
            raise DeviceUnavailable('ReSpeaker 2886:0018 is not connected.')
        run(args, dev)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        event = dict(status='microphone_error', message=str(error),
                     reconnectable=isinstance(error, DeviceUnavailable) or isinstance(error, USBError) and error.errno == 19)
        if isinstance(error, USBError) and error.errno == 13:
            event.update(category='usb_permission')  # udev rule / plugdev group, not a loose cable.
        if isinstance(error, RealtimeUnavailable):
            event.update(category='realtime_unavailable', message=str(error), retryable=error.retryable)
        if isinstance(error, WorkerFailure):
            event.update(category=error.category, message=str(error), reconnectable=error.reconnectable)
        emit(event)
        print('ReSpeaker error: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if dev is not None:
            dev.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
