"""Fee model checked against the brokers' own published examples and DESIGN.md tables."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from athex_agent.config.models import CommissionTier, FeeProfile
from athex_agent.portfolio.fees import (
    compute_fees,
    quarterly_account_fee,
    round_trip_cost,
    tiered_commission,
)
from athex_agent.portfolio.money import round_cents


def test_round_cents_is_half_up():
    assert round_cents(0.875) == 0.88
    assert round_cents(2.0625) == 2.06
    assert round_cents(1.005) == 1.01


def test_nbg_marginal_tiers_match_published_examples(profiles):
    tiers = profiles["nbg_securities"].commission_pct_tiers
    assert tiered_commission(tiers, 2500) == pytest.approx(25.00)
    assert tiered_commission(tiers, 7500) == pytest.approx(63.75)
    assert tiered_commission(tiers, 20000) == pytest.approx(130.00)


@pytest.mark.parametrize(
    "profile_id, value, shares, buy_total, sell_total",
    [
        ("degiro", 2500, 100, 4.90, 7.40),
        ("degiro", 250, 20, 4.90, 5.15),
        ("nbg_securities", 2500, 100, 27.06, 29.56),
        ("nbg_securities", 250, 20, 5.66, 5.91),
        ("eurobank_trader", 2500, 100, 11.12, 13.62),
        ("eurobank_trader", 250, 20, 6.74, 6.99),
        ("freedom24_smart", 2500, 208, 6.16, 8.66),
        ("freedom24_smart", 250, 20, 2.40, 2.65),
        ("piraeus_online", 250, 20, 1.54, 1.79),
        ("degiro_etf_core", 10000, 14, 1.00, 1.00),
    ],
)
def test_per_order_totals(profiles, profile_id, value, shares, buy_total, sell_total):
    p = profiles[profile_id]
    assert compute_fees(p, "BUY", value, shares).total == pytest.approx(buy_total)
    assert compute_fees(p, "SELL", value, shares).total == pytest.approx(sell_total)
    assert round_trip_cost(p, value, shares) == pytest.approx(buy_total + sell_total)


def test_breakdown_components_degiro(profiles):
    fees = compute_fees(profiles["degiro"], "SELL", 2500, 100)
    assert (fees.commission, fees.handling, fees.exchange, fees.sales_tax) == (
        3.90,
        1.00,
        0.0,
        2.50,
    )
    assert fees.broker_and_exchange == 4.90
    assert fees.total == 7.40


def test_minimum_applies_to_commission_only(profiles):
    fees = compute_fees(profiles["eurobank_trader"], "BUY", 250, 20)
    assert fees.commission == 6.00  # 0.35% of 250 is 0.875, lifted to the EUR 6 minimum
    assert fees.exchange == 0.74  # third-party costs are not subject to the minimum


def test_freedom24_is_price_sensitive(profiles):
    p = profiles["freedom24_smart"]
    cheap_stock = compute_fees(p, "BUY", 2500, 2500).total  # EUR 1 share price
    dear_stock = compute_fees(p, "BUY", 2500, 50).total  # EUR 50 share price
    assert cheap_stock == pytest.approx(52.00)
    assert dear_stock == pytest.approx(3.00)


def test_sales_tax_only_on_sells(profiles):
    for p in profiles.values():
        assert compute_fees(p, "BUY", 1000, 10).sales_tax == 0.0


def test_piraeus_account_fee_tiers_and_badge(profiles):
    p = profiles["piraeus_online"]
    assert quarterly_account_fee(p, 5000) == 4.00
    assert quarterly_account_fee(p, 10000) == 4.00
    assert quarterly_account_fee(p, 10000.01) == 5.00
    assert quarterly_account_fee(p, 60000) == 6.00
    assert quarterly_account_fee(profiles["degiro"], 60000) == 0.0
    # Dashboard contract: the unverified minimum is a badge, not a code comment.
    assert p.badge == "UNVERIFIED MINIMUM"
    assert any("minimum" in w.lower() for w in p.warnings)


def test_invalid_inputs(profiles):
    with pytest.raises(ValueError):
        compute_fees(profiles["degiro"], "BUY", -1, 1)
    with pytest.raises(ValueError):
        compute_fees(profiles["degiro"], "BUY", 100, -1)


def test_tiers_must_ascend():
    base = dict(id="x", name="x", source="x", as_of="2026-01-01")
    with pytest.raises(ValidationError):
        FeeProfile(
            **base,
            commission_pct_tiers=[
                CommissionTier(up_to=9000, pct=0.01),
                CommissionTier(up_to=3000, pct=0.01),
            ],
        )
    with pytest.raises(ValidationError):
        FeeProfile(
            **base,
            commission_pct_tiers=[
                CommissionTier(up_to=None, pct=0.01),
                CommissionTier(up_to=3000, pct=0.01),
            ],
        )
