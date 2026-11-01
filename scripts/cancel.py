"""Generation-scoped cancellation: every capture/STT turn checks its own token.

A single shared Event would need someone to clear it, and a wake hit would cancel
the very capture it starts. Instead CancelScope.new() cancels the previous token
and hands out a fresh one, so a trigger cancels what came before but never what
it starts next.
"""
import threading


class CancelToken:
    def __init__(self, generation):
        self.generation = generation
        self.reason = None
        self._event = threading.Event()

    def cancel(self, reason='cancelled'):
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def __repr__(self):
        return f'CancelToken({self.generation}, cancelled={self.cancelled}, reason={self.reason!r})'


class CancelScope:
    """Owns "the current" token; thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self.generation = 0
        self.current = None

    def new(self):
        with self._lock:
            if self.current is not None:
                self.current.cancel('superseded')
            self.generation += 1
            self.current = CancelToken(self.generation)
            return self.current

    def cancel_current(self, reason):
        with self._lock:
            if self.current is not None:
                self.current.cancel(reason)
