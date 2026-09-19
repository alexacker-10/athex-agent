"""Rule-based deciders: buy-and-hold, random picker, momentum, equal weight.

Every decider sees the same inputs an LLM arm sees (as-of prices, universe, its own books) and
returns a Proposal. The rules layer then applies identical limits, so baselines are comparable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from athex_agent.arms.proposal import Action, Proposal, View
from athex_agent.config.models import ArmConfig, ResolvedArm
from athex_agent.data import calendar as cal
from athex_agent.data.prices import AsOfPrices
from athex_agent.portfolio.accounting import BookState


@dataclass
class DecisionInputs:
    arm: ResolvedArm
    decision_date: date
    decision_ts: datetime
    prices: AsOfPrices
    universe: list[str]
    books: dict[str, BookState]
    rng: random.Random
    in_buildup: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_book(self) -> BookState:
        for b in self.arm.books:
            if b.primary and b.id in self.books:
                return self.books[b.id]
        return next(iter(self.books.values()))

    def held(self) -> set[str]:
        """Names the primary book holds or is about to hold (pending buys), minus pending sells."""
        book = self.primary_book
        names = set(book.positions)
        for o in book.pending_orders:
            if o.side == "BUY":
                names.add(o.ticker)
            else:
                names.discard(o.ticker)
        return names

    @property
    def target_positions(self) -> int:
        return self.arm.limits.target_positions

    def first_decision_day_of_month(self) -> bool:
        d = self.decision_date
        return cal.first_trading_day_of_month(d.year, d.month) == d


class Decider(Protocol):
    def tradable(self, inp: DecisionInputs) -> set[str]: ...

    def decide(self, inp: DecisionInputs) -> Proposal: ...


def _proposal(
    inp: DecisionInputs,
    actions: list[Action],
    views: list[View] | None = None,
    meta: dict[str, Any] | None = None,
) -> Proposal:
    return Proposal(
        arm_id=inp.arm.arm.id,
        decision_date=inp.decision_date,
        actions=actions,
        views=views or [],
        meta=meta or {},
    )


class BuyHoldDecider:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def tradable(self, inp: DecisionInputs) -> set[str]:
        return {self.ticker}

    def decide(self, inp: DecisionInputs) -> Proposal:
        if self.ticker in inp.held():
            return _proposal(inp, [])
        return _proposal(
            inp,
            [Action(ticker=self.ticker, kind="buy", weight=1.0, reason="buy-and-hold benchmark")],
        )


class RandomDecider:
    """Null model: random names, random views, swap with probability p_swap per decision day."""

    VIEW_LABELS = ("strong_sell", "sell", "buy", "strong_buy")

    def __init__(self, p_swap: float = 1.0 / 21.0, n_views: int = 10) -> None:
        self.p_swap = p_swap
        self.n_views = n_views

    def tradable(self, inp: DecisionInputs) -> set[str]:
        return set(inp.universe)

    def decide(self, inp: DecisionInputs) -> Proposal:
        rng = inp.rng
        universe = sorted(inp.universe)
        held = inp.held() & set(universe)
        candidates = sorted(set(universe) - held)
        actions: list[Action] = []
        if inp.in_buildup:
            need = max(0, inp.target_positions - len(inp.held()))
            for t in rng.sample(candidates, min(need, len(candidates))):
                actions.append(Action(ticker=t, kind="buy", reason="random build-up"))
        else:
            p = float(inp.extra.get("p_swap", self.p_swap))
            if held and candidates and rng.random() < p:
                out = rng.choice(sorted(held))
                new = rng.choice(candidates)
                actions.append(Action(ticker=out, kind="sell", reason="random swap"))
                actions.append(Action(ticker=new, kind="buy", reason="random swap"))
        views = [
            View(
                ticker=t,
                view=rng.choice(self.VIEW_LABELS),
                conviction=round(rng.uniform(0.2, 0.9), 2),
                horizon_days=20,
                thesis="random null view",
            )
            for t in rng.sample(universe, min(self.n_views, len(universe)))
        ]
        return _proposal(inp, actions, views, {"p_swap": self.p_swap})


class MomentumDecider:
    """Rank by return from t-lookback to t-skip; hold the top names; at most one swap a month."""

    def __init__(self, lookback_days: int = 126, skip_days: int = 21) -> None:
        self.lookback = lookback_days
        self.skip = skip_days

    def tradable(self, inp: DecisionInputs) -> set[str]:
        return set(inp.universe)

    def scores(self, inp: DecisionInputs) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in inp.universe:
            closes = inp.prices.bars(t)["close"]
            if len(closes) <= self.lookback:
                continue
            then, recent = (
                float(closes.iloc[-1 - self.lookback]),
                float(closes.iloc[-1 - self.skip]),
            )
            if then > 0:
                out[t] = recent / then - 1.0
        return out

    def decide(self, inp: DecisionInputs) -> Proposal:
        scores = self.scores(inp)
        ranked = sorted(scores, key=lambda t: scores[t], reverse=True)
        target = inp.target_positions
        held = inp.held()
        actions: list[Action] = []
        if inp.in_buildup:
            for t in ranked:
                if len(held) >= target:
                    break
                if t not in held:
                    actions.append(
                        Action(ticker=t, kind="buy", reason=f"momentum rank {ranked.index(t) + 1}")
                    )
                    held = held | {t}
        elif inp.first_decision_day_of_month() and held:
            top = set(ranked[:target])
            scored_held = [t for t in held if t in scores]
            if scored_held:
                weakest = min(scored_held, key=lambda t: scores[t])
                best_new = next((t for t in ranked if t not in held), None)
                if weakest not in top and best_new is not None and best_new in top:
                    actions.append(
                        Action(
                            ticker=weakest,
                            kind="sell",
                            reason=f"momentum rank {ranked.index(weakest) + 1}",
                        )
                    )
                    actions.append(
                        Action(
                            ticker=best_new,
                            kind="buy",
                            reason=f"momentum rank {ranked.index(best_new) + 1}",
                        )
                    )
        views: list[View] = []
        n = len(ranked)
        k = min(5, max(1, n // 2))  # non-overlapping top and bottom groups
        for i, t in enumerate(ranked[:k]):
            views.append(
                View(
                    ticker=t,
                    view="strong_buy" if i < 2 else "buy",
                    conviction=round(0.9 - 0.1 * i, 2),
                    horizon_days=21,
                    thesis=f"6-1 momentum rank {i + 1} of {n}",
                )
            )
        for i, t in enumerate(ranked[n - k :][::-1]):
            views.append(
                View(
                    ticker=t,
                    view="strong_sell" if i < 2 else "sell",
                    conviction=round(0.9 - 0.1 * i, 2),
                    horizon_days=21,
                    thesis=f"6-1 momentum rank {n - i} of {n}",
                )
            )
        return _proposal(
            inp, actions, views, {"scores": {t: round(s, 4) for t, s in scores.items()}}
        )


class EqualWeightDecider:
    """Hold every universe member at 1/N; add/remove on membership changes every k months."""

    def __init__(self, rebalance_months: int = 3) -> None:
        self.rebalance_months = rebalance_months

    def tradable(self, inp: DecisionInputs) -> set[str]:
        return set(inp.universe)

    def decide(self, inp: DecisionInputs) -> Proposal:
        universe = sorted(inp.universe)
        if not universe:
            return _proposal(inp, [])
        w = 1.0 / len(universe)
        held = inp.held()
        actions: list[Action] = []
        start = inp.primary_book.started_on
        months_since = (
            (inp.decision_date.year - start.year) * 12 + inp.decision_date.month - start.month
        )
        rebalance = (
            inp.first_decision_day_of_month()
            and months_since > 0
            and months_since % self.rebalance_months == 0
        )
        if inp.in_buildup or rebalance:
            for t in sorted(held - set(universe)):
                actions.append(Action(ticker=t, kind="sell", reason="left the universe"))
            for t in universe:
                if t not in held:
                    actions.append(Action(ticker=t, kind="buy", weight=w, reason="equal weight"))
        return _proposal(inp, actions)


def make_decider(arm: ArmConfig) -> Decider:
    if arm.kind != "rule" or arm.rule is None:
        raise ValueError(f"arm {arm.id} is not a rule arm; inject its decider explicitly")
    p = arm.rule.params
    match arm.rule.kind:
        case "buy_hold":
            return BuyHoldDecider(str(p["ticker"]))
        case "random":
            return RandomDecider(
                p_swap=float(p.get("p_swap", 1.0 / 21.0)), n_views=int(p.get("n_views", 10))
            )
        case "momentum":
            return MomentumDecider(int(p.get("lookback_days", 126)), int(p.get("skip_days", 21)))
        case "equal_weight":
            return EqualWeightDecider(int(p.get("rebalance_months", 3)))
    raise ValueError(f"unknown rule kind {arm.rule.kind}")
