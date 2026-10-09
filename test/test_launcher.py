"""Verify the bundled application starts from a real Git clone without Node.js."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


class BrowserProfileCheck(unittest.TestCase):
    def test_snap_profile_is_writable_and_explicit_override_is_preserved(self):
        spec = importlib.util.spec_from_file_location('launch_robot', ROOT / 'scripts/launch_robot.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': '/tmp/custom-config'}, clear=True):
            for browser in ('/snap/bin/chromium', '/var/lib/snapd/snap/bin/chromium'):
                self.assertEqual(launcher.browser_profile(browser), str(Path.home() / 'snap/chromium/common/face-agent-chromium'))
            self.assertEqual(launcher.browser_profile('/usr/bin/chromium'), '/tmp/custom-config/face-agent-chromium')
            os.environ['DOTS_BROWSER_PROFILE'] = '/tmp/robot profile'
            self.assertEqual(launcher.browser_profile('/snap/bin/chromium'), '/tmp/robot profile')


@unittest.skipUnless(shutil.which('git') and importlib.util.find_spec('websockets'), 'Install Git and requirements.txt to run this check.')
class LauncherCheck(unittest.TestCase):
    def test_clone_launch_and_saved_configuration_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); origin = root / 'origin'; origin.mkdir()
            for name in ('.gitignore', 'run.sh', 'requirements.txt', 'robot-config.default.json'):
                shutil.copy2(ROOT / name, origin / name)
            shutil.copytree(ROOT / 'dist', origin / 'dist')
            (origin / 'scripts').mkdir()
            for name in ('serve.py', 'dotsctl.py', 'launch_robot.py', 'prerender.py', 'robot_states.py', 'ros_status.py'):
                shutil.copy2(ROOT / 'scripts' / name, origin / 'scripts' / name)
            subprocess.run(['git', 'init', '--quiet', str(origin)], check=True)
            subprocess.run(['git', '-C', str(origin), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(origin), '-c', 'user.name=Launcher check', '-c', 'user.email=launcher@example.invalid', 'commit', '--quiet', '-m', 'Bundled application'], check=True)
            clone = root / 'clone'
            subprocess.run(['git', 'clone', '--quiet', str(origin), str(clone)], check=True)
            # Reuse installed test dependencies offline; the launcher still selects its own venv.
            subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(clone / '.venv')], check=True, stdout=subprocess.DEVNULL)
            # Include the current test environment's SDK in this offline clone.
            site_packages = next((clone / '.venv' / 'lib').glob('python*/site-packages'))
            (site_packages / 'test-dependencies.pth').write_text(sysconfig.get_path('purelib') + '\n')
            expected = json.loads((clone / 'robot-config.default.json').read_text())
            self.assertFalse((clone / 'robot-config.json').exists())
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1', 'OPENAI_API_KEY': '',
                   'XDG_CONFIG_HOME': str(root / 'private'), 'DOTS_ROBOT_STATUS': '0', 'PATH': '/usr/bin:/bin', 'PIP_NO_INDEX': '1'}
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
                        if (clone / 'dist/prerendered/manifest.json').exists():
                            with urlopen(base + '/api/prerender') as response:
                                pack = json.load(response)
                            with urlopen(base + f'/prerendered/{pack["id"]}/{pack["clips"][0]["sheets"][0]}') as response:
                                self.assertEqual(response.read(4), b'RIFF')
                        with urlopen(base + '/api/voice') as response:
                            self.assertFalse(json.load(response)['configured'])
                    finally:
                        process.terminate(); process.wait(timeout=5)
                if launch == 0:
                    expected['appearance']['name'] = 'Saved robot'
                    (clone / 'robot-config.json').write_text(json.dumps(expected))
            self.assertFalse((clone / 'node_modules').exists())

    def test_robot_launch_allows_audio_and_cleans_up_its_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / 'browser'
            browser.write_text(f'#!{sys.executable}\nimport json, os, sys, time\nfrom pathlib import Path\nPath(os.environ["BROWSER_ARGS"]).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(float(os.environ["BROWSER_SECONDS"]))\n')
            browser.chmod(0o700)
            for interrupt in (False, True):
                with self.subTest(interrupt=interrupt), socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
                arguments = root / f'args-{interrupt}.json'
                env = {**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1', 'DOTS_CONFIG': str(ROOT / 'robot-config.default.json'),
                       'OPENAI_API_KEY': '', 'XDG_CONFIG_HOME': str(root / 'private'), 'DOTS_BROWSER': str(browser),
                       'DOTS_BROWSER_PROFILE': str(root / 'robot profile'), 'BROWSER_ARGS': str(arguments),
                       'BROWSER_SECONDS': '30' if interrupt else '1'}
                if interrupt:
                    env['DOTS_FULLSCREEN'] = '0'
                else:
                    env.pop('DOTS_FULLSCREEN', None)
                with (root / 'robot.log').open('w+') as log:
                    process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/launch_robot.py')], env=env, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 10
                        while not arguments.exists():
                            if process.poll() is not None or time.monotonic() > deadline:
                                log.seek(0); self.fail(log.read())
                            time.sleep(.05)
                        args = json.loads(arguments.read_text())
                        self.assertIn('--autoplay-policy=no-user-gesture-required', args)
                        self.assertEqual('--start-fullscreen' in args, not interrupt)
                        self.assertIn(f'--app=http://127.0.0.1:{port}/robot.html', args)
                        self.assertIn(f'--user-data-dir={root / "robot profile"}', args)
                        if interrupt:
                            process.send_signal(signal.SIGTERM)
                        self.assertEqual(process.wait(timeout=10), 0)
                        with socket.socket() as probe:
                            self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)
                    finally:
                        if process.poll() is None:
                            process.terminate(); process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
