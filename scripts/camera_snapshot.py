#!/usr/bin/env python3
"""Capture one fresh ROS color frame in memory; --check prints metadata only."""
import argparse
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import os
import sys
import threading
import time

DEFAULT_TOPIC = '/head_camera/color/image_raw/compressed'
MAX_BYTES = 4_000_000
MAX_AGE = 1.0


def frame_age(stamp, now):
    seconds = stamp.sec + stamp.nanosec / 1_000_000_000
    age = now - seconds
    if seconds <= 0 or not math.isfinite(age) or not -.25 <= age <= MAX_AGE:
        raise ValueError('The camera frame has a missing, stale, or future timestamp.')
    return seconds, max(0., age)


def validate_crop(crop):
    if crop is not None and (len(crop) != 4 or any(type(value) not in (int, float) or not math.isfinite(value) for value in crop)
                             or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1)):
        raise ValueError('Crop must be [left, top, right, bottom] in normalized camera coordinates (0–1).')


def prepare_image(data, crop=None):
    from PIL import Image, UnidentifiedImageError
    validate_crop(crop)
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError('The compressed camera frame is empty or too large.')
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('JPEG', 'PNG') or not all(0 < side <= 4096 for side in source.size):
                raise ValueError('Use a JPEG/PNG color stream with dimensions up to 4096 pixels.')
            image = source.convert('RGB')
            if crop is not None:
                left, top, right, bottom = crop
                bounds = (int(left * image.width), int(top * image.height),
                          math.ceil(right * image.width), math.ceil(bottom * image.height))
                if bounds[2] - bounds[0] < 8 or bounds[3] - bounds[1] < 8:
                    raise ValueError('That crop is too small to inspect; ask for a closer camera view.')
                image = image.crop(bounds)
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format='JPEG', quality=85)
            return output.getvalue(), image.width, image.height
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError('The camera frame could not be decoded.') from error


def capture(topic, timeout, crop=None):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from sensor_msgs.msg import CompressedImage
    rclpy.init(args=[])
    node = rclpy.create_node(f'kuro_camera_snapshot_{os.getpid()}')
    result = None
    last_error = ''

    def receive(message):
        nonlocal result, last_error
        if result is not None:
            return
        try:
            if 'compressedDepth' in message.format:
                raise ValueError('Vision needs a color image topic, not compressed depth.')
            stamp, _ = frame_age(message.header.stamp, time.time())
            data, width, height = prepare_image(message.data, crop)
            _, age = frame_age(message.header.stamp, time.time())
            result = dict(topic=topic, captured_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                          captured_unix=stamp, age_seconds=age, width=width, height=height, crop=crop,
                          image_url='data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii'))
        except ValueError as error:
            last_error = str(error)

    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    subscription = node.create_subscription(CompressedImage, topic, receive, qos)
    try:
        deadline = time.monotonic() + timeout
        while result is None and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=min(.1, max(0., deadline - time.monotonic())))
        if result is None:
            raise RuntimeError(f'No fresh camera image on {topic} within {timeout:g} seconds. '
                               'Check that the ROS camera node is running. ' + last_error)
        return result
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


class LatestFrame:
    """Retain one compressed frame; decode only when a snapshot is requested."""
    def __init__(self):
        self.condition = threading.Condition()
        self.latest = None

    def receive(self, message):
        try:
            if 'compressedDepth' in message.format:
                return
            stamp, _ = frame_age(message.header.stamp, time.time())
            if not 0 < len(message.data) <= MAX_BYTES:
                return
            data = bytes(message.data)
        except ValueError:
            return
        with self.condition:
            self.latest = stamp, data
            self.condition.notify_all()

    def snapshot(self, topic, timeout, crop=None):
        validate_crop(crop)
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self.latest is not None:
                    stamp, data = self.latest
                    age = time.time() - stamp
                    if -.25 <= age <= MAX_AGE:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f'No fresh camera image on {topic} within {timeout:g} seconds. '
                                       'Check that the ROS camera node is running.')
                self.condition.wait(remaining)
        data, width, height = prepare_image(data, crop)
        age = time.time() - stamp
        if not -.25 <= age <= MAX_AGE:
            raise RuntimeError('The camera frame expired during image preparation. Please try again.')
        return dict(topic=topic, captured_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    captured_unix=stamp, age_seconds=max(0., age), width=width, height=height, crop=crop,
                    image_url='data:image/jpeg;base64,' + base64.b64encode(data).decode('ascii'))


def serve(topic, timeout):
    """Private stdin/stdout snapshot protocol owned by the chatbot process."""
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from sensor_msgs.msg import CompressedImage
    rclpy.init(args=[])
    node = rclpy.create_node(f'kuro_camera_feed_{os.getpid()}')
    frames = LatestFrame()
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    subscription = node.create_subscription(CompressedImage, topic, frames.receive, qos)
    stopping = threading.Event()

    def spin():
        while not stopping.is_set():
            rclpy.spin_once(node, timeout_sec=.1)

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        print(json.dumps(dict(ready=True)), flush=True)
        for line in sys.stdin:
            try:
                if len(line) > 4096:
                    raise ValueError('Snapshot request exceeds the size limit.')
                request = json.loads(line)
                value = frames.snapshot(topic, timeout, request.get('crop'))
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
                value = dict(error=str(error))
            print(json.dumps(value), flush=True)
    finally:
        stopping.set()
        thread.join(timeout=2)
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', default=DEFAULT_TOPIC)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--check', action='store_true', help='Print metadata without the image; no OpenAI request.')
    parser.add_argument('--serve', action='store_true', help='Keep a ROS subscriber alive for private stdin snapshot requests.')
    parser.add_argument('--crop', type=float, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'),
                        help='Inspect a normalized region of the full camera frame (0–1).')
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 15:
        parser.error('--timeout must be between 0 and 15 seconds.')
    if args.serve and (args.check or args.crop is not None):
        parser.error('--serve accepts crop in each request; do not combine with --check or --crop.')
    try:
        if args.serve:
            serve(args.topic, args.timeout)
            return 0
        validate_crop(args.crop)
        value = capture(args.topic, args.timeout, args.crop)
        if args.check:
            value.pop('image_url')
        print(json.dumps(value), flush=True)
        return 0
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(json.dumps(dict(error=str(error))), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
