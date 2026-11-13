#!/usr/bin/env python3
"""What is wrong with Kuro right now, and what went wrong recently.

Read-only. Live checks use local files, systemd and TCP handshakes only; nothing
is sent to OpenAI and no API key is read beyond checking that one exists.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import faults  # noqa: E402

CACHE = faults.log_path().parent
LOGS = ('stack-agent.log', 'microphone-chat.log', 'stack-display.log', 'stack-camera.log',
        'stack-status.log', 'tracking-chat.log', 'robot-status-bridge.log')
INTERESTING = re.compile(r'error|traceback|failed|unavailable|exited|timed out|warning|lost|denied|cannot|could not', re.I)
RESPEAKER = ('2886', '0018')
ORBBEC_VENDOR = '2bc5'
USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')


def paint(text, color):
    codes = dict(red='31', yellow='33', green='32', dim='2', bold='1')
    return f'\x1b[{codes[color]}m{text}\x1b[0m' if USE_COLOR else text


def run(command, timeout=3):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)


def startup_env():
    path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'face-agent/startup.env'
    values = {}
    try:
        for line in path.read_text().splitlines():
            match = re.match(r'\s*([A-Z_][A-Z0-9_]*)=(.*)', line)
            if match:
                values[match[1]] = match[2].split('#')[0].strip().strip('"\'')
    except OSError:
        pass
    return values


def service_url():
    env = startup_env()
    return os.environ.get('DOTS_URL') or f"http://127.0.0.1:{os.environ.get('PORT') or env.get('PORT') or '5173'}"


def robot_status(url):
    try:
        with urlopen(url + '/api/status', timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def usb_device(vendor, product=None):
    for device in Path('/sys/bus/usb/devices').glob('*'):
        try:
            if (device / 'idVendor').read_text().strip() != vendor:
                continue
            if product and (device / 'idProduct').read_text().strip() != product:
                continue
            bus = int((device / 'busnum').read_text()); number = int((device / 'devnum').read_text())
            return Path(f'/dev/bus/usb/{bus:03d}/{number:03d}')
        except (OSError, ValueError):
            continue
    return None


def lock_held(path):
    try:
        with open(path, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return None


def checks():
    """(name, ok: True/False/None for n/a, text)."""
    results = []
    started = time.monotonic()
    code = faults.network_code()
    took = time.monotonic() - started
    results.append(('Internet', code is None, {None: f'api.openai.com:443 reachable ({took:.1f} s)',
                    'internet_down': 'no internet: api.openai.com and public hosts unreachable',
                    'openai_unreachable': 'internet works, but api.openai.com:443 does not answer'}[code]))

    node = usb_device(*RESPEAKER)
    if node is None:
        results.append(('ReSpeaker USB', False, '2886:0018 not connected; replug the microphone'))
    elif not os.access(node, os.R_OK | os.W_OK):
        results.append(('ReSpeaker USB', False, f'connected, but {node} is not accessible: add the udev rule / plugdev group'))
    else:
        results.append(('ReSpeaker USB', True, f'connected ({node})'))

    status, source = run(['pactl', 'get-default-source'])
    if status == 0:
        good = any(word in source.lower() for word in ('respeaker', 'seeed', '2886'))
        results.append(('Default microphone', good, source if good else source + ' (not the ReSpeaker; voice uses the device directly, but check)'))
    else:
        results.append(('Default microphone', None, 'pactl unavailable: ' + source[:80]))

    camera = usb_device(ORBBEC_VENDOR)
    results.append(('Orbbec camera USB', camera is not None, 'connected' if camera else 'not connected (vision and eye tracking unavailable)'))

    hailo = Path('/dev/hailo0').exists()
    status, state = run(['systemctl', 'is-active', 'hailort.service'])
    if not hailo:
        results.append(('Hailo-8L', None, '/dev/hailo0 absent; tracking uses the CPU'))
    else:
        results.append(('Hailo-8L', state == 'active', f'/dev/hailo0 present, hailort.service {state or "unknown"}'))

    status, state = run(['systemctl', '--user', 'is-active', 'face-agent.service'])
    results.append(('face-agent.service', state == 'active', state or 'unknown'))

    domain = os.environ.get('ROS_DOMAIN_ID') or startup_env().get('ROS_DOMAIN_ID') or '0'
    held = lock_held(CACHE / f'robot-status-{domain}.lock')
    results.append(('ROS status manager', held, {True: f'running (ROS_DOMAIN_ID={domain})', False: f'not running (ROS_DOMAIN_ID={domain})',
                                                 None: 'lock file unreadable'}[held]))

    key = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'mimo-dots/openai-api-key'
    if os.environ.get('OPENAI_API_KEY', '').strip():
        results.append(('OpenAI API key', True, 'from OPENAI_API_KEY'))
    else:
        try:
            present = bool(key.read_text().strip())
        except OSError:
            present = False
        results.append(('OpenAI API key', present, f'saved in {key}' if present else 'not set: studio → Voice → Set up API key'))

    navigation = os.environ.get('DOTS_NAV_MCP_URL') or startup_env().get('DOTS_NAV_MCP_URL')
    if navigation:
        target = urlparse(navigation)
        port = target.port or (443 if target.scheme == 'https' else 80)
        reachable = faults._connects(target.hostname, port, 1.5)
        results.append(('Navigation robot', reachable, f'{target.hostname}:{port} ' + (
            'reachable' if reachable else 'unreachable: navigation will fail, and ROS/DDS logs fill with "retcode -3"')))

    free = shutil.disk_usage(CACHE if CACHE.exists() else Path.home()).free / 1024 ** 3
    results.append(('Disk space', free >= .5, f'{free:.1f} GB free'))
    return results


def parse_since(text):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhd])', text.strip())
    if not match:
        raise argparse.ArgumentTypeError('Use a duration like 30m, 2h or 1d.')
    return float(match[1]) * dict(s=1, m=60, h=3600, d=86400)[match[2]]


def read_events(since_seconds):
    path = faults.log_path()
    events = []
    for source in (path.with_name(path.name + '.1'), path):
        try:
            lines = source.read_text(errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    cutoff = time.time() - since_seconds
    return [event for event in events if stamp(event) >= cutoff]


def stamp(event):
    try:
        return time.mktime(time.strptime(event.get('time', '')[:19], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return 0


def format_event(event, detail=False):
    when = event.get('time', '')[5:19].replace('T', ' ')
    kind = event.get('event', '?')
    code = event.get('code') or 'all'
    where = event.get('component', '')
    if kind == 'clear':
        extra = []
        if event.get('reason'):
            extra.append(event['reason'])
        if event.get('lasted') is not None:
            extra.append(f"lasted {duration(event['lasted'])}")
        if event.get('repeats', 1) > 1:
            extra.append(f"seen {event['repeats']}×")
        line = f"{when}  {paint('cleared', 'green')}  {code} [{where}]" + (f" ({', '.join(extra)})" if extra else '')
    else:
        color = 'red' if event.get('severity') == 'error' else 'yellow' if event.get('severity') == 'degraded' else 'dim'
        line = (f"{when}  {paint(event.get('severity', '?'), color):<8} {paint(event.get('title', code), 'bold')}"
                f"  [{where}{' · direct' if event.get('via') == 'direct' else ''}]\n"
                f"{'':16}{event.get('message', '')}")
        if event.get('hint'):
            line += f"\n{'':16}{paint('fix: ' + event['hint'], 'dim')}"
    if detail and event.get('detail'):
        line += '\n' + '\n'.join(f"{'':16}{paint(row, 'dim')}" for row in event['detail'].rstrip().splitlines()[-25:])
    return line


def duration(seconds):
    seconds = float(seconds)
    if seconds < 90:
        return f'{seconds:.0f} s'
    if seconds < 5400:
        return f'{seconds / 60:.0f} min'
    return f'{seconds / 3600:.1f} h'


def log_highlights(limit):
    found = []
    for name in LOGS:
        path = CACHE / name
        try:
            with path.open('rb') as source:
                start = max(0, path.stat().st_size - 65536)
                source.seek(start)
                lines = source.read().decode(errors='replace').splitlines()[1 if start else 0:]  # Skip a cut line.
        except OSError:
            continue
        # Collapse repeats that differ only in numbers (timestamps, ports): DDS spam buries real warnings.
        groups = {}
        for line in lines:
            line = line.strip()
            if INTERESTING.search(line) and '"status": "metrics"' not in line:
                key = re.sub(r'\d+(?:\.\d+)?', '#', line)
                count = groups.pop(key, (None, 0))[1]
                groups[key] = (line, count + 1)
        if groups:
            # The dict keeps insertion order; re-inserting moves the newest occurrence last.
            hits = [line + (f'  (×{count} in the last 64 KB)' if count > 1 else '')
                    for line, count in list(groups.values())[-limit:]]
            found.append((path, hits))
    return found


def follow(detail):
    path = faults.log_path()
    print(paint(f'Following {path} (Ctrl+C stops)…', 'dim'), flush=True)
    position = path.stat().st_size if path.exists() else 0
    try:
        while True:
            size = path.stat().st_size if path.exists() else 0
            if size < position:
                position = 0  # Rotated (copy-truncate).
            if size > position:
                with path.open() as source:
                    source.seek(position)
                    chunk = source.read()
                    position = source.tell()
                for line in chunk.splitlines():
                    try:
                        print(format_event(json.loads(line), detail), flush=True)
                    except ValueError:
                        pass
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--since', type=parse_since, default=parse_since('24h'), help='History window (default 24h).')
    parser.add_argument('--detail', action='store_true', help='Show tracebacks and component log tails.')
    parser.add_argument('--follow', '-f', action='store_true', help='Print new fault events as they happen.')
    parser.add_argument('--json', action='store_true', help='Machine-readable output.')
    parser.add_argument('--no-checks', action='store_true', help='Skip the live checks (they take up to ~6 s offline).')
    args = parser.parse_args()
    if args.follow:
        return follow(args.detail)

    url = service_url()
    status = robot_status(url)
    active = (status or {}).get('faults', {}).get('faults', [])
    results = [] if args.no_checks else checks()
    events = read_events(args.since)
    if args.json:
        print(json.dumps(dict(service=url, robotStatus=(status or {}).get('robotStatus'), faults=active,
                              checks=[dict(name=n, ok=o, text=t) for n, o, t in results],
                              events=events if args.detail else [{k: v for k, v in e.items() if k != 'detail'} for e in events]),
                         indent=2))
        return 1 if any(f['severity'] == 'error' for f in active) or any(o is False for _, o, _ in results) else 0

    print(paint('Now', 'bold'))
    if status is None:
        print(f"  {paint('✗', 'red')} Robot service at {url} is not answering (display/serve.py down?). See stack-display.log.")
    else:
        robot = status.get('robotStatus') or {}
        print(f"  Robot status: {paint(robot.get('status', 'unknown'), 'red' if robot.get('status') == 'ERROR' else 'bold')}"
              f"{' (set by ' + robot['source'] + ')' if robot.get('source') else ''} · displays open: {status.get('displays', 0)}")
        if not active:
            print(f"  {paint('✓', 'green')} No active faults.")
        for fault in active:
            mark = paint('✗', 'red') if fault['severity'] == 'error' else paint('!', 'yellow')
            age = duration(time.time() - fault['since']) if fault.get('since') else '?'
            print(f"  {mark} {paint(fault['title'], 'bold')} [{fault['code']} · {fault['component']}] for {age}"
                  f"{' · seen ' + str(fault['count']) + '×' if fault.get('count', 1) > 1 else ''}")
            if fault.get('message'):
                print(f"      {fault['message']}")
            print(f"      {paint('fix: ' + fault['hint'], 'dim')}")

    if results:
        print('\n' + paint('Live checks', 'bold'))
        for name, ok, text in results:
            mark = paint('✓', 'green') if ok else paint('✗', 'red') if ok is False else paint('–', 'dim')
            print(f'  {mark} {name:<20} {text}')

    print('\n' + paint(f'Fault history ({faults.log_path()})', 'bold'))
    if not events:
        print('  Nothing recorded in this window.')
    for event in events[-20:]:
        print('  ' + format_event(event, args.detail).replace('\n', '\n  '))

    highlights = log_highlights(8 if args.detail else 3)
    if highlights:
        print('\n' + paint('Recent warnings in component logs', 'bold'))
        for path, lines in highlights:
            print(f'  {path}')
            for line in lines:
                print('    ' + paint(line[:220], 'dim'))
    print('\n' + paint('Deeper checks: .venv/bin/python scripts/respeaker.py doctor --diagnostics (stop the stack first) · '
                       'journalctl --user -u face-agent.service', 'dim'))
    return 1 if any(f['severity'] == 'error' for f in active) or any(o is False for _, o, _ in results) else 0


if __name__ == '__main__':
    sys.exit(main())
