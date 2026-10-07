"""Verify the bundled application starts from a real Git clone without Node.js."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('git') and importlib.util.find_spec('websockets'), 'Install Git and requirements.txt to run this check.')
class LauncherCheck(unittest.TestCase):
    def test_clone_launch_and_saved_configuration_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); origin = root / 'origin'; origin.mkdir()
            for name in ('.gitignore', 'run.sh', 'requirements.txt', 'robot-config.default.json'):
                shutil.copy2(ROOT / name, origin / name)
            shutil.copytree(ROOT / 'dist', origin / 'dist')
            (origin / 'scripts').mkdir()
            for name in ('serve.py', 'dotsctl.py'):
                shutil.copy2(ROOT / 'scripts' / name, origin / 'scripts' / name)
            subprocess.run(['git', 'init', '--quiet', str(origin)], check=True)
            subprocess.run(['git', '-C', str(origin), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(origin), '-c', 'user.name=Launcher check', '-c', 'user.email=launcher@example.invalid', 'commit', '--quiet', '-m', 'Bundled application'], check=True)
            clone = root / 'clone'
            subprocess.run(['git', 'clone', '--quiet', str(origin), str(clone)], check=True)
            # Reuse installed test dependencies offline; the launcher still selects its own venv.
            subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(clone / '.venv')], check=True, stdout=subprocess.DEVNULL)
            expected = json.loads((clone / 'robot-config.default.json').read_text())
            self.assertFalse((clone / 'robot-config.json').exists())
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1', 'OPENAI_API_KEY': '',
                   'XDG_CONFIG_HOME': str(root / 'private'), 'PATH': '/usr/bin:/bin', 'PIP_NO_INDEX': '1'}
            env.pop('DOTS_CONFIG', None)
            for launch in range(2):
                with (root / 'launch.log').open('w+') as log:
                    process = subprocess.Popen(['bash', str(clone / 'run.sh')], cwd=root, env=env, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 15
                        while True:
                            try:
                                with urlopen(base + '/api/config', timeout=1) as response: config = json.load(response)
                                break
                            except URLError:
                                if process.poll() is not None or time.monotonic() > deadline:
                                    log.seek(0); self.fail(log.read())
                                time.sleep(.05)
                        self.assertEqual(config, expected)
                        with urlopen(base + '/robot.html') as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.headers['Cross-Origin-Embedder-Policy'], 'require-corp')
                        with urlopen(base + '/local-dots/orbit-characters.wasm') as response:
                            self.assertEqual(response.read(4), b'\0asm')
                        with urlopen(base + '/api/voice') as response:
                            self.assertFalse(json.load(response)['configured'])
                    finally:
                        process.terminate(); process.wait(timeout=5)
                if launch == 0:
                    expected['appearance']['name'] = 'Saved robot'
                    (clone / 'robot-config.json').write_text(json.dumps(expected))
            self.assertFalse((clone / 'node_modules').exists())


if __name__ == '__main__':
    unittest.main()
