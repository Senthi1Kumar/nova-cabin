"""Tool-call guarantee: decide whether a request needs a tool and map it to one when the model does not."""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

NUDGE = "Use a tool for this; do not answer from memory."
LET_ME_TRY = "Let me try that."
ASK_PLACE = "Which place do you mean?"

CANT = re.compile(r"\b(?:can(?:'|’)?t|cannot|can not|unable|not able)\b", re.IGNORECASE)
FIRST_SENTENCE = re.compile(r"^.*?(?:[.!?](?:\s|$)|$)", re.DOTALL)

# category phrase -> (shop_find_places category, find_places query)
CATEGORIES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(?:coffee|cafe|café|latte|espresso|cappuccino)\b", re.IGNORECASE), "coffee", "coffee"),
    (re.compile(r"\b(?:pharmac\w*|chemist|drug\s?store)\b", re.IGNORECASE), "pharmacy", "pharmacy"),
    (re.compile(r"\b(?:gas|petrol|fuel|filling station)\b", re.IGNORECASE), "gas", "gas station"),
    (re.compile(r"\b(?:charger|chargers|charging|ev)\b", re.IGNORECASE), "ev", "EV charger"),
    (re.compile(r"\b(?:restaurants?|food|lunch|dinner|breakfast|eat|sushi|pizza|burgers?|sandwich\w*)\b", re.IGNORECASE),
     "food", "restaurant"),
    (re.compile(r"\b(?:bakery|bagels?)\b", re.IGNORECASE), "bakery", "bakery"),
    (re.compile(r"\b(?:grocer\w*|supermarket|store|shop)\b", re.IGNORECASE), "store", "grocery store"),
    (re.compile(r"\b(?:malls?|shopping (?:centres?|centers?)|shopping)\b", re.IGNORECASE), "", "shopping centre"),
]
FIND_WORDS = re.compile(r"\b(?:find|nearest|nearby|near me|closest|search|look(?:ing)? for|where(?:'s| is)|any|"
                        r"need|want|get me|order|buy|show me|is there)\b", re.IGNORECASE)
NAV = re.compile(r"\b(?:take me|navigate|directions? to|drive to|route to)\s+(?:to\s+)?(.*)", re.IGNORECASE)
WEATHER = re.compile(r"\b(?:weather|forecast|rain|raining|snow|temperature outside|how (?:hot|cold) is it outside)\b", re.IGNORECASE)
CART = re.compile(r"\b(?:(?:my|the) cart|basket|checkout|check out)\b", re.IGNORECASE)
PAYMENT = re.compile(r"\b(?:pay for|payment|my (?:wallet|card)|purchase history|place (?:my|the) order)\b",
                     re.IGNORECASE)
VEHICLE = re.compile(r"\b(?:climate|air con\w*|a/c|heater|heating|seat (?:heater|heat|warmer)|defrost\w*|"
                     r"(?:turn|switch) (?:on|off) the (?:fan|lights?|a/c)|fan speed|battery|how much range|"
                     r"charge level|(?:open|close) the windows?|temperature|warmer|cooler)\b", re.IGNORECASE)
TIME = re.compile(r"\b(?:what time|the time|eta|arrival time)\b", re.IGNORECASE)
WEB_FACTS = re.compile(r"\b(?:news|score|who won|price of|stock|opening hours|open now|latest|"
                       r"how (?:much|many|long|far)|what(?:'s| is) the (?:capital|population))\b", re.IGNORECASE)
GREETING = re.compile(r"^\s*(?:hi|hello|hey|thanks?|thank you|ok(?:ay)?|good (?:morning|evening)|bye|yes|no)\W*$", re.IGNORECASE)


def _rules_needs(text: str) -> bool:
    if GREETING.match(text):
        return False
    if NAV.search(text) or WEATHER.search(text) or CART.search(text) or VEHICLE.search(text) or TIME.search(text):
        return True
    if WEB_FACTS.search(text) or PAYMENT.search(text):
        return True
    return any(p.search(text) for p, _, _ in CATEGORIES) and bool(FIND_WORDS.search(text))


async def needs_tool(text: str, *, judge: Callable[[str], Awaitable[bool | None]] | None = None) -> bool:
    """Rules decide; an optional judge may override when it returns a bool (None defers to the rules)."""
    if judge is not None:
        try:
            verdict = await judge(text)
        except Exception:                                            # noqa: BLE001
            verdict = None
        if verdict is not None:
            return bool(verdict)
    return _rules_needs(text)


def tool_names(schemas) -> set[str]:
    names = set()
    for s in schemas or []:
        name = (s.get("function") or s).get("name") if isinstance(s, dict) else None
        if name:
            names.add(name)
    return names


def rules_call(text: str, available: set[str]) -> tuple[str, dict] | None:
    """Map a request to a single tool call, or None when nothing maps."""
    if CART.search(text) and "shop_view_cart" in available:
        return "shop_view_cart", {}
    m = NAV.search(text)
    if m and m.group(1).strip(" .?!") and "navigate_to" in available:
        return "navigate_to", {"destination": m.group(1).strip(" .?!")}
    if WEATHER.search(text) and "get_weather" in available:
        return "get_weather", {}
    for pattern, category, query in CATEGORIES:
        if pattern.search(text) and FIND_WORDS.search(text):
            if category and "shop_find_places" in available:
                return "shop_find_places", {"category": category}
            if "find_places" in available:
                return "find_places", {"query": query}
    return None


def cant_first(text: str) -> bool:
    """True when the first sentence says the assistant cannot do it."""
    m = FIRST_SENTENCE.match(text.strip())
    return bool(m and CANT.search(m.group(0)))
