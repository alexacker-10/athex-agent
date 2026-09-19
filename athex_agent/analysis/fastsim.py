"""Fast simulator for the historical study.

Same fee model, slippage model, whole-share sizing and hard limits as the live engine (it imports
the same functions), but on price matrices and plain dicts instead of BookState/JSON, so that
hundreds of random books can be run over every rolling window. tests/test_fastsim.py checks that
it reproduces the live engine's NAV on a scripted scenario to the cent.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import numpy as np

from athex_agent.config.models import FeeProfile, RiskLimits, SlippageConfig
from athex_agent.portfolio.fees import compute_fees
from athex_agent.portfolio.money import round_cents, round_price
from athex_agent.portfolio.rules import RulesLayer
from athex_agent.portfolio.slippage import OrderRefused, slippage_fraction

MAX_PENDING_SESSIONS = 3


@dataclass
class WindowData:
    dates: list[date]
    tickers: list[str]
    opens: np.ndarray  # T x N, NaN when the session has no bar
    closes: np.ndarray  # T x N, forward-filled for valuation
    adv: np.ndarray  # T x N, 20-day mean of close*volume as of each day (NaN if unknown)
    dividends: np.ndarray  # T x N gross per share on the ex-date
    splits: np.ndarray  # T x N ratio (1.0 = none)
    scores: np.ndarray | None = None  # T x N momentum score as of each day

    def idx(self, ticker: str) -> int:
        return self.tickers.index(ticker)


@dataclass
class SimParams:
    capital: float
    fee_profile: FeeProfile
    slippage: SlippageConfig
    limits: RiskLimits
    min_order_eur: float
    fee_budget_pct_month: float
    dividend_lag: int = 10
    withholding: float = 0.05


@dataclass
class _Pos:
    shares: int
    cost_total: float
    acquired_t: int

    @property
    def avg_cost(self) -> float:
        return self.cost_total / self.shares if self.shares else 0.0


@dataclass
class _Pending:
    ticker: str
    side: str
    shares: int
    ref_price: float
    decided_t: int
    target_t: int


@dataclass
class _Month:
    used: int = 0
    buys: float = 0.0
    sells: float = 0.0
    fees: float = 0.0


@dataclass
class FastBook:
    params: SimParams
    cash: float
    positions: dict[str, _Pos] = field(default_factory=dict)
    pending: list[_Pending] = field(default_factory=list)
    months: dict[tuple[int, int], _Month] = field(default_factory=dict)
    cash_events: list[tuple[int, float]] = field(default_factory=list)
    fees_paid: float = 0.0
    tax_paid: float = 0.0
    slippage_cost: float = 0.0
    dividends: float = 0.0
    n_trades: int = 0
    n_blocked: int = 0
    nav_series: list[float] = field(default_factory=list)

    def month(self, d: date) -> _Month:
        return self.months.setdefault((d.year, d.month), _Month())

    def nav(self, closes_t: np.ndarray, data: WindowData) -> float:
        mv = sum(p.shares * closes_t[data.idx(t)] for t, p in self.positions.items())
        return round_cents(self.cash + mv)


class Policy(Protocol):
    def actions(
        self, t: int, book: FastBook, data: WindowData, rng: random.Random, buildup: bool
    ) -> tuple[list[str], list[tuple[str, float | None]]]:
        """Return (sells, buys) where buys are (ticker, weight or None)."""
        ...


def _held(book: FastBook) -> set[str]:
    names = set(book.positions)
    for p in book.pending:
        if p.side == "BUY":
            names.add(p.ticker)
        else:
            names.discard(p.ticker)
    return names


def _tradable(t: int, data: WindowData) -> list[str]:
    return [
        tk
        for i, tk in enumerate(data.tickers)
        if not math.isnan(data.opens[t, i])
        and not math.isnan(data.closes[t, i])
        and data.closes[t, i] > 0
    ]


class RandomPolicy:
    def __init__(self, p_swap: float = 1.0 / 21.0) -> None:
        self.p_swap = p_swap

    def actions(self, t, book, data, rng, buildup):
        held = _held(book)
        cands = [tk for tk in _tradable(t, data) if tk not in held]
        if buildup:
            need = max(0, book.params.limits.target_positions - len(held))
            return [], [(tk, None) for tk in rng.sample(cands, min(need, len(cands)))]
        if held and cands and rng.random() < self.p_swap:
            return [rng.choice(sorted(held))], [(rng.choice(cands), None)]
        return [], []


class MomentumPolicy:
    def actions(self, t, book, data, rng, buildup):
        assert data.scores is not None, "momentum needs precomputed scores"
        row = data.scores[t]
        tradable = set(_tradable(t, data))
        ranked = [
            data.tickers[i]
            for i in np.argsort(-row)
            if not math.isnan(row[i]) and data.tickers[i] in tradable
        ]
        held = _held(book)
        target = book.params.limits.target_positions
        if buildup:
            buys = [tk for tk in ranked if tk not in held][: max(0, target - len(held))]
            return [], [(tk, None) for tk in buys]
        first_of_month = t == 0 or data.dates[t].month != data.dates[t - 1].month
        if not first_of_month or not held:
            return [], []
        scores = {tk: row[data.idx(tk)] for tk in ranked}
        scored_held = [tk for tk in held if tk in scores]
        if not scored_held:
            return [], []
        weakest = min(scored_held, key=lambda tk: scores[tk])
        top = set(ranked[:target])
        best_new = next((tk for tk in ranked if tk not in held), None)
        if weakest not in top and best_new is not None and best_new in top:
            return [weakest], [(best_new, None)]
        return [], []


class BuyHoldPolicy:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def actions(self, t, book, data, rng, buildup):
        if self.ticker in _held(book) or self.ticker not in _tradable(t, data):
            return [], []
        return [], [(self.ticker, 1.0)]


class EqualWeightPolicy:
    def actions(self, t, book, data, rng, buildup):
        if not buildup:
            return [], []
        names = _tradable(t, data)
        held = _held(book)
        w = 1.0 / len(names) if names else None
        return [], [(tk, w) for tk in names if tk not in held]


class ScriptedPolicy:
    """For cross-checks: a fixed script {t: (sells, buys)}."""

    def __init__(self, script: dict[int, tuple[list[str], list[tuple[str, float | None]]]]):
        self.script = script

    def actions(self, t, book, data, rng, buildup):
        return self.script.get(t, ([], []))


def simulate(data: WindowData, params: SimParams, policy: Policy, seed: int = 0) -> FastBook:
    rng = random.Random(seed)
    book = FastBook(params=params, cash=round_cents(params.capital))
    lim = params.limits
    T = len(data.dates)
    for t in range(T):
        _fill(book, t, data)
        _corporate_actions(book, t, data)
        _apply_cash_events(book, t)
        buildup = t + 1 <= lim.buildup_trading_days
        sells, buys = policy.actions(t, book, data, rng, buildup)
        _decide(book, t, data, sells, buys, buildup)
        book.nav_series.append(book.nav(data.closes[t], data))
    return book


def _fill(book: FastBook, t: int, data: WindowData) -> None:
    keep: list[_Pending] = []
    for o in book.pending:
        if o.target_t > t:
            keep.append(o)
            continue
        i = data.idx(o.ticker)
        open_px = data.opens[t, i] if o.target_t == t else data.opens[o.target_t, i]
        if math.isnan(open_px) or open_px <= 0:
            if t - o.target_t >= MAX_PENDING_SESSIONS:
                book.month(data.dates[o.decided_t]).used -= 1  # stale: cancelled
                continue
            keep.append(o)
            continue
        adv = data.adv[o.decided_t, i]
        value = o.shares * open_px
        try:
            frac = slippage_fraction(book.params.slippage, value, adv)
        except OrderRefused:
            book.month(data.dates[o.decided_t]).used -= 1  # liquidity refused: cancelled
            continue
        sign = 1.0 if o.side == "BUY" else -1.0
        px = round_price(open_px * (1.0 + sign * frac))
        shares = o.shares
        if o.side == "BUY":
            while shares > 0:
                gross = round_cents(shares * px)
                if (
                    gross + compute_fees(book.params.fee_profile, "BUY", gross, shares).total
                    <= book.cash + 1e-9
                ):
                    break
                shares -= 1
            if shares <= 0:
                book.month(data.dates[o.decided_t]).used -= 1  # unaffordable: cancelled
                continue
        gross = round_cents(shares * px)
        fees = compute_fees(book.params.fee_profile, o.side, gross, shares)
        m = book.month(data.dates[o.decided_t])
        if o.side == "BUY":
            cost = gross + fees.total
            book.cash = round_cents(book.cash - cost)
            pos = book.positions.get(o.ticker)
            if pos is None:
                book.positions[o.ticker] = _Pos(shares, round_cents(cost), o.target_t)
            else:
                pos.shares += shares
                pos.cost_total = round_cents(pos.cost_total + cost)
                pos.acquired_t = o.target_t
            m.buys += gross
        else:
            pos = book.positions[o.ticker]
            take = min(shares, pos.shares)
            cost_sold = round_cents(pos.avg_cost * take)
            pos.shares -= take
            pos.cost_total = round_cents(pos.cost_total - cost_sold)
            if pos.shares == 0:
                del book.positions[o.ticker]
            book.cash = round_cents(book.cash + gross - fees.total)
            m.sells += gross
        m.fees += fees.total
        book.fees_paid = round_cents(book.fees_paid + fees.broker_and_exchange)
        book.tax_paid = round_cents(book.tax_paid + fees.sales_tax)
        book.slippage_cost = round_cents(book.slippage_cost + sign * (px - open_px) * shares)
        book.n_trades += 1
    book.pending = keep


def _corporate_actions(book: FastBook, t: int, data: WindowData) -> None:
    for tk, pos in list(book.positions.items()):
        i = data.idx(tk)
        div = data.dividends[t, i]
        if div > 0:
            net = round_cents(pos.shares * div * (1.0 - book.params.withholding))
            book.cash_events.append((t + book.params.dividend_lag, net))
        ratio = data.splits[t, i]
        if ratio and ratio != 1.0 and ratio > 0:
            exact = pos.shares * ratio
            new = math.floor(exact + 1e-9)
            frac = exact - new
            if frac > 1e-9:
                book.cash_events.append((t, round_cents(frac * data.closes[t, i])))
            pos.shares = new
            if new == 0:
                del book.positions[tk]


def _apply_cash_events(book: FastBook, t: int) -> None:
    keep = []
    for pay_t, amount in book.cash_events:
        if pay_t <= t:
            book.cash = round_cents(book.cash + amount)
            book.dividends = round_cents(book.dividends + amount)
        else:
            keep.append((pay_t, amount))
    book.cash_events = keep


def _decide(
    book: FastBook,
    t: int,
    data: WindowData,
    sells: list[str],
    buys: list[tuple[str, float | None]],
    buildup: bool,
) -> None:
    lim = book.params.limits
    d = data.dates[t]
    closes = data.closes[t]
    nav = book.nav(closes, data)
    m = book.month(d)
    used, mbuys, msells, mfees = m.used, m.buys, m.sells, m.fees
    for o in book.pending:
        ref = round_cents(o.shares * o.ref_price)
        mfees += compute_fees(book.params.fee_profile, o.side, ref, o.shares).total
        if o.side == "BUY":
            mbuys += ref
        else:
            msells += ref
    cap = lim.max_turnover_pct_month * nav
    fee_budget = book.params.fee_budget_pct_month * nav
    cash = book.cash
    planned: set[str] = set()
    target_t = t + 1
    if target_t >= len(data.dates):
        return  # nothing can fill after the window

    def push(ticker: str, side: str, shares: int, ref: float) -> None:
        book.pending.append(_Pending(ticker, side, shares, ref, t, target_t))

    # stop-loss
    for tk, pos in list(book.positions.items()):
        px = closes[data.idx(tk)]
        if lim.stop_loss_pct < 1.0 and px <= pos.avg_cost * (1.0 - lim.stop_loss_pct):
            value = round_cents(pos.shares * px)
            est = compute_fees(book.params.fee_profile, "SELL", value, pos.shares).total
            push(tk, "SELL", pos.shares, px)
            planned.add(tk)
            used += 1
            msells += value
            mfees += est
            cash += value - est
    # sells
    for tk in sells:
        pos = book.positions.get(tk)
        if pos is None or tk in planned:
            continue
        if t - pos.acquired_t < lim.min_hold_trading_days:
            book.n_blocked += 1
            continue
        if not buildup and used >= lim.max_orders_per_month:
            book.n_blocked += 1
            continue
        px = closes[data.idx(tk)]
        value = round_cents(pos.shares * px)
        if not buildup and min(mbuys, msells + value) > cap + 1e-9:
            book.n_blocked += 1
            continue
        est = compute_fees(book.params.fee_profile, "SELL", value, pos.shares).total
        if not buildup and mfees + est > fee_budget + 1e-9:
            book.n_blocked += 1
            continue
        push(tk, "SELL", pos.shares, px)
        planned.add(tk)
        used += 1
        msells += value
        mfees += est
        cash += value - est
    # buys
    n_positions = len(book.positions) - len(planned)
    tradable = set(_tradable(t, data))
    for tk, weight in buys:
        if (tk in book.positions and tk not in planned) or tk not in tradable:
            continue
        if n_positions >= lim.max_positions:
            book.n_blocked += 1
            continue
        if not buildup and used >= lim.max_orders_per_month:
            book.n_blocked += 1
            continue
        i = data.idx(tk)
        price = closes[i]
        w = min(weight or 1.0 / lim.target_positions, lim.max_weight)
        target = round_cents(w * nav)
        if not buildup and min(mbuys + target, msells) > cap + 1e-9:
            book.n_blocked += 1
            continue
        adv = data.adv[t, i]
        try:
            frac = slippage_fraction(book.params.slippage, target, adv)
        except OrderRefused:
            if math.isnan(adv) or adv <= 0:
                book.n_blocked += 1
                continue
            target = round_cents(lim.max_adv_fraction * adv)
            frac = slippage_fraction(book.params.slippage, target, adv)
        per_share = price * (1.0 + frac)
        shares = RulesLayer._size(book.params.fee_profile, min(target, cash), per_share)
        value = round_cents(shares * price)
        if shares <= 0 or value < book.params.min_order_eur:
            book.n_blocked += 1
            continue
        buy_fee = compute_fees(book.params.fee_profile, "BUY", value, shares).total
        if buy_fee / value > lim.max_fee_pct_per_order:
            book.n_blocked += 1
            continue
        if not buildup and mfees + buy_fee > fee_budget + 1e-9:
            book.n_blocked += 1
            continue
        push(tk, "BUY", shares, price)
        n_positions += 1
        used += 1
        mbuys += value
        mfees += buy_fee
        cash = round_cents(cash - shares * per_share - buy_fee)
    m.used = used
