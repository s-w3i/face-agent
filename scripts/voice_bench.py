#!/usr/bin/env python3
"""Measure the running voice stack: per-state CPU and synthetic wake/command latency.

Run against a live stack (./start-stack.sh or face-agent.service). Wake trials play
a synthetic "Hi <name>" clip through the default speaker, timestamp agent log lines,
and record the speaker's monitor so phrase end and acknowledgment onset share one clock.

    .venv/bin/python scripts/voice_bench.py cpu --seconds 120
    .venv/bin/python scripts/voice_bench.py wake --trials 10
    .venv/bin/python scripts/voice_bench.py wake --trials 3 --command "What time is it?"

Results go to test-output/bench/*.json (gitignored) and a Markdown summary is printed.

OpenAI cost: wake-only trials are local apart from the Realtime transcription socket
that waking opens (no audio is sent unless someone speaks). `--command` runs a full
paid turn per trial (Realtime STT + agent model + spoken reply), and synthesizing a
clip that is not cached yet is one TTS request; both require --allow-api.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotsctl import request  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'face-agent'
AGENT_LOG = CACHE / 'stack-agent.log'
CLK_TCK = os.sysconf('SC_CLK_TCK')
GROUPS = {
    'chat.py': lambda argv: any(a.endswith('scripts/chat.py') for a in argv),
    'respeaker.py': lambda argv: any(a.endswith('scripts/respeaker.py') for a in argv),
    'tracker': lambda argv: any(a.endswith(('track_human.py', 'human_tracker.py')) for a in argv),
    'serve.py': lambda argv: any(a.endswith('scripts/serve.py') for a in argv),
    'chromium': lambda argv: bool(argv) and 'chromium' in Path(argv[0]).name,
}
SILENT_DB = -60.0


def percentile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, max(0, math.ceil(q / 100 * len(values)) - 1))]


def summary(values):
    values = [v for v in values if v is not None]
    return dict(n=len(values), median=statistics.median(values) if values else None,
                p90=percentile(values, 90), min=min(values, default=None), max=max(values, default=None))


def robot_status(url):
    try:
        return (request(url, 'status').get('robotStatus') or {}).get('status')
    except (OSError, ValueError):
        return None


def processes():
    """Yield (pid, group, utime+stime ticks) for the voice stack processes."""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            stat = (entry / 'stat').read_text()
        except OSError:
            continue
        for group, match in GROUPS.items():
            if match(argv):
                fields = stat.rsplit(')', 1)[1].split()
                yield int(entry.name), group, int(fields[11]) + int(fields[12]), argv
                break


def thermal():
    """SoC temperature (deg C) and firmware throttle flags; sysfs works without /dev/vcio access."""
    try:
        temp = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
    except (OSError, ValueError):
        temp = None
    try:
        throttled = Path('/sys/devices/platform/soc/soc:firmware/get_throttled').read_text().strip()
    except OSError:
        throttled = None
    return temp, throttled


class CpuSampler(threading.Thread):
    """Sample per-group CPU% (of one core) once per interval, tagged by robot status."""

    def __init__(self, url, interval=1.0):
        super().__init__(daemon=True)
        self.url = url; self.interval = interval
        self.stop = threading.Event()
        self.samples = []  # (t, status, {group: pct})
        self.thermal = []  # (t, status, temp, throttled)
        self.worker_cpu = []  # (t, pid, pct, argv) for the respeaker worker

    def run(self):
        previous = {}; last = time.monotonic(); next_thermal = 0
        while not self.stop.wait(self.interval):
            now = time.monotonic(); elapsed = now - last; last = now
            status = robot_status(self.url)
            usage = {group: 0.0 for group in GROUPS}
            current = {}
            for pid, group, ticks, argv in processes():
                current[pid] = ticks
                if pid in previous:
                    pct = (ticks - previous[pid]) / CLK_TCK / elapsed * 100
                    usage[group] += pct
                    if group == 'respeaker.py':
                        self.worker_cpu.append((now, pid, pct, argv))
            previous = current
            self.samples.append((now, status, usage))
            if now >= next_thermal:
                self.thermal.append((now, status, *thermal()))
                next_thermal = now + 10

    def worker_settled(self, since, quiet_pct=25.0, quiet_seconds=1.5, sleeping=True):
        """True once the current wake worker has been quiet (model loaded) for a while."""
        recent = [(t, pid, pct, argv) for t, pid, pct, argv in self.worker_cpu if t >= since]
        if not recent:
            return False
        pid = recent[-1][1]
        argv = recent[-1][3]
        if sleeping and '0.4' not in argv:
            return False
        tail = [pct for t, p, pct, _ in recent if p == pid and t >= time.monotonic() - quiet_seconds]
        return len(tail) >= max(1, int(quiet_seconds / self.interval)) and max(tail) < quiet_pct

    def report(self):
        by_state = {}
        for _, status, usage in self.samples:
            for group, pct in usage.items():
                by_state.setdefault(status or 'UNKNOWN', {}).setdefault(group, []).append(pct)
        return {state: {group: dict(mean=round(statistics.fmean(v), 1), p90=round(percentile(v, 90), 1), n=len(v))
                        for group, v in groups.items()} for state, groups in by_state.items()}


class LogTail(threading.Thread):
    """Timestamp each new agent log line with time.monotonic() on arrival."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = Path(path); self.lines = []; self.stop = threading.Event()
        self.lock = threading.Condition()

    def run(self):
        with self.path.open('rb') as file:
            file.seek(0, os.SEEK_END)
            pending = b''
            while not self.stop.is_set():
                if self.path.stat().st_size < file.tell():
                    file.seek(0)  # Copy-truncate rotation.
                chunk = file.read()
                if not chunk:
                    time.sleep(.01)
                    continue
                pending += chunk
                *complete, pending = pending.split(b'\n')
                now = time.monotonic()
                with self.lock:
                    self.lines.extend((now, line.decode(errors='replace')) for line in complete)
                    self.lock.notify_all()

    def wait_for(self, pattern, after, timeout):
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                for t, line in self.lines:
                    if t >= after and regex.search(line):
                        return t, line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, None
                self.lock.wait(remaining)


class MonitorRecorder(threading.Thread):
    """Per-20 ms RMS (dBFS) of what the sink is actually playing, stamped on arrival."""

    def __init__(self, sink):
        super().__init__(daemon=True)
        self.sink = sink; self.levels = []; self.stop = threading.Event()
        self.process = None

    def run(self):
        import numpy as np
        self.process = subprocess.Popen(['parec', f'--device={self.sink}.monitor', '--rate=16000',
                                         '--channels=1', '--format=s16le', '--latency-msec=20', '--raw'],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while not self.stop.is_set():
                data = self.process.stdout.read(640)
                if len(data) < 640:
                    break
                samples = np.frombuffer(data, '<i2').astype(np.float32)
                rms = math.sqrt(float(np.mean(samples * samples)))
                self.levels.append((time.monotonic(), 20 * math.log10(rms / 32768) if rms else -120.0))
        finally:
            self.process.kill()

    def last_loud(self, start, end):
        loud = [t for t, db in self.levels if start <= t <= end and db > SILENT_DB]
        return loud[-1] if loud else None

    def first_loud(self, start, end):
        return next((t for t, db in self.levels if start <= t <= end and db > SILENT_DB), None)

    def first_quiet(self, start, end, hold=0.2):
        """First moment after start where the sink stays silent for `hold` seconds."""
        run_start = None
        for t, db in self.levels:
            if t < start or t > end:
                continue
            if db <= SILENT_DB:
                run_start = run_start or t
                if t - run_start >= hold:
                    return run_start
            else:
                run_start = None
        return None


def default_sink():
    return subprocess.run(['pactl', 'get-default-sink'], capture_output=True, text=True, check=True).stdout.strip()


def speech_clip(url, text, voice, allow_api=False):
    """Synthesize once through serve.py /api/speech and cache under ~/.cache/face-agent/bench."""
    cache = CACHE / 'bench'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (re.sub(r'[^a-z0-9]+', '-', f'{voice} {text}'.lower()).strip('-') + '.wav')
    if not path.exists():
        if not allow_api:
            raise SystemExit(f'{path.name} is not cached; synthesizing it is one OpenAI TTS request. '
                             'Rerun with --allow-api.')
        print(f'OpenAI: synthesizing {text!r} once (cached afterwards).', file=sys.stderr)
        body = json.dumps(dict(text=text, voice=voice)).encode()
        with urlopen(Request(url.rstrip('/') + '/api/speech', data=body,
                             headers={'Content-Type': 'application/json'}), timeout=120) as response:
            path.write_bytes(response.read())
    return path


def play(path):
    subprocess.run(['paplay', str(path)], check=True)


def wav_onset_db(path, seconds=0.06):
    """RMS of the first `seconds` of a saved utterance; a loud start means a clipped onset."""
    import numpy as np
    with wave.open(str(path)) as wav:
        frames = wav.readframes(round(wav.getframerate() * seconds))
    samples = np.frombuffer(frames, '<i2').astype(np.float32)
    rms = math.sqrt(float(np.mean(samples * samples))) if len(samples) else 0
    return round(20 * math.log10(rms / 32768), 1) if rms else -120.0


def wait_status(url, wanted, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if robot_status(url) in wanted:
            return True
        time.sleep(.1)
    return False


def wait_sleeping_ready(url, cpu, timeout=90):
    """SLEEPING alone is not enough: the wake worker respawns and loads Whisper afterwards."""
    if not wait_status(url, ('SLEEPING',), timeout):
        return False
    since = time.monotonic()
    deadline = since + timeout
    while time.monotonic() < deadline:
        if robot_status(url) != 'SLEEPING':
            return False
        if time.monotonic() - since > 2 and cpu.worker_settled(since):
            return True
        time.sleep(.2)
    return False


def newest_wav(after_wall):
    files = [p for p in (ROOT / 'test-output').glob('respeaker-*.wav') if p.stat().st_mtime >= after_wall]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def wake_trials(args):
    config = request(args.url, 'config')
    name = config.get('appearance', {}).get('name', 'Kuro')
    robot_voice = config.get('settings', {}).get('ttsVoice', 'marin')
    voice = args.voice if args.voice != robot_voice else ('echo' if robot_voice != 'echo' else 'ash')
    if args.command and not args.allow_api:
        raise SystemExit('--command runs a paid OpenAI turn per trial (Realtime STT, agent model, spoken reply). '
                         'Rerun with --allow-api, and keep --trials small.')
    if args.command:
        print(f'OpenAI: {args.trials} full conversation turn(s) will be used.', file=sys.stderr)
    wake_clip = speech_clip(args.url, args.phrase or f'Hi {name}.', voice, args.allow_api)
    command_clip = speech_clip(args.url, args.command, voice, args.allow_api) if args.command else None
    sink = args.sink or default_sink()
    log = LogTail(AGENT_LOG); log.start()
    monitor = MonitorRecorder(sink); monitor.start()
    cpu = CpuSampler(args.url); cpu.start()  # Also needed to know when the wake worker is ready.
    trials = []
    try:
        for trial in range(1, args.trials + 1):
            if not wait_sleeping_ready(args.url, cpu):
                trials.append(dict(trial=trial, error='robot did not settle in SLEEPING'))
                print(f'trial {trial}: robot did not settle in SLEEPING; stopping.', file=sys.stderr)
                break
            time.sleep(args.settle)
            t_play = time.monotonic()
            play(wake_clip)
            t_done = time.monotonic()
            t_wake, _ = log.wait_for(r'Wake phrase detected', t_play, args.wake_timeout)
            phrase_end = monitor.last_loud(t_play, t_done + .3)
            result = dict(trial=trial, detected=t_wake is not None)
            if t_wake is None:
                print(f'trial {trial}: no wake detected', file=sys.stderr)
                trials.append(result)
                continue
            t_ack_line, _ = log.wait_for(r'^robot> Yes\? I am here', t_play, 15)
            t_ready, _ = log.wait_for(r'Wake command input ready\.', t_play, 60)
            ack_onset = monitor.first_loud(phrase_end + .15, (t_ready or t_wake + 10) + 5) if phrase_end else None
            ms = lambda a, b: round((a - b) * 1000) if a is not None and b is not None else None
            result.update(wake_detect_ms=ms(t_wake, phrase_end), ack_audible_ms=ms(ack_onset, phrase_end),
                          ack_request_ms=ms(t_ack_line, phrase_end), ready_ms=ms(t_ready, phrase_end))
            if command_clip and t_ready:
                time.sleep(args.command_delay)
                wall = time.time()
                t_cmd = time.monotonic()
                play(command_clip)
                t_you, you = log.wait_for(r'^you> ', t_cmd, 30)
                t_robot, _ = log.wait_for(r'^robot> ', t_cmd, 90)
                saved = newest_wav(wall)
                result.update(command_transcript=you[5:] if you else None, reply_ms=ms(t_robot, t_cmd),
                              command_wav=str(saved) if saved else None,
                              command_onset_db=wav_onset_db(saved) if saved else None)
            print(f'trial {trial}: ' + json.dumps({k: v for k, v in result.items() if k != 'trial'}), flush=True)
            trials.append(result)
            # The robot falls asleep after its idle timeout; wait for that before the next trial.
            wait_status(args.url, ('SLEEPING',), 120)
    finally:
        for thread in (log, monitor, cpu):
            thread.stop.set()
    return dict(kind='wake', phrase=args.phrase or f'Hi {name}.', voice=voice, sink=sink,
                command=args.command, trials=trials,
                wake_detect_ms=summary([t.get('wake_detect_ms') for t in trials]),
                ack_audible_ms=summary([t.get('ack_audible_ms') for t in trials]),
                ready_ms=summary([t.get('ready_ms') for t in trials]),
                detected=sum(1 for t in trials if t.get('detected')),
                cpu=cpu.report(), thermal=cpu.thermal)


def cpu_only(args):
    cpu = CpuSampler(args.url); cpu.start()
    try:
        time.sleep(args.seconds)
    finally:
        cpu.stop.set()
    return dict(kind='cpu', seconds=args.seconds, cpu=cpu.report(), thermal=cpu.thermal)


def markdown(result):
    lines = []
    if result['kind'] == 'wake':
        lines += [f"Wake trials: {result['detected']}/{len(result['trials'])} detected "
                  f"(phrase {result['phrase']!r}, voice {result['voice']}, sink {result['sink']})", '',
                  '| Metric | n | median ms | p90 ms | min | max |', '| --- | --- | --- | --- | --- | --- |']
        for key in ('wake_detect_ms', 'ack_audible_ms', 'ready_ms'):
            s = result[key]
            lines.append(f"| {key} | {s['n']} | {s['median']} | {s['p90']} | {s['min']} | {s['max']} |")
        lines.append('')
    states = sorted(result['cpu'])
    groups = list(GROUPS)
    lines += ['| State | ' + ' | '.join(groups) + ' |', '| --- |' + ' --- |' * len(groups)]
    for state in states:
        row = result['cpu'][state]
        lines.append(f'| {state} (n={next(iter(row.values()))["n"]}) | ' +
                     ' | '.join(f"{row[g]['mean']}" for g in groups) + ' |')
    if result.get('thermal'):
        temps = [t[2] for t in result['thermal'] if t[2] is not None]
        throttled = sorted({t[3] for t in result['thermal'] if t[3]})
        if temps:
            lines += ['', f'Temperature {min(temps):.1f}–{max(temps):.1f} °C; get_throttled: {", ".join(throttled)}']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', default=os.environ.get('DOTS_URL', 'http://127.0.0.1:5173'))
    parser.add_argument('--out', help='JSON result path (default test-output/bench/<kind>-<time>.json)')
    sub = parser.add_subparsers(dest='kind', required=True)
    cpu = sub.add_parser('cpu', help='Sample per-process CPU%% by robot status')
    cpu.add_argument('--seconds', type=float, default=120)
    wake = sub.add_parser('wake', help='Synthetic wake trials (robot must be able to reach SLEEPING)')
    wake.add_argument('--trials', type=int, default=10)
    wake.add_argument('--phrase', help='Wake phrase text (default "Hi <name>.")')
    wake.add_argument('--command', help='Speak this command after each wake (paid turn; needs --allow-api)')
    wake.add_argument('--allow-api', action='store_true',
                      help='Permit OpenAI calls: --command turns and synthesizing uncached clips')
    wake.add_argument('--command-delay', type=float, default=0.3)
    wake.add_argument('--voice', default='ash', help='Synthetic speaker voice; never the robot voice')
    wake.add_argument('--sink', help='PulseAudio sink to monitor (default: default sink)')
    wake.add_argument('--settle', type=float, default=1.0, help='Extra seconds after the wake worker is ready')
    wake.add_argument('--wake-timeout', type=float, default=25, help='Seconds to wait for a wake detection')
    args = parser.parse_args()
    result = wake_trials(args) if args.kind == 'wake' else cpu_only(args)
    out = Path(args.out) if args.out else ROOT / 'test-output' / 'bench' / f"{args.kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(markdown(result))
    print(f'\nSaved {out}')


if __name__ == '__main__':
    main()
