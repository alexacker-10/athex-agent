"""Universe membership by rule, evaluated on an as-of price view (never on future data)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from athex_agent.config.models import UniverseConfig
from athex_agent.data.prices import AsOfPrices


@dataclass
class UniverseSnapshot:
    as_of: str
    members: list[str]
    excluded: dict[str, str] = field(default_factory=dict)
    adv_eur: dict[str, float] = field(default_factory=dict)


def select_universe(cfg: UniverseConfig, prices: AsOfPrices) -> UniverseSnapshot:
    snap = UniverseSnapshot(as_of=prices.as_of.isoformat(), members=[])
    for raw in sorted(set(cfg.candidates) | set(cfg.include_always)):
        ticker = cfg.canonical(raw)
        n = prices.history_length(ticker)
        if n == 0:
            snap.excluded[ticker] = "no price data"
            continue
        adv = prices.median_adv_eur(ticker, cfg.adv_window_days)
        snap.adv_eur[ticker] = adv
        if ticker in cfg.include_always:
            snap.members.append(ticker)
            continue
        if n < cfg.min_history_trading_days:
            snap.excluded[ticker] = f"history {n} < {cfg.min_history_trading_days} days"
            continue
        last = prices.last_bar(ticker)
        if last.close < cfg.min_price_eur:
            snap.excluded[ticker] = f"price {last.close:.3f} < {cfg.min_price_eur}"
            continue
        if math.isnan(adv) or adv < cfg.min_adv_eur:
            snap.excluded[ticker] = f"median ADV {adv:,.0f} < {cfg.min_adv_eur:,.0f}"
            continue
        snap.members.append(ticker)
    snap.members = sorted(set(snap.members))
    return snap
