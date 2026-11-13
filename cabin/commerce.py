"""Commerce tools: find a place, browse its menu, build a cart, hand over to checkout.

State lives on ``engine.shop`` (``cabin.services.driveos_shop.Shop``): ``place``, ``menu``, ``cart``.

Cart snapshot passed to ``engine.payment.begin(cart_snapshot)`` (plain dict, amounts in major units)::

    {
        "lines": [{"item_id": str, "name": str, "qty": int, "unit_price": float,
                   "options": {group name: [option names]}}],
        "total": float,            # subtotal + tax
        "subtotal": float, "tax": float,
        "total_cents": int,        # exact integer of ``total``
        "currency": "USD",
        "place": {"id": str, "name": str},
    }

``unit_price`` includes the price of the chosen options. Card buttons registered here:
``shop_checkout``, ``shop_clear`` and ``shop_open_1`` / ``shop_open_2``.
"""
from __future__ import annotations

import logging
from typing import Any

from driveos.domain.models import GeoPoint
from driveos.nav.geo import haversine

from .services.driveos_shop import MAX_QTY, PlaceView, Shop, ShopError, money

log = logging.getLogger("commerce")


def price_text(amount: int, currency: str = "USD") -> str:
    if str(currency).upper() == "INR":
        return f"₹{amount / 100:,.2f}"
    return money(amount, currency)

MAX_MENU_ITEMS = 36
MAX_CARD_ROWS = 4

SCHEMAS = [
    {"type": "function", "function": {
        "name": "shop_find_places",
        "description": "Find places the driver can order from and pick up: coffee, food, sandwiches, bakeries, bars, "
                       "fuel, EV charging, stores, pharmacies. Shows each place with its photo, rating and distance. "
                       "Use this for any 'order', 'get me a latte', 'fill up the tank' request.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": "coffee, food, sandwich, bakery, bar, gas, ev, store, pharmacy"},
            "query": {"type": "string", "description": "Dish, brand or free text, e.g. 'sushi', 'bagel'"},
            "near": {"type": "string", "description": "Optional area; omit for near the car"}}}}},
    {"type": "function", "function": {
        "name": "shop_open_place",
        "description": "Open a place from the last results and show its menu with prices and photos.",
        "parameters": {"type": "object", "properties": {
            "place_id": {"type": "string", "description": "Place id from shop_find_places, or its name"},
            "section": {"type": "string", "description": "Optional menu section to list in full"}},
            "required": ["place_id"]}}},
    {"type": "function", "function": {
        "name": "shop_add_to_cart",
        "description": "Add a menu item of the open place to the cart. Options pick sizes, milk, etc. by name.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string", "description": "Item id from the menu, or the item name"},
            "qty": {"type": "integer", "minimum": 1, "maximum": MAX_QTY},
            "options": {"type": "object", "description": "e.g. {\"Size\": \"Large\", \"Milk\": \"Oat\"}"},
            "place_id": {"type": "string", "description": "Optional: open this place first"},
            "replace_cart": {"type": "boolean", "description": "Start a new cart when it holds another place's items"}},
            "required": ["item_id"]}}},
    {"type": "function", "function": {
        "name": "shop_set_qty",
        "description": "Set the quantity of a cart line (0 removes it).",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string", "description": "Item id or name as in the cart"},
            "name": {"type": "string"}, "qty": {"type": "integer", "minimum": 0, "maximum": MAX_QTY}},
            "required": ["qty"]}}},
    {"type": "function", "function": {
        "name": "shop_remove",
        "description": "Remove an item from the cart, or take some of its quantity off.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string", "description": "Item id or name as in the cart"},
            "name": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}}}}},
    {"type": "function", "function": {
        "name": "shop_view_cart",
        "description": "Show the cart with the total and the check out / clear buttons.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "shop_clear_cart",
        "description": "Empty the cart.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "shop_request_checkout",
        "description": "Start paying for the cart. Call only when the driver asks to check out or pay.",
        "parameters": {"type": "object", "properties": {}}}},
]

CAPTIONS = {"shop_find_places": "Finding places", "shop_open_place": "Opening menu", "shop_add_to_cart": "Adding to cart",
            "shop_set_qty": "Updating cart", "shop_remove": "Updating cart", "shop_view_cart": "Opening cart",
            "shop_clear_cart": "Clearing cart", "shop_request_checkout": "Starting checkout"}


class Commerce:
    def __init__(self, engine):
        self.engine = engine
        self.shop: Shop = engine.shop
        self._slot = 0
        self._live: dict[str, tuple[PlaceView, Any]] = {}
        self._minutes: dict[str, int] = {}
        self._gen = None
        self._said_demo = False

    # ── helpers ──────────────────────────────────────────────
    def _dist(self, m: float) -> str:
        return self.engine.tools.dist(m)

    def _sync(self) -> None:
        self.engine.system_extra[self._slot] = self.shop.summary()

    def _err(self, e: ShopError) -> tuple[dict, None, None]:
        return {"error": str(e)}, None, None

    def _cart_result(self) -> dict[str, Any]:
        s = self.shop
        if not s.cart or not s.cart.lines:
            return {"items": [], "total": money(0)}
        t = s.totals()
        cur = s.cart.lines[0].unit_price.currency
        return {"place": s.cart_place.name if s.cart_place else "",
                "items": [{"name": s.label(ln), "qty": ln.quantity, "unit": money(ln.unit_price.amount, cur),
                           "line_total": money(ln.total.amount, cur)} for ln in s.cart.lines],
                "subtotal": money(t["subtotal"], cur), "tax": money(t["tax"], cur), "total": money(t["total"], cur)}

    # ── cards ────────────────────────────────────────────────
    def places_card(self, places: list[PlaceView]) -> dict:
        rows = []
        for i, p in enumerate(places[:MAX_CARD_ROWS]):
            rows.append({"icon": "pin", "label": self._dist(p.distance_m),
                         "value": f"{p.name} · ★ {p.rating:.1f}", "tint": "#FFB25E" if i == 0 else "",
                         "photo": p.cover})
        first = places[0]
        actions = [{"key": f"shop_open_{i + 1}", "text": f"Menu · {p.name[:22]}", "icon": "grid", "primary": i == 0,
                    "payload": {"place_id": p.id}} for i, p in enumerate(places[:2])]
        return {"kind": "shop_places", "title": first.name, "subtitle": first.address,
                "hero": {"kind": "image", "source": first.cover, "label": first.name},
                "rows": rows, "actions": actions,
                "places": [{"id": p.id, "name": p.name, "photo": p.cover, "rating": p.rating,
                            "distance": self._dist(p.distance_m)} for p in places]}

    def menu_card(self, place: PlaceView, sections: list, badge: str | None = None) -> dict:
        rows = []
        for name, items in sections:
            for it in [next((i for i in items if i.base_price.amount >= 300), items[0])]:
                rows.append({"icon": "sparkle", "label": price_text(it.base_price.amount, it.base_price.currency),
                             "value": f"{it.name} · {name}", "photo": self.shop.photos.item(it)})
            if len(rows) >= MAX_CARD_ROWS:
                break
        if len(rows) < MAX_CARD_ROWS and sections:
            listed = {r["value"].split(" · ")[0] for r in rows}
            for it in [i for i in sections[0][1] if i.name not in listed][:MAX_CARD_ROWS - len(rows)]:
                rows.append({"icon": "sparkle", "label": price_text(it.base_price.amount, it.base_price.currency),
                             "value": it.name, "photo": self.shop.photos.item(it)})
        sub = " · ".join(x for x in (place.hours, self._dist(place.distance_m), f"★ {place.rating:.1f}") if x)
        actions = [{"key": "shop_view_cart", "text": "View Cart", "icon": "grid",
                    "payload": {}}] if self.shop.cart else []
        card = {"kind": "shop_menu", "title": place.name, "subtitle": sub,
                "hero": {"kind": "image", "source": place.cover, "label": place.name},
                "rows": rows, "actions": actions}
        if badge:
            card["badge"] = badge
        return card

    def cart_card(self) -> dict | None:
        s = self.shop
        if not s.cart or not s.cart.lines or not s.cart_place:
            return None
        cur = s.cart.lines[0].unit_price.currency
        lines = s.cart.lines
        shown = lines if len(lines) <= MAX_CARD_ROWS - 1 else lines[:MAX_CARD_ROWS - 2]
        rows = [{"icon": "sparkle", "label": f"{ln.quantity} ×", "value": f"{s.label(ln)} · {money(ln.total.amount, cur)}",
                 "photo": self._photo_for(ln)} for ln in shown]
        if len(shown) < len(lines):
            rest = lines[len(shown):]
            rows.append({"icon": "grid", "label": f"+{len(rest)}", "value": "more items"})
        t = s.totals()
        rows.append({"icon": "flag", "label": "Total", "value": f"{money(t['total'], cur)} incl. tax",
                     "tint": "#8EF0C4"})
        rows = rows[-MAX_CARD_ROWS:] if len(rows) > MAX_CARD_ROWS else rows
        return {"kind": "shop_cart", "title": f"Your order · {s.cart_place.name}",
                "subtitle": f"{s.cart.item_count} item{'s' if s.cart.item_count != 1 else ''} · "
                            f"ready in about {s.cart_place.prep_minutes} min",
                "hero": {"kind": "image", "source": s.cart_place.cover, "label": s.cart_place.name},
                "rows": rows,
                "actions": [{"key": "shop_checkout", "text": f"Check out · {money(t['total'], cur)}", "icon": "send",
                             "primary": True, "payload": {}},
                            {"key": "shop_clear", "text": "Clear cart", "icon": "close", "payload": {}}]}

    def _photo_for(self, ln) -> str:
        s = self.shop
        if not s.cart:
            return ""
        menu = s.menu if getattr(s, "menu", None) is not None and s.menu.merchant_id == s.cart.merchant_id else None
        if menu is None and getattr(s, "catalog", None) is not None:
            try:
                menu = s.catalog.menu(s.cart.merchant_id)
            except KeyError:
                return ""
        if menu is None:
            return ""
        item = menu.item(ln.item_id)
        photos = getattr(s, "photos", None)
        return photos.item(item) if item and photos else ""

    # ── live places ──────────────────────────────────────────
    def _catalog(self):
        if self._gen is None:
            from .services.catalog_gen import CatalogGen
            self._gen = CatalogGen(getattr(self.engine, "places_db", None))
        return self._gen

    def _places_service(self):
        svc = getattr(self.engine, "places", None)
        if svc is not None:
            return svc
        if getattr(self.engine, "sim", False):
            return None
        cfg = getattr(self.engine, "cfg", None)
        http = getattr(self.engine, "http", None)
        if cfg is None or http is None or not cfg.flag("PLACES_LIVE", True):
            return None
        token = cfg.get("MAPBOX_ACCESS_TOKEN")
        if not token:
            return None
        from .services.places import PlacesService
        self.engine.places = PlacesService(http, token, getattr(self.engine, "places_db", None))
        return self.engine.places

    def _along(self):
        nav = getattr(self.engine, "nav", None)
        agent = getattr(nav, "agent", None) if nav is not None else None
        route = getattr(agent, "route", None) if agent is not None else None
        geom = getattr(route, "geometry", None) if route is not None else None
        if not geom:
            return None
        return [(p.lat, p.lon) for p in geom]

    async def _try_live(self, category: str, query: str, near: str, center):
        svc = self._places_service()
        if svc is None:
            return None
        from .services.places import PlacesOffline, live_category, within_minutes
        cat = live_category(category, query)
        minutes = within_minutes(query, near)
        latlon = center or getattr(self.engine.vehicle, "pos", None)
        if not latlon:
            return None
        try:
            found = await svc.search(cat, near=tuple(latlon), along=self._along(), within_min=minutes, limit=6)
        except PlacesOffline:
            return None
        except (OSError, TimeoutError, ValueError, TypeError):
            log.warning("places search failed")
            return None
        gen = self._catalog()
        views = []
        self._live = {}
        self._minutes = {}
        for p in found:
            menu = await gen.amenu_for(p)
            self._minutes[p.id] = round(p.drive_s / 60)
            view = PlaceView(
                id=p.id, name=p.name, address=p.address or "", category=p.category or cat,
                tags=[p.category or cat], lat=p.lat, lon=p.lon, distance_m=float(p.distance_m or 0),
                rating=float(p.rating or 4.5), hours="", cover=p.cover_uri or "",
                tax_rate="0.05" if menu.currency == "INR" else "0.08", prep_minutes=8,
                currency=menu.currency)
            self._live[p.id] = (view, menu)
            views.append(view)
        return views

    def _radius_km(self) -> float:
        cfg = getattr(self.engine, "cfg", None)
        return cfg.num("PLACES_RADIUS_KM", 15.0) if cfg is not None else 15.0

    def _within_radius(self, places: list[PlaceView], center) -> list[PlaceView]:
        ref = center or getattr(self.engine.vehicle, "pos", None)
        if not ref:
            return places
        origin = GeoPoint(lat=float(ref[0]), lon=float(ref[1]))
        limit_m = self._radius_km() * 1000.0
        return [p for p in places if haversine(origin, GeoPoint(lat=p.lat, lon=p.lon)) <= limit_m]

    def _match_live(self, ref: str):
        ref = (ref or "").strip()
        if not ref or not self._live:
            return None
        if ref in self._live:
            return self._live[ref]
        low = ref.casefold()
        for view, menu in self._live.values():
            if view.name.casefold() == low:
                return view, menu
        return None

    # ── tools ────────────────────────────────────────────────
    async def t_find(self, category: str = "", query: str = "", near: str = "", **_):
        await self.shop.load()
        center = None
        if near:
            try:
                p = await self.engine.maps.geocode(near, self.engine.vehicle.pos)
            except Exception as e:                                   # noqa: BLE001
                log.warning("geocode '%s': %s", near, e)
                p = None
            if p:
                center = (p.lat, p.lon)
        live = await self._try_live(category, query, near, center)
        if live is not None:
            self.shop.results = live
            self.shop.view = "places"
            places = live
        else:
            self._minutes = {}
            places = self._within_radius(self.shop.find(category, query, center, 4), center)
            self.shop.results = places
        if not places:
            note = "no matching places" if live is not None else f"nothing nearby within {self._radius_km():g} km"
            return {"results": [], "note": note}, None, None
        self._sync()
        res = [{"place_id": p.id, "name": p.name, "category": p.category, "distance": self._dist(p.distance_m),
                "rating": p.rating, "hours": p.hours, "address": p.address,
                **({"minutes": self._minutes[p.id]} if p.id in self._minutes else {})} for p in places]
        return {"results": res}, self.places_card(places), None

    async def t_open(self, place_id: str = "", section: str = "", **_):
        await self.shop.load()
        live = self._match_live(place_id)
        try:
            if live:
                place, menu = live
                place.currency = menu.currency
                self.shop.place, self.shop.menu, self.shop.view = place, menu, "menu"
            else:
                place, menu = self.shop.open(place_id)
        except ShopError as e:
            return self._err(e)
        sections = self.shop.sections(menu)
        if section:
            low = section.casefold()
            picked = [s for s in sections if low in s[0].casefold()]
            listing = picked or sections
        else:
            listing = sections
        out, budget = [], MAX_MENU_ITEMS
        for name, items in listing:
            take = items[:budget]
            budget -= len(take)
            out.append({"section": name, "total_items": len(items), "items": [
                {"item_id": i.id, "name": i.name,
                 "price": price_text(i.base_price.amount, i.base_price.currency),
                 **({"options": self.shop.option_summary(i)} if i.modifier_groups else {}),
                 **({} if i.available else {"unavailable": "needs options not selectable here"})} for i in take]})
            if budget <= 0:
                break
        self._sync()
        note = "default options are marked * ; prices exclude tax"
        badge = None
        if live:
            badge = "Demo menu"
            if not self._said_demo:
                self._said_demo = True
                note = "menus are demo data. " + note
        res = {"place_id": place.id, "place": place.name, "hours": place.hours, "menu": out,
               "sections": [s[0] for s in sections], "note": note}
        return res, self.menu_card(place, listing, badge=badge), None

    async def t_add(self, item_id: str = "", qty: int = 1, options: dict | None = None, place_id: str = "",
                    replace_cart: bool = False, **_):
        await self.shop.load()
        try:
            if place_id and (not self.shop.place or place_id not in (self.shop.place.id, self.shop.place.name)):
                self.shop.open(place_id)
            item = self.shop.find_item(item_id)
            added = self.shop.add(item, qty, options if isinstance(options, dict) else None, replace=replace_cart)
        except ShopError as e:
            return self._err(e)
        self._sync()
        t = self.shop.totals()
        cur = item.base_price.currency
        card = self.cart_card()
        return {"added": added, "cart_total": money(t["total"], cur), "cart": self._cart_result()["items"]}, card, None

    async def t_set_qty(self, qty: int = 1, item_id: str = "", name: str = "", **_):
        try:
            res = self.shop.set_qty(item_id or name, int(qty))
        except ShopError as e:
            return self._err(e)
        self._sync()
        return {**res, "cart": self._cart_result()}, self.cart_card(), None

    async def t_remove(self, item_id: str = "", name: str = "", qty: int | None = None, **_):
        try:
            res = self.shop.remove(item_id or name, int(qty) if qty else None)
        except ShopError as e:
            return self._err(e)
        self._sync()
        return {**res, "cart": self._cart_result()}, self.cart_card(), None

    async def t_view(self, **_):
        card = self.cart_card()
        if card is None:
            return {"items": [], "note": "the cart is empty"}, None, None
        self.shop.view = "cart"
        return self._cart_result(), card, None

    async def t_clear(self, **_):
        self.shop.clear()
        self._sync()
        return {"ok": True, "cart": "empty"}, None, None

    async def t_checkout(self, **_):
        try:
            snap = self.shop.snapshot()
        except ShopError as e:
            return self._err(e)
        payment = getattr(self.engine, "payment", None)
        if payment:
            self.shop.view = "checkout"
            await payment.begin(snap)
            return {"status": "checkout_started", "total": money(snap["total_cents"], snap["currency"]),
                    "place": snap["place"]["name"]}, None, None
        self.engine.emit("toast", "Checkout is not available yet")
        return {"status": "payment_unavailable", "total": money(snap["total_cents"], snap["currency"])}, \
            self.cart_card(), None

    # ── card buttons ─────────────────────────────────────────
    async def a_open(self, payload: dict) -> None:
        _, card, _ = await self.t_open(payload.get("place_id", ""))
        self._show(card)

    async def a_view(self, payload: dict) -> None:
        _, card, _ = await self.t_view()
        self._show(card)

    async def a_checkout(self, payload: dict) -> None:
        res, _, _ = await self.t_checkout()
        if "error" in res:
            self.engine.emit("toast", res["error"])

    async def a_clear(self, payload: dict) -> None:
        await self.t_clear()
        self.engine.emit("card", None)
        self.engine.emit("toast", "Cart cleared")

    def _show(self, card: dict | None) -> None:
        if card:
            self.engine.last_card = card
            self.engine.emit("card", card)


def install(engine) -> None:
    engine.shop = Shop(pos=lambda: engine.vehicle.pos)
    engine.system_extra.append(engine.shop.summary())
    c = Commerce(engine)
    c._slot = len(engine.system_extra) - 1
    engine.commerce = c
    engine.tools.extend(SCHEMAS, {
        "shop_find_places": c.t_find, "shop_open_place": c.t_open, "shop_add_to_cart": c.t_add,
        "shop_set_qty": c.t_set_qty, "shop_remove": c.t_remove, "shop_view_cart": c.t_view,
        "shop_clear_cart": c.t_clear, "shop_request_checkout": c.t_checkout}, CAPTIONS)
    engine.action_handlers.update({
        "shop_open_1": c.a_open, "shop_open_2": c.a_open, "shop_view_cart": c.a_view,
        "shop_checkout": c.a_checkout, "shop_clear": c.a_clear})
    # the map's own place search stays for landmarks and addresses; anything you can order comes from the shop
    for s in engine.tools.schemas:
        if s["function"]["name"] == "find_places":
            s["function"]["description"] = (
                "Find a landmark, address, parking or a place you cannot order from, and show it on a map. "
                "For cafés, restaurants, fuel, EV charging, pharmacies, stores and bars use shop_find_places instead.")
    engine.system_extra.append(
        "Ordering: use shop_find_places for anything the driver might buy, shop_open_place to read a menu, "
        "shop_add_to_cart to add, and shop_request_checkout when they want to pay. Never claim an order is placed; "
        "payment is approved by the driver with Voice ID or Face ID.")
