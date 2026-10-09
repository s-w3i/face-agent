"""Recoverable speech-service errors shared across microphone IPC."""


class RealtimeUnavailable(RuntimeError):
    """Remote transcription failed; return to the local wake detector."""
