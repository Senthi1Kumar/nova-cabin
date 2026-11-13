"""Encrypted card wallet: tokens, brand, last four and expiry only.

The file holds a Fernet-encrypted JSON list. A full card number never enters this module: ``add`` accepts the
last four digits and refuses anything longer, so a number cannot be stored by mistake.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DATA_ENV = "NOVA_CABIN_DATA"
BRANDS = {"visa": "Visa", "mastercard": "Mastercard", "amex": "Amex"}

SEED_CARDS = (
    {"brand": "Visa", "last4": "4242", "expiry": "12/29", "default": True},
    {"brand": "Mastercard", "last4": "5555", "expiry": "08/28", "default": False},
)


def data_dir() -> Path:
    override = os.environ.get(DATA_ENV)
    path = Path(override) if override else Path.home() / ".local" / "share" / "nova-cabin"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def normalise_brand(name: str) -> str:
    key = re.sub(r"[^a-z]", "", name.lower())
    if key in ("mastercard", "master"):
        return "Mastercard"
    return BRANDS.get(key, name.strip().title())


@dataclass
class SavedCard:
    token: str
    brand: str
    last4: str
    expiry: str
    default: bool = False
    nickname: str = ""

    @property
    def label(self) -> str:
        if self.nickname:
            return self.nickname
        return f"{self.brand} ••{self.last4}"

    @property
    def spoken(self) -> str:
        return f"{self.brand} ending {self.last4}"


class Wallet:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "wallet.enc"
        self.key_path = self.dir / "wallet.key"
        self._fernet = Fernet(self._load_key())
        self._cards: list[SavedCard] = self._load()

    # storage -----------------------------------------------------------------
    def _load_key(self) -> bytes:
        if not self.key_path.exists():
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.key_path.chmod(0o600)
        return self.key_path.read_bytes().strip()

    def _load(self) -> list[SavedCard]:
        if self.path.exists():
            try:
                raw = json.loads(self._fernet.decrypt(self.path.read_bytes()))
                return [SavedCard(**c) for c in raw]
            except (InvalidToken, ValueError, TypeError):
                pass
        cards = [SavedCard(token=self._new_token(), **c) for c in SEED_CARDS]
        self._cards = cards
        self._save()
        return cards

    def _save(self) -> None:
        blob = self._fernet.encrypt(json.dumps([asdict(c) for c in self._cards]).encode())
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)
        self.path.chmod(0o600)

    @staticmethod
    def _new_token() -> str:
        return "tok_" + secrets.token_hex(8)

    # queries -----------------------------------------------------------------
    def cards(self) -> list[SavedCard]:
        return list(self._cards)

    def list(self) -> list[SavedCard]:
        return self.cards()

    def public(self) -> list[dict]:
        """What a screen may show: token, brand, last four, expiry, label and the default flag."""
        return [{"token": c.token, "brand": c.brand, "last4": c.last4, "expiry": c.expiry, "label": c.label,
                 "default": c.default} for c in self._cards]

    def default(self) -> SavedCard:
        for c in self._cards:
            if c.default:
                return c
        return self._cards[0]

    def get(self, token: str) -> SavedCard | None:
        return next((c for c in self._cards if c.token == token), None)

    def by_brand(self, brand: str) -> SavedCard | None:
        want = normalise_brand(brand)
        return next((c for c in self._cards if c.brand == want), None)

    def other(self, token: str) -> SavedCard | None:
        """The next card after ``token`` in wallet order."""
        if len(self._cards) < 2:
            return None
        idx = next((i for i, c in enumerate(self._cards) if c.token == token), -1)
        return self._cards[(idx + 1) % len(self._cards)]

    # changes -----------------------------------------------------------------
    def add(self, brand: str, last4: str, expiry: str, *, default: bool = False) -> SavedCard:
        if not re.fullmatch(r"\d{4}", last4):
            raise ValueError("only the last four digits are stored")
        card = SavedCard(self._new_token(), normalise_brand(brand), last4, expiry, False)
        self._cards.append(card)
        if default:
            self.set_default(card.token)
        else:
            self._save()
        return card

    def set_default(self, token: str) -> None:
        if self.get(token) is None:
            raise ValueError("unknown card")
        for c in self._cards:
            c.default = c.token == token
        self._save()

    def edit(self, token: str, *, label: str | None = None, expiry: str | None = None) -> SavedCard:
        card = self.get(token)
        if card is None:
            raise ValueError("unknown card")
        if label is not None:
            clean = " ".join(str(label).split())
            if not clean or len(clean) > 40:
                raise ValueError("label must be 1-40 characters")
            card.nickname = clean
        if expiry is not None:
            if not re.fullmatch(r"\d{2}/\d{2}", str(expiry)):
                raise ValueError("expiry must be MM/YY")
            card.expiry = str(expiry)
        self._save()
        return card

    def remove(self, token: str) -> None:
        card = self.get(token)
        if card is None:
            raise ValueError("unknown card")
        others = [c for c in self._cards if c.token != token]
        if card.default and others:
            raise ValueError("switch the default card before removing it")
        self._cards = others
        if self._cards and not any(c.default for c in self._cards):
            self._cards[0].default = True
        self._save()
