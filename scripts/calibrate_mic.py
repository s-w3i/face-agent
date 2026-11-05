#!/usr/bin/env python3
"""Guided microphone calibration against the robot's own fan noise.

Run it yourself in a terminal next to the robot:

    cd ~/face-agent && .venv/bin/python scripts/calibrate_mic.py

It stops the voice agent (so nothing answers you mid-test), then runs a few rounds.
Each round tries one set of ReSpeaker settings:

    RED LED   = stay completely quiet (fan noise only)
    GREEN LED = say the sentence shown on screen, at your normal volume

Afterwards it transcribes your sentences with local Whisper (nothing goes to OpenAI),
picks the settings with no false triggers that caught every sentence most accurately,
saves them for the voice worker, and starts the voice agent again.
"""
import argparse
import json
import math
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import respeaker_profile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'test-output' / 'calibration'
RATE = 16000
FRAME = 320
SERVICE = 'face-agent.service'
SENTENCES = (
    'Pick up the soda can.',
    'Go to the kitchen and wait there.',
    'What time is it right now?',
    'Turn left and move forward two meters.',
    'Can you hear me clearly?',
)
FACTORY_NS = {'HPFONOFF': 1, 'STATNOISEONOFF': 1, 'STATNOISEONOFF_SR': 1,
              'GAMMA_NS': 1.0, 'GAMMA_NS_SR': 1.0, 'MIN_NS': 0.15, 'MIN_NS_SR': 0.15}
STRONG_NS = {'HPFONOFF': 2, 'STATNOISEONOFF': 1, 'STATNOISEONOFF_SR': 1,
             'GAMMA_NS': 1.5, 'GAMMA_NS_SR': 1.5, 'MIN_NS': 0.1, 'MIN_NS_SR': 0.1}
ROUNDS = (  # Interleaved so tiredness or moving around doesn't favour one setting.
    ('factory-ns-vad5', {**FACTORY_NS, 'GAMMAVAD_SR': 5.0}),
    ('strong-ns-vad8', {**STRONG_NS, 'GAMMAVAD_SR': 8.0}),
    ('factory-ns-vad8', {**FACTORY_NS, 'GAMMAVAD_SR': 8.0}),
    ('strong-ns-vad5', {**STRONG_NS, 'GAMMAVAD_SR': 5.0}),
)
GET_READY, QUIET, SAY_EACH = 4, 30, 4.0
TRIGGER_GAP = 1.0  # Hardware-VAD hits closer than this are one trigger (one capture).
ENDPOINT_SECONDS = 0.8
GATE_DB = 8.0

# ---------------------------------------------------------------- terminal + LED


def paint(text, code):
    return f'\x1b[{code}m{text}\x1b[0m' if sys.stdout.isatty() else text


def banner(text, code):
    print('\n' + paint(f'  {text}  ', f'1;{code}'), flush=True)


class Ring:
    """LED ring colour per phase; works without the LED too."""

    def __init__(self, lock):
        self.lock = lock
        try:
            from respeaker_led import LEDController
            self.led = LEDController(brightness=24)
        except Exception:
            self.led = None

    def show(self, rgb):
        if self.led:
            with self.lock:
                try:
                    self.led._set_pixels([rgb] * 12)
                except Exception:
                    pass

    def off(self):
        self.show((0, 0, 0))


RED, GREEN, AMBER, OFF = (160, 0, 0), (0, 160, 0), (120, 60, 0), (0, 0, 0)

# ---------------------------------------------------------------- recording


class Recorder:
    """One continuous capture plus a 20 ms poll of the hardware VAD, both timestamped."""

    def __init__(self, sd, device, dev, lock):
        self.sd, self.device, self.dev, self.lock = sd, device, dev, lock
        available = int(sd.query_devices(device, 'input')['max_input_channels'])
        self.channels = max(1, min(available, 6))

    def __enter__(self):
        self.blocks, self.flags = [], []
        self.stop = threading.Event()
        self.stream = self.sd.InputStream(samplerate=RATE, channels=self.channels, dtype='int16',
                                          device=self.device, blocksize=FRAME, callback=self._audio)
        self.started = time.monotonic()
        self.stream.start()
        self.poller = threading.Thread(target=self._poll, daemon=True)
        self.poller.start()
        return self

    def _audio(self, data, frames, time_info, status):
        self.blocks.append(data.copy())

    def _poll(self):
        while not self.stop.is_set():
            at = time.monotonic() - self.started
            try:
                with self.lock:
                    value = self.dev.read('VOICEACTIVITY')
                self.flags.append((at, int(value)))
            except Exception:
                pass
            self.stop.wait(.02)

    def now(self):
        return time.monotonic() - self.started

    def __exit__(self, *error):
        self.stop.set()
        self.poller.join(1)
        self.stream.stop()
        self.stream.close()
        self.audio = np.concatenate(self.blocks) if self.blocks else np.zeros((0, self.channels), np.int16)


def countdown(seconds, label):
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        print(f'\r    {label} … {math.ceil(left):2d} s ', end='', flush=True)
        time.sleep(min(.2, left))
    print('\r' + ' ' * 60 + '\r', end='', flush=True)


def run_round(name, settings, number, total, dev, lock, ring, sd, device):
    with lock:
        for key, value in settings.items():
            dev.write(key, value)
    banner(f'ROUND {number} of {total}', '37;44')
    ring.show(AMBER)
    print('    Get ready. Next: stay quiet (red light).')
    countdown(GET_READY, 'starting in')
    with Recorder(sd, device, dev, lock) as recorder:
        ring.show(RED)
        banner('🔴  STAY QUIET — don\'t talk, don\'t move things, just let the fans run', '1;37;41')
        quiet = (recorder.now(), None)
        countdown(QUIET, 'quiet')
        quiet = (quiet[0], recorder.now())
        windows = []
        ring.show(GREEN)
        banner('🟢  TALK — say each sentence ONCE, at your normal volume, when it appears', '1;30;42')
        for index, sentence in enumerate(SENTENCES, 1):
            start = recorder.now()
            print(f'\n    {index}/{len(SENTENCES)}  SAY:  ' + paint(sentence, '1;32'), flush=True)
            countdown(SAY_EACH, 'then wait')
            windows.append(dict(sentence=sentence, start=start, end=recorder.now()))
        ring.show(OFF)
    banner('✔  round done — you can relax for a moment', '1;32')
    return dict(name=name, settings=settings, quiet=quiet, windows=windows, audio=recorder.audio,
                flags=recorder.flags, channels=recorder.channels)

# ---------------------------------------------------------------- analysis (pure, tested)


def frame_rms(samples):
    samples = np.asarray(samples, dtype=np.float64)
    usable = samples[:samples.size // FRAME * FRAME].reshape(-1, FRAME)
    return np.sqrt(np.mean(usable * usable, axis=1)) if usable.size else np.zeros(0)


def db(value):
    return round(float(20 * np.log10(max(float(value), 1e-3) / 32768)), 1)


def triggers(flags, start, end, gap=TRIGGER_GAP):
    """Times of distinct hardware-VAD triggers (rising edges, merged within `gap`)."""
    hits, last = [], None
    for at, value in flags:
        if start <= at < end and value:
            if last is None or at - last > gap:
                hits.append(at)
            last = at
    return hits


def words(text):
    return re.sub(r"[^a-z0-9' ]+", ' ', text.lower()).split()


def wer(reference, hypothesis):
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        previous, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (r != h))
    return row[len(hyp)] / max(len(ref), 1)


def speech_span(rms, floor, gate_db=10.0):
    """First/last 20 ms frame clearly above the floor (None if the window has no speech)."""
    loud = np.flatnonzero(rms > floor * 10 ** (gate_db / 20))
    return (int(loud[0]), int(loud[-1])) if loud.size else None


def premature_end(pcm, floor, gate_db, vad_mode=2):
    """Would the 0.8 s end-of-speech wait fire before the speaker finished this window?"""
    import webrtcvad
    vad = webrtcvad.Vad(vad_mode)
    rms = frame_rms(pcm)
    span = speech_span(rms, floor)
    if span is None:
        return None
    threshold = floor * 10 ** (gate_db / 20) if gate_db > 0 else 0
    silent, limit = 0, round(ENDPOINT_SECONDS * RATE / FRAME)
    data = np.asarray(pcm, dtype='<i2')
    for index in range(span[0], span[1] + 1):
        frame = data[index * FRAME:(index + 1) * FRAME].tobytes()
        voiced = vad.is_speech(frame, RATE) and rms[index] >= threshold
        silent = 0 if voiced else silent + 1
        if silent >= limit:
            return True
    return False


def analyse_round(result, transcribe=None):
    audio = result['audio'][:, 0]
    rate_frames = RATE / FRAME
    q0, q1 = result['quiet']
    quiet = audio[int(q0 * RATE):int(q1 * RATE)]
    floor = float(np.percentile(frame_rms(quiet), 15)) if quiet.size else 1.0
    false = triggers(result['flags'], q0 + 1, q1)  # First second: estimators settling.
    minutes = max((q1 - q0 - 1) / 60, 1e-6)
    sentences = []
    for window in result['windows']:
        pcm = audio[int(window['start'] * RATE):int(window['end'] * RATE)]
        rms = frame_rms(pcm)
        span = speech_span(rms, floor)
        onset = window['start'] + span[0] / rate_frames if span else None
        fired = triggers(result['flags'], window['start'], window['end'])
        entry = dict(sentence=window['sentence'], spoke=span is not None, triggered=bool(fired),
                     onset_latency=round(fired[0] - onset, 2) if fired and onset is not None else None,
                     peak_db=db(np.percentile(rms, 95)) if rms.size else None,
                     premature_plain=premature_end(pcm, floor, 0),
                     premature_gated=premature_end(pcm, floor, GATE_DB))
        if transcribe is not None:
            entry['heard'] = transcribe(pcm)
            entry['wer'] = round(wer(window['sentence'], entry['heard']), 2)
        sentences.append(entry)
    spoken = [s for s in sentences if s['spoke']]
    latencies = [s['onset_latency'] for s in spoken if s['onset_latency'] is not None]
    wers = [s['wer'] for s in spoken if 'wer' in s]
    return dict(name=result['name'], settings=result['settings'],
                floor_db=db(floor), false_triggers_per_min=round(len(false) / minutes, 2),
                false_trigger_times=[round(t - q0, 1) for t in false],
                spoken=len(spoken), triggered=sum(s['triggered'] for s in spoken),
                onset_latency_max=max(latencies) if latencies else None,
                wer=round(float(np.mean(wers)), 3) if wers else None,
                premature_plain=sum(bool(s['premature_plain']) for s in spoken),
                premature_gated=sum(bool(s['premature_gated']) for s in spoken),
                sentences=sentences)


def choose(rounds):
    """Fewest false triggers, then most sentences caught, then best accuracy, then fastest."""
    usable = [r for r in rounds if r['spoken'] >= len(SENTENCES) - 1]
    if not usable:
        return None
    return min(usable, key=lambda r: (r['false_triggers_per_min'] > 0, r['false_triggers_per_min'],
                                      -(r['triggered'] / max(r['spoken'], 1)), r['wer'] if r['wer'] is not None else 1,
                                      r['onset_latency_max'] if r['onset_latency_max'] is not None else 9))


def calibration_for(best, rounds):
    gate = GATE_DB if sum(r['premature_gated'] for r in rounds) <= sum(r['premature_plain'] for r in rounds) else 0.0
    latency = best['onset_latency_max'] or 0
    # The fan floor drifts with AGC between rounds; the lowest one measured with the same
    # audio settings keeps the end-of-speech gate from clipping quiet word endings.
    audio = {k: v for k, v in best['settings'].items() if k != 'GAMMAVAD_SR'}
    floor = min(r['floor_db'] for r in rounds
                if {k: v for k, v in r['settings'].items() if k != 'GAMMAVAD_SR'} == audio)
    return dict(time=time.strftime('%Y-%m-%dT%H:%M:%S'), chosen=best['name'],
                parameters=best['settings'], noise_floor_db=floor, energy_gate_db=gate,
                preroll_seconds=round(min(max(0.3, latency + 0.25), 1.0), 2),
                summary={r['name']: {k: r[k] for k in ('floor_db', 'false_triggers_per_min', 'spoken', 'triggered',
                                                       'onset_latency_max', 'wer', 'premature_plain', 'premature_gated')}
                         for r in rounds})

# ---------------------------------------------------------------- main


def service(action):
    subprocess.run(['systemctl', '--user', action, SERVICE], check=False)


def wait_for_worker_exit(timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if subprocess.run(['pgrep', '-f', 'scripts/respeaker.py --diagnostics'], capture_output=True).returncode:
            return True
        time.sleep(.5)
    return False


def make_transcriber():
    from respeaker_stt import Transcriber
    import tempfile
    import wave
    engine = Transcriber('base.en', 'en', cpu_threads=4, beam_size=5)

    def transcribe(pcm):
        with tempfile.NamedTemporaryFile(suffix='.wav') as file:
            with wave.open(file.name, 'wb') as out:
                out.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                out.writeframes(np.asarray(pcm, dtype='<i2').tobytes())
            return engine.transcribe(file.name)['text']
    return transcribe


def table(rounds, best):
    print('\n' + paint('  setting            fan false/min  caught  fastest  accuracy  cut-off(plain/gated)', '1'))
    for r in rounds:
        mark = paint(' ◀ chosen', '1;32') if best and r['name'] == best['name'] else ''
        accuracy = f"{100 * (1 - r['wer']):5.0f}%" if r['wer'] is not None else '   n/a'
        latency = f"{r['onset_latency_max']:.2f}s" if r['onset_latency_max'] is not None else '  n/a'
        print(f"  {r['name']:18s} {r['false_triggers_per_min']:13.1f}  {r['triggered']}/{r['spoken']}     "
              f"{latency:>6s}  {accuracy:>8s}  {r['premature_plain']}/{r['premature_gated']}{mark}")


def main():
    try:
        return calibrate()
    finally:
        if '--no-restart' not in sys.argv:
            print('Starting the voice agent…', flush=True)
            service('start')


def calibrate():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--no-restart', action='store_true', help='Leave the voice agent stopped afterwards')
    parser.add_argument('--no-save', action='store_true', help='Measure only; keep the current calibration')
    args = parser.parse_args()

    import sounddevice as sd
    from respeaker import select_input
    from respeaker_vendor.tuning import find, PARAMETERS

    print(paint('\nKuro microphone calibration', '1') + '  (about 4 minutes; nothing is sent to OpenAI)\n')
    print('  • Stand where you normally talk to the robot (under 1 m).')
    print('  • ' + paint('RED light', '1;31') + ' = stay completely quiet.  ' + paint('GREEN light', '1;32')
          + ' = say the sentence on screen, once, normal volume.')
    print('  • Don\'t say the robot\'s name. The voice agent is stopped while this runs.\n')
    print('Stopping the voice agent…', flush=True)
    service('stop')
    if not wait_for_worker_exit():
        print('The microphone worker is still running; stop it and try again.')
        return 1
    dev = find()
    if dev is None:
        print('ReSpeaker not found.')
        return 1
    lock = threading.Lock()
    ring = Ring(lock)
    names = sorted({key for _, settings in ROUNDS for key in settings})
    original = {name: dev.read(name) for name in names}
    results = []
    try:
        input(paint('Press Enter when you are in place and ready… ', '1'))
        device = select_input(None)
        for number, (name, settings) in enumerate(ROUNDS, 1):
            results.append(run_round(name, settings, number, len(ROUNDS), dev, lock, ring, sd, device))
        banner('All recordings done — you can talk freely now. Analysing (about a minute)…', '1;37;44')
    except KeyboardInterrupt:
        print('\nStopped early.')
    finally:
        with lock:
            for name, value in original.items():
                dev.write(name, value)
        ring.off()

    if not results:
        return 1
    stamp = time.strftime('%Y%m%d-%H%M%S')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    import wave
    for result in results:
        with wave.open(str(OUTPUT / f"{stamp}-{result['name']}.wav"), 'wb') as out:
            out.setparams((result['channels'], 2, RATE, 0, 'NONE', 'not compressed'))
            out.writeframes(np.ascontiguousarray(result['audio'], dtype='<i2').tobytes())
    transcribe = make_transcriber()
    rounds = []
    for result in results:
        print(f"  analysing {result['name']}…", flush=True)
        rounds.append(analyse_round(result, transcribe))
    best = choose(rounds)
    table(rounds, best)
    for r in rounds:
        print(paint(f"\n  {r['name']}", '1') + (f"  (false triggers at {r['false_trigger_times']} s)" if r['false_trigger_times'] else ''))
        for s in r['sentences']:
            flag = '' if s['triggered'] else paint('  NOT TRIGGERED', '31')
            print(f"    said: {s['sentence']:40s} heard: {s.get('heard', '')!r}{flag}")
    report = dict(time=stamp, rounds=rounds)
    if best:
        calibration = calibration_for(best, rounds)
        report['calibration'] = calibration
        if not args.no_save:
            path = respeaker_profile.calibration_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(calibration, indent=2))
            print(paint(f"\n  Saved {best['name']} to {path}", '1;32'))
    else:
        print(paint('\n  Not enough speech was recorded to choose; nothing saved. Run it again.', '1;31'))
    (OUTPUT / f'{stamp}.json').write_text(json.dumps(report, indent=2, default=str))
    print(f'  Full report: {OUTPUT / f"{stamp}.json"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
