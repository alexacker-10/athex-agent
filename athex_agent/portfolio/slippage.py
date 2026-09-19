"""Slippage: half-spread by liquidity tier plus a square-root impact term, with a hard ADV cap."""

from __future__ import annotations

import math

from athex_agent.config.models import SlippageConfig
from athex_agent.portfolio.money import round_price
from athex_agent.portfolio.types import Side


class OrderRefused(ValueError):
    """The order cannot be executed realistically (no liquidity data or too large for the ADV)."""


def slippage_fraction(cfg: SlippageConfig, order_value_eur: float, adv_eur: float) -> float:
    if order_value_eur < 0:
        raise ValueError("order_value_eur must be >= 0")
    if adv_eur is None or not adv_eur > 0 or math.isnan(adv_eur):
        raise OrderRefused("no average-daily-volume data for this ticker")
    participation = order_value_eur / adv_eur
    if participation > cfg.max_adv_fraction:
        raise OrderRefused(
            f"order is {participation:.2%} of 20-day ADV, above the {cfg.max_adv_fraction:.2%} cap"
        )
    tier = next(t for t in cfg.sorted_tiers() if adv_eur >= t.adv_at_least_eur)
    return tier.half_spread_pct + cfg.impact_coeff * math.sqrt(participation)


def fill_price(
    cfg: SlippageConfig, side: Side, reference_price: float, order_value_eur: float, adv_eur: float
) -> tuple[float, float]:
    """Adverse fill price for `side` at `reference_price` (the opening auction print)."""
    frac = slippage_fraction(cfg, order_value_eur, adv_eur)
    sign = 1.0 if side == "BUY" else -1.0
    return round_price(reference_price * (1.0 + sign * frac)), frac
