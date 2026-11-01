#!/usr/bin/env python3
"""Record and evaluate wake-word clips offline (test-output/wake-eval/, gitignored).

Stop the voice stack first; recording opens the ReSpeaker's processed channel 0.

    .venv/bin/python scripts/wake_eval.py record-positive --count 20    # say "Hi Kuro" when prompted
    .venv/bin/python scripts/wake_eval.py record-negative --minutes 10  # TV/conversation, no wake phrase
    .venv/bin/python scripts/wake_eval.py evaluate                      # sweep thresholds

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
        path = EVAL / 'positive' / f"{time.strftime('%Y%m%d-%H%M%S')}-{index:02d}.wav"
        write_wav(path, pcm[:, 0])
        print(f'  saved {path.name}')


def record_negative(args):
    sd, device = input_device()
    print(f'Recording {args.minutes:g} min of normal conversation/TV without the wake phrase…')
    pcm = sd.rec(round(args.minutes * 60 * ww.RATE), samplerate=ww.RATE, channels=1, dtype='int16', device=device)
    sd.wait()
    path = EVAL / 'negative' / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"
    write_wav(path, pcm[:, 0])
    print(f'Saved {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    positive = sub.add_parser('record-positive')
    positive.add_argument('--count', type=int, default=20)
    positive.add_argument('--seconds', type=float, default=3)
    positive.add_argument('--phrase', default='Hi Kuro')
    negative = sub.add_parser('record-negative')
    negative.add_argument('--minutes', type=float, default=10)
    check = sub.add_parser('evaluate')
    check.add_argument('--model', type=Path, default=ww.default_model_path())
    check.add_argument('--patience', type=int, default=2)
    check.add_argument('--thresholds', type=float, nargs='+', default=[.3, .4, .5, .6, .7, .8])
    args = parser.parse_args()
    {'record-positive': record_positive, 'record-negative': record_negative, 'evaluate': evaluate}[args.command](args)


if __name__ == '__main__':
    main()
