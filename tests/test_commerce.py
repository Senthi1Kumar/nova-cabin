"""Commerce tools against the bundled DriveOS catalogue; no network, no engine."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin import commerce

CAR = (37.7793, -122.4193)


class FakeTools:
    def __init__(self):
        self.methods: dict = {}
        self.schemas: list = []

    def extend(self, schemas, methods, captions=None):
        self.schemas += schemas
        self.methods.update(methods)

    def dist(self, m):
        return f"{m / 1609.344:.1f} mi"

    async def run(self, name, args):
        return await self.methods[name](**args)


class FakeEngine:
    def __init__(self, payment=None):
        self.tools = FakeTools()
        self.vehicle = SimpleNamespace(pos=CAR)
        self.system_extra: list[str] = []
        self.action_handlers: dict = {}
        self.intercepts: list = []
        self.events: list = []
        self.last_card = None
        if payment:
            self.payment = payment
        commerce.install(self)

    def emit(self, k, v):
        self.events.append((k, v))


class FakePayment:
    def __init__(self):
        self.carts = []

    async def begin(self, cart):
        self.carts.append(cart)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def loaded():
    e = FakeEngine()
    run(e.shop.load())
    return e.shop.catalog


@pytest.fixture
def eng(loaded):
    e = FakeEngine()
    e.shop.catalog = loaded
    return e


def call(e, name, **args):
    return run(e.tools.run(name, args))


def first_coffee(e):
    res, card, _ = call(e, "shop_find_places", category="coffee")
    return res, card


def test_catalogue_loads_and_tools_register(eng):
    names = {s["function"]["name"] for s in eng.tools.schemas}
    assert {"shop_find_places", "shop_open_place", "shop_add_to_cart", "shop_set_qty", "shop_remove",
            "shop_view_cart", "shop_request_checkout"} <= names
    assert len(eng.shop.catalog.merchants) >= 10
    assert "cart empty" in eng.system_extra[0] and any("shop_find_places" in s for s in eng.system_extra)


def test_find_by_category_has_distinct_covers_and_photos_exist(eng):
    res, card = first_coffee(eng)
    assert res["results"] and card["kind"] == "shop_places"
    covers = [p["photo"] for p in card["places"]]
    assert all(c.startswith("file://") and Path(c[7:]).is_file() for c in covers)
    assert len(set(covers)) == len(covers)
    assert card["hero"]["kind"] == "image"
    assert all("coffee" in p.tags for p in eng.shop.results)


@pytest.mark.parametrize("cat", ["food", "sandwich", "gas", "ev", "pharmacy", "store", "bar"])
def test_find_other_categories(eng, cat):
    res, card, _ = call(eng, "shop_find_places", category=cat)
    assert res["results"], cat
    assert Path(card["hero"]["source"][7:]).is_file()


def test_find_by_query_text(eng):
    res, _, _ = call(eng, "shop_find_places", query="tacos")
    assert "results" in res


def test_menus_differ_per_place(eng):
    res, _ = first_coffee(eng)
    ids = [r["place_id"] for r in res["results"]]
    menus = []
    for pid in ids[:2]:
        m, card, _ = call(eng, "shop_open_place", place_id=pid)
        assert card["kind"] == "shop_menu" and card["rows"]
        menus.append({(i["name"], i["price"]) for s in m["menu"] for i in s["items"]})
    if len(ids) > 1:
        assert menus[0] != menus[1]


def _open_with_options(eng):
    call(eng, "shop_find_places", category="coffee")
    for p in eng.shop.results:
        call(eng, "shop_open_place", place_id=p.id)
        item = next((i for i in eng.shop.menu.items if i.modifier_groups and i.available), None)
        if item:
            return item
    pytest.skip("no coffee item with options")


def test_add_set_remove_totals(eng):
    first_coffee(eng)
    call(eng, "shop_open_place", place_id=eng.shop.results[0].id)
    items = [i for i in eng.shop.menu.items if i.available][:2]
    a, b = items
    res, card, _ = call(eng, "shop_add_to_cart", item_id=a.id, qty=2)
    assert res["added"]["quantity"] == 2 and card["kind"] == "shop_cart"
    sub = 2 * a.base_price.amount
    assert eng.shop.totals()["subtotal"] >= sub - 1 or a.modifier_groups
    call(eng, "shop_add_to_cart", item_id=a.id, qty=1)
    assert eng.shop.cart.lines[0].quantity == 3
    call(eng, "shop_add_to_cart", item_id=b.name, qty=1)
    assert len(eng.shop.cart.lines) == 2 or b.id == a.id
    res, _, _ = call(eng, "shop_set_qty", item_id=a.name, qty=1)
    assert res["quantity"] == 1
    res, _, _ = call(eng, "shop_remove", item_id=a.name)
    assert all(ln.item_id != a.id for ln in eng.shop.cart.lines)
    assert "total" in call(eng, "shop_view_cart")[0] or "note" in call(eng, "shop_view_cart")[0]


def test_quantity_cap(eng):
    first_coffee(eng)
    call(eng, "shop_open_place", place_id=eng.shop.results[0].id)
    item = next(i for i in eng.shop.menu.items if i.available)
    res, _, _ = call(eng, "shop_add_to_cart", item_id=item.id, qty=50)
    assert res["added"]["quantity"] == commerce.MAX_QTY and res["added"]["capped_at_20"]
    call(eng, "shop_add_to_cart", item_id=item.id, qty=5)
    assert eng.shop.cart.lines[0].quantity == commerce.MAX_QTY
    res, _, _ = call(eng, "shop_set_qty", item_id=item.name, qty=99)
    assert res["quantity"] == commerce.MAX_QTY


def test_options_change_price_and_split_lines(eng):
    item = _open_with_options(eng)
    group = next((g for g in item.modifier_groups if any(o.price_delta.amount for o in g.options)), None)
    if group is None:
        pytest.skip("no priced option")
    pricey = max(group.options, key=lambda o: o.price_delta.amount)
    call(eng, "shop_add_to_cart", item_id=item.id, qty=1)
    call(eng, "shop_add_to_cart", item_id=item.id, qty=1, options={group.name: pricey.name})
    lines = eng.shop.cart.lines
    assert len(lines) == 2
    assert lines[1].unit_price.amount - lines[0].unit_price.amount == pricey.price_delta.amount - next(
        (o.price_delta.amount for o in group.options if o.default), 0)
    snap = eng.shop.snapshot()
    assert snap["lines"][1]["options"][group.name] == [pricey.name]


def test_other_place_requires_replace(eng):
    call(eng, "shop_find_places", category="food")
    results = list(eng.shop.results)
    assert len(results) >= 2
    call(eng, "shop_open_place", place_id=results[0].id)
    item = next(i for i in eng.shop.menu.items if i.available)
    call(eng, "shop_add_to_cart", item_id=item.id)
    call(eng, "shop_open_place", place_id=results[1].id)
    other = next(i for i in eng.shop.menu.items if i.available)
    res, _, _ = call(eng, "shop_add_to_cart", item_id=other.id)
    assert "error" in res and eng.shop.cart.merchant_id == results[0].id
    res, _, _ = call(eng, "shop_add_to_cart", item_id=other.id, replace_cart=True)
    assert "error" not in res and eng.shop.cart.merchant_id == results[1].id


def test_errors_are_returned_not_raised(eng):
    assert "error" in call(eng, "shop_add_to_cart", item_id="x")[0]
    assert "error" in call(eng, "shop_request_checkout")[0]
    assert "error" in call(eng, "shop_open_place", place_id="zzzz nothing")[0]


def test_cart_card_actions_and_handlers(eng):
    first_coffee(eng)
    call(eng, "shop_open_place", place_id=eng.shop.results[0].id)
    item = next(i for i in eng.shop.menu.items if i.available)
    _, card, _ = call(eng, "shop_add_to_cart", item_id=item.id, qty=2)
    keys = [a["key"] for a in card["actions"]]
    assert keys == ["shop_checkout", "shop_clear"] and all(k in eng.action_handlers for k in keys)
    assert len(card["rows"]) <= 4
    assert "total" in eng.system_extra[0].lower()
    run(eng.action_handlers["shop_clear"]({}))
    assert eng.shop.cart is None and "cart empty" in eng.system_extra[0]
    assert ("card", None) in eng.events


def test_checkout_hands_snapshot_to_payment(loaded):
    pay = FakePayment()
    e = FakeEngine(payment=pay)
    e.shop.catalog = loaded
    first_coffee(e)
    call(e, "shop_open_place", place_id=e.shop.results[0].id)
    item = next(i for i in e.shop.menu.items if i.available)
    call(e, "shop_add_to_cart", item_id=item.id, qty=2)
    res, _, _ = call(e, "shop_request_checkout")
    assert res["status"] == "checkout_started" and len(pay.carts) == 1
    snap = pay.carts[0]
    assert set(snap) >= {"lines", "total", "currency", "place"}
    assert set(snap["lines"][0]) == {"item_id", "name", "qty", "unit_price", "options"}
    assert snap["place"]["id"] == e.shop.cart_place.id
    assert snap["total_cents"] == e.shop.totals()["total"]
    assert abs(snap["subtotal"] + snap["tax"] - snap["total"]) < 0.006


def test_checkout_without_payment_toasts(eng):
    first_coffee(eng)
    call(eng, "shop_open_place", place_id=eng.shop.results[0].id)
    call(eng, "shop_add_to_cart", item_id=next(i for i in eng.shop.menu.items if i.available).id)
    res, card, _ = call(eng, "shop_request_checkout")
    assert res["status"] == "payment_unavailable" and card
    assert any(k == "toast" for k, _ in eng.events)


def test_find_has_no_minutes_on_catalogue_results(eng):
    res, _ = first_coffee(eng)
    assert all("minutes" not in r for r in res["results"])


def test_cart_photo_uses_the_open_menu_when_the_place_is_not_in_the_catalogue():
    item = SimpleNamespace(id="latte")

    def missing(_mid):
        raise KeyError(_mid)

    shop = SimpleNamespace(
        menu=SimpleNamespace(merchant_id="urn:mbx", item=lambda i: item if i == "latte" else None),
        cart=SimpleNamespace(merchant_id="urn:mbx"),
        catalog=SimpleNamespace(menu=missing),
        photos=SimpleNamespace(item=lambda _item: "photo"),
    )
    card = commerce.Commerce(SimpleNamespace(shop=shop))
    assert card._photo_for(SimpleNamespace(item_id="latte")) == "photo"
    shop.menu = None
    assert card._photo_for(SimpleNamespace(item_id="latte")) == ""
