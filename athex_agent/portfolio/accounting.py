"""Book accounting: cash, whole-share lots, fills, dividends, splits, periodic fees, divergences.

A BookState is one book of one arm (e.g. arm A, EUR 10,000). It is persisted as JSON in the
arm's state directory and is the only mutable trading state. It never sees prices for dates
after the run's as-of date; that is enforced upstream by the price store.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from athex_agent.portfolio.money import round_cents
from athex_agent.portfolio.orders import Fill, Order
from athex_agent.portfolio.types import Side


class AccountingError(RuntimeError):
    pass


class InsufficientCash(AccountingError):
    pass


class InsufficientShares(AccountingError):
    pass


class MissingPrice(AccountingError):
    pass


class _Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Lot(_Mutable):
    acquired: date
    shares: int = Field(ge=0)
    cost_total: float = Field(ge=0)  # includes buy-side fees

    @property
    def cost_per_share(self) -> float:
        return self.cost_total / self.shares if self.shares else 0.0


class Position(_Mutable):
    ticker: str
    lots: list[Lot] = []

    @property
    def shares(self) -> int:
        return sum(lot.shares for lot in self.lots)

    @property
    def cost_basis(self) -> float:
        return round_cents(sum(lot.cost_total for lot in self.lots))

    @property
    def avg_cost(self) -> float:
        return self.cost_basis / self.shares if self.shares else 0.0

    @property
    def first_acquired(self) -> date:
        return min(lot.acquired for lot in self.lots)

    @property
    def last_acquired(self) -> date:
        return max(lot.acquired for lot in self.lots)


CashEventKind = Literal["DIVIDEND", "ACCOUNT_FEE", "CASH_IN_LIEU"]


class CashEvent(_Mutable):
    kind: CashEventKind
    ticker: str | None = None
    amount_eur: float  # signed
    due: date
    description: str = ""
    applied_on: date | None = None


DivergenceKind = Literal[
    "STOP_LOSS_FORCED",
    "HOLD_PERIOD_BLOCK",
    "ORDER_CAP_BLOCK",
    "POSITION_CAP_BLOCK",
    "NOT_IN_UNIVERSE",
    "ORDER_STALE_CANCELLED",
    "ORDER_SKIPPED_MIN_SIZE",
    "ORDER_SHRUNK_CASH",
    "FEE_ECONOMICS_EXCLUSION",
    "FEE_BUDGET_BLOCK",
    "INSUFFICIENT_CASH",
    "TURNOVER_BLOCK",
    "LIQUIDITY_REFUSED",
]


class Divergence(_Mutable):
    """Where this book could not follow the arm's decision. Surfaced on the dashboard."""

    date: date
    ticker: str
    kind: DivergenceKind
    detail: str
    intended_shares: int = Field(ge=0)
    actual_shares: int = Field(ge=0)


class CancelledOrder(_Mutable):
    order: Order
    reason: str
    on: date


class TradeRecord(_Mutable):
    order_id: str
    ticker: str
    side: Side
    shares: int
    decision_date: date
    fill_date: date
    open_price: float
    fill_price: float
    gross_value: float
    commission: float
    handling: float
    exchange: float
    sales_tax: float
    slippage_cost: float
    cash_delta: float
    cost_basis_sold: float | None = None
    realized_pnl: float | None = None  # after all fees and tax, for sells


class BookState(_Mutable):
    book_id: str
    arm_id: str
    capital_initial_eur: float = Field(gt=0)
    started_on: date
    cash_eur: float
    positions: dict[str, Position] = {}
    pending_orders: list[Order] = []
    cancelled_orders: list[CancelledOrder] = []
    trades: list[TradeRecord] = []
    cash_events: list[CashEvent] = []
    divergences: list[Divergence] = []
    fees_paid_eur: float = 0.0  # commission + handling + exchange
    sales_tax_paid_eur: float = 0.0
    account_fees_paid_eur: float = 0.0
    slippage_cost_eur: float = 0.0
    dividends_received_eur: float = 0.0

    # ---- construction / persistence
    @classmethod
    def new(cls, book_id: str, arm_id: str, capital_eur: float, started_on: date) -> BookState:
        return cls(
            book_id=book_id,
            arm_id=arm_id,
            capital_initial_eur=capital_eur,
            started_on=started_on,
            cash_eur=round_cents(capital_eur),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> BookState:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    # ---- queries
    def shares(self, ticker: str) -> int:
        pos = self.positions.get(ticker)
        return pos.shares if pos else 0

    def market_value(self, prices: Mapping[str, float]) -> float:
        total = 0.0
        for ticker, pos in self.positions.items():
            price = prices.get(ticker)
            if price is None or math.isnan(price):
                raise MissingPrice(f"no price for held ticker {ticker}")
            total += pos.shares * price
        return round_cents(total)

    def nav(self, prices: Mapping[str, float]) -> float:
        return round_cents(self.cash_eur + self.market_value(prices))

    def weights(self, prices: Mapping[str, float]) -> dict[str, float]:
        nav = self.nav(prices)
        return {t: p.shares * prices[t] / nav for t, p in self.positions.items()} if nav else {}

    # ---- mutations
    def apply_fill(self, fill: Fill) -> TradeRecord:
        if fill.book_id != self.book_id or fill.arm_id != self.arm_id:
            raise AccountingError("fill belongs to a different book")
        cost_sold: float | None = None
        realized: float | None = None
        if fill.side == "BUY":
            cost = fill.gross_value + fill.fees.total
            if cost > self.cash_eur + 1e-9:
                raise InsufficientCash(
                    f"{fill.ticker}: need {cost:.2f} but cash is {self.cash_eur:.2f}"
                )
            pos = self.positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
            pos.lots.append(
                Lot(acquired=fill.fill_date, shares=fill.shares, cost_total=round_cents(cost))
            )
            self.cash_eur = round_cents(self.cash_eur - cost)
        else:
            pos = self.positions.get(fill.ticker)
            if pos is None or pos.shares < fill.shares:
                raise InsufficientShares(
                    f"{fill.ticker}: selling {fill.shares} but holding {self.shares(fill.ticker)}"
                )
            cost_sold = self._consume_lots_fifo(pos, fill.shares)
            proceeds = fill.gross_value - fill.fees.total
            self.cash_eur = round_cents(self.cash_eur + proceeds)
            realized = round_cents(proceeds - cost_sold)
            if pos.shares == 0:
                del self.positions[fill.ticker]
        self.fees_paid_eur = round_cents(self.fees_paid_eur + fill.fees.broker_and_exchange)
        self.sales_tax_paid_eur = round_cents(self.sales_tax_paid_eur + fill.fees.sales_tax)
        self.slippage_cost_eur = round_cents(self.slippage_cost_eur + fill.slippage_cost)
        record = TradeRecord(
            order_id=fill.order_id,
            ticker=fill.ticker,
            side=fill.side,
            shares=fill.shares,
            decision_date=fill.decision_date,
            fill_date=fill.fill_date,
            open_price=fill.open_price,
            fill_price=fill.fill_price,
            gross_value=fill.gross_value,
            commission=fill.fees.commission,
            handling=fill.fees.handling,
            exchange=fill.fees.exchange,
            sales_tax=fill.fees.sales_tax,
            slippage_cost=fill.slippage_cost,
            cash_delta=fill.cash_delta,
            cost_basis_sold=cost_sold,
            realized_pnl=realized,
        )
        self.trades.append(record)
        return record

    @staticmethod
    def _consume_lots_fifo(pos: Position, shares: int) -> float:
        remaining = shares
        cost = 0.0
        kept: list[Lot] = []
        for lot in pos.lots:
            if remaining == 0:
                kept.append(lot)
                continue
            take = min(lot.shares, remaining)
            cost += lot.cost_per_share * take
            remaining -= take
            if take < lot.shares:
                lot.cost_total = round_cents(lot.cost_total - lot.cost_per_share * take)
                lot.shares -= take
                kept.append(lot)
        pos.lots = kept
        return round_cents(cost)

    def record_dividend(
        self,
        ticker: str,
        per_share_gross: float,
        ex_date: date,
        pay_date: date,
        withholding_pct: float = 0.05,
    ) -> CashEvent | None:
        """Schedule the net cash dividend for the shares held on the ex-date."""
        held = self.shares(ticker)
        if held == 0 or per_share_gross <= 0:
            return None
        net = round_cents(held * per_share_gross * (1.0 - withholding_pct))
        ev = CashEvent(
            kind="DIVIDEND",
            ticker=ticker,
            amount_eur=net,
            due=pay_date,
            description=(
                f"{held} x {per_share_gross:.4f} gross, ex {ex_date.isoformat()}, "
                f"{withholding_pct:.0%} withholding"
            ),
        )
        self.cash_events.append(ev)
        return ev

    def apply_split(
        self, ticker: str, ratio: float, effective: date, price_after: float
    ) -> tuple[int, int]:
        """Scale holdings by `ratio` (2.0 = 2-for-1, 0.1 = 1-for-10 reverse). Fractional
        shares are paid out as cash in lieu at `price_after`. Cost basis is preserved."""
        if ratio <= 0:
            raise ValueError("split ratio must be positive")
        pos = self.positions.get(ticker)
        if pos is None:
            return (0, 0)
        old = pos.shares
        exact = old * ratio
        new_total = math.floor(exact + 1e-9)
        fractional = exact - new_total
        # scale lots, giving any rounding remainder to the oldest lot
        new_lots: list[Lot] = []
        assigned = 0
        for lot in sorted(pos.lots, key=lambda lo: lo.acquired):
            n = math.floor(lot.shares * ratio + 1e-9)
            new_lots.append(Lot(acquired=lot.acquired, shares=n, cost_total=lot.cost_total))
            assigned += n
        if new_lots and assigned < new_total:
            new_lots[0].shares += new_total - assigned
        pos.lots = [lot for lot in new_lots if lot.shares > 0]
        if fractional > 1e-9:
            cash = round_cents(fractional * price_after)
            ev = CashEvent(
                kind="CASH_IN_LIEU",
                ticker=ticker,
                amount_eur=cash,
                due=effective,
                description=f"{fractional:.4f} fractional share after {ratio}:1 split",
            )
            self.cash_events.append(ev)
        if pos.shares == 0:
            del self.positions[ticker]
        return (old, new_total)

    def schedule_account_fee(self, amount_eur: float, due: date, description: str) -> CashEvent:
        ev = CashEvent(
            kind="ACCOUNT_FEE", amount_eur=-abs(amount_eur), due=due, description=description
        )
        self.cash_events.append(ev)
        return ev

    def apply_due_cash_events(self, as_of: date) -> list[CashEvent]:
        applied: list[CashEvent] = []
        for ev in self.cash_events:
            if ev.applied_on is None and ev.due <= as_of:
                self.cash_eur = round_cents(self.cash_eur + ev.amount_eur)
                if ev.kind == "DIVIDEND":
                    self.dividends_received_eur = round_cents(
                        self.dividends_received_eur + ev.amount_eur
                    )
                elif ev.kind == "ACCOUNT_FEE":
                    self.account_fees_paid_eur = round_cents(
                        self.account_fees_paid_eur - ev.amount_eur
                    )
                ev.applied_on = as_of
                applied.append(ev)
        return applied

    def log_divergence(
        self,
        on: date,
        ticker: str,
        kind: DivergenceKind,
        detail: str,
        intended_shares: int,
        actual_shares: int,
    ) -> Divergence:
        d = Divergence(
            date=on,
            ticker=ticker,
            kind=kind,
            detail=detail,
            intended_shares=intended_shares,
            actual_shares=actual_shares,
        )
        self.divergences.append(d)
        return d

    # ---- order bookkeeping
    def add_pending(self, order: Order) -> None:
        if any(o.order_id == order.order_id for o in self.pending_orders):
            raise AccountingError(f"duplicate order id {order.order_id}")
        self.pending_orders.append(order)

    def remove_pending(self, order_id: str) -> Order:
        for i, o in enumerate(self.pending_orders):
            if o.order_id == order_id:
                return self.pending_orders.pop(i)
        raise AccountingError(f"no pending order {order_id}")

    def cancel_pending(self, order_id: str, reason: str, on: date) -> CancelledOrder:
        c = CancelledOrder(order=self.remove_pending(order_id), reason=reason, on=on)
        self.cancelled_orders.append(c)
        return c

    def trades_in_month(self, year: int, month: int) -> list[TradeRecord]:
        return [
            t
            for t in self.trades
            if t.decision_date.year == year and t.decision_date.month == month
        ]

    def pending_in_month(self, year: int, month: int) -> list[Order]:
        return [
            o
            for o in self.pending_orders
            if o.decision_date.year == year and o.decision_date.month == month
        ]

    def orders_placed_in_month(self, year: int, month: int) -> int:
        return len(self.trades_in_month(year, month)) + len(self.pending_in_month(year, month))

    def gross_by_side_in_month(self, year: int, month: int) -> tuple[float, float]:
        """(buys, sells) gross value this month: filled at fill price, pending at reference."""
        buys = sells = 0.0
        for t in self.trades_in_month(year, month):
            if t.side == "BUY":
                buys += t.gross_value
            else:
                sells += t.gross_value
        for o in self.pending_in_month(year, month):
            if o.side == "BUY":
                buys += o.reference_value
            else:
                sells += o.reference_value
        return round_cents(buys), round_cents(sells)

    def fees_in_month(self, year: int, month: int) -> float:
        """Fees and sales duty already paid on this month's decisions (pending orders excluded)."""
        return round_cents(
            sum(
                t.commission + t.handling + t.exchange + t.sales_tax
                for t in self.trades_in_month(year, month)
            )
        )

    # ---- summary metrics
    def total_costs(self) -> dict[str, float]:
        return {
            "fees": self.fees_paid_eur,
            "sales_tax": self.sales_tax_paid_eur,
            "account_fees": self.account_fees_paid_eur,
            "slippage": self.slippage_cost_eur,
        }
