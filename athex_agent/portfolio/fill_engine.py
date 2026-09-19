"""Fill pending orders at the target session's opening auction, under the no-lookahead guard.

An order may only fill if it was decided (and, when known, committed to git) strictly before the
opening auction of its target session. That is checked here on every order, every time, and a
violation raises LookaheadGuard: it is a bug in the pipeline, never something to skip.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from athex_agent.config.models import FeeProfile, SlippageConfig
from athex_agent.data import calendar as cal
from athex_agent.data.prices import PriceStore
from athex_agent.portfolio.accounting import BookState, InsufficientCash
from athex_agent.portfolio.fees import compute_fees
from athex_agent.portfolio.orders import Fill, Order, make_fill
from athex_agent.portfolio.slippage import OrderRefused, fill_price

MAX_PENDING_SESSIONS = (
    3  # cancel an order whose target open never appears within this many sessions
)


class LookaheadGuard(RuntimeError):
    pass


@dataclass
class FillOutcome:
    order: Order
    status: Literal["filled", "pending", "cancelled"]
    fill: Fill | None = None
    reason: str = ""


def check_no_lookahead(order: Order, committed_ts: datetime | None) -> datetime:
    open_ts = cal.session_open_utc(order.target_fill_date)
    if order.decision_ts >= open_ts:
        raise LookaheadGuard(
            f"order {order.order_id} decided at {order.decision_ts.isoformat()} but the target "
            f"session opened at {open_ts.isoformat()}"
        )
    if committed_ts is not None and committed_ts >= open_ts:
        raise LookaheadGuard(
            f"order {order.order_id} committed at {committed_ts.isoformat()}, after the target "
            f"session opened at {open_ts.isoformat()}"
        )
    return open_ts


class FillEngine:
    def __init__(self, store: PriceStore, slippage: SlippageConfig) -> None:
        self.store = store
        self.slippage = slippage

    def process(
        self,
        book: BookState,
        fee_profile: FeeProfile,
        adv_by_ticker: Mapping[str, float],
        now_utc: datetime,
        committed_ts: Mapping[str, datetime] | None = None,
        today: date | None = None,
    ) -> list[FillOutcome]:
        outcomes: list[FillOutcome] = []
        today = today or cal.last_completed_session(now_utc)
        for order in list(book.pending_orders):
            open_ts = check_no_lookahead(order, (committed_ts or {}).get(order.order_id))
            if now_utc < open_ts:
                outcomes.append(FillOutcome(order, "pending", reason="session not open yet"))
                continue
            open_price = self.store.session_open(order.ticker, order.target_fill_date)
            if open_price is None:
                late = cal.trading_days_between(order.target_fill_date, today)
                if late >= MAX_PENDING_SESSIONS:
                    book.cancel_pending(order.order_id, "no opening price within window", today)
                    book.log_divergence(
                        today,
                        order.ticker,
                        "ORDER_STALE_CANCELLED",
                        f"no open for {order.target_fill_date} after {late} sessions",
                        order.shares,
                        0,
                    )
                    outcomes.append(FillOutcome(order, "cancelled", reason="stale"))
                else:
                    outcomes.append(
                        FillOutcome(order, "pending", reason="open price not available")
                    )
                continue
            outcomes.append(
                self._fill(
                    book,
                    order,
                    open_price,
                    fee_profile,
                    adv_by_ticker.get(order.ticker, float("nan")),
                    now_utc,
                )
            )
        return outcomes

    def _fill(
        self,
        book: BookState,
        order: Order,
        open_price: float,
        profile: FeeProfile,
        adv: float,
        now_utc: datetime,
    ) -> FillOutcome:
        value = order.shares * open_price
        try:
            px, frac = fill_price(self.slippage, order.side, open_price, value, adv)
        except OrderRefused as exc:
            book.cancel_pending(order.order_id, f"liquidity: {exc}", order.target_fill_date)
            book.log_divergence(
                order.target_fill_date, order.ticker, "LIQUIDITY_REFUSED", str(exc), order.shares, 0
            )
            return FillOutcome(order, "cancelled", reason=str(exc))
        shares = order.shares
        if order.side == "BUY":
            shares = self._affordable(book.cash_eur, px, profile, shares)
            if shares <= 0:
                book.cancel_pending(
                    order.order_id, "insufficient cash at the open", order.target_fill_date
                )
                book.log_divergence(
                    order.target_fill_date,
                    order.ticker,
                    "INSUFFICIENT_CASH",
                    f"cash {book.cash_eur:.2f} cannot buy 1 share at {px:.4f}",
                    order.shares,
                    0,
                )
                return FillOutcome(order, "cancelled", reason="insufficient cash")
            if shares < order.shares:
                book.log_divergence(
                    order.target_fill_date,
                    order.ticker,
                    "ORDER_SHRUNK_CASH",
                    f"open {open_price:.4f} above reference {order.reference_price:.4f}",
                    order.shares,
                    shares,
                )
        gross = round(shares * px, 2)
        fees = compute_fees(profile, order.side, gross, shares)
        fill = make_fill(
            order,
            order.target_fill_date,
            shares=shares,
            open_price=open_price,
            fill_price=px,
            slippage_pct=frac,
            fees=fees,
            fill_ts=now_utc,
        )
        try:
            book.apply_fill(fill)
        except InsufficientCash as exc:  # defensive: sizing above should prevent this
            book.cancel_pending(order.order_id, str(exc), order.target_fill_date)
            return FillOutcome(order, "cancelled", reason=str(exc))
        book.remove_pending(order.order_id)
        return FillOutcome(order, "filled", fill=fill)

    @staticmethod
    def _affordable(cash: float, px: float, profile: FeeProfile, shares: int) -> int:
        while shares > 0:
            gross = round(shares * px, 2)
            if gross + compute_fees(profile, "BUY", gross, shares).total <= cash + 1e-9:
                return shares
            shares -= 1
        return 0
