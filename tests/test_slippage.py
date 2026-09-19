from __future__ import annotations

import pytest

from athex_agent.config.models import SlippageConfig, SlippageTier
from athex_agent.portfolio.slippage import OrderRefused, fill_price, slippage_fraction


@pytest.fixture(scope="module")
def cfg(repo):
    return repo.slippage()


def test_tier_and_impact(cfg):
    frac = slippage_fraction(cfg, 2500, 10_000_000)
    assert frac == pytest.approx(0.0010 + 0.001 * (2500 / 10_000_000) ** 0.5)
    assert slippage_fraction(cfg, 2500, 500_000) == pytest.approx(0.0060 + 0.001 * 0.005**0.5)
    assert slippage_fraction(cfg, 2500, 250_000) == pytest.approx(0.0100 + 0.001 * 0.01**0.5)


def test_fill_price_is_adverse(cfg):
    buy, frac = fill_price(cfg, "BUY", 10.0, 2500, 10_000_000)
    sell, _ = fill_price(cfg, "SELL", 10.0, 2500, 10_000_000)
    assert buy == pytest.approx(10.0102, abs=1e-4)
    assert sell == pytest.approx(9.9898, abs=1e-4)
    assert buy > 10.0 > sell


def test_refuses_large_or_illiquid_orders(cfg):
    with pytest.raises(OrderRefused):
        slippage_fraction(cfg, 2500, 200_000)  # 1.25% of ADV
    with pytest.raises(OrderRefused):
        slippage_fraction(cfg, 2500, 0)
    with pytest.raises(OrderRefused):
        slippage_fraction(cfg, 2500, float("nan"))
    slippage_fraction(cfg, 2500, 250_000)  # exactly the cap is allowed


def test_tiers_sorted_regardless_of_config_order():
    cfg = SlippageConfig(
        tiers=[
            SlippageTier(adv_at_least_eur=0, half_spread_pct=0.01),
            SlippageTier(adv_at_least_eur=5_000_000, half_spread_pct=0.001),
            SlippageTier(adv_at_least_eur=1_000_000, half_spread_pct=0.0025),
        ],
        impact_coeff=0.0,
        max_adv_fraction=0.01,
    )
    assert slippage_fraction(cfg, 100, 2_000_000) == 0.0025
    assert slippage_fraction(cfg, 100, 6_000_000) == 0.001
    assert slippage_fraction(cfg, 10, 5_000) == 0.01


def test_config_requires_floor_tier():
    with pytest.raises(ValueError):
        SlippageConfig(
            tiers=[SlippageTier(adv_at_least_eur=1000, half_spread_pct=0.01)],
            impact_coeff=0.0,
            max_adv_fraction=0.01,
        )
