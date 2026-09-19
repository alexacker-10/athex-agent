from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from athex_agent.arms.deciders import (
    BuyHoldDecider,
    DecisionInputs,
    EqualWeightDecider,
    MomentumDecider,
    RandomDecider,
    make_decider,
)
from athex_agent.data.prices import PriceStore
from athex_agent.portfolio.accounting import BookState
from tests.conftest import build_order, synthetic_store

D = date(2026, 9, 21)
TS = datetime(2026, 9, 21, 18, 40, tzinfo=UTC)


def trending_store(root, n=200):
    """Five names with returns from +40% (T1) down to -20% (T5) over the last 200 bdays."""
    store = PriceStore(root)
    days = pd.bdate_range(end=pd.Timestamp(D), periods=n)
    for i, total in enumerate([0.4, 0.2, 0.05, -0.05, -0.2]):
        px = [10.0 * (1 + total * k / (n - 1)) for k in range(n)]
        df = pd.DataFrame(
            {"open": px, "high": px, "low": px, "close": px, "volume": [1e5] * n},
            index=pd.DatetimeIndex(days, name="date"),
        )
        store.upsert(f"T{i + 1}.AT", df, "test", datetime(2026, 9, 22, tzinfo=UTC))
    return store


def inputs(repo, store, universe, arm_id="random", buildup=False, books=None, seed=1):
    resolved = repo.resolve_arm(arm_id)
    books = books or {
        "10k": BookState.new("10k", arm_id, 10_000, D),
        "1k": BookState.new("1k", arm_id, 1_000, D),
    }
    return DecisionInputs(
        arm=resolved,
        decision_date=D,
        decision_ts=TS,
        prices=store.as_of(D),
        universe=universe,
        books=books,
        rng=random.Random(seed),
        in_buildup=buildup,
    )


def test_buy_hold_once(repo, tmp_path):
    store = synthetic_store(tmp_path / "p", {"AETF.AT": 60.0}, end=D)
    inp = inputs(repo, store, [], arm_id="bh_athex", buildup=True)
    dec = make_decider(repo.arm("bh_athex"))
    assert isinstance(dec, BuyHoldDecider) and dec.tradable(inp) == {"AETF.AT"}
    p = dec.decide(inp)
    assert [(a.kind, a.ticker, a.weight) for a in p.actions] == [("buy", "AETF.AT", 1.0)]
    inp.books["10k"].add_pending(build_order("AETF.AT", "BUY", 100, 60.0, arm_id="bh_athex"))
    assert dec.decide(inp).actions == []


def test_random_buildup_swap_and_views(repo, tmp_path):
    universe = [f"U{i}.AT" for i in range(8)]
    store = synthetic_store(tmp_path / "p", dict.fromkeys(universe, 10.0), end=D)
    dec = RandomDecider(p_swap=1.0, n_views=5)
    p = dec.decide(inputs(repo, store, universe, buildup=True))
    assert len(p.buys) == 4 and len(set(a.ticker for a in p.buys)) == 4 and not p.sells
    assert len(p.views) == 5 and all(v.view != "neutral" for v in p.views)
    # outside build-up with p_swap=1: one sell of a holding + one buy of a non-holding
    inp = inputs(repo, store, universe)
    inp.books["10k"].add_pending(build_order("U0.AT", "BUY", 100, 10.0, arm_id="random"))
    p = dec.decide(inp)
    assert [a.kind for a in p.actions] == ["sell", "buy"]
    assert p.sells[0].ticker == "U0.AT" and p.buys[0].ticker != "U0.AT"
    # deterministic given the seed
    a = RandomDecider(p_swap=1.0).decide(inputs(repo, store, universe, seed=7))
    b = RandomDecider(p_swap=1.0).decide(inputs(repo, store, universe, seed=7))
    assert a == b
    assert RandomDecider(p_swap=0.0).decide(inputs(repo, store, universe)).actions == []


def test_momentum_ranks_and_swaps(repo, tmp_path):
    store = trending_store(tmp_path / "p")
    universe = [f"T{i}.AT" for i in range(1, 6)]
    dec = MomentumDecider(lookback_days=126, skip_days=21)
    inp = inputs(repo, store, universe, arm_id="momentum", buildup=True)
    scores = dec.scores(inp)
    assert sorted(scores, key=scores.get, reverse=True) == universe
    p = dec.decide(inp)
    assert [a.ticker for a in p.buys] == ["T1.AT", "T2.AT", "T3.AT", "T4.AT"]
    assert p.views[0].ticker == "T1.AT" and p.views[0].view == "strong_buy"
    assert any(v.ticker == "T5.AT" and v.view == "strong_sell" for v in p.views)
    # holding T5 (weakest, outside top-4) on the first decision day of a month -> swap for T1
    first = date(2026, 10, 1)
    inp = inputs(repo, store, universe, arm_id="momentum")
    inp.decision_date = first
    inp.prices = store.as_of(D)
    inp.books["10k"].add_pending(build_order("T5.AT", "BUY", 100, 10.0, arm_id="momentum"))
    p = dec.decide(inp)
    assert [(a.kind, a.ticker) for a in p.actions] == [("sell", "T5.AT"), ("buy", "T1.AT")]
    inp.decision_date = first + timedelta(days=1)  # not the first decision day -> no action
    assert dec.decide(inp).actions == []


def test_equal_weight_buys_all_then_rebalances_membership(repo, tmp_path):
    universe = [f"U{i}.AT" for i in range(6)]
    store = synthetic_store(tmp_path / "p", dict.fromkeys(universe, 10.0), end=D)
    dec = EqualWeightDecider(rebalance_months=3)
    p = dec.decide(inputs(repo, store, universe, arm_id="equal_weight", buildup=True))
    assert len(p.buys) == 6 and all(a.weight == pytest.approx(1 / 6) for a in p.buys)
    inp = inputs(repo, store, universe[:5], arm_id="equal_weight")
    inp.books["10k"].add_pending(build_order("U5.AT", "BUY", 10, 10.0, arm_id="equal_weight"))
    inp.decision_date = date(2026, 12, 1)  # 3 months after the September start, first trading day
    p = dec.decide(inp)
    assert [(a.kind, a.ticker) for a in p.sells] == [("sell", "U5.AT")]
    assert len(p.buys) == 5
