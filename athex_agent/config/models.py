"""Pydantic models for every config file under configs/.

All models are strict: unknown keys are errors (a typo must not become a silent default) and
instances are immutable. Arm configs are the unit of experiment design; see DESIGN.md.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- fees


class CommissionTier(StrictModel):
    """One marginal commission tier: `pct` applies to the part of the value up to `up_to`."""

    up_to: float | None = Field(default=None, gt=0)
    pct: float = Field(ge=0, le=0.1)


class AccountFeeTier(StrictModel):
    nav_up_to: float | None = Field(default=None, gt=0)
    per_quarter_eur: float = Field(ge=0)


class FeeProfile(StrictModel):
    """A broker's fee schedule for ATHEX orders. Sources are cited in `source`."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    source: str
    as_of: date
    warnings: list[str] = []
    badge: str | None = None  # rendered verbatim on the dashboard, e.g. "UNVERIFIED MINIMUM"
    commission_fixed_eur: float = Field(default=0.0, ge=0)
    handling_fixed_eur: float = Field(default=0.0, ge=0)
    commission_pct_tiers: list[CommissionTier] = []
    commission_per_share_eur: float = Field(default=0.0, ge=0)
    commission_min_eur: float = Field(default=0.0, ge=0)
    exchange_pct: float = Field(default=0.0, ge=0, le=0.01)
    exchange_fixed_eur: float = Field(default=0.0, ge=0)
    sales_tax_pct: float = Field(default=0.0, ge=0, le=0.01)
    account_fee_tiers: list[AccountFeeTier] = []

    @model_validator(mode="after")
    def _tiers_are_ascending(self) -> FeeProfile:
        _check_ascending([t.up_to for t in self.commission_pct_tiers], "commission_pct_tiers")
        _check_ascending([t.nav_up_to for t in self.account_fee_tiers], "account_fee_tiers")
        return self


def _check_ascending(bounds: list[float | None], name: str) -> None:
    if not bounds:
        return
    if any(b is None for b in bounds[:-1]):
        raise ValueError(f"{name}: only the last tier may be unbounded (null)")
    finite = [b for b in bounds if b is not None]
    if finite != sorted(finite) or len(set(finite)) != len(finite):
        raise ValueError(f"{name}: tier bounds must be strictly ascending")


# --------------------------------------------------------------------------- books & limits


class BookConfig(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    label: str
    capital_eur: float = Field(gt=0)
    fee_profile: str
    min_order_eur: float = Field(ge=0)
    fee_budget_pct_month: float = Field(ge=0, le=0.2)
    primary: bool = False


class BooksConfig(StrictModel):
    books: list[BookConfig]
    recost_profiles: list[str] = []

    @model_validator(mode="after")
    def _one_primary_unique_ids(self) -> BooksConfig:
        ids = [b.id for b in self.books]
        if len(set(ids)) != len(ids):
            raise ValueError("book ids must be unique")
        if sum(b.primary for b in self.books) != 1:
            raise ValueError("exactly one book must be marked primary")
        return self


class RiskLimits(StrictModel):
    id: str
    target_positions: int = Field(ge=1)
    max_positions: int = Field(ge=1)
    max_weight: float = Field(gt=0, le=1)
    min_weight: float = Field(ge=0, le=1)
    min_hold_trading_days: int = Field(ge=0)
    max_orders_per_month: int = Field(ge=0)
    max_turnover_pct_month: float = Field(ge=0, le=1)
    stop_loss_pct: float = Field(ge=0, le=1)
    buildup_trading_days: int = Field(ge=0)
    max_adv_fraction: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _coherent(self) -> RiskLimits:
        if self.target_positions > self.max_positions:
            raise ValueError("target_positions must not exceed max_positions")
        equal_weight = 1.0 / self.target_positions
        if not (self.min_weight <= equal_weight + 1e-9 and equal_weight <= self.max_weight + 1e-9):
            raise ValueError(
                "equal weight 1/target_positions must lie within [min_weight, max_weight]"
            )
        if equal_weight > self.max_turnover_pct_month + 1e-9:
            raise ValueError(
                "one-sided turnover cap is smaller than one equal-weight position: the book could "
                "never swap a holding (with 3 positions the cap must be >= 0.34; with 4, >= 0.25)"
            )
        return self


# --------------------------------------------------------------------------- slippage


class SlippageTier(StrictModel):
    adv_at_least_eur: float = Field(ge=0)
    half_spread_pct: float = Field(ge=0, le=0.05)


class SlippageConfig(StrictModel):
    tiers: list[SlippageTier] = Field(min_length=1)
    impact_coeff: float = Field(ge=0, le=0.1)
    max_adv_fraction: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _has_floor_tier(self) -> SlippageConfig:
        if min(t.adv_at_least_eur for t in self.tiers) != 0:
            raise ValueError("slippage tiers must include a tier with adv_at_least_eur: 0")
        return self

    def sorted_tiers(self) -> list[SlippageTier]:
        return sorted(self.tiers, key=lambda t: t.adv_at_least_eur, reverse=True)


# --------------------------------------------------------------------------- arms


class SourceToggles(StrictModel):
    prices: bool = True
    official: bool = True
    news: bool = True
    macro: bool = True
    rumors: bool = True


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMSettings(StrictModel):
    model: str
    effort: Effort = "medium"
    prompt_variant: str = "base"
    max_output_tokens: int = Field(default=8000, ge=256, le=64000)


RuleKind = Literal["buy_hold", "random", "momentum", "equal_weight"]


class RuleParams(StrictModel):
    kind: RuleKind
    params: dict[str, Any] = {}


class ArmConfig(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    description: str
    kind: Literal["llm", "rule"]
    base_arm: str | None = None
    factor: list[str] = []  # dotted config paths in which this arm differs from base_arm
    llm: LLMSettings | None = None
    rule: RuleParams | None = None
    sources: SourceToggles = SourceToggles()
    universe: str = "base"
    limits: str = "base"
    books: list[str] = ["10k", "1k"]
    fee_profile_override: str | None = None
    seed: int = 0
    cohort_start: date | None = None
    active: bool = True

    @model_validator(mode="after")
    def _kind_matches_settings(self) -> ArmConfig:
        if self.kind == "llm" and (self.llm is None or self.rule is not None):
            raise ValueError("llm arms need `llm` settings and no `rule`")
        if self.kind == "rule" and (self.rule is None or self.llm is not None):
            raise ValueError("rule arms need `rule` settings and no `llm`")
        if self.factor and self.base_arm is None:
            raise ValueError("`factor` requires `base_arm`")
        if not self.books or len(set(self.books)) != len(self.books):
            raise ValueError("books must be a non-empty list of unique ids")
        return self


class UniverseConfig(StrictModel):
    id: str
    description: str
    min_adv_eur: float = Field(ge=0)
    min_price_eur: float = Field(ge=0)
    min_history_trading_days: int = Field(ge=0)
    adv_window_days: int = Field(ge=1)
    include_always: list[str] = []
    candidates: list[str]
    aliases: dict[str, str] = {}

    @model_validator(mode="after")
    def _consistent(self) -> UniverseConfig:
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("candidates must be unique")
        known = set(self.candidates) | set(self.include_always)
        bad = [old for old, new in self.aliases.items() if new not in known]
        if bad:
            raise ValueError(f"alias targets must be candidates: {bad}")
        return self

    def canonical(self, ticker: str) -> str:
        return self.aliases.get(ticker, ticker)


class CohortSchedule(StrictModel):
    arms: list[str] = Field(min_length=1)
    frequency: Literal["monthly"] = "monthly"
    start_on: Literal["first_trading_day"] = "first_trading_day"
    months: int = Field(ge=1)


class ResolvedArm(StrictModel):
    """An arm with every referenced config resolved. This is what gets fingerprinted and frozen."""

    arm: ArmConfig
    limits: RiskLimits
    books: list[BookConfig]
    fee_profiles: dict[str, FeeProfile]
    slippage: SlippageConfig
    universe: UniverseConfig

    def fee_profile_for(self, book: BookConfig) -> FeeProfile:
        return self.fee_profiles[self.arm.fee_profile_override or book.fee_profile]
