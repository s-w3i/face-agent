#!/usr/bin/env python3
"""ReSpeaker hardware voice trigger, two-second audio endpoint, Realtime or local STT."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import wave

import sounddevice as sd
import webrtcvad
from usb.core import USBError
from voice_errors import RealtimeUnavailable
from respeaker_vendor.tuning import find, PARAMETERS

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000
FRAME_SAMPLES = 320  # 20 ms of mono PCM16; supported by WebRTC VAD.
DIAGNOSTICS = False


class DeviceUnavailable(RuntimeError):
    """The USB microphone or its active audio stream disappeared."""


def emit(value):
    if DIAGNOSTICS:
        print(json.dumps(value), flush=True)
    elif value.get('status') == 'transcript_final':
        print(value['text'], flush=True)
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
    def __init__(self, seconds=2.0):
        self.limit = round(seconds * RATE)
        self.silent_samples = 0

    def update(self, voiced, samples):
        self.silent_samples = 0 if voiced else self.silent_samples + samples
        return self.silent_samples >= self.limit


class Controls:
    def __init__(self, paused=False):
        self.paused = paused
        self.sequence = self.generation = 0
        self.stopping = False
        self.resume_at = 0
        self.capture_closed = asyncio.Event()
        self.capture_closed.set()

    async def apply(self, value):
        if (not isinstance(value, dict) or set(value) != {'paused', 'sequence'}
                or type(value['paused']) is not bool or type(value['sequence']) is not int
                or value['sequence'] <= self.sequence):
            raise RuntimeError('Invalid microphone capture control.')
        self.paused = value['paused']
        self.sequence = value['sequence']
        self.generation += 1  # Invalidate recordings/STT from before this control.
        self.resume_at = time.monotonic() + .5
        if self.paused:
            await self.capture_closed.wait()
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


async def record_utterance(args, dev, device, state, led, utterance_id, deadline, controller, realtime=None):
    """Capture only after hardware VAD. Pausing discards and closes the clip."""
    endpoint = SpeechEndpoint(args.silence_seconds)
    vad = webrtcvad.Vad(1)
    pcm = bytearray()
    direction = dev.direction
    next_direction = 0.0
    generation = state.generation
    state.capture_closed.clear()
    try:
        with sd.InputStream(samplerate=RATE, channels=1, dtype='int16',
                            device=device, blocksize=FRAME_SAMPLES) as stream:
            emit({'status': 'utterance_started', 'utterance_id': utterance_id,
                  'doa_deg': direction})
            if realtime:
                realtime.begin(utterance_id)
            if led:
                led.set_state(led.states.LISTEN)
            while True:
                check_controller(controller)
                if state.paused or state.stopping or state.generation != generation:
                    emit({'status': 'utterance_discarded', 'utterance_id': utterance_id})
                    return None
                try:
                    chunk, overflow = await asyncio.to_thread(stream.read, FRAME_SAMPLES)
                except sd.PortAudioError as error:
                    raise DeviceUnavailable('ReSpeaker audio stream stopped.') from error
                if overflow:
                    raise RuntimeError('Audio capture overflowed; refusing to transcribe incomplete speech.')
                if state.paused or state.stopping or state.generation != generation:
                    return None
                data = chunk.astype('<i2', copy=False).tobytes()
                pcm.extend(data)
                if realtime:
                    realtime.append(data)
                voiced = vad.is_speech(data, RATE)
                finished = endpoint.update(voiced, len(chunk))
                now = time.monotonic()
                if voiced and now >= next_direction:
                    direction = dev.direction
                    emit({'status': 'speech_direction', 'native_doa_deg': direction,
                          'utterance_id': utterance_id})
                    if led:
                        led.set_listen(direction)
                    next_direction = now + .25
                reason = 'silence' if finished else None
                if not reason and len(pcm) >= round(args.max_seconds * RATE) * 2:
                    reason = 'maximum_duration'
                if not reason and now >= deadline:
                    reason = 'time_limit'
                if reason:
                    return {'pcm': bytes(pcm), 'doa_deg': direction,
                            'utterance_id': utterance_id, 'reason': reason,
                            'silence_seconds': endpoint.silent_samples / RATE,
                            'generation': generation}
    finally:
        # Stream has closed before a playback-pause acknowledgment can be sent.
        state.capture_closed.set()


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


async def listen(args, dev):
    device = select_input(args.device)
    output = Path(args.wav).resolve()
    if output.exists():
        raise RuntimeError(f'Recording already exists: {output}; choose another --wav path.')
    state = Controls(paused=args.control_stdin)
    controller = asyncio.create_task(read_controls(state)) if args.control_stdin else None
    led = None
    realtime = None
    try:
        if not args.no_led:
            from respeaker_led import LEDController
            led = LEDController(brightness=20)
        transcriber = None
        if not args.no_stt:
            emit({'status': 'transcription_loading', 'model': args.stt_model})
            if args.stt_model == 'gpt-live-transcribe':
                from respeaker_realtime import RealtimeTranscriber
                realtime = await RealtimeTranscriber.open(emit, args.stt_model, args.language)
            else:
                from respeaker_stt import Transcriber
                transcriber = await asyncio.to_thread(Transcriber, args.stt_model, args.language, getattr(args, 'stt_prompt', None))
            emit({'status': 'transcription_ready', 'model': args.stt_model,
                  'backend': 'realtime' if realtime else 'local'})
        emit({'status': 'listening', 'input': sd.query_devices(device, 'input')['name'],
              'trigger': 'hardware_vad', 'silence_seconds': args.silence_seconds})
        deadline = time.monotonic() + args.seconds if args.seconds else math.inf
        utterance_id = clip_count = 0
        while not state.stopping and time.monotonic() < deadline:
            check_controller(controller)
            if led:
                led.off() if state.paused else led.set_state(led.states.IDLE)
            if state.paused or time.monotonic() < state.resume_at:
                await asyncio.sleep(.05)
                continue
            # Firmware alone triggers capture. No idle InputStream or pre-roll.
            if not dev.is_voice():
                await asyncio.sleep(.05)
                continue
            utterance_id += 1
            event = await record_utterance(args, dev, device, state, led, utterance_id, deadline, controller, realtime)
            if event is None:
                if realtime:
                    # Drop unfinished remote audio and its context before accepting a new turn.
                    await realtime.close()
                    if state.stopping:
                        break
                    realtime = await RealtimeTranscriber.open(emit, args.stt_model, args.language)
                continue
            clip_count += 1
            path = save_clip(output, clip_count, event)
            if transcriber or realtime:
                if led:
                    led.set_state(led.states.THINK)
                result = await realtime.finish() if realtime else await asyncio.to_thread(transcriber.transcribe, path)
                check_controller(controller)
                if not state.stopping and not state.paused and state.generation == event['generation']:
                    final_fields = {}
                    if getattr(args, 'single_utterance', False):
                        state.paused = True
                        final_fields['paused_after'] = True
                    emit({'status': 'transcript_final', 'item_id': f'local-{utterance_id}',
                          'utterance_id': utterance_id, 'wav': str(path), 'doa_deg': event['doa_deg'],
                          **result, **final_fields})
            if args.once:
                break
    finally:
        if realtime:
            await realtime.close()
        if controller:
            controller.cancel()
            await asyncio.gather(controller, return_exceptions=True)
        if led:
            led.off()


def run(args, dev):
    if args.command == 'doctor':
        emit({'control_version_byte': dev.version, 'usb_device_revision': hex(dev.dev.bcdDevice),
              'parameters': {name: dev.read(name) for name in PARAMETERS}})
    else:
        asyncio.run(listen(args, dev))


def main():
    global DIAGNOSTICS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='listen', choices=['listen', 'doctor'])
    parser.add_argument('--device', help='PortAudio input index/name; auto-detect ReSpeaker by default')
    parser.add_argument('--seconds', type=float, default=0, help='Limit session duration; 0 waits indefinitely')
    parser.add_argument('--once', action='store_true', help='Exit after one utterance')
    parser.add_argument('--silence-seconds', type=float, default=2.0)
    parser.add_argument('--max-seconds', type=float, default=30, help='Maximum length of one utterance')
    parser.add_argument('--wav', default=str(ROOT / 'test-output' / f'respeaker-{time.time_ns()}.wav'))
    parser.add_argument('--no-stt', action='store_true', help='Save utterances without transcription')
    parser.add_argument('--no-led', action='store_true', help='Leave the LED ring alone')
    parser.add_argument('--diagnostics', action='store_true', help='Print microphone events as JSON')
    parser.add_argument('--single-utterance', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--control-stdin', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--stt-model', default='gpt-live-transcribe', help='Realtime gpt-live-transcribe, or a local Whisper model/directory')
    parser.add_argument('--stt-prompt', help='Optional local Whisper vocabulary hint (not sent to Realtime).')
    parser.add_argument('--language', default='en', help='Language code, or auto to detect it')
    args = parser.parse_args()
    DIAGNOSTICS = args.diagnostics or args.command != 'listen'
    if (not all(math.isfinite(n) for n in (args.seconds, args.silence_seconds, args.max_seconds))
            or args.seconds < 0 or args.silence_seconds <= 0 or args.max_seconds <= args.silence_seconds):
        parser.error('Seconds must be finite: session >= 0; silence > 0; maximum > silence.')
    dev = None
    try:
        dev = find()
        if dev is None:
            raise DeviceUnavailable('ReSpeaker 2886:0018 is not connected.')
        run(args, dev)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        event = dict(status='microphone_error', reconnectable=isinstance(error, DeviceUnavailable) or isinstance(error, USBError) and error.errno == 19)
        if isinstance(error, RealtimeUnavailable):
            event.update(category='realtime_unavailable', message=str(error), retryable=error.retryable)
        emit(event)
        print('ReSpeaker error: ' + str(error), file=sys.stderr)
        return 1
    finally:
        if dev is not None:
            dev.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
