import os
import tempfile

os.environ.setdefault("NOVA_AUDIO_MUTE", "1")
# Tests never touch the real wallet, ledger, memory or last-fix files.
os.environ.setdefault("NOVA_CABIN_DATA", tempfile.mkdtemp(prefix="nova-cabin-tests-"))

import pytest  # noqa: E402

_HOOKS = ("vocabulary_provider", "on_asr_retry", "on_asr_ok", "gate_bypass", "wake_required", "wake_transcriber")


@pytest.fixture(autouse=True)
def _restore_frontend_hooks():
    """The frontend plugin swaps module-level hooks; put the defaults back after every test."""
    from cabin import frontend
    saved = {name: getattr(frontend, name) for name in _HOOKS}
    yield
    for name, fn in saved.items():
        setattr(frontend, name, fn)
