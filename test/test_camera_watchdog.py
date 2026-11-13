import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('start_stack', ROOT / 'scripts/start_stack.py')
start_stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_stack)

FAILURE = b'[ERROR] [head_camera]: Device initialization failed after 3 attempts.\n'


class FakeFaults:
    def __init__(self):
        self.reports, self.clears = [], []

    def report(self, code, message='', **kwargs):
        self.reports.append((code, message, kwargs))

    def clear(self, code=None):
        self.clears.append(code)


class FakeCamera:
    def __init__(self, path):
        self.output = path.open('ab', buffering=0)
        self.process = object()
        self.faults = FakeFaults()
        self.next_start = 0


class CameraWatchdogTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.log = Path(folder.name) / 'stack-camera.log'
        self.log.write_bytes(FAILURE * 5)  # an earlier run's failures
        self.camera = FakeCamera(self.log)
        self.addCleanup(self.camera.output.close)
        self.resets = []
        self.watchdog = start_stack.CameraWatchdog(
            self.camera, reset=lambda vendor: self.resets.append(vendor) or ['3-1'])
        stopper = mock.patch.object(start_stack, 'stop')
        self.stop = stopper.start()
        self.addCleanup(stopper.stop)
        self.assertFalse(self.watchdog.poll(0))

    def write(self, data):
        self.camera.output.write(data)

    def test_earlier_failures_are_ignored(self):
        self.assertFalse(self.watchdog.poll(1))
        self.assertEqual(self.resets, [])

    def test_repeated_failures_reset_and_restart_the_camera(self):
        process = self.camera.process
        self.write(b'noise\n' + FAILURE)
        self.assertFalse(self.watchdog.poll(10))
        self.write(FAILURE)
        self.assertTrue(self.watchdog.poll(21))
        self.assertEqual(self.resets, ['2bc5'])
        self.stop.assert_called_once_with(process)
        self.assertIsNone(self.camera.process)
        self.assertEqual(self.camera.next_start, 24)
        code, message, extra = self.camera.faults.reports[0]
        self.assertEqual(code, 'camera_unavailable')
        self.assertIn('3-1', message)
        self.assertEqual(extra['title'], 'Resetting camera')

    def test_a_camera_that_answers_nothing_is_reset(self):
        silent = b'[ERROR] Failed to TemperatureUpdate1: Request failed, statusCode: 8, msg: Device response size(0) < 10\n'
        self.write(silent * 19)
        self.assertFalse(self.watchdog.poll(19))  # One a second; a short glitch is tolerated.
        self.write(silent)
        self.assertTrue(self.watchdog.poll(20))
        self.assertEqual(self.resets, ['2bc5'])

    def test_failures_far_apart_do_not_reset(self):
        self.write(FAILURE)
        self.watchdog.poll(10)
        self.write(FAILURE)
        self.assertFalse(self.watchdog.poll(80))
        self.assertEqual(self.resets, [])

    def test_failure_line_split_between_reads(self):
        self.write(FAILURE)
        self.watchdog.poll(10)
        self.write(FAILURE[:20])
        self.assertFalse(self.watchdog.poll(11))
        self.write(FAILURE[20:])
        self.assertTrue(self.watchdog.poll(12))

    def test_cooldown_and_fault_clear(self):
        self.write(FAILURE * 2)
        self.assertTrue(self.watchdog.poll(10))
        self.camera.process = object()  # restarted by Component.maintain
        self.write(FAILURE * 2)
        self.assertFalse(self.watchdog.poll(20))  # still cooling down
        self.assertEqual(self.camera.faults.clears, [])
        self.assertFalse(self.watchdog.poll(310))  # the failures at 20 s have aged out; new ones are needed
        self.assertEqual(len(self.resets), 1)
        self.write(FAILURE * 2)
        self.assertTrue(self.watchdog.poll(320))
        self.assertEqual(len(self.resets), 2)
        self.camera.process = object()
        self.assertFalse(self.watchdog.poll(400))
        self.assertEqual(self.camera.faults.clears, ['camera_unavailable'] * 2)

    def test_continuous_failures_reset_again_after_the_cooldown(self):
        self.write(FAILURE * 2)
        self.assertTrue(self.watchdog.poll(10))
        self.camera.process = object()
        for t in range(21, 320, 11):  # the camera keeps failing every ~11 s
            self.write(FAILURE)
            self.watchdog.poll(t)
        self.assertEqual(len(self.resets), 2)
        self.assertEqual(self.camera.faults.clears, [])

    def test_fault_clears_once_the_camera_stays_quiet(self):
        self.write(FAILURE * 2)
        self.watchdog.poll(10)
        self.camera.process = object()
        self.watchdog.poll(50)
        self.assertEqual(self.camera.faults.clears, [])
        self.watchdog.poll(75)
        self.assertEqual(self.camera.faults.clears, ['camera_unavailable'])
        self.watchdog.poll(90)
        self.assertEqual(self.camera.faults.clears, ['camera_unavailable'])

    def test_rotated_log_is_read_from_the_start(self):
        os.truncate(self.log, 0)
        self.write(FAILURE * 2)
        self.assertTrue(self.watchdog.poll(5))

    def test_no_reset_while_the_camera_is_not_running(self):
        self.camera.process = None
        self.write(FAILURE * 2)
        self.assertFalse(self.watchdog.poll(5))


class UsbResetTest(unittest.TestCase):
    def test_resets_only_the_vendor_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            sysfs, devfs = Path(folder, 'sys'), Path(folder, 'dev')
            for name, vendor, bus, dev in (('3-1', '2bc5', 3, 4), ('4-1.2', '2886', 4, 21)):
                (sysfs / name).mkdir(parents=True)
                (sysfs / name / 'idVendor').write_text(vendor + '\n')
                (sysfs / name / 'busnum').write_text(f'{bus}\n')
                (sysfs / name / 'devnum').write_text(f'{dev}\n')
                (devfs / f'{bus:03d}').mkdir(parents=True, exist_ok=True)
                (devfs / f'{bus:03d}' / f'{dev:03d}').write_bytes(b'')
            (sysfs / '3-1:1.0').mkdir()  # an interface: no idVendor
            calls = []
            ports = start_stack.reset_usb_devices('2bc5', sysfs, devfs, lambda fd, request, arg: calls.append(request))
            self.assertEqual(ports, ['3-1'])
            self.assertEqual(calls, [0x5514])

    def test_permission_error_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as folder:
            sysfs = Path(folder, 'sys', '3-1')
            sysfs.mkdir(parents=True)
            for key, value in (('idVendor', '2bc5'), ('busnum', '3'), ('devnum', '4')):
                (sysfs / key).write_text(value)
            ports = start_stack.reset_usb_devices('2bc5', sysfs.parent, Path(folder, 'missing'))
            self.assertEqual(ports, [])


if __name__ == '__main__':
    unittest.main()
