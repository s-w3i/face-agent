"""Supervision helpers for the microphone worker's threads (phase 5).

Python threads cannot be killed, so the policy is: a thread that *raised* may be
restarted within a budget; a thread that is *stuck* (no heartbeat) is reported with
its stack and escalated, so the parent replaces the whole worker through its
existing recovery path.
"""
from collections import deque
from pathlib import Path
import sys
import threading
import time
import traceback


class Heartbeat:
    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self.last = time.monotonic()
        self.paused = False  # e.g. while a model loads

    def beat(self):
        self.last = time.monotonic()

    def stale(self, now=None):
        return not self.paused and (time.monotonic() if now is None else now) - self.last > self.limit


class RestartBudget:
    """At most `limit` restarts per `window` seconds."""

    def __init__(self, limit=3, window=300.0):
        self.limit = limit; self.window = window
        self.times = deque()

    def allow(self, now=None):
        now = time.monotonic() if now is None else now
        while self.times and now - self.times[0] > self.window:
            self.times.popleft()
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True


class DropRate:
    """Warn when a subscription drops more than `ratio` of its blocks over `window` seconds."""

    def __init__(self, ratio=.01, window=10.0):
        self.ratio = ratio; self.window = window
        self.start = None  # (t, published, dropped)

    def check(self, published, dropped, now=None):
        now = time.monotonic() if now is None else now
        if self.start is None or published < self.start[1]:
            self.start = (now, published, dropped)
            return None
        t0, published0, dropped0 = self.start
        if now - t0 < self.window:
            return None
        self.start = (now, published, dropped)
        blocks = published - published0
        lost = dropped - dropped0
        if blocks > 0 and lost / blocks > self.ratio:
            return lost / blocks
        return None


def stack_of(thread):
    frame = sys._current_frames().get(thread.ident)
    return ''.join(traceback.format_stack(frame)) if frame else '(no frame)'


def thread_cpu():
    """CPU seconds per live thread name (from /proc; works for threads we did not write)."""
    result = {}
    tick = 100.0
    try:
        import os
        tick = float(os.sysconf('SC_CLK_TCK'))
    except (ValueError, OSError, AttributeError):
        pass
    for thread in threading.enumerate():
        native = getattr(thread, 'native_id', None)
        if native is None:
            continue
        try:
            fields = Path(f'/proc/self/task/{native}/stat').read_text().rsplit(')', 1)[1].split()
        except OSError:
            continue
        result[thread.name] = round(result.get(thread.name, 0) + (int(fields[11]) + int(fields[12])) / tick, 2)
    return result


def rss_kb():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def join_all(threads, timeout=2.0):
    """Join each thread with a timeout; return the names that did not stop."""
    stuck = []
    for thread in threads:
        if thread is None:
            continue
        thread.join(timeout)
        if thread.is_alive():
            stuck.append(thread.name)
    return stuck
