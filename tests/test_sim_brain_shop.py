"""SimBrain routes shop and payment phrases to the commerce tools (no engine involved)."""
import asyncio
import json

import pytest

from cabin.sim.brain import SimBrain

SHOP = ["shop_find_places", "shop_open_place", "shop_add_to_cart", "shop_set_qty", "shop_remove",
        "shop_view_cart", "shop_clear_cart", "shop_request_checkout", "pay_list_cards"]
PLACES = {"results": [{"place_id": "p1", "name": "Plur Coffee", "category": "coffee", "distance": "0.5 mi", "rating": 4.5},
                      {"place_id": "p2", "name": "Blue Bottle", "category": "coffee", "distance": "0.9 mi", "rating": 4.6}]}
MENU = {"place_id": "p1", "place": "Plur Coffee",
        "menu": [{"section": "Drinks", "items": [{"item_id": "i1", "name": "Latte", "price": "$4.50"},
                                                   {"item_id": "i2", "name": "Cold Brew", "price": "$5.00"}]}]}
CART = {"added": {"name": "Latte", "quantity": 2}, "cart_total": "$12.50",
        "cart": [{"name": "Latte", "qty": 2, "unit": "$4.50", "line_total": "$9.00"}]}


def tools(names=SHOP):
    return [{"type": "function", "function": {"name": n}} for n in names]


def tool_msg(payload):
    return {"role": "tool", "tool_call_id": "c", "content": json.dumps(payload)}


async def plan(text, history=(), names=SHOP):
    brain = SimBrain()
    brain.rng.seed(1)
    msgs = [*history, {"role": "user", "content": text}]
    calls = []
    async for kind, val in brain.stream(msgs, tools(names)):
        if kind == "tool_calls":
            calls = [(c["name"], json.loads(c["arguments"])) for c in val]
    return calls


def run(text, history=(), names=SHOP):
    return asyncio.run(plan(text, history, names))


def test_find_coffee_nearby():
    assert run("Find a coffee shop nearby") == [("shop_find_places", {"category": "coffee"})]


def test_order_latte_searches_when_no_menu():
    assert run("Order a latte") == [("shop_find_places", {"category": "coffee", "query": "latte"})]


def test_menu_opens_first_place():
    assert run("Show me the menu", [tool_msg(PLACES)]) == [("shop_open_place", {"place_id": "p1"})]


def test_open_named_place():
    assert run("Open Blue Bottle", [tool_msg(PLACES)]) == [("shop_open_place", {"place_id": "p2"})]


def test_add_two_lattes_after_menu():
    assert run("Add two lattes", [tool_msg(MENU)]) == [("shop_add_to_cart", {"item_id": "latte", "qty": 2})]


def test_make_it_one_and_remove():
    hist = [tool_msg(MENU), tool_msg(CART)]
    assert run("Make it one", hist) == [("shop_set_qty", {"name": "Latte", "qty": 1})]
    assert run("Remove the latte", hist) == [("shop_remove", {"name": "latte"})]


def test_cart_checkout_and_cards():
    assert run("What's in my cart") == [("shop_view_cart", {})]
    assert run("I'm ready to pay") == [("shop_request_checkout", {})]
    assert run("Which cards do I have") == [("pay_list_cards", {})]


def test_named_area_stays_on_find_places():
    assert run("Find sushi near Union Square") == [("find_places", {"query": "sushi", "near": "Union Square"})]
    assert run("Find food near Union Square")[0][0] == "find_places"


def test_without_commerce_tools_falls_back():
    assert run("Find a coffee shop nearby", names=["find_places"])[0][0] == "find_places"
    assert run("What's in my cart", names=["find_places"])[0][0] == "web_search"


def test_trip_and_climate_unchanged():
    assert run("Plan a trip to Tahoe with charging stops")[0][0] == "plan_trip"
    assert run("Warm up my seat")[0][0] == "set_climate"


def test_spoken_replies():
    b = SimBrain()
    b._ctx = {"card": "Visa ending 4242"}
    assert b._say_shop_find_places(PLACES).startswith("Plur Coffee is closest, half a mile, rated four point five.")
    assert b._say_shop_add_to_cart(CART) == "Added two Latte, your cart is twelve fifty."
    assert b._say_shop_request_checkout({"status": "checkout_started", "total": "$12.50"}) == \
        "Your total is twelve fifty on your Visa ending 4242. Say approve to pay with Voice ID."


@pytest.mark.parametrize("text", ["Hello", "Take me to Tahoe", "What's the weather"])
def test_non_shop_untouched(text):
    assert not any(n.startswith(("shop_", "pay_")) for n, _ in run(text))
