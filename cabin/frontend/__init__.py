"""Audio front end: a worker thread that runs the mic pipeline off the event loop."""
from .worker import Context, FrontEnd, FrontEvent, Stage


def vocabulary_provider() -> list[str]:
    """On-screen names the recogniser should favour; the frontend plugin replaces this."""
    return []


def on_asr_retry() -> None:
    """Called when the transcript gate wants the driver to repeat; the frontend plugin replaces this."""


def on_asr_ok() -> None:
    """Called when the transcript gate accepts an utterance; the frontend plugin replaces this."""


def gate_bypass() -> bool:
    """True while the next utterance must reach its handler untouched (a pending one-time code)."""
    return False


def wake_required() -> bool:
    """True when the next utterance must begin with the wake phrase; the frontend plugin replaces this."""
    return True


def wake_transcriber():
    """The ``pcm16 bytes -> text`` function that checks for the wake phrase, or None; the frontend plugin replaces this."""
    return


__all__ = ["Context", "FrontEnd", "FrontEvent", "Stage", "gate_bypass", "on_asr_ok", "on_asr_retry", "vocabulary_provider", "wake_required", "wake_transcriber"]
