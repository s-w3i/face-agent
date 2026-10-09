"""Exercise real supervisor processes without the camera, browser, or cloud."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for the supervisor.')
        time.sleep(.05)


class StartupCheck(unittest.TestCase):
    def run_stack(self, existing=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            (root / '.venv/bin/python').symlink_to(sys.executable)
            shutil.copy2(ROOT / 'scripts/start_stack.py', root / 'scripts/start_stack.py')
            # Stand in for ROS discovery, keeping the actual process supervision.
            (root / 'rclpy.py').write_text(
                'def init(): pass\ndef shutdown(): pass\n'
                'class Node:\n'
                ' def get_service_names_and_types(self): return [("/robot/set_status", [])]\n'
                ' def destroy_node(self): pass\n'
                'def create_node(name): return Node()\n')
            common = (
                f'#!{sys.executable}\n'
                'import json, os, signal, sys, time\nfrom pathlib import Path\n'
                'root = Path(os.environ["FAKE_ROOT"])\n'
                'name = Path(sys.argv[0]).stem\n'
                'with (root / "events").open("a") as log:\n'
                ' log.write(json.dumps({"name": name, "pid": os.getpid()}) + "\\n")\n')
            for filename in ('camera-rgbd.sh', 'camera.sh', 'robot-status.sh', 'scripts/chat.py'):
                path = root / filename
                body = common
                if filename == 'robot-status.sh' and existing:
                    body += 'raise SystemExit(0)\n'
                else:
                    body += 'signal.pause()\n'
                path.write_text(body)
                path.chmod(0o700)
            display = root / 'run.sh'
            display.write_text(common +
                'from http.server import BaseHTTPRequestHandler, HTTPServer\n'
                'class Handler(BaseHTTPRequestHandler):\n'
                ' def do_GET(self):\n'
                '  self.send_response(200); self.end_headers()\n'
                '  self.wfile.write(json.dumps({"displays": int((root / "ready").exists()), '
                '"robotStatus": {"status": "SLEEPING"}}).encode())\n'
                ' def log_message(self, *args): pass\n'
                'HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()\n')
            display.chmod(0o700)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            env = {**os.environ, 'DISPLAY': ':99', 'XDG_CACHE_HOME': str(root / 'cache'),
                   'FAKE_ROOT': str(root), 'PYTHONPATH': str(root), 'PORT': str(port),
                   'HOST': '127.0.0.1', 'DOTS_CAMERA_PROFILE': 'rgbd'}
            def events():
                path = root / 'events'
                return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            def ready():
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/status', timeout=.2):
                        return True
                except OSError:
                    return False
            with (root / 'supervisor.log').open('w+') as log:
                process = subprocess.Popen([sys.executable, str(root / 'scripts/start_stack.py')],
                                           env=env, stdout=log, stderr=log)
                try:
                    wait_for(ready)
                    self.assertFalse(any(event['name'] == 'chat' for event in events()))
                    duplicate = subprocess.run([sys.executable, str(root / 'scripts/start_stack.py')],
                                               env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                    self.assertIn('already running', duplicate.stdout)
                    (root / 'ready').touch()
                    wait_for(lambda: any(event['name'] == 'chat' for event in events()))
                    first = next(event['pid'] for event in events() if event['name'] == 'chat')
                    os.kill(first, signal.SIGKILL)
                    wait_for(lambda: sum(event['name'] == 'chat' for event in events()) == 2)
                    # A duplicate status launcher exits successfully; it must not be
                    # restarted continuously or take ownership of the existing node.
                    self.assertEqual(sum(event['name'] == 'robot-status' for event in events()), 1)
                    process.terminate()
                    self.assertEqual(process.wait(timeout=15), 0)
                    for event in events():
                        with self.assertRaises(ProcessLookupError, msg=str(event)):
                            os.kill(event['pid'], 0)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)
                    log.seek(0)
                    if process.returncode:
                        self.fail(log.read())

    def test_ready_gate_duplicate_guard_restart_and_cleanup(self):
        self.run_stack()

    def test_existing_status_manager_is_reused(self):
        self.run_stack(existing=True)


if __name__ == '__main__':
    unittest.main()
