"""Measure background noise (fans) and how the ReSpeaker's voice detection reacts to it.

Records the processed channel 0 and the raw microphones while polling the XVF3000's
hardware VAD (VOICEACTIVITY, the gate for follow-up commands) every 20 ms, then reports
the noise floor, where the noise sits in frequency, and how often each VAD fires.
Everything stays local: nothing is sent to OpenAI.

    systemctl --user stop face-agent.service
    .venv/bin/python scripts/respeaker.py doctor --noise --label quiet           # nobody talking
    .venv/bin/python scripts/respeaker.py doctor --noise --label speech          # talk normally at ~0.7 m
    .venv/bin/python scripts/respeaker.py doctor --noise --label quiet-hpf --set HPFONOFF=2
    .venv/bin/python scripts/respeaker.py doctor --noise --compare
    systemctl --user start face-agent.service

`--set` values are written to the chip's RAM for this run only and restored afterwards.
"""
import json
import threading
import time
from pathlib import Path

import numpy as np

from respeaker_aec import PROCESSED, RAW, dbfs, save

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'test-output' / 'noise'
FRAME = 320  # 20 ms at 16 kHz, matching the capture pipeline.
BANDS = ((0, 125), (125, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000))
POLLED = ('VOICEACTIVITY', 'SPEECHDETECTED')


def parse_settings(pairs, parameters):
    """['HPFONOFF=2', ...] -> {'HPFONOFF': 2.0}; only writable parameters."""
    settings = {}
    for pair in pairs or ():
        name, sep, value = pair.partition('=')
        name = name.strip().upper()
        if not sep or name not in parameters:
            raise ValueError(f'Unknown ReSpeaker parameter in --set {pair!r}.')
        if parameters[name][5] != 'rw':
            raise ValueError(f'{name} is read-only.')
        number = float(value)
        low, high = parameters[name][4], parameters[name][3]
        if not low <= number <= high:
            raise ValueError(f'{name}={number} is outside {low}..{high}.')
        settings[name] = number
    return settings


def frame_rms(samples):
    samples = np.asarray(samples, dtype=np.float64)
    usable = samples[:samples.size // FRAME * FRAME].reshape(-1, FRAME)
    return np.sqrt(np.mean(usable * usable, axis=1)) if usable.size else np.zeros(0)


def to_db(value):
    return round(float(20 * np.log10(max(float(value), 1e-3) / 32768)), 1)


def band_levels(samples, rate):
    """dB (relative, per-bin mean power) of each band, from 20 ms Hann frames."""
    frames = np.asarray(samples, dtype=np.float64)
    frames = frames[:frames.size // FRAME * FRAME].reshape(-1, FRAME)
    if not frames.size:
        return {}
    power = np.mean(np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(FRAME, 1 / rate)
    return {f'{low}-{high}': round(float(10 * np.log10(power[(freqs >= low) & (freqs < high)].mean() + 1e-9)), 1)
            for low, high in BANDS}


def tonal_peaks(samples, rate, count=5, size=4096):
    """Strongest narrow peaks (fan hum, blade-pass tones) over a smoothed spectrum."""
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size < size:
        return []
    hop = size // 2
    windows = np.stack([samples[i:i + size] for i in range(0, samples.size - size + 1, hop)])
    power = np.mean(np.abs(np.fft.rfft(windows * np.hanning(size), axis=1)) ** 2, axis=0)
    level = 10 * np.log10(power + 1e-9)
    freqs = np.fft.rfftfreq(size, 1 / rate)
    kernel = 31
    padded = np.pad(level, kernel // 2, mode='edge')
    smooth = np.array([np.median(padded[i:i + kernel]) for i in range(level.size)])
    prominence = level - smooth
    peaks = []
    for i in np.argsort(prominence)[::-1]:
        if freqs[i] < 40 or prominence[i] < 6:
            continue
        if any(abs(freqs[i] - p['hz']) < 30 for p in peaks):
            continue
        peaks.append(dict(hz=round(float(freqs[i])), above_neighbours_db=round(float(prominence[i]), 1)))
        if len(peaks) == count:
            break
    return peaks


def webrtc_voiced(samples, rate, mode):
    import webrtcvad
    vad = webrtcvad.Vad(mode)
    pcm = np.asarray(samples, dtype='<i2')
    frames = pcm[:pcm.size // FRAME * FRAME].reshape(-1, FRAME)
    if not len(frames):
        return 0.0
    return round(100 * sum(vad.is_speech(frame.tobytes(), rate) for frame in frames) / len(frames), 1)


def analyse(audio, flags, rate, started=None):
    """Summary of one recording: floors, spectrum, and how often each VAD fires."""
    channels = audio.shape[1]
    ch0 = audio[:, PROCESSED]
    rms0 = frame_rms(ch0)
    floor0 = np.percentile(rms0, 10) if rms0.size else 0
    raw = [c for c in RAW if c < channels]
    result = dict(channels=channels, seconds=round(audio.shape[0] / rate, 1),
                  ch0_db=dbfs(ch0), ch0_floor_db=to_db(floor0),
                  ch0_p90_db=to_db(np.percentile(rms0, 90) if rms0.size else 0),
                  ch0_bands_db=band_levels(ch0, rate))
    if raw:
        mix = audio[:, raw].astype(np.float64).mean(axis=1)
        rms_raw = frame_rms(mix)
        result.update(raw_mics_db=dbfs(audio[:, raw]),
                      raw_floor_db=to_db(np.percentile(rms_raw, 10) if rms_raw.size else 0),
                      raw_bands_db=band_levels(mix, rate), raw_tonal_peaks=tonal_peaks(mix, rate))
    result['ch0_tonal_peaks'] = tonal_peaks(ch0, rate)
    for name in POLLED:
        values = [value for _, polled in flags for key, value in polled.items() if key == name]
        result[f'{name.lower()}_percent'] = round(100 * sum(1 for v in values if v) / len(values), 1) if values else None
    if rms0.size and flags and started is not None:
        # Split hardware-VAD hits by whether channel 0 was clearly speech (10 dB over the floor)
        # at that moment, so pauses in a speech run don't count against it.
        loud = rms0 > floor0 * 10 ** (10 / 20)
        hits = {True: [], False: []}
        for at, polled in flags:
            index = int((at - started) / (FRAME / rate))
            if 'VOICEACTIVITY' in polled and 0 <= index < loud.size:
                hits[bool(loud[index])].append(bool(polled['VOICEACTIVITY']))
        result['voiceactivity_in_speech_percent'] = round(100 * np.mean(hits[True]), 1) if hits[True] else None
        result['voiceactivity_in_noise_percent'] = round(100 * np.mean(hits[False]), 1) if hits[False] else None
        result['speech_seconds'] = round(float(loud.sum()) * FRAME / rate, 1)
    result['webrtc_voiced_percent'] = {f'mode{mode}': webrtc_voiced(ch0, rate, mode) for mode in (2, 3)}
    result['ch0_frames_above_floor_percent'] = {f'{gate}db': round(100 * float(np.mean(rms0 > floor0 * 10 ** (gate / 20))), 1)
                                                for gate in (6, 8, 10)} if rms0.size else {}
    return result


def poll(dev, stop, flags, interval=.02):
    while not stop.is_set():
        started = time.monotonic()
        try:
            flags.append((started, {name: dev.read(name) for name in POLLED}))
        except Exception as error:  # A failed poll must not abort the recording.
            flags.append((started, {'error': repr(error)}))
        stop.wait(max(0.0, interval - (time.monotonic() - started)))


def measure(sd, device, rate, dev, parameters, seconds=20.0, label='noise', settings=None):
    """Record `seconds`, optionally with temporary DSP `settings`; save WAV + JSON."""
    available = int(sd.query_devices(device, 'input')['max_input_channels'])
    channels = max(1, min(available, 6))
    settings = settings or {}
    previous = {name: dev.read(name) for name in settings}
    try:
        for name, value in settings.items():
            dev.write(name, value)
        if settings:
            time.sleep(1.0)  # Let the noise estimators settle on the new settings.
        current = {name: dev.read(name) for name in parameters if parameters[name][5] == 'rw'}
        stop, flags = threading.Event(), []
        poller = threading.Thread(target=poll, args=(dev, stop, flags), daemon=True)
        poller.start()
        try:
            started = time.monotonic()
            audio = sd.rec(round(seconds * rate), samplerate=rate, channels=channels, dtype='int16', device=device)
            sd.wait()
        finally:
            stop.set()
            poller.join(1)
    finally:
        for name, value in previous.items():
            dev.write(name, value)
    result = analyse(np.asarray(audio), flags, rate, started)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    base = OUTPUT / f'{label}-{stamp}'
    save(base.with_suffix('.wav'), np.asarray(audio), channels, rate)
    result.update(label=label, time=stamp, settings=settings, parameters=current,
                  wav=str(base.with_suffix('.wav')), poll_errors=sum(1 for _, f in flags if 'error' in f))
    base.with_suffix('.json').write_text(json.dumps(result, indent=2))
    return result


def compare(directory=OUTPUT):
    """Latest run per label, one row each, for comparing settings."""
    runs = {}
    for path in sorted(Path(directory).glob('*.json')):
        run = json.loads(path.read_text())
        runs[run['label']] = run
    keys = ('time', 'settings', 'ch0_floor_db', 'ch0_p90_db', 'raw_floor_db', 'voiceactivity_percent',
            'voiceactivity_in_speech_percent', 'voiceactivity_in_noise_percent', 'speech_seconds',
            'speechdetected_percent', 'webrtc_voiced_percent', 'ch0_frames_above_floor_percent')
    return dict(runs={label: {key: run.get(key) for key in keys} for label, run in runs.items()})
