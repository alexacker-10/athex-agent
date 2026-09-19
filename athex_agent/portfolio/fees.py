"""Fee model: turns a fee profile plus an order into an itemised fee breakdown.

Each component is rounded to cents separately, as brokers itemise them on the contract note.
The broker's minimum applies to its own commission only, not to third-party or handling charges.
"""

from __future__ import annotations

from pydantic import Field

from athex_agent.config.models import CommissionTier, FeeProfile, StrictModel
from athex_agent.portfolio.money import round_cents
from athex_agent.portfolio.types import Side


class FeeBreakdown(StrictModel):
    commission: float = Field(ge=0)
    handling: float = Field(ge=0)
    exchange: float = Field(ge=0)
    sales_tax: float = Field(ge=0)

    @property
    def broker_and_exchange(self) -> float:
        """Everything except the government sales duty."""
        return round_cents(self.commission + self.handling + self.exchange)

    @property
    def total(self) -> float:
        return round_cents(self.commission + self.handling + self.exchange + self.sales_tax)


def tiered_commission(tiers: list[CommissionTier], gross_value: float) -> float:
    """Marginal tiers: NBG example, EUR 7,500 -> 3,000 x 1% + 4,500 x 0.75% = 63.75."""
    total = 0.0
    lower = 0.0
    for tier in tiers:
        upper = tier.up_to if tier.up_to is not None else float("inf")
        if gross_value <= lower:
            break
        span = min(gross_value, upper) - lower
        total += span * tier.pct
        lower = upper
    return total


def compute_fees(profile: FeeProfile, side: Side, gross_value: float, shares: int) -> FeeBreakdown:
    if gross_value < 0:
        raise ValueError("gross_value must be >= 0")
    if shares < 0 or int(shares) != shares:
        raise ValueError("shares must be a non-negative integer")
    commission = (
        profile.commission_fixed_eur
        + tiered_commission(profile.commission_pct_tiers, gross_value)
        + profile.commission_per_share_eur * shares
    )
    commission = max(commission, profile.commission_min_eur)
    exchange = profile.exchange_pct * gross_value + profile.exchange_fixed_eur
    sales_tax = profile.sales_tax_pct * gross_value if side == "SELL" else 0.0
    return FeeBreakdown(
        commission=round_cents(commission),
        handling=round_cents(profile.handling_fixed_eur),
        exchange=round_cents(exchange),
        sales_tax=round_cents(sales_tax),
    )


def quarterly_account_fee(profile: FeeProfile, nav: float) -> float:
    for tier in profile.account_fee_tiers:
        if tier.nav_up_to is None or nav <= tier.nav_up_to:
            return round_cents(tier.per_quarter_eur)
    return 0.0


def round_trip_cost(profile: FeeProfile, gross_value: float, shares: int) -> float:
    """Buy plus sell of the same value; used for fee-drag tables and fee-economics checks."""
    buy = compute_fees(profile, "BUY", gross_value, shares).total
    sell = compute_fees(profile, "SELL", gross_value, shares).total
    return round_cents(buy + sell)
