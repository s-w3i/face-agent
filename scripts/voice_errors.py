"""Recoverable speech-service errors shared across microphone IPC."""


class RealtimeUnavailable(RuntimeError):
    """Remote transcription failed; distinguish reconnects from configuration fixes."""
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable
