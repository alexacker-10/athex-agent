"""Order and fill records. Orders are decided at T's close and can only fill at T+1's open."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, model_validator

from athex_agent.config.models import StrictModel
from athex_agent.portfolio.fees import FeeBreakdown
from athex_agent.portfolio.money import round_cents
from athex_agent.portfolio.types import Side


class Order(StrictModel):
    order_id: str
    arm_id: str
    book_id: str
    ticker: str
    side: Side
    shares: int = Field(gt=0)
    reference_price: float = Field(gt=0)  # last close used for sizing (never the fill price)
    decision_date: date
    target_fill_date: date
    decision_ts: datetime  # UTC, recorded before the order is committed to git
    reason: str = ""
    tags: list[str] = []

    @model_validator(mode="after")
    def _timing(self) -> Order:
        if self.decision_ts.tzinfo is None or self.decision_ts.utcoffset() is None:
            raise ValueError("decision_ts must be timezone-aware (UTC)")
        if self.target_fill_date <= self.decision_date:
            raise ValueError("target_fill_date must be after decision_date")
        return self

    @property
    def reference_value(self) -> float:
        return round_cents(self.shares * self.reference_price)


class Fill(StrictModel):
    order_id: str
    arm_id: str
    book_id: str
    ticker: str
    side: Side
    shares: int = Field(gt=0)
    fill_date: date
    open_price: float = Field(gt=0)  # the session's opening-auction price
    slippage_pct: float = Field(ge=0)
    fill_price: float = Field(gt=0)
    gross_value: float = Field(ge=0)
    fees: FeeBreakdown
    cash_delta: float  # negative for buys (gross + fees), positive for sells (gross - fees)
    fill_ts: datetime

    @property
    def slippage_cost(self) -> float:
        sign = 1.0 if self.side == "BUY" else -1.0
        return round_cents(sign * (self.fill_price - self.open_price) * self.shares)


def make_fill(
    order: Order,
    fill_date: date,
    open_price: float,
    fill_price: float,
    slippage_pct: float,
    fees: FeeBreakdown,
    fill_ts: datetime,
) -> Fill:
    gross = round_cents(order.shares * fill_price)
    delta = -(gross + fees.total) if order.side == "BUY" else gross - fees.total
    return Fill(
        order_id=order.order_id,
        arm_id=order.arm_id,
        book_id=order.book_id,
        ticker=order.ticker,
        side=order.side,
        shares=order.shares,
        fill_date=fill_date,
        open_price=open_price,
        slippage_pct=slippage_pct,
        fill_price=fill_price,
        gross_value=gross,
        fees=fees,
        cash_delta=round_cents(delta),
        fill_ts=fill_ts,
    )
