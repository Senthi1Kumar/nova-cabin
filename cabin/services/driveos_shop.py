"""In-process shop backed by the DriveOS merchant catalogue.

Wraps ``driveos.merchants.local_catalog.LocalCatalog`` (places, per-place menus, varied prices) and the
DriveOS cart model (``Cart`` / ``CartLine``, option-aware line matching, quantity cap of 20). Photos are
local files under ``assets/places``; every place gets a cover from a category pool and every menu item a
photo matched on its name, so two places in one result never share a cover while the pool lasts.

The service is synchronous apart from ``load`` and keeps all session state (open place, menu, cart) so the
payment module can read it from ``engine.shop``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from driveos.config import LocationConfig
from driveos.domain.categories import CATEGORIES, detect, matches
from driveos.domain.models import Cart, GeoPoint, Menu, MenuItem
from driveos.merchants.base import describe_selections, make_line, normalise_selections
from driveos.merchants.local_catalog import LocalCatalog
from rapidfuzz import fuzz, process

from .maps import haversine_m as haversine

log = logging.getLogger("shop")

PHOTO_DIR = Path(__file__).resolve().parents[2] / "assets" / "places"
MAX_QTY = 20
RADII_M = (8_000, 25_000, 80_000, 300_000, 5_000_000)

_STOP = {"the", "a", "an", "of", "and", "with", "in", "on", "bag", "can", "large", "small", "medium", "regular",
         "oz", "co", "bar", "house", "special", "classic", "original", "fresh", "mini", "double", "single"}
_FALLBACK_DRINK = ("coffee", "tea", "drink", "juice", "soda", "water", "beer", "wine", "shake", "lemonade", "cola",
                   "latte", "brew", "smoothie", "cocktail", "milk")
_GLYPH_PHOTO = {"fuel": "fuel_pump", "wash": "fuel_pump", "bolt": "ev_charging", "coffee": "latte",
                "croissant": "croissant", "water": "water_bottle", "bag": "chips", "glass": "soda"}
_COVER_BY_KIND = {"fuel": "fuel", "ev": "ev", "pharmacy": "pharmacy", "store": "grocery"}
_COVER_BY_TAG = (("bakery", "bakery"), ("coffee", "cafe"), ("bar", "bar"), ("sandwich", "deli"), ("gas", "fuel"),
                 ("ev", "ev"), ("pharmacy", "pharmacy"), ("store", "grocery"), ("hotel", "hotel"))
_CUISINE_COVER = (("sushi", "rest_sushi"), ("pizza", "rest_pizza"), ("thai", "rest_thai"), ("indian", "rest_indian"),
                  ("naan", "rest_indian"), ("mexic", "rest_mexican"), ("taco", "rest_mexican"),
                  ("burrito", "rest_mexican"), ("chicken", "rest_chicken"), ("salad", "rest_salad"),
                  ("ice cream", "rest_icecream"), ("gelato", "rest_icecream"), ("med", "rest_med"),
                  ("burger", "fastfood"), ("diner", "rest_diner"))


class ShopError(Exception):
    """A user-facing problem with a shop request; the message is safe to hand to the model."""


def _tokens(s: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z]+", s.lower()):
        if t.endswith("ies") and len(t) > 4:
            t = t[:-3] + "y"
        elif t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def money(cents: int, currency: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, currency + " ")
    return f"{sym}{cents / 100:,.2f}"


class Photos:
    """Cover and item photos from ``assets/places`` as ``file://`` URIs."""

    def __init__(self, root: Path = PHOTO_DIR):
        self.root = root
        self.stems = sorted(p.stem for p in root.glob("*.jpg")) if root.is_dir() else []
        self._tok = {s: set(_tokens(s.replace("_", " "))) - _STOP for s in self.stems if not s.startswith("cover_")}
        self.pools: dict[str, list[str]] = {}
        for s in self.stems:
            m = re.match(r"cover_([a-z]+(?:_[a-z]+)?)_?\d*$", s)
            if m:
                self.pools.setdefault(m.group(1), []).append(s)

    def uri(self, stem: str) -> str:
        return (self.root / f"{stem}.jpg").as_uri() if stem in self.stems or not self.stems else ""

    def cover(self, place: dict[str, Any], used: set[str]) -> str:
        name = (place["name"] + " " + place.get("description", "")).lower()
        kind = place.get("kind", "")
        pool = _COVER_BY_KIND.get(kind)
        tags = place.get("tags", [])
        if not pool:
            pool = next((p for t, p in _COVER_BY_TAG if t in tags and t != "food"), "")
        if pool == "cafe" and "tea" in name and "coffee" not in name:
            pool = "tea"
        keys = [pool] if pool else []
        if not keys:
            cuisine = next((c for k, c in _CUISINE_COVER if k in name), "")
            keys = [cuisine] if cuisine else []
            keys += ["restaurant", "rest_booth", "rest_kitchen", "rest_patio", "rest_diner", "rest_fine"]
        stems = [s for k in keys for s in self.pools.get(k, [])]
        if not stems:
            return ""
        h = int(hashlib.sha1(place["id"].encode()).hexdigest()[:8], 16)
        for i in range(len(stems)):
            s = stems[(h + i) % len(stems)]
            if s not in used:
                used.add(s)
                return self.uri(s)
        return self.uri(stems[h % len(stems)])

    def item(self, item: MenuItem) -> str:
        name = _tokens(item.name)
        want = set(name) - _STOP
        best, score = "", 0.0
        for stem, toks in self._tok.items():
            if not toks:
                continue
            hit = len(toks & want)
            if not hit:
                continue
            s = hit * 10 + hit / len(toks) * 5 - len(toks) * 0.1
            if s > score:
                best, score = stem, s
        if not best:
            glyph = item.refs.get("glyph", "")
            hay = (item.name + " " + item.category).lower()
            best = _GLYPH_PHOTO.get(glyph) or ("drink_generic" if any(w in hay for w in _FALLBACK_DRINK)
                                              else "food_generic")
        return self.uri(best)


@dataclass
class PlaceView:
    id: str
    name: str
    address: str
    category: str
    tags: list[str]
    lat: float
    lon: float
    distance_m: float
    rating: float
    hours: str
    cover: str
    tax_rate: str
    prep_minutes: int
    description: str = ""
    pickup_spot: str = ""
    currency: str = "USD"


@dataclass
class Shop:
    """Session state: last results, open place and menu, the cart."""

    pos: Any = None                       # callable -> (lat, lon)
    photos: Photos = field(default_factory=Photos)
    catalog: LocalCatalog | None = None
    view: str = "idle"                    # idle | places | menu | cart | checkout
    results: list[PlaceView] = field(default_factory=list)
    place: PlaceView | None = None
    menu: Menu | None = None
    cart: Cart | None = None
    cart_place: PlaceView | None = None
    _covers_used: set[str] = field(default_factory=set)
    _lines: int = 0

    # ── loading ──────────────────────────────────────────────
    async def load(self) -> None:
        if self.catalog is None:
            self.catalog = await asyncio.to_thread(LocalCatalog)
            log.info("catalogue: %d merchants", len(self.catalog.merchants))

    def _center(self, center: tuple[float, float] | None) -> GeoPoint:
        if center is not None:
            lat, lon = center
        elif self.pos:
            lat, lon = self.pos()
        else:
            home = LocationConfig().fallback
            lat, lon = home.lat, home.lon
        return GeoPoint(lat=lat, lon=lon)

    # ── places ───────────────────────────────────────────────
    def _view(self, m: dict[str, Any], used: set[str]) -> PlaceView:
        h = int(hashlib.sha1(m["id"].encode()).hexdigest()[8:12], 16)
        rating = m.get("rating") or round(4.0 + (h % 10) / 10, 1)
        loc = m["location"]
        return PlaceView(
            id=m["id"], name=m["name"], address=m["address"], category=m["tags"][0], tags=list(m["tags"]),
            lat=loc["lat"], lon=loc["lon"], distance_m=m.get("distance_m", 0.0), rating=float(rating),
            hours=m.get("hours", ""), cover=self.photos.cover(m, used), tax_rate=str(m.get("tax_rate", "0.08")),
            prep_minutes=int(m.get("prep_minutes", 8)), description=m.get("description", ""),
            pickup_spot=m.get("pickup_spot", ""))

    def find(self, category: str = "", query: str = "", center: tuple[float, float] | None = None,
             limit: int = 4) -> list[PlaceView]:
        assert self.catalog is not None
        c = self._center(center)
        self.catalog.ensure_venues(c)
        text = f"{category} {query}".strip()
        cat = category if category in CATEGORIES else (detect(text)[0] or "")
        words = [w for w in _tokens(query or ("" if cat else category)) if w not in _STOP]
        hits: list[dict[str, Any]] = []
        for radius in RADII_M:
            near = self.catalog.near(c, radius)
            pool = [m for m in near if matches(m["tags"], cat)] if cat else near
            if words:
                scored = [(self._text_score(m, words), m) for m in pool]
                hit = [m for s, m in scored if s > 0]
                if hit:
                    pool = hit
                elif not cat:
                    pool = []
            if pool:
                hits = pool
                if len(pool) >= 2 or radius >= RADII_M[2]:
                    break
        used: set[str] = set()
        hits.sort(key=lambda m: (-self._text_score(m, words) if words else 0, m["distance_m"]))
        self.results = [self._view(m, used) for m in hits[:limit]]
        self.view = "places"
        return self.results

    def _text_score(self, m: dict[str, Any], words: list[str]) -> int:
        hay = set(_tokens(m["name"] + " " + m.get("description", "")))
        menu = set(_tokens(" ".join(i["name"] for c in m["categories"] for i in c["items"])))
        return sum(2 for w in words if w in hay) + sum(1 for w in words if w in menu)

    def resolve_place(self, ref: str) -> PlaceView:
        assert self.catalog is not None
        ref = (ref or "").strip()
        if not ref:
            if self.place:
                return self.place
            raise ShopError("which place? call shop_find_places first")
        pool = {p.id: p for p in self.results}
        if ref in pool:
            return pool[ref]
        if ref in self.catalog.merchants:
            return self._view({**self.catalog.merchants[ref], "distance_m": self._dist(ref)}, set())
        names = {p.name: p for p in self.results}
        if self.place:
            names.setdefault(self.place.name, self.place)
        if not names:
            names = {m["name"]: None for m in self.catalog.merchants.values()}
        hit = process.extractOne(ref, list(names), scorer=fuzz.WRatio)
        if not hit or hit[1] < 75:
            raise ShopError(f"no place called '{ref}' in the results")
        if names[hit[0]] is not None:
            return names[hit[0]]
        mid = next(i for i, m in self.catalog.merchants.items() if m["name"] == hit[0])
        return self._view({**self.catalog.merchants[mid], "distance_m": self._dist(mid)}, set())

    def _dist(self, mid: str) -> float:
        assert self.catalog is not None
        c = self._center(None)
        loc = self.catalog.merchants[mid]["location"]
        return haversine((c.lat, c.lon), (loc["lat"], loc["lon"]))

    def open(self, ref: str) -> tuple[PlaceView, Menu]:
        assert self.catalog is not None
        place = self.resolve_place(ref)
        menu = self.catalog.menu(place.id)
        if not menu.items:
            raise ShopError(f"{place.name} has no menu to order from")
        place.currency = menu.currency
        self.place, self.menu, self.view = place, menu, "menu"
        return place, menu

    def sections(self, menu: Menu) -> list[tuple[str, list[MenuItem]]]:
        return [(c, [i for i in menu.items if i.category == c]) for c in menu.categories
                if any(i.category == c for i in menu.items)]

    # ── item lookup ──────────────────────────────────────────
    def find_item(self, ref: str) -> MenuItem:
        if not self.menu or not self.place:
            raise ShopError("no place is open; call shop_open_place first")
        ref = (ref or "").strip()
        if not ref:
            raise ShopError("which item?")
        hit = self.menu.item(ref)
        if hit:
            return hit
        low = ref.casefold()
        exact = [i for i in self.menu.items if i.name.casefold() == low]
        if exact:
            return exact[0]
        names = [i.name for i in self.menu.items]
        res = process.extract(ref, names, scorer=fuzz.token_set_ratio, limit=3)
        if not res or res[0][1] < 70:
            raise ShopError(f"'{ref}' is not on the {self.place.name} menu")
        top = [r for r in res if r[1] >= res[0][1] - 3]
        if len(top) > 1 and res[0][1] < 100:
            cand = ", ".join(r[0] for r in top)
            raise ShopError(f"'{ref}' could mean: {cand}. Ask which one")
        return self.menu.items[res[0][2]]

    def resolve_options(self, item: MenuItem, options: dict[str, Any] | None) -> tuple[dict[str, list[str]], list[str]]:
        """Map spoken group/option names (or ids) to ids. Returns (selections, ignored)."""
        sel: dict[str, list[str]] = {}
        ignored: list[str] = []
        for key, val in (options or {}).items():
            vals = val if isinstance(val, list) else [val]
            group = next((g for g in item.modifier_groups
                          if g.id == key or key.casefold() in [g.name.casefold(), *[a.casefold() for a in g.aliases]]), None)
            for v in vals:
                v = str(v)
                groups = [group] if group else item.modifier_groups
                found = None
                for g in groups:
                    for o in g.options:
                        if v.casefold() in (o.id.casefold(), o.name.casefold(), *[a.casefold() for a in o.aliases]):
                            found = (g, o)
                            break
                    if found:
                        break
                if not found:
                    for g in groups:
                        names = [o.name for o in g.options]
                        r = process.extractOne(v, names, scorer=fuzz.WRatio)
                        if r and r[1] >= 85:
                            found = (g, g.options[r[2]])
                            break
                if found:
                    sel.setdefault(found[0].id, []).append(found[1].id)
                else:
                    ignored.append(f"{key}={v}")
        return sel, ignored

    def option_summary(self, item: MenuItem) -> list[dict[str, Any]]:
        return [{"group": g.name, "choose": g.max_select or 1, "options": [
            o.name + (f" (+{money(o.price_delta.amount)})" if o.price_delta.amount else "") + (" *" if o.default else "")
            for o in g.options]} for g in item.modifier_groups]

    # ── cart ─────────────────────────────────────────────────
    def _new_line_id(self) -> str:
        self._lines += 1
        return f"ln{self._lines}"

    def add(self, item: MenuItem, qty: int, options: dict[str, Any] | None, *, replace: bool = False) -> dict[str, Any]:
        assert self.place and self.menu
        if not item.available:
            raise ShopError(f"{item.name} needs options that cannot be chosen from the car yet")
        if self.cart and self.cart.lines and self.cart.merchant_id != self.place.id and not replace:
            other = self.cart_place.name if self.cart_place else "another place"
            raise ShopError(f"the cart holds items from {other}. Ask whether to start a new cart for "
                            f"{self.place.name}, then repeat with replace_cart=true")
        if replace or not self.cart or self.cart.merchant_id != self.place.id:
            self.cart, self.cart_place = Cart(merchant_id=self.place.id), self.place
        qty = max(1, int(qty or 1))
        capped = qty > MAX_QTY
        qty = min(qty, MAX_QTY)
        picked, ignored = self.resolve_options(item, options)
        sel = normalise_selections(item, picked)
        line = next((ln for ln in self.cart.lines if ln.item_id == item.id and ln.selections == sel), None)
        if line:
            line.quantity = min(MAX_QTY, line.quantity + qty)
            capped = capped or line.quantity == MAX_QTY
        else:
            line = make_line(self._new_line_id(), item, sel, qty)
            self.cart.lines.append(line)
        self.view = "cart"
        return {"line_id": line.line_id, "name": line.name, "quantity": line.quantity,
                "options": describe_selections(item, line.selections), "capped_at_20": capped,
                "ignored_options": ignored}

    def label(self, line) -> str:
        item = self.menu.item(line.item_id) if self.menu and self.menu.merchant_id == (self.cart.merchant_id if self.cart else "") else None
        if item is None and self.catalog and self.cart:
            try:
                item = self.catalog.menu(self.cart.merchant_id).item(line.item_id)
            except KeyError:
                item = None
        opts = describe_selections(item, line.selections, skip_defaults=True) if item else ""
        return line.name + (f" ({opts})" if opts else "")

    def find_line(self, ref: str):
        if not self.cart or not self.cart.lines:
            raise ShopError("the cart is empty")
        ref = (ref or "").strip()
        if not ref:
            if len(self.cart.lines) == 1:
                return self.cart.lines[0]
            raise ShopError("which cart item?")
        for ln in self.cart.lines:
            if ref in (ln.line_id, ln.item_id):
                return ln
        labels = [self.label(ln) for ln in self.cart.lines]
        low = ref.casefold()
        for ln, lab in zip(self.cart.lines, labels, strict=True):
            if lab.casefold() == low or ln.name.casefold() == low:
                return ln
        res = process.extract(ref, labels, scorer=fuzz.token_set_ratio, limit=2)
        if not res or res[0][1] < 70:
            raise ShopError(f"'{ref}' is not in the cart")
        if len(res) > 1 and res[1][1] >= res[0][1] - 3 and res[0][1] < 100:
            raise ShopError(f"'{ref}' could mean: {res[0][0]} or {res[1][0]}. Ask which one")
        return self.cart.lines[res[0][2]]

    def set_qty(self, ref: str, qty: int) -> dict[str, Any]:
        line = self.find_line(ref)
        qty = int(qty)
        capped = qty > MAX_QTY
        if qty <= 0:
            return self.remove(ref)
        line.quantity = min(MAX_QTY, qty)
        self.view = "cart"
        return {"name": line.name, "quantity": line.quantity, "capped_at_20": capped}

    def remove(self, ref: str, qty: int | None = None) -> dict[str, Any]:
        line = self.find_line(ref)
        if qty and 0 < qty < line.quantity:
            line.quantity -= qty
            return {"name": line.name, "quantity": line.quantity, "removed": qty}
        assert self.cart
        self.cart.lines = [ln for ln in self.cart.lines if ln.line_id != line.line_id]
        if not self.cart.lines:
            self.cart, self.cart_place = None, None
            self.view = "menu" if self.menu else "idle"
        return {"name": line.name, "removed": line.quantity}

    def clear(self) -> None:
        self.cart, self.cart_place = None, None
        self.view = "menu" if self.menu else "idle"

    # ── totals / snapshot ────────────────────────────────────
    def totals(self) -> dict[str, int]:
        if not self.cart or not self.cart_place:
            return {"subtotal": 0, "tax": 0, "total": 0}
        sub = self.cart.subtotal
        tax = sub.apply_rate(self.cart_place.tax_rate)
        return {"subtotal": sub.amount, "tax": tax.amount, "total": sub.amount + tax.amount}

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict cart for the payment module; see the shape documented in ``cabin.commerce``."""
        if not self.cart or not self.cart.lines or not self.cart_place:
            raise ShopError("the cart is empty")
        t = self.totals()
        cur = self.cart.lines[0].unit_price.currency
        lines = []
        for ln in self.cart.lines:
            opts = self._line_options(ln)
            lines.append({"item_id": ln.item_id, "name": ln.name, "qty": ln.quantity,
                          "unit_price": ln.unit_price.amount / 100, "options": opts})
        return {"lines": lines, "total": t["total"] / 100, "subtotal": t["subtotal"] / 100, "tax": t["tax"] / 100,
                "total_cents": t["total"], "currency": cur,
                "place": {"id": self.cart_place.id, "name": self.cart_place.name}}

    def _line_options(self, ln) -> dict[str, list[str]]:
        item = None
        if self.menu and self.cart and self.menu.merchant_id == self.cart.merchant_id:
            item = self.menu.item(ln.item_id)
        elif self.catalog and self.cart:
            try:
                item = self.catalog.menu(self.cart.merchant_id).item(ln.item_id)
            except KeyError:
                item = None
        out: dict[str, list[str]] = {}
        for g in (item.modifier_groups if item else []):
            names = [o.name for oid in ln.selections.get(g.id, []) if (o := g.option(oid))]
            if names:
                out[g.name] = names
        return out

    def summary(self) -> str:
        if self.cart and self.cart.lines and self.cart_place:
            t = self.totals()
            items = ", ".join(f"{ln.quantity}x {ln.name}" for ln in self.cart.lines[:6])
            cart = f"cart at {self.cart_place.name}: {items}, total {money(t['total'])} incl. tax"
        else:
            cart = "cart empty"
        place = f"open place: {self.place.name}" if self.place else "no place open"
        return f"Shopping state: {place}; {cart}."
