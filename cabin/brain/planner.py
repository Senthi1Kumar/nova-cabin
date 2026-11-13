"""Rules-first planner: deterministic commands inside an utterance run before the model.

Approve/cancel/digit phrases are handled by the payment intercepts and are not repeated here.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from .ledger import ActionLedger

log = logging.getLogger("planner")

SPLIT = re.compile(r"\s+and\s+|,\s*then\s+|\s*;\s*also\s+|\s*;\s*|,\s*and\s+|\s+then\s+", re.IGNORECASE)
REQUEST_WORDS = re.compile(
    r"\b(?:set|make|put|change|turn|increase|decrease|lower|raise|warm|cool|find|search|look|take|navigate|"
    r"route|plan|play|call|order|buy|add|check|tell|give|stop|heat|defrost)\b|\b(?:what|where|how)\b",
    re.IGNORECASE)
UNIT = r"(?:\s*(?:degrees?|deg\.?|°)\s*(c(?:elsius|entigrade)?|f(?:ahrenheit)?)?|\s+(celsius|centigrade|fahrenheit)|(?<=\d)\s*°?([cf])\b)"
TEMP_WORD = re.compile(r"\b(?:temperature|temp|degrees?|deg|warmer|cooler)\b|°", re.IGNORECASE)
NOT_TEMP = re.compile(r"\b(?:fan|seat|seats)\b", re.IGNORECASE)
RANGE_C, RANGE_F = (15, 30), (59, 86)
SET_TO = re.compile(r"\b(?:set|make|put|change|turn|bring|adjust|increase|decrease|lower|raise|warm|cool)\b"
                    r"[^0-9]*?\bto\s+(\d{1,3}(?:\.\d)?)" + UNIT + "?", re.IGNORECASE)
AC_WORD = re.compile(r"\b(?:ac|a/c|air\s?con\w*|climate|cabin)\b", re.IGNORECASE)
BRING = re.compile(r"\bbring\s+it\s+(?:down|up)\s+to\s+(\d{1,3}(?:\.\d)?)" + UNIT + "?", re.IGNORECASE)
DEGREES_N = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d)?)(?=\s*(?:degrees?|deg\b|°))" + UNIT, re.IGNORECASE)
IMPERATIVE = re.compile(r"\b(?:set|make|put|bring|adjust|keep|turn|change|want|need|please|i(?:'|’)?d like)\b", re.IGNORECASE)
STATEMENT = re.compile(r"^\s*(?:it(?:'|’)?s|it is|is|was|about|around|feels like)\b", re.IGNORECASE)
NOT_SETTING = re.compile(r"\b(?:by|outside|weather|forecast|today|tomorrow)\b", re.IGNORECASE)
BY = re.compile(r"\b(warmer|cooler|increase|decrease|lower|raise|up|down)\b[^0-9]*?\bby\s+(\d{1,3}(?:\.\d)?)" + UNIT + "?",
                re.IGNORECASE)


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Plan:
    kind: Literal["chat", "tool", "compound"] = "chat"
    rule_actions: list[ToolCall] = field(default_factory=list)
    hint: str = ""


def _unit_of(groups: tuple, default_metric: bool) -> bool:
    """True for Celsius."""
    for g in groups:
        if g:
            return g[0].lower() == "c"
    return default_metric


def _absolute(m: re.Match, metric: bool) -> ToolCall | None:
    val, celsius = float(m.group(1)), _unit_of(m.groups()[1:], metric)
    lo, hi = RANGE_C if celsius else RANGE_F
    if not lo <= val <= hi:
        return None
    f = val * 9 / 5 + 32 if celsius else val
    return ToolCall("set_climate", {"temperature_f": round(f)})


def climate_rule(clause: str, metric: bool, current_f: float | None) -> ToolCall | None:
    if NOT_TEMP.search(clause):
        return None
    m = SET_TO.search(clause)
    if m and (TEMP_WORD.search(clause) or AC_WORD.search(clause) or any(m.groups()[1:])):
        return _absolute(m, metric)
    m = BRING.search(clause)
    if m:
        return _absolute(m, metric)
    m = DEGREES_N.search(clause)
    if m and IMPERATIVE.search(clause) and not STATEMENT.search(clause) and not NOT_SETTING.search(clause):
        return _absolute(m, metric)
    m = BY.search(clause)
    if m and current_f is not None and TEMP_WORD.search(clause):
        d = float(m.group(2))
        if _unit_of(m.groups()[2:], metric):
            d *= 9 / 5
        if d > 18:
            return None
        sign = 1 if m.group(1).lower() in ("warmer", "increase", "raise", "up") else -1
        return ToolCall("set_climate", {"temperature_f": round(current_f + sign * d)})
    return None


I = re.IGNORECASE
FIND_VERB = re.compile(r"\b(?:find|search(?:\s+for)?|any|look(?:ing)?\s+for|show\s+me)\b", I)
# (pattern, shop_find_places category, free-text query, find_places query); first match wins
PLACE_CATEGORIES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"\b(?:ev\s+charg\w*|charging\s+(?:station|point)s?|chargers?)\b", I), "ev", "", "EV charger"),
    (re.compile(r"\b(?:shopping\s+(?:centre|center|mall)s?|malls?)\b", I), "store", "shopping centre", "shopping centre"),
    (re.compile(r"\b(?:petrol|fuel|gas)\b(?:\s+(?:station|bunk|pump))?", I), "gas", "", "petrol station"),
    (re.compile(r"\b(?:pharmac\w*|medical\s+shop|chemist)\b", I), "pharmacy", "", "pharmacy"),
    (re.compile(r"\b(?:coffee|caf[eé]|tea)\b", I), "coffee", "", "coffee"),
    (re.compile(r"\b(?:dosa|biryani)\b", I), "food", "", "restaurant"),
    (re.compile(r"\b(?:restaurants?|food|meals?)\b", I), "food", "", "restaurant"),
    (re.compile(r"\b(?:grocer\w*|stores?|supermarkets?)\b", I), "store", "", "grocery store"),
    (re.compile(r"\b(?:bars?|pubs?)\b", I), "bar", "", "bar"),
]
DISH = re.compile(r"\b(?:dosa|biryani)\b", I)
WITHIN_MIN = re.compile(r"\bwithin\s+(?:about\s+)?(\d{1,3})\s*(?:min(?:ute)?s?)\b", I)
WITHIN_KM = re.compile(r"\bwithin\s+(?:about\s+)?(\d{1,3}(?:\.\d)?)\s*(?:km|kilomet\w+)\b", I)


def _tool_props(schemas) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for s in schemas or []:
        fn = s.get("function") or s if isinstance(s, dict) else {}
        if fn.get("name"):
            out[fn["name"]] = set((fn.get("parameters") or {}).get("properties") or {})
    return out


def place_rule(clause: str, tools: dict[str, set[str]]) -> ToolCall | None:
    """Place search for one clause of a compound sentence; None when the clause is not a plain search."""
    if not FIND_VERB.search(clause):
        return None
    hit = next(((c, q, f) for pat, c, q, f in PLACE_CATEGORIES if pat.search(clause)), None)
    if hit is None:
        return None
    category, query, find_query = hit
    dish = DISH.search(clause)
    if dish:
        query = dish.group(0).lower()
    minutes, km = WITHIN_MIN.search(clause), WITHIN_KM.search(clause)
    if "shop_find_places" in tools:
        props, name, args = tools["shop_find_places"], "shop_find_places", {"category": category}
    elif "find_places" in tools:
        props, name, args = tools["find_places"], "find_places", {}
        query = query or find_query
    else:
        return None
    for key, m, cast in (("within_min", minutes, int), ("radius_km", km, float)):
        if m and key in props:
            args[key] = cast(m.group(1))
    if minutes and "within_min" not in props and name == "shop_find_places":
        query = f"{query} within {minutes.group(1)} minutes".strip()
    if query and "query" in props:
        args["query"] = query
    return ToolCall(name, args)


class Planner:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.ledger = ActionLedger()

    @staticmethod
    def split(text: str) -> list[str]:
        pieces = re.split("(" + SPLIT.pattern + ")", text, flags=re.IGNORECASE)
        parts: list[str] = []
        for i in range(0, len(pieces), 2):
            body = pieces[i].strip(" ,.;")
            sep = pieces[i - 1] if i else ""
            if not body:
                continue
            if parts and not REQUEST_WORDS.search(body):             # "within time and distance": a request-less tail stays with its clause
                parts[-1] += sep + body
            else:
                parts.append(body)
        return parts if len(parts) > 1 and all(REQUEST_WORDS.search(p) for p in parts) else [text.strip()]

    async def before(self, text: str) -> Plan:
        self.ledger.clear()
        parts = self.split(text)
        metric = bool(getattr(getattr(self.engine, "cfg", None), "metric", False))
        current = getattr(getattr(self.engine, "vehicle", None), "cabin_temp_f", None)
        tools = _tool_props(getattr(getattr(self.engine, "tools", None), "schemas", None))
        actions: list[ToolCall] = []
        for p in parts:
            call = climate_rule(p, metric, current)
            if not call and len(parts) > 1:
                call = place_rule(p, tools)
            if call:
                actions.append(call)
        kind = "compound" if len(parts) > 1 else ("tool" if actions else "chat")
        return Plan(kind, actions, "")

    def after(self, name: str, args: dict | None) -> None:
        self.ledger.record(name, args)

    def seen(self, name: str, args: dict | None) -> bool:
        return self.ledger.seen(name, args)
