import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('start_stack', ROOT / 'scripts/start_stack.py')
start_stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_stack)


class LogRotationTest(unittest.TestCase):
    def test_oversized_log_keeps_tail_and_limits_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            log, small = logs / 'stack-camera.log', logs / 'small.log'
            small.write_text('keep')
            for round_number in range(5):
                log.write_bytes(bytes([65 + round_number]) * 100)
                start_stack.rotate_logs(logs, 40, 2)
                self.assertEqual(log.stat().st_size, 0)
            self.assertEqual((logs / 'stack-camera.log.1').read_bytes(), b'E' * 40)
            self.assertEqual((logs / 'stack-camera.log.2').read_bytes(), b'D' * 40)
            self.assertFalse((logs / 'stack-camera.log.3').exists())
            self.assertEqual(small.read_text(), 'keep')
            self.assertFalse(list(logs.glob('*.tmp')))

    def test_append_writer_continues_after_truncate(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            log = logs / 'a.log'
            with log.open('a') as writer:
                writer.write('x' * 100); writer.flush()
                start_stack.rotate_logs(logs, 10, 1)
                writer.write('new'); writer.flush()
            self.assertEqual(log.read_text(), 'new')


if __name__ == '__main__':
    unittest.main()
