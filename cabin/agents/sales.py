"""Sales specialist: reports and adjusts the cart, reads purchase history and cards. Never approves payment."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .base import Agent, AgentRequest

log = logging.getLogger("agents.sales")

_ADJUST = {"add": "shop_add_to_cart", "remove": "shop_remove", "set_qty": "shop_set_qty", "clear": "shop_clear_cart"}


class SalesAgent(Agent):
    description = ("Sales specialist: what is in the cart and its total, past purchases, which card is active, "
                   "and cart changes (add, remove, quantity, clear). It never pays; checkout needs the driver's approval.")

    def __init__(self, engine, ledger_path: Path | None = None) -> None:
        super().__init__("sales", engine)
        self.ledger_path = ledger_path

    async def execute(self, request: AgentRequest) -> tuple[dict, str]:
        ctx = request.context
        action = str(ctx.get("action") or self._intent(request.request))
        if action in _ADJUST:
            args = {k: v for k, v in (ctx.get("args") or {}).items()}
            res, _card, _nav = await self.engine.tools.run(_ADJUST[action], args)
            if res.get("error"):
                raise RuntimeError(res["error"])
            return {"action": action, "result": res, "cart": self._cart()}, f"Cart updated ({action})"
        if action == "history":
            rows = self.history(int(ctx.get("limit") or 5))
            return {"purchases": rows}, (f"{len(rows)} recent purchases" if rows else "No purchases yet")
        if action == "cards":
            cards = self.cards()
            return {"cards": cards}, ", ".join(c["label"] for c in cards) or "No saved cards"
        cart = self._cart()
        return {"cart": cart}, self._cart_line(cart)

    @staticmethod
    def _intent(text: str) -> str:
        t = text.lower()
        if any(w in t for w in ("history", "bought", "buy", "ordered", "past", "previous", "last order")):
            return "history"
        if "card" in t or "wallet" in t:
            return "cards"
        for key, words in (("clear", ("clear", "empty the cart")), ("remove", ("remove", "delete", "take off"))):
            if any(w in t for w in words):
                return key
        return "status"

    # ── cart ──────────────────────────────────────────────────
    def _cart(self) -> dict:
        shop = getattr(self.engine, "shop", None)
        if shop is None:
            return {"empty": True}
        try:
            snap = shop.snapshot()
        except Exception:                                            # noqa: BLE001  (empty cart)
            return {"empty": True}
        return {"empty": False, "place": snap["place"]["name"], "total": snap["total"], "currency": snap["currency"],
                "lines": [{"name": ln["name"], "qty": ln["qty"], "unit_price": ln["unit_price"]} for ln in snap["lines"]]}

    @staticmethod
    def _cart_line(cart: dict) -> str:
        if cart.get("empty"):
            return "The cart is empty"
        items = ", ".join(f"{ln['qty']}x {ln['name']}" for ln in cart["lines"][:6])
        return f"{items} at {cart['place']}, total {cart['total']:.2f} {cart['currency']}"

    # ── history and cards ─────────────────────────────────────
    def history(self, limit: int = 5) -> list[dict]:
        rows = self._ledger_rows(limit)
        out = []
        for r in rows:
            lines = (r.get("snapshot") or {}).get("lines") or []
            out.append({"when": r["created_at"], "merchant": (r.get("snapshot") or {}).get("place", {}).get("name")
                        or r["merchant_id"], "total": r["total_minor"] / 100, "currency": r["currency"],
                        "state": r["state"], "items": [ln.get("name", "") for ln in lines][:6]})
        return out

    def _ledger_rows(self, limit: int) -> list[dict]:
        pay = getattr(self.engine, "payment", None)
        if self.ledger_path is None and pay is not None:
            try:
                return list(pay.ledger.recent_transactions(limit))
            except Exception as e:                                   # noqa: BLE001
                log.warning("ledger via payment unavailable: %s", type(e).__name__)
        path = self.ledger_path
        if path is None:
            from ..wallet import data_dir
            path = data_dir() / "ledger.db"
        if not Path(path).exists():
            return []
        import json
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) | {"snapshot": json.loads(r["snapshot"])} for r in rows]
        finally:
            db.close()

    @staticmethod
    def _active_token(pay) -> str:
        for source, getter in (("active_card", getattr(pay, "active_card", None)),
                               ("_active_card", getattr(pay, "_active_card", None)),
                               ("wallet.default", pay.wallet.default)):
            if getter is None:
                continue
            try:
                token = getter().token
            except Exception as e:                                   # noqa: BLE001
                log.info("active card source %s failed (%s)", source, type(e).__name__)
                continue
            log.info("active card from %s", source)
            return token
        return ""

    def cards(self) -> list[dict]:
        pay = getattr(self.engine, "payment", None)
        if pay is None:
            return []
        active = self._active_token(pay)
        return [{"label": c.label, "brand": c.brand, "last4": c.last4, "active": c.token == active}
                for c in pay.wallet.cards()]
