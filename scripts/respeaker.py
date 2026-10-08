#!/usr/bin/env python3
"""Calibrate background noise, record channel 0, and display live transcription."""
import argparse
import asyncio
import array
from contextlib import contextmanager, ExitStack
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import wave

from respeaker_vendor.tuning import find, PARAMETERS
from usb.core import USBError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'respeaker-config.json'
SOURCE = None
DIAGNOSTICS = False
PARTIALS = {}


class DeviceUnavailable(RuntimeError):
    """The USB microphone or its audio stream disappeared."""
# Balanced onboard defaults preserve quiet speech; AGC supports distant speakers.
PROFILE = {'HPFONOFF': 1, 'AGCONOFF': 1, 'AGCMAXGAIN': 31.6,
           'AGCDESIREDLEVEL': .005, 'AGCTIME': .98,
           'CNIONOFF': 1, 'STATNOISEONOFF': 1, 'NONSTATNOISEONOFF': 1,
           'STATNOISEONOFF_SR': 1, 'NONSTATNOISEONOFF_SR': 1,
           'FREEZEONOFF': 0, 'AECFREEZEONOFF': 0, 'ECHOONOFF': 1,
           'RT60ONOFF': 1, 'GAMMAVAD_SR': 1.5,
           'GAMMA_NS': 1, 'MIN_NS': .15,
           'GAMMA_NN': 1.1, 'MIN_NN': .3,
           'GAMMA_NS_SR': 1, 'MIN_NS_SR': .15,
           'GAMMA_NN_SR': 1.1, 'MIN_NN_SR': .3}
BLOCK_BYTES = 1600  # 50 ms of mono PCM16LE at 16 kHz


def distance(a, b):
    return abs((a - b + 180) % 360 - 180)


def level_dbfs(pcm):
    values = array.array('h', pcm)
    if sys.byteorder != 'little':
        values.byteswap()
    rms = math.sqrt(sum(v*v for v in values)/len(values))/32768
    return 20*math.log10(max(rms, 1e-9))


def noise_threshold(levels):
    if not levels:
        raise RuntimeError('No audio received for background calibration.')
    ordered = sorted(levels)
    p95 = ordered[min(len(ordered)-1, int(len(ordered)*.95))]
    return p95, p95 + 3


def accepts(voice, level, threshold, angle, target, width):
    # ponytail: dominant-direction gating cannot separate overlapping speakers.
    return voice and level >= threshold and (target is None or distance(angle, target) <= width)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def emit(value):
    if DIAGNOSTICS:
        print(json.dumps(value), flush=True)
        return
    status = value.get('status')
    if status == 'transcript_partial' and sys.stdout.isatty():
        ident = value['item_id']
        PARTIALS[ident] = PARTIALS.get(ident, '') + value['delta']
        print('\r\x1b[2K' + PARTIALS[ident].strip(), end='', flush=True)
    elif status == 'transcript_final':
        PARTIALS.pop(value['item_id'], None)
        if sys.stdout.isatty():
            print('\r\x1b[2K', end='')
        print(value['text'].strip(), flush=True)
    elif status in ('warning', 'transcription_incomplete'):
        print(value.get('message', 'Transcription incomplete; local recording was retained.'),
              file=sys.stderr, flush=True)


def apply_profile(dev):
    backup = ROOT / 'respeaker-backup.json'
    if not backup.exists():
        save(backup, {n: dev.read(n) for n, p in PARAMETERS.items() if p[5] == 'rw'})
    for name, value in PROFILE.items():
        dev.write(name, value)
        actual = dev.read(name)
        if not math.isclose(actual, value, rel_tol=1e-5, abs_tol=.01 if name == 'AGCTIME' else 1e-6):
            raise RuntimeError(f'{name} readback mismatch: {actual} != {value}')


@contextmanager
def capture(source):
    process = subprocess.Popen(['parec', '--device=' + source, '--raw',
                                '--format=s16le', '--rate=16000', '--channels=1',
                                '--latency-msec=40'], stdout=subprocess.PIPE)
    try:
        yield process.stdout
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def read_block(stream):
    pcm = stream.read(BLOCK_BYTES)
    if len(pcm) != BLOCK_BYTES:
        raise RuntimeError('Audio capture stopped or returned an incomplete block.')
    return pcm


def calibrate(stream, dev, seconds):
    emit({'status': 'calibrating_background', 'seconds': seconds,
          'instruction': 'Stay silent; leave normal fans and background noise running.'})
    # Warm the audio stream before measuring background levels.
    for _ in range(10):
        read_block(stream)
    levels, voiced = [], 0
    for _ in range(math.ceil(seconds * 20)):
        levels.append(level_dbfs(read_block(stream)))
        voiced += bool(dev.is_voice())
    p95, threshold = noise_threshold(levels)
    config = {'noise_p95_dbfs': p95, 'min_dbfs': threshold,
              'noise_median_dbfs': sorted(levels)[len(levels)//2],
              'background_vad_fraction': voiced/len(levels),
              'frames': len(levels), 'profile': PROFILE,
              'calibrated_at_unix': time.time()}
    save(CONFIG, config)
    emit({'status': 'background_calibrated', **config})
    if voiced / len(levels) > .25:
        emit({'status': 'warning', 'message': 'Background triggers speech detection. Music/vocals can attract native DOA; calibration does not identify your voice. Use --target for a known bearing or provide playback to the ReSpeaker for echo cancellation.'})
    if threshold > -15:
        emit({'status': 'warning', 'message': 'Background is loud; the gate may reject normal speech. Reduce noise and restart.'})
    return threshold


def audio_source(name=None):
    sources = json.loads(subprocess.check_output(['pactl', '-f', 'json', 'list', 'sources']))
    candidates = [s for s in sources if (s['name'] == name if name else 'alsa_input.usb-SEEED_ReSpeaker' in s['name'])]
    if not candidates:
        raise DeviceUnavailable('ReSpeaker audio input is unavailable.')
    if len(candidates) != 1:
        raise RuntimeError('Select the ReSpeaker input with --source; expected exactly one matching source.')
    return candidates[0]


def run(args, dev):
    if args.command == 'doctor':
        emit({'control_version_byte': dev.version, 'usb_device_revision': hex(dev.dev.bcdDevice),
              'parameters': {name: dev.read(name) for name in PARAMETERS}})
        return
    if args.command == 'restore':
        for name, value in json.loads((ROOT / 'respeaker-backup.json').read_text()).items():
            dev.write(name, value)
        emit({'status': 'original_tuning_restored'})
        return
    if args.command in ['listen', 'calibrate']:
        source = audio_source(args.source)
        args.source = source['name']
        if args.backend == 'odas':
            from respeaker_odas import run as run_odas
            asyncio.run(run_odas(args, dev, source))
            return
        if not args.no_stt:
            raise RuntimeError('Live transcription requires the default ODAS capture path; use --no-stt for legacy DSP.')
        if '1ch' not in str(source['sample_specification']):
            raise RuntimeError('Legacy DSP mode requires one-channel firmware; use the default ODAS mode with raw firmware.')
    apply_profile(dev)
    if args.command == 'tune':
        emit({'status': 'tuned', 'parameters': {n: dev.read(n) for n in PROFILE}})
        return
    with capture(args.source) as stream:
        threshold = calibrate(stream, dev, args.noise_seconds)
        if args.command == 'calibrate':
            return
        path = Path(args.wav)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Each run gets a unique default name; never silently overwrite a recording.
        if path.exists():
            raise RuntimeError(f'Recording already exists: {path}; choose a new --wav path.')
        with ExitStack() as stack:
            recording_file = stack.enter_context(path.open('xb'))
            wav = stack.enter_context(wave.open(recording_file, 'wb'))
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            pcm_output = stack.enter_context(open(args.pcm, 'wb', buffering=0)) if args.pcm else None
            emit({'status': 'recording', 'wav': str(path.resolve()),
                  'target_deg': args.target, 'half_width_deg': args.width,
                  'format': 'PCM16LE, mono, 16000 Hz'})
            frames = accepted = voiced = 0
            try:
                while not args.seconds or frames < math.ceil(args.seconds * 20):
                    pcm = read_block(stream)
                    voice = bool(dev.is_voice())
                    angle = dev.direction  # Native device angle; no coordinate calibration.
                    level = level_dbfs(pcm)
                    allow = accepts(voice, level, threshold, angle, args.target, args.width)
                    data = pcm if allow else bytes(len(pcm))
                    wav.writeframesraw(data)
                    if pcm_output:
                        pcm_output.write(data)
                    frames += 1
                    voiced += voice
                    accepted += allow
                    if frames % 4 == 1:
                        emit({'doa_deg': angle, 'doa_valid': allow, 'voice': voice,
                              'accepted': allow, 'rms_dbfs': round(level, 1),
                              'threshold_dbfs': round(threshold, 1)})
            finally:
                emit({'status': 'stopped', 'frames': frames, 'voice_frames': voiced,
                      'accepted_frames': accepted, 'duration_seconds': frames/20,
                      'wav': str(path.resolve())})


def main():
    global DIAGNOSTICS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='listen',
                        choices=['listen', 'doctor', 'tune', 'restore', 'calibrate'])
    parser.add_argument('--source', default=SOURCE)
    parser.add_argument('--backend', choices=['odas', 'dsp'], default='odas')
    parser.add_argument('--settle-seconds', type=float, default=10)
    parser.add_argument('--target', type=float, help='Optional native target angle, 0..359')
    parser.add_argument('--width', type=float, default=25, help='Accepted half-angle in degrees')
    parser.add_argument('--noise-seconds', type=float, default=8)
    parser.add_argument('--seconds', type=float, default=0, help='Recording duration; 0 runs until Ctrl+C')
    parser.add_argument('--wav', default=str(ROOT / 'test-output' / f'respeaker-{time.time_ns()}.wav'))
    parser.add_argument('--no-stt', action='store_true', help='Record without OpenAI transcription')
    parser.add_argument('--diagnostics', action='store_true', help='Print all diagnostic and transcription events as JSON')
    parser.add_argument('--control-stdin', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--stt-model', choices=['gpt-live-transcribe', 'gpt-transcribe'], default='gpt-live-transcribe')
    parser.add_argument('--stt-delay', choices=['minimal', 'low', 'medium', 'high', 'xhigh'], default='low')
    parser.add_argument('--language', help='Optional language hint, e.g. en, ms, zh')
    parser.add_argument('--pcm', help='PCM16LE file or existing FIFO for robot speech input')
    args = parser.parse_args()
    DIAGNOSTICS = args.diagnostics or args.command != 'listen'
    numbers = [args.width, args.seconds, args.noise_seconds, args.settle_seconds]
    if args.target is not None:
        numbers.append(args.target)
    if not all(math.isfinite(n) for n in numbers) or (args.target is not None and not 0 <= args.target < 360) or not 0 < args.width <= 180 or args.seconds < 0 or not 1 <= args.noise_seconds <= 60 or not 0 <= args.settle_seconds <= 60:
        parser.error('Target: 0..359; width: (0,180]; noise seconds: 1..60; recording seconds: >=0.')
    dev = None
    try:
        dev = find()
        if dev is None:
            raise DeviceUnavailable('ReSpeaker 2886:0018 is not connected.')
        run(args, dev)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        emit({'status': 'microphone_error',
              'reconnectable': isinstance(exc, DeviceUnavailable) or isinstance(exc, USBError) and exc.errno == 19})
        print('ReSpeaker error: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if dev is not None:
            dev.close()
    return 0


if __name__ == '__main__':
    # Imported capture/transcription modules share this script's output settings.
    sys.modules['respeaker'] = sys.modules[__name__]
    sys.exit(main())
