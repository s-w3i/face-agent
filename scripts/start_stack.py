#!/usr/bin/env python3
"""Supervise the ROS camera, face display, and voice agent in a desktop session."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import subprocess
import time
import sys
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Also when loaded by path (tests).
import faults  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HEALTHY_SECONDS = 30  # A restarted component running this long clears its fault.
CAMERA_VENDOR = '2bc5'  # Orbbec
# orbbec_camera log lines that mean the camera is stuck, and how many within the window
# trigger a USB reset. Neither appears while it streams normally.
CAMERA_FAILURES = (
    (b'Device initialization failed after', 2),  # Its 3 attempts failed (~11 s per cycle).
    (b'Device response size(0)', 20),  # Opened, but the device answers nothing (~1 per second).
)
USBDEVFS_RESET = 0x5514  # _IO('U', 20); what usbreset(1) does. /dev/bus/usb is writable via the Orbbec udev rule.


def stop(process, timeout=40):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def log_tail(path, size=3000):
    try:
        with open(path, 'rb') as source:
            source.seek(max(0, os.path.getsize(path) - size))
            return source.read().decode(errors='replace')
    except OSError:
        return ''


class Component:
    def __init__(self, name, command, env, logs, url=None):
        self.name, self.command, self.env = name, command, env
        self.output = (logs / f'stack-{name}.log').open('a', buffering=1)
        self.process = None
        self.next_start = 0
        self.faults = faults.Reporter('stack-' + name, url) if url else faults.NULL
        self.restarted = None  # monotonic start time after an unexpected exit

    def start(self):
        print(f'Starting {self.name}; log: {self.output.name}', flush=True)
        if self.restarted is not None:
            self.restarted = time.monotonic()
        self.process = subprocess.Popen(self.command, cwd=ROOT, env=self.env,
                                        stdin=subprocess.DEVNULL, stdout=self.output,
                                        stderr=subprocess.STDOUT, start_new_session=True)

    def maintain(self):
        if self.process is not None and self.process.poll() is not None:
            print(f'{self.name} exited ({self.process.returncode}); retrying in 5 seconds.', flush=True)
            self.faults.report('component_restarting', f'{self.name} exited with code {self.process.returncode}; '
                               f'restarting. Log: {self.output.name}', title=f'Restarting {self.name}',
                               detail=log_tail(self.output.name))
            self.restarted = 0
            self.process = None
            self.next_start = time.monotonic() + 5
        elif self.restarted and time.monotonic() - self.restarted >= HEALTHY_SECONDS:
            self.restarted = None
            self.faults.clear()
        if self.process is None and time.monotonic() >= self.next_start:
            self.start()

    def close(self):
        stop(self.process)
        self.process = None


def reset_usb_devices(vendor, sysfs=Path('/sys/bus/usb/devices'), devfs=Path('/dev/bus/usb'), ioctl=fcntl.ioctl):
    """USB-reset every device from `vendor`, so the kernel re-binds its drivers. Returns the ports reset."""
    done = []
    for device in sorted(sysfs.glob('*')):
        try:
            if (device / 'idVendor').read_text().strip() != vendor:
                continue
            node = devfs / f"{int((device / 'busnum').read_text()):03d}" / f"{int((device / 'devnum').read_text()):03d}"
        except (OSError, ValueError):
            continue  # Interfaces and root hubs have no idVendor.
        try:
            fd = os.open(node, os.O_WRONLY)
            try:
                ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
            done.append(device.name)
        except OSError as error:
            print(f'Could not reset USB device {device.name}: {error}', flush=True)
    return done


class CameraWatchdog:
    """Reset the camera over USB when its driver keeps failing to open it.

    After a USB drop the Orbbec can come back with its streaming interfaces unbound
    ("No v4l device found", "'color sensor' not found"), or stay open but answer
    nothing. orbbec_camera then retries or idles forever without exiting, and
    relaunching it alone never helps; a USB reset first does.
    """

    def __init__(self, camera, failures=CAMERA_FAILURES, window=60, cooldown=300, settle=3, reset=reset_usb_devices):
        self.camera, self.failures, self.window, self.cooldown, self.settle = camera, failures, window, cooldown, settle
        self.reset = reset
        self.offset = None
        self.tail = b''
        self.keep = max(len(marker) for marker, _ in failures) - 1
        self.seen = {marker: [] for marker, _ in failures}  # monotonic times of recent failure lines
        self.last_reset = None

    def new_output(self, limit=4 << 20):
        path = self.camera.output.name
        try:
            size = os.path.getsize(path)
        except OSError:
            return b''
        if self.offset is None:  # Earlier runs' failures are not ours to act on.
            self.offset = size
        if size < self.offset:  # Copy-truncated by rotate_logs.
            self.offset, self.tail = 0, b''
        self.offset = max(self.offset, size - limit)
        try:
            with open(path, 'rb') as source:
                source.seek(self.offset)
                data = source.read(size - self.offset)
        except OSError:
            return b''
        self.offset += len(data)
        return data

    def poll(self, now):
        """Check the camera log; returns True when it reset the camera."""
        data = self.tail + self.new_output()
        self.tail = data[-self.keep:]
        for marker, _ in self.failures:
            self.seen[marker] = [t for t in self.seen[marker] if now - t < self.window] + [now] * data.count(marker)
        quiet = not any(self.seen.values())
        if self.last_reset is not None and quiet and now - self.last_reset > self.window:
            self.camera.faults.clear('camera_unavailable')
            self.last_reset = None
        stuck = [marker for marker, limit in self.failures if len(self.seen[marker]) >= limit]
        cooled = self.last_reset is None or now - self.last_reset >= self.cooldown
        if not stuck or not cooled or self.camera.process is None:
            return False
        print(f'Camera stuck ({stuck[0].decode()!r} {len(self.seen[stuck[0]])} times in {self.window} s); '
              'resetting it over USB.', flush=True)
        stop(self.camera.process)
        self.camera.process = None
        ports = self.reset(CAMERA_VENDOR)
        self.camera.faults.report(
            'camera_unavailable',
            (f'The camera stopped responding; reset it over USB (port {", ".join(ports)}) and restarting it.'
             if ports else 'The camera stopped responding and no Orbbec device is on USB; restarting it.'),
            title='Resetting camera', detail=log_tail(self.camera.output.name))
        self.camera.next_start = now + self.settle
        self.seen = {marker: [] for marker, _ in self.failures}
        self.tail, self.last_reset = b'', now
        return True


def rotate_logs(logs, max_bytes, keep):
    """Copy-truncate oversized *.log files; every writer opens them in append mode."""
    for path in logs.glob('*.log'):
        try:
            size = path.stat().st_size
            if size <= max_bytes:
                continue
            for number in range(keep, 0, -1):
                older = path.with_name(f'{path.name}.{number}')
                if number == keep:
                    older.unlink(missing_ok=True)
                elif older.exists():
                    older.rename(path.with_name(f'{path.name}.{number + 1}'))
            partial = path.with_name(path.name + '.1.tmp')
            with path.open('rb') as source, partial.open('wb') as target:
                source.seek(max(0, size - max_bytes))
                shutil.copyfileobj(source, target)
            partial.replace(path.with_name(path.name + '.1'))
            os.truncate(path, 0)
        except OSError as error:
            print(f'Could not rotate {path.name}: {error}', flush=True)


def display_ready(base):
    try:
        with urlopen(base + '/api/status', timeout=1) as response:
            status = json.load(response)
        return bool(status.get('displays') and status.get('robotStatus'))
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise SystemExit('A desktop session is required. Start after graphical login.')
    if not (ROOT / '.venv/bin/python').exists():
        raise SystemExit('Run bash scripts/setup_respeaker.sh before starting the stack.')
    profile = os.environ.get('DOTS_CAMERA_PROFILE', 'rgbd')
    if profile not in ('rgbd', 'color'):
        raise SystemExit('DOTS_CAMERA_PROFILE must be rgbd or color.')
    logs = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent'
    logs.mkdir(parents=True, exist_ok=True)
    try:
        max_log_bytes = int(float(os.environ.get('DOTS_LOG_MAX_MB', '10')) * 1024 * 1024)
        log_backups = max(1, int(os.environ.get('DOTS_LOG_KEEP', '3')))
    except ValueError:
        raise SystemExit('DOTS_LOG_MAX_MB must be a number and DOTS_LOG_KEEP an integer.')
    lock = (logs / 'stack.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('The face-agent stack is already running.', flush=True)
        return 0

    env = os.environ.copy()
    env.setdefault('DOTS_RENDERER', 'prerendered')
    env.setdefault('DOTS_FULLSCREEN', '1')
    env.setdefault('DOTS_ACCESS_LOG', '0')
    env['PYTHONUNBUFFERED'] = '1'
    port = int(env.get('PORT', '5173'))
    host = env.get('HOST', '127.0.0.1')
    host = '127.0.0.1' if host == '0.0.0.0' else host
    base = f'http://{host}:{port}'
    with socket.socket() as probe:
        if probe.connect_ex((host, port)) == 0:
            raise SystemExit(f'Port {port} is already in use. Stop the old display/server first.')

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    components = []
    next_rotation = 0
    try:
        # Start our authority first so a fresh boot begins asleep. An existing
        # authority keeps its state and is never killed by this launcher.
        rotate_logs(logs, max_log_bytes, log_backups)
        manager = Component('status', [str(ROOT / 'robot-status.sh'), '--initial-status',
                                      env.get('DOTS_INITIAL_STATUS', 'SLEEPING')], env, logs)
        components.append(manager)
        manager.start()
        # Allow the manager to initialize before a display subscriber autostarts one.
        from rclpy import init, shutdown, create_node
        init()
        node = create_node('face_agent_startup')
        try:
            # Availability is checked without importing the not-yet-built custom types.
            deadline = time.monotonic() + 120
            while not any(name == '/robot/set_status' for name, _ in node.get_service_names_and_types()):
                if manager.process.poll() not in (None, 0) or time.monotonic() >= deadline:
                    raise RuntimeError('Status manager did not become ready; see stack-status.log.')
                time.sleep(.2)
        finally:
            node.destroy_node()
            shutdown()
        owns_manager = manager.process.poll() is None
        if not owns_manager:
            print('Using the existing robot status manager.', flush=True)

        camera = Component('camera', [str(ROOT / ('camera-rgbd.sh' if profile == 'rgbd' else 'camera.sh'))], env, logs, base)
        display = Component('display', [str(ROOT / 'run.sh'), '--robot'], env, logs, base)
        agent = Component('agent', [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/chat.py'),
                                   '--mode', 'voice', '--url', base], env, logs, base)
        components.extend((camera, display, agent))
        watchdog = CameraWatchdog(camera) if env.get('DOTS_CAMERA_USB_RESET', '1') != '0' else None
        camera.start()
        display.start()
        waiting = True
        while True:
            if time.monotonic() >= next_rotation:
                rotate_logs(logs, max_log_bytes, log_backups)
                next_rotation = time.monotonic() + 60
            if owns_manager:
                if manager.process is not None and manager.process.poll() == 0:
                    owns_manager = False
                    print('Using the existing robot status manager.', flush=True)
                else:
                    manager.maintain()
            camera.maintain()
            if watchdog:
                watchdog.poll(time.monotonic())
            if display.process is not None and display.process.poll() is not None:
                agent.close()
                waiting = True
            display.maintain()
            if display.process is not None and display.process.poll() is None and display_ready(base):
                if waiting:
                    print('Face display and ROS status ready; starting voice conversation.', flush=True)
                    waiting = False
                agent.maintain()
            time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping the face-agent stack.', flush=True)
        return 0
    except (OSError, RuntimeError) as error:
        print(str(error), flush=True)
        return 1
    finally:
        for component in reversed(components):
            component.close()
            component.output.close()
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
