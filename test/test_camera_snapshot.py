"""Run with ROS sourced and /usr/bin/python3 (uses the system Pillow/ROS packages)."""
from io import BytesIO
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from camera_snapshot import capture, frame_age, prepare_image, validate_crop
from camera_vision import CameraFeed, VisionUnavailable
try:
    from PIL import Image
except ImportError:
    Image = None


def picture(width=160, height=90, format='JPEG'):
    output = BytesIO()
    Image.new('RGB', (width, height), (190, 20, 30)).save(output, format=format)
    return output.getvalue()


@unittest.skipIf(Image is None, 'Run with /usr/bin/python3 and python3-pil installed.')
class ImageCheck(unittest.TestCase):
    def test_resize_preserves_aspect_and_produces_decodable_jpeg(self):
        data, width, height = prepare_image(picture(2560, 1440, 'PNG'))
        self.assertEqual((width, height), (1280, 720))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.mode, 'RGB')

    def test_bad_images_and_missing_stale_future_timestamps_are_rejected(self):
        for value in (b'', b'not an image', b'x' * 4_000_001):
            with self.assertRaises(ValueError):
                prepare_image(value)
        for seconds in (0, 90, 102):
            with self.assertRaises(ValueError):
                frame_age(SimpleNamespace(sec=seconds, nanosec=0), 100)
        self.assertAlmostEqual(frame_age(SimpleNamespace(sec=99, nanosec=800_000_000), 100)[1], .2)

    def test_focused_crop_uses_full_frame_coordinates_and_rejects_bad_regions(self):
        data, width, height = prepare_image(picture(400, 200), [.25, .25, .75, .75])
        self.assertEqual((width, height), (200, 100))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (200, 100))
        for crop in ([0, 0, 2, 1], [0, 0, 0, 1], [.5, .5, .4, .8], [0, 0, float('nan'), 1], [0, 0]):
            with self.assertRaises(ValueError):
                validate_crop(crop)
        with self.assertRaises(ValueError):
            prepare_image(picture(100, 100), [.1, .1, .11, .11])


@unittest.skipUnless(Image is not None and importlib.util.find_spec('rclpy'), 'Source ROS and run with /usr/bin/python3 and python3-pil.')
class RosCameraCheck(unittest.TestCase):
    def check_capture(self, stale):
        import rclpy
        from rclpy.context import Context
        from sensor_msgs.msg import CompressedImage
        with patch.dict(os.environ, {'ROS_DOMAIN_ID': '95'}):
            context = Context()
            rclpy.init(context=context)
            node = rclpy.create_node('snapshot_test_camera', context=context)
            publisher = node.create_publisher(CompressedImage, '/snapshot_test/color/compressed', 1)
            stopped = threading.Event()
            data = picture()
            def publish():
                while not stopped.is_set():
                    message = CompressedImage()
                    stamp = time.time() - (10 if stale else 0)
                    message.header.stamp.sec = int(stamp)
                    message.header.stamp.nanosec = int((stamp % 1) * 1_000_000_000)
                    message.format = 'bgr8; jpeg compressed bgr8'
                    message.data = data
                    publisher.publish(message)
                    stopped.wait(.05)
            thread = threading.Thread(target=publish)
            thread.start()
            try:
                if stale:
                    with self.assertRaisesRegex(RuntimeError, 'stale'):
                        capture('/snapshot_test/color/compressed', 3)
                else:
                    value = capture('/snapshot_test/color/compressed', 5)
                    self.assertEqual((value['width'], value['height']), (160, 90))
                    self.assertTrue(value['image_url'].startswith('data:image/jpeg;base64,'))
                    self.assertLess(value['age_seconds'], 1)
            finally:
                stopped.set()
                thread.join(timeout=2)
                node.destroy_node()
                context.shutdown()

    def test_receives_fresh_live_ros_frame(self):
        self.check_capture(False)

    def test_stale_publisher_never_supplies_visual_evidence(self):
        self.check_capture(True)


@unittest.skipUnless(Image is not None and importlib.util.find_spec('rclpy'), 'Source ROS and run with /usr/bin/python3 and python3-pil.')
class PersistentRosCameraCheck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import rclpy
        from rclpy.context import Context
        from sensor_msgs.msg import CompressedImage
        self.environment = patch.dict(os.environ, {'ROS_DOMAIN_ID': '96'})
        self.environment.start()
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node('persistent_snapshot_test_camera', context=self.context)
        self.topic = '/persistent_snapshot_test/color/compressed'
        publisher = self.node.create_publisher(CompressedImage, self.topic, 1)
        self.stopped = threading.Event()
        self.publishing, self.stale = True, False
        data = picture()

        def publish():
            while not self.stopped.is_set():
                if self.publishing:
                    message = CompressedImage()
                    stamp = time.time() - (10 if self.stale else 0)
                    message.header.stamp.sec = int(stamp)
                    message.header.stamp.nanosec = int((stamp % 1) * 1_000_000_000)
                    message.format = 'bgr8; jpeg compressed bgr8'
                    message.data = data
                    publisher.publish(message)
                self.stopped.wait(.05)

        self.thread = threading.Thread(target=publish)
        self.thread.start()

    async def asyncTearDown(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.node.destroy_node()
        self.context.shutdown()
        self.environment.stop()

    async def test_warm_feed_reuses_process_crops_and_recovers_after_child_exit(self):
        async with CameraFeed(self.topic) as feed:
            process = feed.process
            value = await feed.capture()
            self.assertEqual((value['width'], value['height']), (160, 90))
            cropped = await feed.capture([0, 0, .5, 1])
            self.assertEqual((cropped['width'], cropped['height']), (80, 90))
            self.assertIs(feed.process, process)
            process.kill()
            await process.wait()
            recovered = await feed.capture()
            self.assertLess(recovered['age_seconds'], 1)
            self.assertNotEqual(feed.process.pid, process.pid)
            owned = feed.process
        self.assertIsNotNone(owned.returncode)

    async def test_stale_stream_fails_but_same_helper_recovers_on_fresh_frames(self):
        self.stale = True
        async with CameraFeed(self.topic) as feed:
            process = feed.process
            with self.assertRaisesRegex(VisionUnavailable, 'No fresh'):
                await feed.capture()
            self.stale = False
            value = await feed.capture()
            self.assertLess(value['age_seconds'], 1)
            self.assertIs(feed.process, process)

    async def test_cancellation_discards_inflight_response_and_next_turn_recovers(self):
        async with CameraFeed(self.topic) as feed:
            await feed.capture()
            process = feed.process
            self.publishing = False
            await asyncio.sleep(1.2)
            task = asyncio.create_task(feed.capture())
            await asyncio.sleep(.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(process.returncode)
            self.assertIsNone(feed.process)
            self.publishing = True
            value = await feed.capture()
            self.assertLess(value['age_seconds'], 1)
            self.assertNotEqual(feed.process.pid, process.pid)


if __name__ == '__main__':
    unittest.main()
