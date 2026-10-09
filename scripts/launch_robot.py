#!/usr/bin/env python3
"""Launch the local service and a dedicated Chromium window with audio on."""
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def browser_profile(browser):
    if 'DOTS_BROWSER_PROFILE' in os.environ:
        return os.environ['DOTS_BROWSER_PROFILE']
    # Snap Chromium cannot write arbitrary hidden directories in the user's home.
    if browser in ('/snap/bin/chromium', '/var/lib/snapd/snap/bin/chromium'):
        return str(Path.home() / 'snap/chromium/common/face-agent-chromium')
    return str(Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'face-agent-chromium')


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main():
    cached = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent/chrome/chrome-linux64/chrome'
    browser = os.environ.get('DOTS_BROWSER') or next((path for name in ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable') if (path := shutil.which(name))), None)
    browser = browser or (str(cached) if cached.is_file() else None)
    if not browser:
        raise SystemExit('Install Chromium to use --robot, or set DOTS_BROWSER to its executable path.')
    host = os.environ.get('HOST', '127.0.0.1')
    local_host = '127.0.0.1' if host == '0.0.0.0' else host
    port = int(os.environ.get('PORT', '5173'))
    base = f'http://{local_host}:{port}'
    with socket.socket() as probe:
        if probe.connect_ex((local_host, port)) == 0:
            raise SystemExit(f'Port {port} is already in use. Stop that instance or choose another PORT.')
    profile = browser_profile(browser)
    service = window = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        service = subprocess.Popen([sys.executable, str(ROOT / 'scripts/serve.py')], cwd=ROOT, start_new_session=True)
        deadline = time.monotonic() + 15
        while True:
            if service.poll() is not None:
                raise RuntimeError('The local service could not start.')
            try:
                with urlopen(base + '/api/status', timeout=1):
                    break
            except URLError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Timed out waiting for the local service.')
                time.sleep(.1)
        display = base + '/robot.html' + ('?renderer=prerendered' if os.environ.get('DOTS_RENDERER') == 'prerendered' else '')
        arguments = [browser, f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                     '--autoplay-policy=no-user-gesture-required', f'--app={display}']
        if os.environ.get('DOTS_FULLSCREEN', '1') != '0':
            arguments.append('--start-fullscreen')
        window = subprocess.Popen(arguments, start_new_session=True)
        print('Robot voice is on automatically. Close the robot window or press Ctrl+C to stop.', flush=True)
        while window.poll() is None and service.poll() is None:
            time.sleep(.2)
        return window.returncode or service.returncode or 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        try:
            stop(window)
        finally:
            stop(service)


if __name__ == '__main__':
    sys.exit(main())
