"""Front-end plugin: spoken retry prompt for rejected transcripts and on-screen vocabulary for Parakeet."""
from __future__ import annotations

import asyncio
import logging
import time

from .. import frontend
from .asr_gate import ASR_RETRY_PROMPT

log = logging.getLogger("frontend.plugin")
MAX_SCREEN_NAMES = 40
COOLDOWN_S = 1.5


def otp_pending(engine) -> bool:
    pay = getattr(engine, "payment", None)
    if pay is None:
        return False
    try:
        return bool(pay.snapshot().get("otp_pending"))
    except Exception:  # noqa: BLE001
        return False


WAKE_FOLLOWUP_S = 8.0


def checkout_open(engine) -> bool:
    """A payment is on screen waiting for the driver (approve, Face ID, or a code)."""
    pay = getattr(engine, "payment", None)
    return pay is not None and getattr(pay, "co", None) is not None


def needs_wake(engine) -> bool:
    """Hands-free turns start with the wake phrase. A tap is push-to-talk, and so is
    an open checkout or the short window after Nova finishes speaking."""
    if getattr(engine, "_from_tap", False):
        return False
    return not (otp_pending(engine) or followup(engine))


def followup(engine) -> bool:
    """A bare answer needs no wake phrase while a checkout is open, and for a few
    seconds after Nova finishes speaking."""
    if checkout_open(engine):
        return True
    ended = float(getattr(engine, "_playback_ended_at", 0.0) or 0.0)
    return ended > 0.0 and (time.monotonic() - ended) < WAKE_FOLLOWUP_S


def screen_names(engine) -> list[str]:
    """Driver name, open place, its menu items and the last search's places."""
    names: list[str] = []
    cfg = getattr(engine, "cfg", None)
    driver = cfg.get("DRIVER_NAME", "") if cfg is not None else ""
    if driver and driver != "Driver":
        names.append(driver)
    shop = getattr(engine, "shop", None)
    if shop is not None:
        if getattr(shop, "place", None) is not None:
            names.append(shop.place.name)
        menu = getattr(shop, "menu", None)
        names += [i.name for i in (getattr(menu, "items", None) or [])]
        names += [p.name for p in getattr(shop, "results", None) or []]
    return names[:MAX_SCREEN_NAMES + 1]


async def _speak_retry(engine, state: dict) -> None:
    for _ in range(40):                                              # the turn that raised it is still winding down
        if getattr(engine, "state", "idle") == "idle":
            break
        await asyncio.sleep(0.05)
    else:
        return
    if otp_pending(engine):
        return
    engine.emit("reply", ASR_RETRY_PROMPT)
    try:
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(ASR_RETRY_PROMPT)
        q.put_nowait(None)
        await engine._speak_loop(q)
    except Exception:
        log.exception("could not speak retry prompt")
    finally:
        if getattr(engine, "state", "") == "speaking":
            engine._set_state("idle", "")
        state["cooldown_until"] = time.monotonic() + COOLDOWN_S


def install(engine) -> None:
    state = {"retries": 0, "cooldown_until": 0.0, "speaking": False}

    def retry() -> None:
        """Speak the prompt once in a row; verdicts during the prompt and its cooldown are ignored."""
        if state["retries"] >= 1 or state["speaking"] or time.monotonic() < state["cooldown_until"]:
            return
        if otp_pending(engine):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("no running loop for the retry prompt")
            return
        state["retries"] += 1
        state["speaking"] = True

        async def run() -> None:
            try:
                await _speak_retry(engine, state)
            finally:
                state["speaking"] = False
        loop.create_task(run())

    def accepted() -> None:
        state["retries"] = 0

    frontend.on_asr_retry = retry
    frontend.on_asr_ok = accepted
    frontend.vocabulary_provider = lambda: screen_names(engine)
    frontend.gate_bypass = lambda: otp_pending(engine)
    frontend.wake_required = lambda: needs_wake(engine)
    frontend.wake_transcriber = lambda: getattr(getattr(engine, "stt", None), "transcribe_sync", None)
