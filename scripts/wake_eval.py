#!/usr/bin/env python3
"""Record and evaluate wake-word clips offline (test-output/wake-eval/, gitignored).

Stop the voice stack first; recording opens the ReSpeaker's processed channel 0.

    .venv/bin/python scripts/wake_eval.py record-positive --count 20    # say "Hi Kuro" when prompted
    .venv/bin/python scripts/wake_eval.py record-negative --minutes 10  # TV/conversation, no wake phrase
    .venv/bin/python scripts/wake_eval.py evaluate                      # sweep thresholds
    .venv/bin/python scripts/wake_eval.py evaluate-rolling --negatives  # rolling Hailo engine, with TV noise

Acceptance (plans/phase-2): >= 19/20 positives and <= 1 false wake in 10 negative minutes.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wake_word as ww  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / 'test-output' / 'wake-eval'


def read_wav(path):
    with wave.open(str(path)) as file:
        rate, channels = file.getframerate(), file.getnchannels()
        pcm = np.frombuffer(file.readframes(file.getnframes()), '<i2')
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    if rate != ww.RATE:
        from scipy.signal import resample_poly
        pcm = resample_poly(pcm.astype(np.float32), ww.RATE, rate).astype(np.int16)
    return pcm


def write_wav(path, pcm):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as file:
        file.setparams((1, 2, ww.RATE, 0, 'NONE', 'not compressed'))
        file.writeframes(pcm.astype('<i2').tobytes())


def scores_for(model, pcm, lead_in=1.0):
    """Frame scores for a clip, after `lead_in` seconds of silence to settle the feature buffers."""
    model.reset()
    pcm = np.concatenate([np.zeros(round(lead_in * ww.RATE), np.int16), pcm, np.zeros(ww.RATE // 2, np.int16)])
    name = None
    scores = []
    for start in range(0, len(pcm) - ww.FRAME_SAMPLES + 1, ww.FRAME_SAMPLES):
        result = model.predict(pcm[start:start + ww.FRAME_SAMPLES])
        name = name or next(iter(result))
        scores.append(float(result[name]))
    return scores


def count_hits(scores, threshold, patience=2, refractory=1.5, frame_seconds=ww.FRAME_SAMPLES / ww.RATE):
    """Same rule as WakeDetector, replayed on precomputed scores."""
    hits = streak = 0
    last = -float('inf')
    for index, score in enumerate(scores):
        t = (index + 1) * frame_seconds
        streak = streak + 1 if score >= threshold else 0
        if streak >= patience:
            streak = 0
            if t - last >= refractory:
                hits += 1
                last = t
    return hits


def evaluate(args):
    model = ww.load_model(args.model)
    positives = sorted((EVAL / 'positive').glob('*.wav'))
    negatives = sorted((EVAL / 'negative').glob('*.wav'))
    if not positives and not negatives:
        raise SystemExit(f'No clips under {EVAL}; record them first.')
    pos_scores = [scores_for(model, read_wav(p)) for p in positives]
    neg_scores = [scores_for(model, read_wav(p), lead_in=0) for p in negatives]
    neg_minutes = sum(len(s) for s in neg_scores) * ww.FRAME_SAMPLES / ww.RATE / 60
    rows = []
    for threshold in args.thresholds:
        true = sum(1 for s in pos_scores if count_hits(s, threshold, args.patience) >= 1)
        false = sum(count_hits(s, threshold, args.patience) for s in neg_scores)
        rows.append(dict(threshold=threshold, true_wakes=true, positives=len(positives), false_wakes=false,
                         negative_minutes=round(neg_minutes, 1),
                         passes=bool(positives) and true >= len(positives) * .95 and false <= max(1, neg_minutes / 10)))
    result = dict(model=str(args.model), patience=args.patience, rows=rows,
                  positive_max_scores=[round(max(s), 3) for s in pos_scores],
                  negative_max_score=round(max((max(s) for s in neg_scores), default=0), 3))
    print('| threshold | true wakes | false wakes | negative min | passes |')
    print('| --- | --- | --- | --- | --- |')
    for row in rows:
        print(f"| {row['threshold']} | {row['true_wakes']}/{row['positives']} | {row['false_wakes']} | "
              f"{row['negative_minutes']} | {'yes' if row['passes'] else 'no'} |")
    out = EVAL / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=2))
    print(f'Saved {out}')


def noisy(clip, noise, snr_db, rng, lead_seconds=3.0, tail_seconds=2.0):
    """Room noise before, under and after `clip`, at `snr_db` against the clip's speech level.

    Returns (stream, clip start in seconds). snr_db None: digital silence around the clip;
    'room': unscaled `noise` before and after (clips recorded in that same noise).
    """
    lead, tail = round(lead_seconds * ww.RATE), round(tail_seconds * ww.RATE)
    if snr_db is None:
        return np.concatenate([np.zeros(lead, np.int16), clip, np.zeros(tail, np.int16)]), lead_seconds
    if snr_db == 'room':
        start = rng.integers(0, max(1, len(noise) - lead - tail))
        return np.concatenate([noise[start:start + lead], clip, noise[start + lead:start + lead + tail]]), lead_seconds
    clip = clip.astype(np.float32)
    frames = clip[:len(clip) // 320 * 320].reshape(-1, 320)
    loud = np.sort(np.sqrt((frames ** 2).mean(axis=1)))[-max(1, len(frames) // 4):]  # speech frames
    total = lead + len(clip) + tail
    start = rng.integers(0, max(1, len(noise) - total))
    part = noise[start:start + total].astype(np.float32)
    gain = loud.mean() / (np.sqrt((part ** 2).mean()) or 1.0) / 10 ** (snr_db / 20)
    stream = part * gain
    stream[lead:lead + len(clip)] += clip
    return np.clip(stream, -32768, 32767).astype(np.int16), lead_seconds


def evaluate_rolling(args):
    """Stream clips through the deployed rolling detector (VAD gate, windows, Hailo + CPU check)."""
    import rolling_wake as rw
    scorer = rw.HailoPhraseScorer(args.name)
    checks = []
    positives = sorted((EVAL / args.positive_set).glob('*.wav'))
    negatives = sorted((EVAL / args.negative_set).glob('*.wav'))
    noise = read_wav(args.noise) if args.noise else (read_wav(negatives[0]) if negatives else None)
    rng = np.random.default_rng(0)
    rows = []
    for snr in args.snr:
        caught, missed = 0, []
        for path in positives:
            stream, start = noisy(read_wav(path), noise, snr, rng)
            detector = rw.RollingWakeDetector(scorer, args.threshold)
            hits = detector.feed(stream.tobytes(), 0.0)
            checks.append(detector.verifications)
            if any(hit['continue_from'] > start for hit in hits):
                caught += 1
            else:
                missed.append(path.name)
        rows.append(dict(snr=snr, caught=caught, positives=len(positives), missed=missed))
        label = 'clean' if snr is None else snr if snr == 'room' else f'{snr:g} dB'
        print(f'SNR {label}: {caught}/{len(positives)}', flush=True)
    false = {}
    if args.negatives:
        for path in negatives:
            detector = rw.RollingWakeDetector(scorer, args.threshold)
            audio = read_wav(path)
            false[path.name] = [dict(t=round(hit['continue_from'], 1), score=hit['score'], verified=hit['verified'])
                                for hit in detector.feed(audio.tobytes(), 0.0)]
            minutes = len(audio) / ww.RATE / 60
            print(f'{path.name}: {detector.verifications / minutes:.1f} CPU checks/min '
                  f'({detector.verify_seconds / minutes:.1f} s/min)', flush=True)
            print(f'{path.name}: {len(false[path.name])} false wakes {false[path.name]}', flush=True)
    print(f'CPU checks on positives: {sum(checks)}')
    import hailo_stt
    hailo_stt.close_shared()  # Explicitly: interpreter teardown with a live session tends to crash hailort.service.
    out = EVAL / f"rolling-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(dict(threshold=args.threshold, rows=rows, false=false, verifications=sum(checks)), indent=2))
    print(f'Saved {out}')


def input_device():
    import sounddevice as sd
    import respeaker
    return sd, respeaker.select_input(None)


def record_positive(args):
    sd, device = input_device()
    for index in range(1, args.count + 1):
        input(f'[{index}/{args.count}] Press Enter, then say "{args.phrase}" within {args.seconds:.0f} s… ')
        pcm = sd.rec(round(args.seconds * ww.RATE), samplerate=ww.RATE, channels=1, dtype='int16', device=device)
        sd.wait()
        path = EVAL / args.set / f"{time.strftime('%Y%m%d-%H%M%S')}-{index:02d}.wav"
        write_wav(path, pcm[:, 0])
        print(f'  saved {path.name}')


def record_negative(args):
    sd, device = input_device()
    print(f'Recording {args.minutes:g} min of normal conversation/TV without the wake phrase…')
    pcm = sd.rec(round(args.minutes * 60 * ww.RATE), samplerate=ww.RATE, channels=1, dtype='int16', device=device)
    sd.wait()
    path = EVAL / args.set / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"
    write_wav(path, pcm[:, 0])
    print(f'Saved {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    positive = sub.add_parser('record-positive')
    positive.add_argument('--count', type=int, default=20)
    positive.add_argument('--seconds', type=float, default=3)
    positive.add_argument('--phrase', default='Hi Kuro')
    positive.add_argument('--set', default='positive', help='folder under test-output/wake-eval, e.g. positive-tv')
    negative = sub.add_parser('record-negative')
    negative.add_argument('--minutes', type=float, default=10)
    negative.add_argument('--set', default='negative', help='folder under test-output/wake-eval, e.g. negative-tv')
    check = sub.add_parser('evaluate')
    check.add_argument('--model', type=Path, default=ww.default_model_path())
    check.add_argument('--patience', type=int, default=2)
    check.add_argument('--thresholds', type=float, nargs='+', default=[.3, .4, .5, .6, .7, .8])
    rolling = sub.add_parser('evaluate-rolling', help='stream clips through the rolling Hailo detector')
    rolling.add_argument('--name', default='Kuro')
    rolling.add_argument('--threshold', type=float, default=None)
    rolling.add_argument('--snr', type=lambda v: None if v == 'clean' else v if v == 'room' else float(v), nargs='+',
                         default=[None, 10, 5, 0],
                         help="'clean', 'room' (noise around clips recorded in it) or dB of noise mixed over each "
                              "positive (default: clean 10 5 0)")
    rolling.add_argument('--noise', type=Path, help='noise WAV for mixing (default: the first negative recording)')
    rolling.add_argument('--negatives', action='store_true', help='also stream the negative recordings (slow)')
    rolling.add_argument('--positive-set', default='positive', help='folder of positives, e.g. positive-tv')
    rolling.add_argument('--negative-set', default='negative', help='folder of negatives, e.g. negative-tv')
    args = parser.parse_args()
    if args.command == 'evaluate-rolling' and args.threshold is None:
        import rolling_wake
        args.threshold = rolling_wake.DEFAULT_THRESHOLD
    {'record-positive': record_positive, 'record-negative': record_negative, 'evaluate': evaluate,
     'evaluate-rolling': evaluate_rolling}[args.command](args)


if __name__ == '__main__':
    main()
