"""Live Mapbox places and generated per-place menus. HTTP is a fixture client; no network."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin.services.catalog_gen import CatalogGen
from cabin.services.places import Place, PlacesOffline, PlacesService

FIX = Path(__file__).resolve().parent / "fixtures" / "mapbox"
NEAR = (10.7905, 78.7047)


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Resp:
    def __init__(self, body: dict, status: int = 200):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FixtureHttp:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method, url, params=None, **_):
        params = dict(params or {})
        self.calls.append((url, params))
        if "/isochrone/" in url:
            return Resp(load("isochrone_10.json"))
        if "/category/shopping_mall" in url:
            return Resp(load("category_within.json"))
        if "/category/coffee" in url:
            return Resp(load("category_coffee.json"))
        if "/category/" in url:
            return Resp({"type": "FeatureCollection", "features": []})
        if "/directions-matrix/" in url:
            body = load("matrix_within.json") if "79.5000" in url or "79.5" in url else load("matrix_coffee.json")
            return Resp(body)
        raise AssertionError(f"unexpected {method} {url}")


def place(pid: str, *, lat: float, lon: float, category: str = "coffee") -> Place:
    return Place(pid, pid, category, lat, lon, "", 0.0, 0.0, 4.5, "", "1970-01-01T00:00:00Z")


def test_menu_is_deterministic_and_differs_between_places(tmp_path):
    gen = CatalogGen(tmp_path / "places.db")
    anna = place("cafe-anna", lat=10.80, lon=78.70)
    bharath = place("cafe-bharath", lat=10.82, lon=78.72)
    first = gen.menu_for(anna)
    second = gen.menu_for(anna)
    assert first.currency == "INR"
    assert [(i.id, i.name, i.base_price.amount) for i in first.items] == [
        (i.id, i.name, i.base_price.amount) for i in second.items]
    other = gen.menu_for(bharath)
    names_a = [i.name for i in first.items]
    names_b = [i.name for i in other.items]
    assert names_a != names_b
    shared = set(names_a) & set(names_b)
    prices_a = {i.name: i.base_price.amount for i in first.items}
    prices_b = {i.name: i.base_price.amount for i in other.items}
    assert shared and any(prices_a[n] != prices_b[n] for n in shared)
    n = len(gen.base_items("coffee", "INR"))
    assert int(n * 0.60) <= len(first.items) <= int(n * 0.80)
    bases = {i["name"]: i["price"] for i in gen.base_items("coffee", "INR")}
    for item in first.items:
        base = bases[item.name]
        assert abs(item.base_price.amount - base) <= max(1, round(base * 0.08))
    us = gen.menu_for(place("cafe-us", lat=37.4, lon=-122.1))
    assert us.currency == "USD"


def run(coro):
    return asyncio.run(coro)


def test_search_ranks_by_drive_time(tmp_path):
    http = FixtureHttp()
    svc = PlacesService(http, "mbx-test", tmp_path / "places.db")
    found = run(svc.search("coffee", near=NEAR, limit=6))
    assert [p.name for p in found] == ["Fast Far Cafe", "Slow Near Cafe"]
    assert found[0].drive_s == 180
    assert found[1].drive_s == 900
    assert found[0].drive_s < found[1].drive_s
    assert len({p.cover_uri for p in found}) == len(found)
    assert all(p.cover_uri.startswith("file://") for p in found)
    urls = [u for u, _ in http.calls]
    assert any("/search/searchbox/v1/category/coffee" in u for u in urls)
    assert any("/directions-matrix/" in u for u in urls)
    assert not any("/isochrone/" in u for u in urls)
    assert all(params.get("access_token") == "mbx-test" for _, params in http.calls)


def test_within_minutes_uses_isochrone(tmp_path):
    http = FixtureHttp()
    svc = PlacesService(http, "mbx-test", tmp_path / "places.db")
    found = run(svc.search("shopping", near=NEAR, within_min=10, limit=6))
    assert [p.name for p in found] == ["Inside Mall"]
    iso = [(u, p) for u, p in http.calls if "/isochrone/" in u]
    assert iso and iso[0][1].get("contours_minutes") == "10"
    assert all("Outside Mall" != p.name for p in found)


class _Tools:
    def __init__(self):
        self.methods, self.schemas = {}, []

    def extend(self, schemas, methods, captions=None):
        self.schemas += schemas
        self.methods.update(methods)

    def dist(self, m):
        return f"{m / 1000:.1f} km"

    async def run(self, name, args):
        return await self.methods[name](**args)


class _Engine:
    def __init__(self, places=None, db=None):
        self.tools = _Tools()
        self.vehicle = SimpleNamespace(pos=NEAR, lat=NEAR[0], lon=NEAR[1])
        self.system_extra = []
        self.action_handlers = {}
        self.intercepts = []
        self.events = []
        self.places = places
        self.places_db = db
        from cabin import commerce
        commerce.install(self)

    def emit(self, k, v):
        self.events.append((k, v))


class _Live:
    def __init__(self, places):
        self.places = places
        self.kwargs = {}

    async def search(self, category, *, near, along=None, within_min=None, limit=6):
        self.kwargs = {"category": category, "near": near, "along": along,
                       "within_min": within_min, "limit": limit}
        return self.places


class _Down:
    async def search(self, *a, **k):
        raise PlacesOffline("network")


@pytest.fixture(scope="module")
def catalog():
    e = _Engine()
    run(e.shop.load())
    return e.shop.catalog


def test_live_search_badges_demo_menu_and_offline_uses_catalogue(catalog, tmp_path):
    anna = Place("poi-anna", "Anna Cafe", "coffee", 10.80, 78.71, "Trichy", 400, 180, 4.6,
                 "file:///tmp/anna.jpg", "2026-11-11T00:00:00Z")
    live = _Live([anna])
    eng = _Engine(live, tmp_path / "places.db")
    eng.shop.catalog = catalog
    res, _card, _ = run(eng.tools.run("shop_find_places", {"query": "shopping centre within 10 minutes"}))
    assert live.kwargs["category"] == "shopping"
    assert live.kwargs["within_min"] == 10
    assert live.kwargs["limit"] == 6
    assert res["results"][0]["name"] == "Anna Cafe"
    opened, menu_card, _ = run(eng.tools.run("shop_open_place", {"place_id": "poi-anna"}))
    assert menu_card["badge"] == "Demo menu"
    assert "menus are demo data" in opened["note"]
    assert opened["menu"]
    assert "₹" in opened["menu"][0]["items"][0]["price"]

    down = _Engine(_Down(), tmp_path / "other.db")
    down.shop.catalog = catalog
    cat_res, cat_card, _ = run(down.tools.run("shop_find_places", {"category": "coffee"}))
    assert cat_res["results"]
    _opened, cat_menu, _ = run(down.tools.run("shop_open_place", {"place_id": cat_res["results"][0]["place_id"]}))
    assert "badge" not in cat_menu
    assert cat_card["kind"] == "shop_places"


def test_matrix_time_over_budget_is_dropped_even_when_close_in_a_straight_line(tmp_path):
    class Http(FixtureHttp):
        async def request(self, method, url, params=None, **kw):
            if "/directions-matrix/" in url:
                return Resp({"code": "Ok", "durations": [[0, 900, 3600]], "distances": [[0, 1500, 90000]]})
            return await super().request(method, url, params=params, **kw)

    svc = PlacesService(Http(), "mbx-test", tmp_path / "places.db")
    found = run(svc.search("shopping", near=NEAR, within_min=10, limit=6))
    assert found == []


def test_straight_line_crawl_only_when_no_matrix_time(tmp_path):
    class Http(FixtureHttp):
        async def request(self, method, url, params=None, **kw):
            if "/directions-matrix/" in url:
                return Resp({"code": "Ok", "durations": [[0, None, None]], "distances": [[0, None, None]]})
            return await super().request(method, url, params=params, **kw)

    svc = PlacesService(Http(), "mbx-test", tmp_path / "places.db")
    found = run(svc.search("shopping", near=NEAR, within_min=10, limit=6))
    assert [p.name for p in found] == ["Inside Mall"]


def test_live_results_carry_minutes(catalog, tmp_path):
    anna = Place("poi-anna", "Anna Cafe", "coffee", 10.80, 78.71, "Trichy", 400, 540, 4.6,
                 "file:///tmp/anna.jpg", "2026-11-11T00:00:00Z")
    eng = _Engine(_Live([anna]), tmp_path / "places.db")
    eng.shop.catalog = catalog
    res, _card, _ = run(eng.tools.run("shop_find_places", {"category": "coffee"}))
    assert res["results"][0]["minutes"] == 9


def test_offline_catalogue_is_limited_to_radius(catalog, tmp_path):
    from cabin.config import Settings
    far = _Engine(_Down(), tmp_path / "far.db")
    far.shop.catalog = catalog
    far.vehicle.pos = (10.7905, 78.7047)
    far.cfg = Settings({"PLACES_RADIUS_KM": "0.001"})
    res, card, _ = run(far.tools.run("shop_find_places", {"category": "coffee"}))
    assert res["results"] == [] and "nothing nearby" in res["note"] and card is None
    wide = _Engine(_Down(), tmp_path / "wide.db")
    wide.shop.catalog = catalog
    wide.cfg = Settings({"PLACES_RADIUS_KM": "15"})
    res, _card, _ = run(wide.tools.run("shop_find_places", {"category": "coffee"}))
    assert res["results"]
    assert all("minutes" not in r for r in res["results"])
