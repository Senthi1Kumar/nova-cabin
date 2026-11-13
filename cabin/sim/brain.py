"""Offline "LLM": intent parsing → the same tool calls a real model makes →
spoken replies composed from the tool results. Implements ChatClient.stream()
so the engine's tool loop, streaming, sentence TTS and interruption all run
exactly as they do with a cloud model.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator

from ..brain.planner import REQUEST_WORDS, SPLIT, climate_rule

STOP = r"(?=\s+(?:for|this|next|on|tomorrow|tonight|today|and|then|please|with|via|by|leaving|at \d)\b|[,.?!]|$)"
CLAUSE_VERB = re.compile(REQUEST_WORDS.pattern + r"|\b(?:clear|show|switch|empty|who|which|is|are|does|will)\b", re.IGNORECASE)
PLACE_TAIL = rf"([A-Za-z][\w'’ .&-]*?){STOP}"


def speakable(s: str) -> str:
    s = re.sub(r"(\d+) h (\d+) min", lambda m: f"{m[1]} hour{'s' if m[1] != '1' else ''} {int(m[2])} minutes", s)
    s = re.sub(r"(\d+(?:\.\d)?) mi\b", r"\1 miles", s)
    s = re.sub(r"(\d+(?:\.\d)?) km\b", r"\1 kilometers", s)
    s = re.sub(r"(-?\d+)\s*[–-]\s*(-?\d+)\s*°(?:\s*[FC]\b)?", r"\1 to \2 degrees", s, flags=re.IGNORECASE)
    s = re.sub(r"(-?\d+)\s*°(?:\s*[FC]\b)?", r"\1 degrees", s, flags=re.IGNORECASE)
    s = s.replace("%", " percent").replace(" · ", ", ").replace("★", "rated")
    return re.sub(r"\s+", " ", s).strip()


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
NUM_WORDS = {w: i for i, w in enumerate(_ONES) if i} | {"a": 1, "an": 1, "another": 1, "couple": 2, "a couple": 2}
NUM = r"(\d{1,2}|" + "|".join(sorted((w for w in _ONES[1:]), key=len, reverse=True)) + r"|a couple of|an?|another)"

# keyword → (shop category, free-text query kept for item words)
SHOP_KEYWORDS = [
    (r"filter coffees?|coffee|caf[eé]s?|espresso", "coffee", None),
    (r"lattes?|cappuccinos?|mochas?|cold brew", "coffee", None),
    (r"gas|fuel|petrol", "gas", None),
    (r"chargers?|charging|ev charg\w*|charge (?:the |my )?(?:car|ev|vehicle)", "ev", None),
    (r"pharmac\w+|drugstore|drug store|medical (?:shop|store)s?|chemists?", "pharmacy", None),
    (r"groceries|grocery|stores?|supermarket", "store", None),
    (r"bars?|pubs?", "bar", None),
    (r"bakery|pastr\w+|donuts?", "bakery", None),
    (r"sandwich\w*|deli|bagels?", "sandwich", None),
    (r"food|lunch|dinner|breakfast|restaurants?|burgers?|pizza|tacos?|dos[ae]i?s?|idlis?|biryani|meals|hungry|starving", "food", None),
]
ITEM_QUERY = {"dosa": "dosa", "dosai": "dosa", "idli": "idli", "idlis": "idli", "biryani": "biryani",
              "filter coffee": "filter coffee", "filter coffees": "filter coffee","latte": "latte", "lattes": "latte", "cappuccino": "cappuccino", "cappuccinos": "cappuccino",
              "espresso": "espresso", "mocha": "mocha", "cold brew": "cold brew", "bagel": "bagel", "bagels": "bagel",
              "burger": "burger", "burgers": "burger", "pizza": "pizza", "taco": "taco", "tacos": "taco"}


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return f"{_ONES[n // 100]} hundred" + (f" {_num_words(n % 100)}" if n % 100 else "")
    return str(n)


def spoken_money(s: str) -> str:
    """'$12.50' -> 'twelve fifty', '$4.00' -> 'four dollars'."""
    m = re.search(r"(\d[\d,]*)(?:\.(\d{2}))?", s or "")
    if not m:
        return s or ""
    d, c = int(m[1].replace(",", "")), int(m[2] or 0)
    if not c:
        return f"{_num_words(d)} dollar{'s' if d != 1 else ''}"
    if not d:
        return f"{_num_words(c)} cents"
    return f"{_num_words(d)} {'oh ' + _num_words(c) if c < 10 else _num_words(c)}"


def spoken_distance(s: str) -> str:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(mi|km|ft|m)\s*", s or "")
    if not m:
        return speakable(s or "")
    v, unit = float(m[1]), {"mi": "mile", "km": "kilometer", "ft": "feet", "m": "meters"}[m[2]]
    if unit in ("feet", "meters"):
        return f"{_num_words(int(v))} {unit}"
    if v == 0.5:
        return f"half a {unit}"
    if v == 1:
        return f"one {unit}"
    whole, _, frac = f"{v:g}".partition(".")
    spoken = _num_words(int(whole)) + (" point " + " ".join(_ONES[int(d)] for d in frac) if frac else "")
    return f"{spoken} {unit}s"


def spoken_rating(v) -> str:
    return " point ".join(_num_words(int(x)) for x in f"{float(v):g}".split("."))


def _singular(w: str) -> str:
    w = w.strip()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if re.search(r"(ch|sh|ss|x)es$", w):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _qty(tok: str | None) -> int:
    if not tok:
        return 1
    tok = tok.lower().replace(" of", "")
    return int(tok) if tok.isdigit() else NUM_WORDS.get(tok, 1)


class SimBrain:
    label = "Simulation"
    model = "offline intent engine"

    def __init__(self, metric: bool = False) -> None:
        self.metric = metric
        self.rng = random.Random()
        self.shop_tools: set[str] = set()
        self._ctx: dict = {}

    # ── public API (matches ChatClient) ──────────────────────────
    async def stream(self, messages: list[dict], tools=None) -> AsyncIterator[tuple]:
        await asyncio.sleep(self.rng.uniform(0.25, 0.55))           # time to first token
        last = messages[-1]
        if last["role"] == "tool":
            text = self._compose(messages)
            async for ev in self._words(text):
                yield ev
            yield ("done", "stop")
            return
        self.shop_tools = {t["function"]["name"] for t in (tools or []) if t.get("function")}
        plan = self._plan(last["content"], messages)
        if plan["say"]:
            async for ev in self._words(plan["say"]):
                yield ev
        if plan["calls"]:
            yield ("tool_calls", [{"id": f"call_{i}_{self.rng.randint(1000, 9999)}", "name": n,
                                   "arguments": json.dumps(a)} for i, (n, a) in enumerate(plan["calls"])])
            yield ("done", "tool_calls")
        else:
            yield ("done", "stop")

    @staticmethod
    def _clauses(text: str) -> list[str]:
        """Split on conjunctions and commas before a new request; a fragment with no request verb joins its neighbour."""
        parts = [p.strip(" ,.;") for p in SPLIT.split(text) if p and p.strip(" ,.;")]
        parts = [c.strip(" ,.;") for p in parts for c in re.split(rf",\s*(?={CLAUSE_VERB.pattern})", p) if c.strip(" ,.;")]
        out: list[str] = []
        for part in parts:
            if out and not CLAUSE_VERB.search(part):
                out[-1] += " and " + part
            else:
                out.append(part)
        return out or [text.strip()]

    def _plan(self, text: str, messages) -> dict:
        """One intent per request clause; clauses that make no tool call add nothing."""
        clauses = self._clauses(text)
        if len(clauses) < 2:
            return self._intent(text, messages)
        say, calls = "", []
        for clause in clauses:
            plan = self._intent(clause, messages)
            say = say or plan["say"]
            calls += [c for c in plan["calls"] if c not in calls]
        return {"say": say if not calls else "", "calls": calls}

    async def _words(self, text: str):
        for w in re.findall(r"\S+\s*", text):
            yield ("text", w)
            await asyncio.sleep(self.rng.uniform(0.018, 0.045))

    def _to_f(self, v: int) -> int:
        """A bare number in a metric car is Celsius when it sits in the cabin range."""
        return round(v * 9 / 5 + 32) if self.metric and 15 <= v <= 30 else v

    # ── understanding ────────────────────────────────────────────
    def _last_place(self, messages) -> str | None:
        for m in reversed(messages):
            if m["role"] != "tool":
                continue
            try:
                r = json.loads(m["content"])
            except (ValueError, TypeError):
                continue
            for k in ("destination", "navigating_to", "location"):
                if r.get(k):
                    return r[k]
            if r.get("results") and isinstance(r["results"], list) and r["results"] and r["results"][0].get("name"):
                return r["results"][0]["name"]
        return None

    def _shop_ctx(self, messages) -> dict:
        """What the shop flow has surfaced so far, read back from earlier tool results."""
        ctx: dict = {"places": [], "menu": [], "place_id": None, "place": None, "cart": [], "last_item": None,
                     "card": None}
        for m in messages:
            if m.get("role") != "tool":
                continue
            try:
                r = json.loads(m["content"])
            except (ValueError, TypeError, KeyError):
                continue
            if not isinstance(r, dict) or r.get("error"):
                continue
            if isinstance(r.get("results"), list) and r["results"] and r["results"][0].get("place_id"):
                ctx["places"] = r["results"]
            if isinstance(r.get("menu"), list):
                ctx["menu"] = [i["name"] for sec in r["menu"] for i in sec.get("items", []) if i.get("name")]
                ctx["place_id"], ctx["place"] = r.get("place_id"), r.get("place")
            if isinstance(r.get("added"), dict) and r["added"].get("name"):
                ctx["last_item"] = r["added"]["name"]
            elif r.get("name") and ("quantity" in r or "removed" in r):
                ctx["last_item"] = r["name"]
            cart = r.get("cart")
            if isinstance(cart, dict):
                cart = cart.get("items")
            if isinstance(r.get("items"), list):
                cart = r["items"]
            if isinstance(cart, list):
                ctx["cart"] = cart
            if isinstance(r.get("cards"), list):
                act = next((c for c in r["cards"] if c.get("active")), None)
                if act:
                    ctx["card"] = f"{act.get('brand', 'card')} ending {act.get('last4', '')}"
        return ctx

    def _shop_intent(self, t: str, low: str, messages) -> dict | None:
        have = self.shop_tools
        if not have & {"shop_find_places", "pay_list_cards"}:
            return None
        ctx = self._ctx = self._shop_ctx(messages)

        def call(tool, /, **args):
            return {"say": "", "calls": [(tool, args)]} if tool in have else None

        if re.search(r"\b(which|what|my|saved|show)\b.*\b(cards?|payment methods?|wallet)\b|\bcards? do i have\b", low):
            return call("pay_list_cards")
        if re.search(r"\b(check ?out|ready to pay|pay (?:now|for (?:it|this|that|my order))|i'?ll pay|let'?s pay|"
                     r"place (?:the|my) order|pay)\b", low) and not re.search(r"\bpay (attention|back|off)\b", low):
            return call("shop_request_checkout")
        if re.search(r"\b(clear|empty)\b.*\b(cart|basket|order)\b", low):
            return call("shop_clear_cart")
        if re.search(r"\b(cart|basket)\b|\bmy order\b|\bwhat have i (?:got|ordered)\b", low) and \
                not re.search(r"\b(add|put)\b", low):
            return call("shop_view_cart")

        m = re.search(rf"\b(?:remove|take off|take out|delete|drop)\s+(?:{NUM}\s+)?(?:the\s+|my\s+)?(.+?)"
                      r"(?:\s+(?:from|off)\b.*)?[.!?]*$", low)
        if m and (ctx["cart"] or ctx["last_item"]):
            name = _singular(m[2])
            if name in ("it", "that", "them"):
                name = ctx["last_item"] or ""
            if name:
                args = {"name": name}
                if m[1] and re.match(r"\d|[a-z]", m[1]) and m[1] not in ("a", "an"):
                    args["qty"] = _qty(m[1])
                return call("shop_remove", **args)

        m = re.search(rf"\b(?:make|change|set|update)\s+(?:it|that|them|the\s+(.+?))\s+(?:to\s+)?{NUM}\b\s*(.*?)[.!?]*$", low)
        if m and (ctx["cart"] or ctx["last_item"]):
            name = _singular(m[1] or m[3] or "") or ctx["last_item"]
            if name and name not in ("it", "that"):
                return call("shop_set_qty", name=name, qty=_qty(m[2]))

        add = re.search(rf"\b(?:add|put|throw in|i'?ll (?:have|take)|get me|give me|i want|i'd like|order|grab)\s+"
                        rf"(?:{NUM}\s+)?(?:more\s+|of\s+)?(?:the\s+|some\s+)?(.+?)"
                        r"(?:\s+(?:to|in|into|on)\s+(?:my|the)\s+(?:cart|order|basket))?[.!?]*$", low)
        if add:
            item = _singular(add[2])
            qty = _qty(add[1])
            on_menu = any(item in n.lower() or n.lower() in item for n in ctx["menu"]) or \
                any(set(item.split()) & set(n.lower().split()) for n in ctx["menu"])
            if ctx["menu"] and (on_menu or re.match(r"add|put|throw", add.group(0))) and item not in ("it", "that"):
                return call("shop_add_to_cart", item_id=item, qty=qty)
            if not re.match(r"add|put|throw", add.group(0)):
                found = self._shop_find(low, force=True)
                if found:
                    return found

        if re.search(r"\bmenu\b|\bopen (?:it|that|this|the|[a-z0-9'’&.-]+(?: [a-z0-9'’&.-]+)*)\b|what do they have|"
                     r"\bwhat'?s on offer\b", low) and not re.search(r"\b(open|the) (?:window|sunroof|trunk|door)", low):
            return self._shop_open(low, ctx)

        return self._shop_find(low)

    def _shop_open(self, low: str, ctx: dict) -> dict | None:
        if "shop_open_place" not in self.shop_tools:
            return None
        places = ctx["places"]
        named = next((p for p in places if p["name"].lower() in low), None)
        if not named:
            skip = {"the", "menu", "open", "show", "see"}
            asked = _tokens(low) - skip
            named = next((p for p in places if _tokens(p["name"]) & asked), None)
        if not named and places:
            ordinal = {"second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "last": -1}
            idx = next((v for k, v in ordinal.items() if k in low), 0)
            named = places[idx] if -len(places) <= idx < len(places) else places[0]
        if named:
            return {"say": "", "calls": [("shop_open_place", {"place_id": named["place_id"]})]}
        if ctx["place_id"]:
            return {"say": "", "calls": [("shop_open_place", {"place_id": ctx["place_id"]})]}
        m = re.search(r"\bopen\s+(?:the\s+)?(.+?)[.!?]*$", low)
        if m and m[1] not in ("it", "that", "this", "menu"):
            return {"say": "", "calls": [("shop_open_place", {"place_id": m[1]})]}
        return {"say": "Tell me what you're after and I'll find a place first.", "calls": []}

    def _shop_find(self, low: str, force: bool = False) -> dict | None:
        if "shop_find_places" not in self.shop_tools:
            return None
        if re.search(r"\b(trip|plan|navigate|take me|directions|weather|forecast|battery|range|stops?)\b", low):
            return None
        if re.search(r"\b(?:near|in|around|by)\s+(?!me\b|here\b|us\b|my\b|the car\b|the area\b)\w", low) and not force:
            return None
        for pat, cat, _q in SHOP_KEYWORDS:
            kw = re.search(rf"\b({pat})\b", low)
            if kw:
                args = {"category": cat}
                item = ITEM_QUERY.get(kw.group(1))
                if item:
                    args["query"] = item
                return {"say": "", "calls": [("shop_find_places", args)]}
        return None

    def _intent(self, text: str, messages) -> dict:
        t = text.strip()
        low = t.lower()
        there = self._last_place(messages)

        def place_after(*verbs):
            for v in verbs:
                m = re.search(rf"\b{v}\s+(?:the\s+)?{PLACE_TAIL}", t, re.IGNORECASE)
                if m and m.group(1).strip().lower() not in ("there", "it", "me"):
                    return re.sub(r"^(visit|see|go to|drive to|head to|get to)\s+", "", m.group(1).strip(), flags=re.IGNORECASE)
            return None

        def resolve_there(p):
            return there if (p is None and re.search(r"\b(there|that place)\b", low)) else p

        # small talk
        if re.fullmatch(r"(hi|hello|hey|good (morning|afternoon|evening))[\s,!.]*(\w+)?[!.]*", low):
            return {"say": "Hi! I can plan a trip, find places, check the weather, or adjust the cabin. "
                           "What would you like?", "calls": []}
        if re.search(r"\b(thank|thanks|cheers|perfect|great)\b", low) and len(low) < 40:
            return {"say": self.rng.choice(["Anytime.", "You got it.", "Happy to help."]), "calls": []}
        if re.search(r"what can you do|help me|what do you do|who are you|your name", low):
            return {"say": "I'm your in-car assistant. Try asking me to plan a trip with charging stops, find coffee "
                           "nearby, check the weather, or warm up your seat.", "calls": []}
        if re.search(r"\b(cancel|stop|never ?mind|forget it)\b", low) and len(low) < 30:
            return {"say": "Okay, cancelled.", "calls": []}

        if re.search(r"\b(joke|sing|song|music|podcast|radio|call (?:my|the|a)\b|text (?:my|the|a)\b|"
                     r"(?:send|write) (?:a |an )?(?:message|text|email)|sunroof|windows?|trunk|lights?|wipers?)\b", low):
            return {"say": "I can't do that one yet, but I can plan trips, find places, check the weather or "
                           "adjust the climate.", "calls": []}
        if "agent_time" in self.shop_tools and re.search(r"\bwhat(?:'s| is)? the time\b|\bwhat time is it\b|"
                                                         r"\bcurrent time\b", low):
            return {"say": "", "calls": [("agent_time", {})]}

        # shop / payment (only when the commerce plugin registered its tools)
        shop = self._shop_intent(t, low, messages)
        if shop:
            return shop

        # climate
        rule = climate_rule(low, self.metric, None)
        if rule:
            return {"say": "", "calls": [(rule.name, rule.args)]}
        temp = re.search(r"(\d{2})(?:\s*(?:degrees|°|f\b))?", low)
        climate_words = re.search(r"\b(cabin|temp|temperature|thermostat|warm|cool|heat|heater|seat|fan|a/?c|air|"
                                  r"cold|hot|chilly|freezing|stuffy|degrees?|deg)\b", low)
        if climate_words and not re.search(r"weather|outside|forecast", low):
            args = {}
            if temp and re.search(r"\b(set|cabin|temp|temperature|to|make it)\b", low):
                args["temperature_f"] = self._to_f(int(temp.group(1)))
            if re.search(r"seat", low):
                lvl = re.search(r"level\s*(\d)", low)
                args["seat_heat"] = 0 if re.search(r"(turn off|off|disable).*seat|seat.*\boff\b", low) else \
                    int(lvl.group(1)) if lvl else 2
            if re.search(r"\b(i'?m|it'?s|feel(ing)?)\s+(?:so\s+|too\s+|really\s+|very\s+)?(cold|chilly|freezing)", low) and "temperature_f" not in args:
                args["temperature_f"] = 74
            if re.search(r"\b(i'?m|it'?s|feel(ing)?)\s+(?:so\s+|too\s+|really\s+|very\s+)?(hot|warm|stuffy)", low) and "temperature_f" not in args:
                args["temperature_f"] = 68
            fan = re.search(r"fan\s*(?:to|on|at)?\s*(\d+)", low)
            if fan:
                args["fan"] = int(fan.group(1))
            elif re.search(r"fan\s+(up|higher|more)", low):
                args["fan"] = 6
            if re.search(r"\b(a/?c|air ?con)\b.*\boff\b|\b(?:off|disable)\b.*\b(?:a/?c|air ?con)\b", low):
                args["ac_on"] = False
            elif re.search(r"\b(a/?c|air ?con)\b", low):
                args["ac_on"] = True
            if args:
                return {"say": "", "calls": [("set_climate", args)]}

        # vehicle
        if re.search(r"\b(battery|range|charge level|how much charge|state of charge|miles left|how far can i)\b", low) \
                and not re.search(r"\btrip|plan\b", low):
            return {"say": "", "calls": [("vehicle_status", {})]}

        # navigation
        if re.search(r"\b(take me|navigate|drive me|directions|go to|head to|get me to|route me)\b", low):
            dest = resolve_there(place_after("to", "navigate to", "take me to"))
            if dest:
                return {"say": "", "calls": [("navigate_to", {"destination": dest})]}

        # trip planning
        if re.search(r"\b(trip|plan|road ?trip|getaway|weekend|drive to|visit|going to)\b", low):
            dest = resolve_there(place_after("to", "visit", "for"))
            if dest:
                when = re.search(r"\b(this weekend|next weekend|tomorrow|tonight|today|on \w+day|\w+day)\b", low)
                filler = self.rng.choice(["Let me map that out.", "On it.", "Planning that now."])
                return {"say": filler, "calls": [("plan_trip", {"destination": dest,
                                                                "when": when.group(1) if when else ""})]}

        # weather
        if re.search(r"\b(weather|forecast|rain|snow|sunny|temperature outside|how hot|how cold|umbrella|jacket)\b", low):
            loc = resolve_there(place_after("in", "at", "for", "near"))
            if loc and re.fullmatch(r"(the )?(weekend|morning|evening|afternoon|week)", loc.lower()):
                loc = None
            return {"say": "", "calls": [("get_weather", {"location": loc} if loc else {})]}

        # places
        if re.search(r"\b(find|nearby|near me|closest|nearest|where can i|where's a|where is a|looking for|any good|"
                     r"coffee|restaurant|food|eat|sushi|pizza|taco|burger|charger|charging|parking|gas|grocer|"
                     r"pharmacy|hotel|bakery|gym)\b", low):
            near = None
            m = re.search(rf"\b(?:near|in|around|by)\s+(?:the\s+)?{PLACE_TAIL}", t, re.IGNORECASE)
            if m and m.group(1).lower() not in ("me", "here", "us", "by"):
                near = m.group(1).strip()
            q = re.sub(r"\b(find|me|a|an|the|some|any|good|nearby|near me|closest|nearest|please|where can i|"
                       r"get|where's|where is|is there|i'm looking for|looking for|i want|i need|show)\b", " ", low)
            q = re.sub(r"\b(near|in|around|by)\b.*$", "", q)
            q = re.sub(r"[^\w\s&'-]", " ", q)
            q = re.sub(r"\s+", " ", q).strip() or "restaurant"
            args = {"query": q}
            if near:
                args["near"] = near
            return {"say": "", "calls": [("find_places", args)]}

        # anything else → search
        q = re.sub(r"^(hey|ok|okay)[, ]+", "", t).strip()
        return {"say": self.rng.choice(["Let me look that up.", "Checking.", ""]), "calls": [("web_search", {"query": q})]}

    # ── composing the spoken answer ──────────────────────────────
    def _compose(self, messages) -> str:
        calls = next(m["tool_calls"] for m in reversed(messages) if m.get("tool_calls"))
        results = {}
        for m in messages[-len(calls):]:
            if m["role"] == "tool":
                name = next(c["function"]["name"] for c in calls if c["id"] == m["tool_call_id"])
                try:
                    results[name] = json.loads(m["content"])
                except ValueError:
                    results[name] = {"error": "bad result"}
        self._ctx = self._shop_ctx(messages)
        parts = []
        for name, r in results.items():
            if r.get("already_done"):
                parts.append("Done.")
                continue
            if r.get("error"):
                parts.append("I couldn't get that just now, sorry.")
                continue
            parts.append(speakable(getattr(self, "_say_" + name, lambda _r: "Done.")(r)))
        return " ".join(parts)

    def _say_plan_trip(self, r):
        s = f"Your trip to {r['destination']} is {r['distance']}, about {r['drive_time']} of driving."
        if r.get("charging_stops"):
            n = r["charging_stops"]
            loc = r.get("charge_location")
            where = (f" in {loc}" if n == 1 else f", the first in {loc}") if loc else ""
            s += (f" You'll make {'one quick charging stop' if n == 1 else f'{n} charging stops'}"
                  f"{where}, and arrive with "
                  f"{r['arrival_battery_pct']}% battery.")
        else:
            s += f" No charging needed — you'll arrive with {r['arrival_battery_pct']}% battery."
        if r.get("weather") and r["weather"] != "—":
            temps, _, cond = r["weather"].partition(", ")
            s += f" Expect {temps}{', ' + cond.lower() if cond else ''}."
        return s

    def _say_find_places(self, r):
        res = r.get("results") or []
        if not res:
            return "I couldn't find anything like that nearby."
        f = res[0]
        more = f" I've put {len(res) - 1} more on the map." if len(res) > 1 else ""
        rating = f", rated {f['rating']}" if f.get("rating") else ""
        return f"The closest is {f['name']}, {f['distance']} away{rating}.{more}"

    # shop / payment replies
    def _say_shop_find_places(self, r):
        res = r.get("results") or []
        if not res:
            return "I couldn't find a place like that nearby."
        f = res[0]
        rating = f", rated {spoken_rating(f['rating'])}" if f.get("rating") else ""
        s = f"{f['name']} is closest, {spoken_distance(f.get('distance', ''))}{rating}."
        if len(res) > 1:
            s += f" I found {_num_words(len(res) - 1)} more. Want to see the menu?"
        else:
            s += " Want to see the menu?"
        return s

    def _say_shop_open_place(self, r):
        items = [i["name"] for sec in r.get("menu", []) for i in sec.get("items", [])]
        place = r.get("place", "the menu")
        if not items:
            return f"{place} has nothing I can order right now."
        picks = " and ".join(items[:2])
        return f"Here's the menu for {place}. Popular picks include {picks}. What would you like?"

    @staticmethod
    def _cart_total(r) -> str:
        total = r.get("cart_total") or r.get("total")
        if not total and isinstance(r.get("cart"), dict):
            total = r["cart"].get("total")
        return spoken_money(total) if total else ""

    def _say_shop_add_to_cart(self, r):
        a = r.get("added") or {}
        name = a.get("name", "that") if isinstance(a, dict) else str(a)
        q = a.get("quantity", 1) if isinstance(a, dict) else 1
        total = self._cart_total(r)
        return f"Added {_num_words(int(q))} {name}, your cart is {total}." if total else f"Added {name}."

    def _say_shop_set_qty(self, r):
        total = self._cart_total(r)
        name = r.get("name", "that")
        if not r.get("quantity"):
            return f"Took {name} out." + (f" Your cart is {total}." if total else "")
        return f"Okay, {_num_words(int(r['quantity']))} {name}." + (f" Your cart is {total}." if total else "")

    def _say_shop_remove(self, r):
        total = self._cart_total(r)
        base = f"Removed {r.get('name', 'it')}."
        return base + (f" Your cart is {total}." if total else " Your cart is empty.")

    def _say_shop_view_cart(self, r):
        items = r.get("items") or []
        if not items:
            return "Your cart is empty."
        n = sum(int(i.get("qty", 1)) for i in items)
        first = items[0]
        what = f"{_num_words(int(first.get('qty', 1)))} {first['name']}" if len(items) == 1 else \
            f"{_num_words(n)} items"
        return f"You have {what}, total {spoken_money(r.get('total', ''))}. Say check out when you're ready."

    def _say_shop_clear_cart(self, r):
        return "Your cart is empty now."

    def _say_shop_request_checkout(self, r):
        if r.get("status") == "payment_unavailable":
            return f"Your total is {spoken_money(r.get('total', ''))}, but payment isn't available yet."
        card = self._ctx.get("card")
        on = f" on your {card}" if card else ""
        return f"Your total is {spoken_money(r.get('total', ''))}{on}. Say approve to pay with Voice ID."

    def _say_pay_list_cards(self, r):
        cards = r.get("cards") or []
        if not cards:
            return "You have no saved cards."
        act = next((c for c in cards if c.get("active")), cards[0])
        s = f"You have {_num_words(len(cards))} card{'s' if len(cards) != 1 else ''}. " \
            f"{act['brand']} ending {' '.join(act['last4'])} is active."
        return s

    def _say_web_search(self, r):
        if r.get("answer"):
            first = re.split(r"(?<=[.!?])\s", r["answer"])[0]
            return first + " Sources are on screen."
        if r.get("results"):
            return "Here's what I found. The top sources are on screen."
        return "I couldn't find anything on that."

    def _say_get_weather(self, r):
        now = r["now"]
        days = r.get("days", [])
        s = f"In {r.get('location', 'your area')} it's {now['temp']}° and {now['cond'].lower()}."
        if len(days) > 1:
            d = days[1]
            s += f" {d['day']} looks {d['cond'].lower()}, {d['lo']}–{d['hi']}°."
        return s

    def _say_navigate_to(self, r):
        return f"Starting navigation to {r['navigating_to']}. It's {r['distance']}, arriving around {r['eta']}."

    def _say_set_climate(self, r):
        bits = [f"cabin set to {r['cabin_temp_f']:.0f}°"]
        if r.get("seat_heat"):
            bits.append(f"seat heater on level {r['seat_heat']}")
        return "Done — " + " and ".join(bits) + "."

    def _say_vehicle_status(self, r):
        return f"You're at {r['battery_pct']:.0f}% with about {r['range_mi']} mi of range."
