#!/usr/bin/env python3
"""Replay a video through PersonTracker with each face backend and compare CPU per frame.

Runs in the tracking environment, without ROS or the robot service:

    ~/.cache/face-agent/tracking-venv/bin/python scripts/tracker_bench.py VIDEO [--seconds 60]

Each frame is JPEG-encoded and decoded first, like the live CompressedImage path.
Frames are processed at 15 fps of video time (the live loop's rate); CPU is the process
time used per frame, so the comparison is independent of how fast the replay runs.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from human_tracker import BACKEND_THRESHOLDS, PersonTracker, make_backend  # noqa: E402
from setup_tracking import model_paths  # noqa: E402


def frames(path, seconds, fps=15):
    video = cv2.VideoCapture(str(path))
    source_fps = video.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, round(source_fps / fps))
    index = produced = 0
    while produced < seconds * fps:
        ok, frame = video.read()
        if not ok:
            break
        if index % step == 0:
            produced += 1
            yield cv2.resize(frame, (640, round(frame.shape[0] * 640 / frame.shape[1])))
        index += 1
    video.release()


def run(backend_name, video, seconds):
    backend = make_backend(backend_name, model_paths(), 1, log=lambda line: print(line, file=sys.stderr))
    if backend.name != backend_name:
        raise SystemExit(f'{backend_name} backend unavailable')
    tracker = PersonTracker(backend, BACKEND_THRESHOLDS[backend.name], mirror=True)
    tracker.set_session(1, True)
    cpu = []; detected = 0; t = 0.0
    for frame in frames(video, seconds):
        jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
        started = time.process_time()
        image = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
        packet = tracker.process(image, t)
        cpu.append(time.process_time() - started)
        detected += packet.get('detected', False)
        t += 1 / 15
    backend.close()
    cpu = np.array(cpu) * 1000
    return dict(backend=backend.name, frames=len(cpu), cpu_ms_per_frame=round(float(cpu.mean()), 2),
                cpu_ms_p90=round(float(np.percentile(cpu, 90)), 2),
                cpu_percent_at_15fps=round(float(cpu.mean()) * 15 / 10, 1),
                detector_calls=backend.detections, identity_calls=backend.recognitions,
                frames_with_face=detected, state=tracker.state)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('video', type=Path)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--backends', nargs='+', default=['opencv', 'hailo'])
    args = parser.parse_args()
    results = [run(name, args.video, args.seconds) for name in args.backends]
    for result in results:
        print(json.dumps(result))
    if len(results) == 2:
        first, second = results
        print(f"{second['backend']} vs {first['backend']}: CPU per frame "
              f"{(1 - second['cpu_ms_per_frame'] / first['cpu_ms_per_frame']) * 100:.0f}% lower")


if __name__ == '__main__':
    main()
