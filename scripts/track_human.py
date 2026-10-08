#!/usr/bin/env python3
"""Track one wake-session customer from a ROS color topic into Shiro's local gaze API."""
import argparse
import json
import math
import os
import platform
import signal
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import cv2
import numpy as np

from human_tracker import PersonTracker, Vision
from setup_tracking import MODEL_DIR, model_paths


def api(base, path, value=None):
    body = json.dumps(value).encode() if value is not None else None
    with urlopen(Request(base.rstrip('/') + '/api/' + path, data=body,
                         headers={'Content-Type': 'application/json'}), timeout=.4) as response:
        return json.load(response)


class RobotLink:
    """Keep HTTP independent of inference; late frames cannot cross a sleep/wake boundary."""
    def __init__(self, url):
        self.url = url; self.lock = threading.Lock(); self.stop = threading.Event()
        self.status = None; self.status_at = 0; self.output = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def snapshot(self):
        with self.lock:
            return self.status, self.status_at

    def publish(self, session, packet):
        with self.lock:
            self.output = (session, packet, time.monotonic())

    def run(self):
        next_status = 0; offline = None
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                if started >= next_status:
                    status = api(self.url, 'status')
                    if not isinstance(status.get('trackingSession'), str) or type(status.get('awake')) is not bool:
                        raise ValueError('Update face-agent: /api/status needs trackingSession and awake.')
                    with self.lock:
                        self.status = status; self.status_at = time.monotonic()
                    next_status = started + .2
                status, status_at = self.snapshot()
                with self.lock:
                    output = self.output
                if status is not None and started - status_at < 1:
                    session = status['trackingSession']
                    packet = dict(detected=False)
                    if status['awake'] and output is not None and output[0] == session and started - output[2] < .35:
                        packet = output[1]
                    api(self.url, 'gaze', dict(packet, session=session))
                if offline:
                    print('Robot service connected.', flush=True)
                offline = False
            except HTTPError as error:
                if error.code == 409:
                    next_status = 0  # Sleep/wake changed during this request.
                elif not offline:
                    print(f'Robot API returned HTTP {error.code}; gaze will expire safely.', flush=True)
                    offline = True
            except (URLError, OSError, ValueError) as error:
                if not offline:
                    print(f'Waiting for robot service at {self.url}: {error}', flush=True)
                    offline = True
                next_status = 0
            self.stop.wait(max(0, .1 - (time.monotonic() - started)))

    def close(self):
        self.stop.set(); self.thread.join(timeout=2)
        status, _ = self.snapshot()
        try:
            if status:
                api(self.url, 'gaze', dict(detected=False, session=status['trackingSession']))
        except (OSError, ValueError):
            pass  # The display also expires targets after one second.


class LatestFrame:
    def __init__(self):
        self.lock = threading.Lock(); self.value = None; self.sequence = 0

    def receive(self, message):
        with self.lock:
            self.sequence += 1
            self.value = (self.sequence, message, time.monotonic())

    def take(self):
        with self.lock:
            value = self.value; self.value = None
            return value


def decode(message, compressed):
    if compressed:
        image = cv2.imdecode(np.frombuffer(message.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError('Invalid compressed color image.')
        return image
    if message.encoding not in ('rgb8', 'bgr8', 'rgba8', 'bgra8', 'mono8'):
        raise ValueError(f'Unsupported color encoding {message.encoding}; use the compressed color topic.')
    channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}[message.encoding]
    if not 0 < message.width <= 4096 or not 0 < message.height <= 4096 or message.step < message.width * channels:
        raise ValueError('Invalid color image dimensions or stride.')
    rows = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
    image = rows[:, :message.width * channels].reshape(message.height, message.width, channels)
    conversion = {'rgb8': cv2.COLOR_RGB2BGR, 'rgba8': cv2.COLOR_RGBA2BGR,
                  'bgra8': cv2.COLOR_BGRA2BGR, 'mono8': cv2.COLOR_GRAY2BGR}
    return cv2.cvtColor(image, conversion[message.encoding]) if message.encoding in conversion else image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('DOTS_URL', 'http://127.0.0.1:' + os.environ.get('PORT', '5173')))
    parser.add_argument('--topic', default='/head_camera/color/image_raw/compressed')
    parser.add_argument('--raw', action='store_true', help='Subscribe to sensor_msgs/Image instead of CompressedImage.')
    parser.add_argument('--mirror', action='store_true', help='Reverse horizontal gaze for your camera mounting.')
    parser.add_argument('--threshold', type=float, default=.55, help='SFace cosine match threshold; validate on your robot (default: .55).')
    parser.add_argument('--threads', type=int, default=1, help='OpenCV CPU threads (default: 1).')
    parser.add_argument('--check', action='store_true', help='Check ROS imports and warm both CPU models, then exit.')
    parser.add_argument('--duration', type=float, default=0, help='Stop after this many seconds; zero runs until Ctrl+C.')
    parser.add_argument('--stats', action='store_true', help='Print throughput and model call counts every five seconds.')
    args = parser.parse_args()
    if urlparse(args.url).hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('--url must be the robot service on this device (localhost).')
    if not math.isfinite(args.threshold) or not .363 <= args.threshold <= .95:
        parser.error('--threshold must be between .363 and .95.')
    if not 1 <= args.threads <= 4 or not math.isfinite(args.duration) or args.duration < 0:
        parser.error('Use 1–4 CPU threads and a nonnegative, finite duration.')
    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
        from rclpy.signals import SignalHandlerOptions
        from sensor_msgs.msg import CompressedImage, Image
    except ImportError:
        parser.exit(1, 'Source ROS first and use ./track.sh (a separate environment matching ROS Python).\n')
    paths = model_paths()
    if not all(path.is_file() for path in paths):
        parser.exit(1, f'Missing tracking models in {MODEL_DIR}. Run ./track.sh for first-time setup.\n')
    vision = Vision(*paths, threads=args.threads)
    if args.check:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        vision.detect(blank)
        face = np.array([80, 40, 120, 150, 115, 90, 165, 90, 140, 120, 120, 155, 160, 155, 1], dtype=np.float32)
        feature = vision.feature(blank, face, 1)
        if not np.isfinite(feature).all():
            raise ValueError('SFace returned invalid features.')
        print(json.dumps(dict(platform=platform.machine(), python=sys.version.split()[0], opencv=cv2.__version__,
                              threads=cv2.getNumThreads(), ros='available', models='loaded and executed', topic=args.topic)))
        return 0
    frames = LatestFrame(); link = RobotLink(args.url); tracker = PersonTracker(vision, args.threshold, args.mirror)
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node('shiro_human_tracker')
    qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    subscription = node.create_subscription(Image if args.raw else CompressedImage, args.topic, frames.receive, qos)
    executor = SingleThreadedExecutor(); executor.add_node(node)
    spin = threading.Thread(target=executor.spin, daemon=True)
    started = time.monotonic(); last_stats = started; count = 0; processing = 0; state = None; camera_error = False
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    print(f'Shiro tracker · {args.topic} → {args.url} · one CPU thread' if args.threads == 1 else
          f'Shiro tracker · {args.topic} → {args.url} · {args.threads} CPU threads', flush=True)
    print('The first confirmed person stays locked until sleep. No camera images or identity features are saved.', flush=True)
    try:
        spin.start(); link.thread.start()
        while rclpy.ok() and (not args.duration or time.monotonic() - started < args.duration):
            tick = time.monotonic()
            status, status_at = link.snapshot()
            fresh = status is not None and tick - status_at < 1
            if fresh:
                tracker.set_session(status['trackingSession'], status['awake'])
            value = frames.take()
            packet = dict(detected=False)
            if fresh and tracker.awake and value is not None and tick - value[2] < .25:
                try:
                    packet = tracker.process(decode(value[1], not args.raw), tick)
                    # Inference that missed its deadline must not refresh stale gaze.
                    if time.monotonic() - value[2] > .35:
                        packet = dict(detected=False)
                    camera_error = False
                except (ValueError, cv2.error) as error:
                    tracker.lost()
                    if not camera_error:
                        print(f'Camera frame rejected: {error}', flush=True)
                    camera_error = True
                count += 1; processing += time.monotonic() - tick
            else:
                tracker.stale(tick)
            if fresh and (not tracker.awake or value is not None or tick - tracker.last_frame > .25):
                link.publish(tracker.session, packet)
            shown = tracker.state if fresh else 'waiting-for-robot-service'
            if shown != state:
                print('Tracking: ' + shown, flush=True); state = shown
            if args.stats and tick - last_stats >= 5:
                print(f'{count / max(.001, tick - started):.1f} processed fps · '
                      f'{processing * 1000 / max(1, count):.1f} ms/frame mean · '
                      f'{vision.detections} detector / {vision.recognitions} identity calls · {shown}', flush=True)
                last_stats = tick
            time.sleep(max(0, 1 / 15 - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        executor.shutdown(timeout_sec=2); spin.join(timeout=2); rclpy.shutdown()
        node.destroy_subscription(subscription); node.destroy_node()
        tracker.reset()
        print('Tracker stopped; original eyes restored.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, cv2.error) as error:
        print(str(error), file=sys.stderr); sys.exit(1)
