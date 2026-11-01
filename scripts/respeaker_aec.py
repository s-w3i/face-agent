"""Measure ReSpeaker echo suppression: record all channels while playing through a sink.

The XVF3000 can only cancel the robot's own voice when playback goes through the
ReSpeaker's 3.5 mm output, because that is its AEC reference. This check records
the processed channel 0 and the raw microphones at the same time, so one run shows
how much of the playback the processed channel removes. Rerun it for each sink:

    .venv/bin/python scripts/respeaker.py doctor --aec --sink jieli        # today
    .venv/bin/python scripts/respeaker.py doctor --aec --sink respeaker    # once the jack is connected
    .venv/bin/python scripts/respeaker.py doctor --aec --compare           # table of saved runs

Stop the voice stack first (systemctl --user stop face-agent.service): the
worker and this check cannot both open the ReSpeaker capture device.
"""
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'test-output' / 'aec'
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent'
SINK_ALIASES = {'respeaker': 'respeaker', 'jieli': 'jieli', 'hdmi': 'hdmi'}
# 6-channel firmware: 0 processed (beamformed + AEC), 1-4 raw microphones, 5 playback reference.
PROCESSED, RAW, REFERENCE = 0, (1, 2, 3, 4), 5


def dbfs(samples):
    samples = np.asarray(samples, dtype=np.float64)
    rms = math.sqrt(float(np.mean(samples * samples))) if samples.size else 0.0
    return round(20 * math.log10(rms / 32768), 1) if rms else -120.0


def sinks():
    lines = subprocess.run(['pactl', 'list', 'short', 'sinks'], capture_output=True, text=True,
                           check=True).stdout.splitlines()
    return [line.split('\t')[1] for line in lines if '\t' in line]


def resolve_sink(name):
    """Accept a full sink name, an alias (respeaker/jieli/hdmi), or None for the default sink."""
    if not name:
        return subprocess.run(['pactl', 'get-default-sink'], capture_output=True, text=True,
                              check=True).stdout.strip()
    available = sinks()
    if name in available:
        return name
    matches = [sink for sink in available if SINK_ALIASES.get(name.lower(), name.lower()) in sink.lower()]
    if len(matches) != 1:
        raise RuntimeError(f'Sink {name!r} matched {len(matches)} of: ' + ', '.join(available))
    return matches[0]


def playback_clip(path=None):
    """A speech clip is closer to the robot's real echo than a tone; fall back to pink noise."""
    if path:
        return Path(path)
    cached = sorted((CACHE / 'wake-ack').glob('*.wav')) + sorted((CACHE / 'startup-ready').glob('*.wav'))
    if cached:
        return cached[0]
    rng = np.random.default_rng(0)
    white = rng.standard_normal(48000 * 3)
    spectrum = np.fft.rfft(white) / np.sqrt(np.arange(1, white.size // 2 + 2))
    pink = np.fft.irfft(spectrum, white.size)
    pcm = (pink / np.max(np.abs(pink)) * 0.3 * 32767).astype('<i2')
    path = Path(tempfile.gettempdir()) / 'respeaker-aec-pink.wav'
    with wave.open(str(path), 'wb') as file:
        file.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        file.writeframes(pcm.tobytes())
    return path


def analyse(quiet, playback, channels):
    """Per-channel dBFS for both phases and the derived echo-suppression figures."""
    result = dict(channels=channels,
                  quiet_db=[dbfs(quiet[:, c]) for c in range(channels)],
                  playback_db=[dbfs(playback[:, c]) for c in range(channels)])
    result['ch0_quiet_db'] = result['quiet_db'][PROCESSED]
    result['ch0_playback_db'] = result['playback_db'][PROCESSED]
    result['ch0_echo_above_floor_db'] = round(result['ch0_playback_db'] - result['ch0_quiet_db'], 1)
    if channels >= 6:
        raw = dbfs(playback[:, list(RAW)])
        result['raw_mics_quiet_db'] = dbfs(quiet[:, list(RAW)])
        result['raw_mics_playback_db'] = raw
        result['raw_echo_above_floor_db'] = round(raw - result['raw_mics_quiet_db'], 1)
        # Channel 0 has AGC gain, so compare how far each rises above its own quiet floor:
        # positive means the processed channel hears less of the playback than the raw mics.
        result['echo_suppression_db'] = round(result['raw_echo_above_floor_db'] - result['ch0_echo_above_floor_db'], 1)
        result['reference_playback_db'] = result['playback_db'][REFERENCE]
        # Channel 5 carries the playback the XVF3000 receives; silence here means no AEC reference.
        result['aec_reference_present'] = result['reference_playback_db'] > result['quiet_db'][REFERENCE] + 10
    return result


def save(path, pcm, channels, rate):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as file:
        file.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        file.writeframes(np.ascontiguousarray(pcm, dtype='<i2').tobytes())


def measure(sd, device, rate, sink=None, seconds=10.0, quiet_seconds=3.0, clip=None, label=None):
    """Record a quiet floor, then `seconds` while looping `clip` through `sink`."""
    channels = int(sd.query_devices(device, 'input')['max_input_channels'])
    sink = resolve_sink(sink)
    clip = playback_clip(clip)
    quiet = sd.rec(round(quiet_seconds * rate), samplerate=rate, channels=channels, dtype='int16', device=device)
    sd.wait()
    player = subprocess.Popen(['bash', '-c', 'while paplay --device="$0" "$1"; do :; done', sink, str(clip)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(.3)  # Let the sink resume from suspend before recording.
        playback = sd.rec(round(seconds * rate), samplerate=rate, channels=channels, dtype='int16', device=device)
        sd.wait()
    finally:
        player.terminate()
        player.wait()
    result = analyse(np.asarray(quiet), np.asarray(playback), channels)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    short = next((alias for alias in SINK_ALIASES if alias in sink.lower()), 'sink')
    base = OUTPUT / f"{label or short}-{stamp}"
    save(base.with_suffix('.wav'), np.concatenate([quiet, playback]), channels, rate)
    result.update(sink=sink, label=label or short, clip=str(clip), seconds=seconds,
                  quiet_seconds=quiet_seconds, wav=str(base.with_suffix('.wav')), time=stamp)
    base.with_suffix('.json').write_text(json.dumps(result, indent=2))
    return result


def compare(directory=OUTPUT):
    """Latest run per label, plus the channel-0 drop between the Jieli and ReSpeaker sinks."""
    runs = {}
    for path in sorted(Path(directory).glob('*.json')):
        run = json.loads(path.read_text())
        runs[run['label']] = run
    rows = {label: {key: run.get(key) for key in ('time', 'ch0_quiet_db', 'ch0_playback_db',
                                                   'ch0_echo_above_floor_db', 'raw_echo_above_floor_db', 'echo_suppression_db',
                                                   'aec_reference_present')}
            for label, run in runs.items()}
    result = dict(runs=rows)
    if 'jieli' in runs and 'respeaker' in runs:
        result['ch0_reduction_db'] = round(runs['jieli']['ch0_playback_db'] - runs['respeaker']['ch0_playback_db'], 1)
        result['target_met'] = result['ch0_reduction_db'] >= 15
    return result
