"""Generated per-place menus. The same place id always yields the same subset and prices.

Indian coordinates use INR at Indian price points. Every other place uses USD. Prices move at most
8 percent from the category base, and the kept items are 60 to 80 percent of that base. The result
is cached in SQLite so a place does not change menu between sessions.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sqlite3
import threading
from pathlib import Path

from driveos.domain.models import Menu, MenuItem
from driveos.domain.money import Money

# (name, section, inr paise, usd cents)
_ROWS: dict[str, list[tuple[str, str, int, int]]] = {
    "coffee": [
        ("Filter coffee", "Coffee", 4000, 350),
        ("Masala chai", "Coffee", 3000, 325),
        ("Cappuccino", "Coffee", 12000, 495),
        ("Cold coffee", "Coffee", 9000, 450),
        ("Espresso", "Coffee", 6000, 325),
        ("Iced latte", "Coffee", 14000, 525),
        ("Bun butter jam", "Snacks", 2500, 375),
        ("Veg puff", "Snacks", 3500, 395),
        ("Brownie", "Snacks", 7000, 425),
        ("Banana cake", "Snacks", 5000, 395),
    ],
    "restaurant": [
        ("Masala dosa", "Tiffin", 8000, 1295),
        ("Idli", "Tiffin", 4000, 895),
        ("Meals", "Meals", 15000, 1495),
        ("Chicken biryani", "Meals", 22000, 1695),
        ("Parotta", "Breads", 6000, 495),
        ("Chicken curry", "Curry", 18000, 1595),
        ("Paneer butter masala", "Curry", 19000, 1495),
        ("Samosa", "Snacks", 3000, 595),
        ("Payasam", "Sweets", 5000, 695),
        ("Lime soda", "Drinks", 4000, 350),
    ],
    "shopping": [
        ("Food court meal", "Food court", 25000, 1495),
        ("Pizza slice", "Food court", 12000, 450),
        ("Filter coffee", "Food court", 5000, 395),
        ("Ice cream", "Food court", 8000, 495),
        ("Movie ticket", "Tickets", 20000, 1600),
        ("Popcorn", "Tickets", 15000, 800),
        ("Cotton tee", "Stores", 49900, 1995),
        ("Phone charger", "Stores", 39900, 1495),
        ("Water bottle", "Stores", 2000, 200),
        ("Gift wrap", "Stores", 3000, 350),
    ],
    "fuel": [
        ("Petrol 5 L", "Pump", 52500, 1995),
        ("Petrol 10 L", "Pump", 105000, 3890),
        ("Diesel 5 L", "Pump", 47000, 1895),
        ("Diesel 10 L", "Pump", 94000, 3690),
        ("Premium petrol 5 L", "Pump", 57500, 2295),
        ("Windscreen wash", "Shop", 15000, 495),
        ("Engine oil top-up", "Shop", 45000, 1295),
        ("Air freshener", "Shop", 9900, 395),
        ("Bottled water", "Shop", 2000, 200),
        ("Car wash", "Shop", 20000, 1200),
    ],
    "ev": [
        ("Charge 15 min", "Charger", 7500, 600),
        ("Charge 30 min", "Charger", 14000, 1100),
        ("Charge 60 min", "Charger", 26000, 2000),
        ("Fast charge 20 min", "Charger", 18000, 1500),
        ("Stall hold", "Charger", 5000, 400),
        ("Bottled water", "Shop", 2000, 200),
        ("Snack pack", "Shop", 6000, 350),
        ("Coffee", "Shop", 8000, 395),
        ("Cable check", "Shop", 1500, 100),
        ("Receipt copy", "Shop", 500, 50),
    ],
    "pharmacy": [
        ("Paracetamol", "Medicine", 2500, 495),
        ("ORS", "Medicine", 2000, 650),
        ("Antacid", "Medicine", 4500, 795),
        ("Cough syrup", "Medicine", 9500, 995),
        ("Bandage", "Care", 3000, 450),
        ("Antiseptic", "Care", 5500, 695),
        ("Sanitizer", "Care", 7500, 395),
        ("Sunscreen", "Care", 22000, 1295),
        ("Vitamin C", "Care", 15000, 1095),
        ("Thermometer", "Care", 12000, 895),
    ],
    "store": [
        ("Milk 1 L", "Grocery", 6000, 189),
        ("Bread", "Grocery", 4500, 349),
        ("Eggs 6", "Grocery", 4800, 399),
        ("Rice 1 kg", "Grocery", 7000, 249),
        ("Bananas", "Grocery", 4000, 199),
        ("Yogurt", "Grocery", 3500, 149),
        ("Chips", "Snacks", 2000, 299),
        ("Biscuits", "Snacks", 3000, 349),
        ("Bottled water", "Drinks", 2000, 150),
        ("Soap", "Household", 4000, 249),
    ],
}

_ALIAS = {
    "cafe": "coffee", "food": "restaurant", "mall": "shopping", "gas": "fuel",
    "charging": "ev", "grocery": "store", "bakery": "coffee", "bar": "restaurant",
    "sandwich": "restaurant",
}


def in_india(lat: float, lon: float) -> bool:
    return 8.0 <= lat <= 37.1 and 68.1 <= lon <= 97.4


def currency_for(lat: float, lon: float) -> str:
    return "INR" if in_india(lat, lon) else "USD"


class CatalogGen:
    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path) if db_path else _default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("CREATE TABLE IF NOT EXISTS menus (place_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self._db.commit()

    def base_items(self, category: str, currency: str) -> list[dict]:
        key = _ALIAS.get(category, category)
        rows = _ROWS.get(key) or _ROWS["restaurant"]
        cur = "INR" if currency.upper() == "INR" else "USD"
        return [{"name": name, "section": section, "price": inr if cur == "INR" else usd}
                for name, section, inr, usd in rows]

    async def amenu_for(self, place) -> Menu:
        return await asyncio.to_thread(self.menu_for, place)

    def menu_for(self, place) -> Menu:
        cached = self._read(place.id)
        if cached is not None:
            return cached
        currency = currency_for(place.lat, place.lon)
        base = self.base_items(place.category or "restaurant", currency)
        items = []
        sections: list[str] = []
        for row in _subset(place.id, base):
            if row["section"] not in sections:
                sections.append(row["section"])
            slug = row["name"].lower().replace(" ", "-")
            items.append(MenuItem(
                id=f"{place.id}:{slug}", name=row["name"], category=row["section"],
                base_price=Money(amount=_jitter(place.id, row["name"], row["price"]), currency=currency),
                available=True))
        menu = Menu(merchant_id=place.id, currency=currency, categories=sections, items=items)
        self._write(place.id, menu)
        return menu

    def _read(self, place_id: str) -> Menu | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM menus WHERE place_id = ?", (place_id,)).fetchone()
        if not row:
            return None
        return Menu.model_validate_json(row[0])

    def _write(self, place_id: str, menu: Menu) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO menus (place_id, body) VALUES (?, ?)",
                             (place_id, menu.model_dump_json()))
            self._db.commit()


def _default_db() -> Path:
    override = os.environ.get("NOVA_CABIN_DATA")
    root = Path(override) if override else Path.home() / ".local" / "share" / "nova-cabin"
    root.mkdir(parents=True, exist_ok=True)
    return root / "places.db"


def _subset(place_id: str, rows: list[dict]) -> list[dict]:
    n = len(rows)
    lo, hi = int(n * 0.60), int(n * 0.80)
    hi = max(hi, lo)
    seed = int(hashlib.sha256(place_id.encode()).hexdigest()[:16], 16)
    keep_n = lo + (seed % (hi - lo + 1))
    order = list(range(n))
    random.Random(seed).shuffle(order)
    return [rows[i] for i in sorted(order[:keep_n])]


def _jitter(place_id: str, name: str, base: int) -> int:
    cap = max(1, round(base * 0.08))
    digest = hashlib.sha256(f"{place_id}\n{name}".encode()).hexdigest()
    h = int(digest[:8], 16)
    pct = (h % 17) - 8
    if pct == 0:
        pct = 1 if h & 1 else -1
    delta = round(base * pct / 100.0)
    if abs(delta) > cap:
        delta = cap if delta > 0 else -cap
    if delta == 0:
        delta = 1 if h & 1 else -1
    return max(1, base + delta)
