"""Per-turn record of executed tool actions, so one action never runs twice."""
from __future__ import annotations

import json
import re

PLACE_TOOLS = ("find_places", "shop_find_places")
QUERY_CATEGORY: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(?:coffee|caf[eé]|tea)\b", re.IGNORECASE), "coffee", ""),
    (re.compile(r"\b(?:pharmac\w*|medical|chemist)\b", re.IGNORECASE), "pharmacy", ""),
    (re.compile(r"\b(?:petrol|fuel|gas)\b", re.IGNORECASE), "gas", ""),
    (re.compile(r"\b(?:ev|chargers?|charging)\b", re.IGNORECASE), "ev", ""),
    (re.compile(r"\b(?:malls?|shopping)\b", re.IGNORECASE), "store", "shopping centre"),
    (re.compile(r"\b(?:restaurants?|food|meals?)\b", re.IGNORECASE), "food", ""),
    (re.compile(r"\b(?:bars?|pubs?)\b", re.IGNORECASE), "bar", ""),
    (re.compile(r"\b(?:grocer\w*|stores?|supermarkets?)\b", re.IGNORECASE), "store", ""),
]


FILLER = re.compile(r"\b(?:a|an|the|some|any|nearby|near|me|closest|nearest|place|places|shops?|station|bunk|pump|point|"
                    r"within\s+\d+(?:\.\d)?\s*(?:min(?:ute)?s?|km|kilomet\w+))\b", re.IGNORECASE)
DISH = re.compile(r"\b(?:dosa|biryani)\b", re.IGNORECASE)


def _place_key(name: str, a: dict) -> str:
    """One key per place search, whichever tool issued it; specific words beyond the category word stay in the key."""
    category = " ".join(str(a.get("category") or "").lower().split()) if name == "shop_find_places" else ""
    query = " ".join(str(a.get("query") or "").lower().split())
    for pat, cat, canon in QUERY_CATEGORY:
        if pat.search(query):
            category = category or cat
            query = canon or pat.sub(" ", query)
            break
    if canon_hit := ("shopping" in query and category == "store"):
        query = "shopping centre"
    if not canon_hit:
        query = " ".join(FILLER.sub(" ", query).split())
    if not category and DISH.search(query):
        category = "food"
    return "place:" + category + (":" + query if query else "")


def _norm_value(v):
    if isinstance(v, float):
        return int(v) if v == int(v) else round(v, 2)
    if isinstance(v, str):
        return " ".join(v.lower().split())
    if isinstance(v, dict):
        return {k: _norm_value(x) for k, x in sorted(v.items()) if x is not None}
    if isinstance(v, (list, tuple)):
        return [_norm_value(x) for x in v]
    return v


def normalise(name: str, args: dict | None) -> str:
    a = dict(args or {})
    if name in PLACE_TOOLS:
        return _place_key(name, a)
    if name == "set_climate":
        t = a.pop("temperature_c", None)
        if t is not None and a.get("temperature_f") is None:
            a["temperature_f"] = float(t) * 9 / 5 + 32
        t = a.get("temperature_f")
        if t is not None:
            t = float(t)
            a["temperature_f"] = round(t * 9 / 5 + 32) if t < 45 else round(t)
    return name + ":" + json.dumps(_norm_value(a), sort_keys=True, separators=(",", ":"))


class ActionLedger:
    def __init__(self) -> None:
        self._done: set[str] = set()

    def record(self, name: str, args: dict | None) -> None:
        self._done.add(normalise(name, args))

    def seen(self, name: str, args: dict | None) -> bool:
        return normalise(name, args) in self._done

    def clear(self) -> None:
        self._done.clear()
