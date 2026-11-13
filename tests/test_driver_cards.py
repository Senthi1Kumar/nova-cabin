"""Wallet edits used by the driver sheet."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cabin.wallet import Wallet


def test_add_dummy_card_rejects_more_than_4_digits(tmp_path):
    wallet = Wallet(tmp_path)
    with pytest.raises(ValueError):
        wallet.add("Visa", "424242", "12/30")


def test_card_edit_remove_and_default(tmp_path):
    wallet = Wallet(tmp_path)
    visa = wallet.default()
    other = next(c for c in wallet.list() if c.token != visa.token)
    with pytest.raises(ValueError):
        wallet.remove(visa.token)
    wallet.set_default(other.token)
    edited = wallet.edit(visa.token, label="Fuel card", expiry="01/31")
    assert edited.label == "Fuel card"
    assert edited.expiry == "01/31"
    wallet.remove(visa.token)
    assert wallet.get(visa.token) is None
    assert wallet.default().token == other.token


def test_public_rows_carry_no_more_than_the_last_four(tmp_path):
    wallet = Wallet(tmp_path)
    rows = wallet.public()
    assert rows and all(set(r) >= {"token", "brand", "last4", "expiry", "label", "default"} for r in rows)
    assert all(len(r["last4"]) == 4 for r in rows)
