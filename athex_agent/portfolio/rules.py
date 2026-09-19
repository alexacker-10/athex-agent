"""The rules layer: proposal + book + hard limits -> whole-share orders for one book.

Both books of an arm receive the same proposal; everything a book cannot follow is recorded as a
Divergence on that book so the two books can be compared on the dashboard.

Monthly turnover is replacement churn, min(buys, sells) / NAV: one 25% swap per month at the base
limits. Build-up buys and pure de-risking sells are governed by the order cap and hold period.
During the build-up window (first N trading days of a cohort) the order cap, turnover cap and fee
budget are waived so the book can deploy; the stop-loss bypasses the hold period and order cap only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from athex_agent.arms.proposal import Proposal
from athex_agent.config.models import BookConfig, FeeProfile, RiskLimits, SlippageConfig
from athex_agent.data import calendar as cal
from athex_agent.data.prices import AsOfPrices, NoPriceData
from athex_agent.portfolio.accounting import BookState, DivergenceKind
from athex_agent.portfolio.fees import compute_fees
from athex_agent.portfolio.money import round_cents
from athex_agent.portfolio.orders import Order
from athex_agent.portfolio.slippage import OrderRefused, max_order_value, slippage_fraction


@dataclass
class PlanContext:
    decision_date: date
    decision_ts: datetime
    prices: AsOfPrices
    tradable: set[str]
    limits: RiskLimits
    book_cfg: BookConfig
    fee_profile: FeeProfile
    slippage: SlippageConfig

    def adv(self, ticker: str) -> float:
        return self.prices.adv_eur(ticker, 20)


@dataclass
class PlanResult:
    orders: list[Order] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # human-readable, also logged as divergences
    nav: float = 0.0
    buildup: bool = False


class _Ledger:
    """Running monthly counters while a plan is being built."""

    def __init__(self, book: BookState, ctx: PlanContext, nav: float) -> None:
        d = ctx.decision_date
        self.used = book.orders_placed_in_month(d.year, d.month)
        self.buys, self.sells = book.gross_by_side_in_month(d.year, d.month)
        self.fees = book.fees_in_month(d.year, d.month)
        for o in book.pending_in_month(d.year, d.month):
            self.fees += compute_fees(ctx.fee_profile, o.side, o.reference_value, o.shares).total
        self.cash = book.cash_eur
        self.nav = nav
        self.turnover_cap = ctx.limits.max_turnover_pct_month * nav
        self.fee_budget = ctx.book_cfg.fee_budget_pct_month * nav


class RulesLayer:
    def plan(self, proposal: Proposal, book: BookState, ctx: PlanContext) -> PlanResult:
        d = ctx.decision_date
        lim = ctx.limits
        closes = {t: ctx.prices.close(t) for t in book.positions}
        nav = book.nav(closes)
        res = PlanResult(nav=nav, buildup=self._in_buildup(book, d, lim))
        led = _Ledger(book, ctx, nav)
        planned_sells: set[str] = set()
        seq = 0

        def order_id(ticker: str, side: str) -> str:
            nonlocal seq
            seq += 1
            return f"{book.arm_id}:{book.book_id}:{d.isoformat()}:{seq:02d}:{side}:{ticker}"

        def block(ticker: str, kind: DivergenceKind, detail: str, intended: int) -> None:
            book.log_divergence(d, ticker, kind, detail, intended, 0)
            res.blocked.append(f"{ticker} {kind}: {detail}")

        def sell_order(ticker: str, shares: int, reason: str, tags: list[str]) -> Order:
            return Order(
                order_id=order_id(ticker, "SELL"),
                arm_id=book.arm_id,
                book_id=book.book_id,
                ticker=ticker,
                side="SELL",
                shares=shares,
                reference_price=closes[ticker],
                decision_date=d,
                target_fill_date=cal.next_trading_day(d),
                decision_ts=ctx.decision_ts,
                reason=reason,
                tags=tags,
            )

        # 1. forced stop-loss sells: exempt from hold and order cap, count toward turnover/fees
        for ticker, pos in list(book.positions.items()):
            close = closes[ticker]
            if lim.stop_loss_pct < 1.0 and close <= pos.avg_cost * (1.0 - lim.stop_loss_pct):
                reason = f"stop-loss: close {close:.3f} vs cost {pos.avg_cost:.3f}"
                o = sell_order(ticker, pos.shares, reason, ["stop_loss"])
                res.orders.append(o)
                planned_sells.add(ticker)
                est = compute_fees(ctx.fee_profile, "SELL", o.reference_value, o.shares).total
                led.used += 1
                led.sells += o.reference_value
                led.fees += est
                led.cash += o.reference_value - est
                book.log_divergence(d, ticker, "STOP_LOSS_FORCED", reason, pos.shares, pos.shares)

        # 2. proposed sells
        for a in proposal.sells:
            pos = book.positions.get(a.ticker)
            if pos is None or a.ticker in planned_sells:
                continue
            held_days = cal.trading_days_between(pos.last_acquired, d)
            if held_days < lim.min_hold_trading_days:
                block(
                    a.ticker,
                    "HOLD_PERIOD_BLOCK",
                    f"held {held_days} < {lim.min_hold_trading_days} trading days",
                    pos.shares,
                )
                continue
            if not res.buildup and led.used >= lim.max_orders_per_month:
                block(
                    a.ticker,
                    "ORDER_CAP_BLOCK",
                    f"{led.used} orders already this month (cap {lim.max_orders_per_month})",
                    pos.shares,
                )
                continue
            value = round_cents(pos.shares * closes[a.ticker])
            churn = min(led.buys, led.sells + value)
            if not res.buildup and churn > led.turnover_cap + 1e-9:
                block(
                    a.ticker,
                    "TURNOVER_BLOCK",
                    f"turnover min(buys, sells) would reach {churn / nav:.1%} "
                    f"(cap {lim.max_turnover_pct_month:.0%})",
                    pos.shares,
                )
                continue
            est = compute_fees(ctx.fee_profile, "SELL", value, pos.shares).total
            if not res.buildup and led.fees + est > led.fee_budget + 1e-9:
                block(
                    a.ticker,
                    "FEE_BUDGET_BLOCK",
                    f"fees this month {led.fees + est:.2f} > budget {led.fee_budget:.2f}",
                    pos.shares,
                )
                continue
            res.orders.append(sell_order(a.ticker, pos.shares, a.reason, ["proposed"]))
            planned_sells.add(a.ticker)
            led.used += 1
            led.sells += value
            led.fees += est
            led.cash += value - est

        # 3. proposed buys
        n_positions = len(book.positions) - len(planned_sells)
        for a in proposal.buys:
            t = a.ticker
            if t in book.positions and t not in planned_sells:
                continue  # no adding to an existing position
            if t not in ctx.tradable:
                block(t, "NOT_IN_UNIVERSE", "not a universe member today", 0)
                continue
            if n_positions >= lim.max_positions:
                block(t, "POSITION_CAP_BLOCK", f"already {n_positions} positions", 0)
                continue
            if not res.buildup and led.used >= lim.max_orders_per_month:
                block(
                    t,
                    "ORDER_CAP_BLOCK",
                    f"{led.used} orders already this month (cap {lim.max_orders_per_month})",
                    0,
                )
                continue
            try:
                price = ctx.prices.close(t)
            except NoPriceData:
                block(t, "NOT_IN_UNIVERSE", "no price", 0)
                continue
            weight = min(a.weight or 1.0 / lim.target_positions, lim.max_weight)
            target = round_cents(weight * nav)
            intended = math.floor(target / price) if price > 0 else 0
            churn = min(led.buys + target, led.sells)
            if not res.buildup and churn > led.turnover_cap + 1e-9:
                block(
                    t,
                    "TURNOVER_BLOCK",
                    f"turnover min(buys, sells) would reach {churn / nav:.1%} "
                    f"(cap {lim.max_turnover_pct_month:.0%})",
                    intended,
                )
                continue
            adv = ctx.adv(t)
            try:
                frac = slippage_fraction(ctx.slippage, target, adv)
            except OrderRefused as exc:
                if math.isnan(adv) or adv <= 0:
                    block(t, "LIQUIDITY_REFUSED", str(exc), intended)
                    continue
                target = max_order_value(ctx.slippage, adv)
                frac = slippage_fraction(ctx.slippage, target, adv)
            per_share = price * (1.0 + frac)
            affordable = min(target, led.cash)
            shares = self._size(ctx.fee_profile, affordable, per_share)
            value = round_cents(shares * price)
            if shares <= 0 or value < ctx.book_cfg.min_order_eur:
                kind: DivergenceKind = (
                    "ORDER_SKIPPED_MIN_SIZE"
                    if target < ctx.book_cfg.min_order_eur or led.cash >= target
                    else "INSUFFICIENT_CASH"
                )
                block(
                    t,
                    kind,
                    f"target {target:.2f}, cash {led.cash:.2f}, sized {value:.2f} < min order "
                    f"{ctx.book_cfg.min_order_eur:.0f}",
                    intended,
                )
                continue
            buy_fee = compute_fees(ctx.fee_profile, "BUY", value, shares).total
            if buy_fee / value > lim.max_fee_pct_per_order:
                block(
                    t,
                    "FEE_ECONOMICS_EXCLUSION",
                    f"buy fee {buy_fee:.2f} is {buy_fee / value:.1%} of {value:.2f} "
                    f"(cap {lim.max_fee_pct_per_order:.0%}) under {ctx.fee_profile.id}",
                    intended,
                )
                continue
            if not res.buildup and led.fees + buy_fee > led.fee_budget + 1e-9:
                block(
                    t,
                    "FEE_BUDGET_BLOCK",
                    f"fees this month {led.fees + buy_fee:.2f} > budget {led.fee_budget:.2f}",
                    intended,
                )
                continue
            if shares < intended and target > led.cash:
                book.log_divergence(
                    d,
                    t,
                    "ORDER_SHRUNK_CASH",
                    f"target {target:.2f} > cash {led.cash:.2f}",
                    intended,
                    shares,
                )
            res.orders.append(
                Order(
                    order_id=order_id(t, "BUY"),
                    arm_id=book.arm_id,
                    book_id=book.book_id,
                    ticker=t,
                    side="BUY",
                    shares=shares,
                    reference_price=price,
                    decision_date=d,
                    target_fill_date=cal.next_trading_day(d),
                    decision_ts=ctx.decision_ts,
                    reason=a.reason,
                    tags=["proposed"],
                )
            )
            n_positions += 1
            led.used += 1
            led.buys += value
            led.fees += buy_fee
            led.cash = round_cents(led.cash - shares * per_share - buy_fee)
        return res

    # ---- helpers
    @staticmethod
    def _in_buildup(book: BookState, d: date, lim: RiskLimits) -> bool:
        days = cal.trading_days_between(cal.prev_trading_day(book.started_on), d)
        return days <= lim.buildup_trading_days

    @staticmethod
    def _size(profile: FeeProfile, budget: float, per_share_cost: float) -> int:
        """Largest whole-share count whose cost including the buy fee fits in `budget`."""
        if per_share_cost <= 0 or budget <= 0:
            return 0
        shares = math.floor(budget / per_share_cost)
        while shares > 0:
            value = shares * per_share_cost
            fee = compute_fees(profile, "BUY", value, shares).total
            if value + fee <= budget + 1e-9:
                return shares
            shares -= 1
        return 0
