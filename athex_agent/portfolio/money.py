"""Money helpers: broker-style rounding to whole cents (half up), never banker's rounding."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")


def round_cents(value: float) -> float:
    return float(Decimal(repr(float(value))).quantize(_CENT, rounding=ROUND_HALF_UP))


def round_price(value: float, places: int = 4) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))
