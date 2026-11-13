"""Freshness stamps on tool results so stale data is never presented as current."""
from __future__ import annotations

import datetime as dt

MAX_AGE_S = 600
PROMPT_LINE = ("Tool results carry as_of; if stale is true or as_of is older than 10 minutes, "
               "say 'as of <time>'.")


def _parse(value) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


def stamp(result: dict, now: dt.datetime | None = None) -> dict:
    """Return a copy of a tool result with an ISO as_of time; an as_of the tool set is kept."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    if not out.get("as_of"):
        out["as_of"] = (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
    return out


def label_stale(result: dict, now: dt.datetime | None = None, max_age_s: int = MAX_AGE_S) -> dict:
    """Return a copy with stale: true when as_of is older than max_age_s (or unreadable)."""
    if not isinstance(result, dict) or "as_of" not in result:
        return result
    now = now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    then = _parse(result["as_of"])
    if then is not None and (now - then).total_seconds() <= max_age_s:
        return result
    out = dict(result)
    out["stale"] = True
    return out
