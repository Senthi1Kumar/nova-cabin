"""Per-tool outcomes of a turn and the templated one-sentence answer for compound requests."""
from __future__ import annotations

from dataclasses import dataclass

COMPOUND_HINT = ("This request has several parts; call the tools for every part in one round, "
                 "then answer all parts in one or two short sentences.")
PLACE_TOOLS = ("find_places", "shop_find_places")


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    summary: str
    error: str | None = None


def _plural(noun: str) -> str:
    noun = noun.strip() or "places"
    return noun if noun.endswith("s") else noun + "s"


def _degrees(result: dict, metric: bool) -> str | None:
    f = result.get("cabin_temp_f")
    if f is None:
        return None
    return f"{round((float(f) - 32) * 5 / 9) if metric else round(float(f))} degrees"


def _place_phrase(first: dict) -> str:
    name = first.get("name") or "a place"
    when = first.get("duration") or first.get("drive_time") or first.get("eta")
    if when is None and first.get("minutes") is not None:
        when = f"{first['minutes']} minutes"
    far = when or first.get("distance")
    return f"{name}, {far} away" if far else name


def outcome_from(name: str, args: dict | None, result, metric: bool = False) -> ToolOutcome:
    """Build an outcome from a tool's (result_for_llm) dict."""
    args = args or {}
    if not isinstance(result, dict):
        return ToolOutcome(name, True, "done")
    err = result.get("error")
    subject = str(args.get("query") or args.get("category") or "places")
    if err:
        return ToolOutcome(name, False, "", str(err))
    if name in PLACE_TOOLS:
        results = result.get("results") or []
        if not results:
            return ToolOutcome(name, False, "", str(result.get("note") or "nothing found"))
        return ToolOutcome(name, True, f"the nearest {subject.strip() or 'place'} is {_place_phrase(results[0])}")
    if name == "set_climate":
        deg = _degrees(result, metric)
        return ToolOutcome(name, True, f"set to {deg}" if deg else "climate updated")
    for key in ("summary", "message", "note", "destination", "answer"):
        if result.get(key):
            return ToolOutcome(name, True, str(result[key])[:80])
    return ToolOutcome(name, True, "done")


def _failure_phrase(o: ToolOutcome, subject: str | None) -> str:
    if o.name in PLACE_TOOLS:
        return f"I couldn't find {_plural(subject or 'places')}"
    if o.name == "set_climate":
        return "I couldn't change the climate"
    return f"I couldn't do {o.name.replace('_', ' ')}"


def merge_reply(outcomes: list[ToolOutcome], subjects: dict[str, str] | None = None) -> str | None:
    """One spoken sentence covering every part; None when there is nothing to say."""
    if not outcomes:
        return None
    subjects = subjects or {}
    good = [o.summary for o in outcomes if o.ok and o.summary]
    bad = [_failure_phrase(o, subjects.get(o.name)) for o in outcomes if not o.ok]
    if not good and not bad:
        return None
    head = ". ".join(s[:1].upper() + s[1:] for s in good)
    failed = " and ".join(bad)
    if good and bad:
        return f"{head}, but {failed}."
    if good:
        return head + "."
    return failed + "."
