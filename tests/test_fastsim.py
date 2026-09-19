"""The fast simulator must reproduce the live engine on a scripted scenario, to the cent."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from athex_agent.analysis.fastsim import (
    RandomPolicy,
    ScriptedPolicy,
    SimParams,
    simulate,
)
from athex_agent.analysis.historical import _Frames, build_window_data
from athex_agent.arms.proposal import Action, Proposal
from athex_agent.arms.runner import ArmRunner
from athex_agent.arms.simulate import simulate_arm
from athex_agent.data import calendar as cal
from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import PriceStore

START, END = date(2026, 10, 1), date(2026, 12, 18)


def wavy_store(root) -> PriceStore:
    """Six names with distinct drifts and daily oscillation, 220 bdays ending mid-December."""
    store = PriceStore(root)
    days = pd.bdate_range(end=pd.Timestamp(END), periods=220)
    rng = np.random.default_rng(3)
    for i in range(6):
        base = 8.0 + 5 * i
        drift = np.linspace(0, 0.25 - 0.1 * i, len(days))
        noise = 0.02 * np.sin(np.arange(len(days)) / (2 + i)) + rng.normal(0, 0.004, len(days))
        close = base * (1 + drift + noise)
        opn = close * (1 + rng.normal(0, 0.003, len(days)))
        df = pd.DataFrame(
            {
                "open": opn,
                "high": np.maximum(opn, close) * 1.005,
                "low": np.minimum(opn, close) * 0.995,
                "close": close,
                "volume": 150_000 + 10_000 * i + rng.integers(0, 20_000, len(days)),
            },
            index=pd.DatetimeIndex(days, name="date"),
        )
        store.upsert(f"W{i}.AT", df, "test", datetime(2026, 12, 19, tzinfo=UTC))
    return store


class ScriptedDecider:
    def __init__(self, script: dict[date, list[Action]], universe: list[str]) -> None:
        self.script, self.universe = script, universe

    def tradable(self, inp):
        return set(self.universe)

    def decide(self, inp):
        return Proposal(
            arm_id=inp.arm.arm.id,
            decision_date=inp.decision_date,
            actions=self.script.get(inp.decision_date, []),
        )


@pytest.fixture
def env(tmp_path, repo):
    store = wavy_store(tmp_path / "p")
    actions = ActionsStore(tmp_path / "a")
    actions.upsert(
        "W1.AT",
        pd.DataFrame(
            {"kind": ["dividend"], "value": [0.25]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-10-20")], name="date"),
        ),
        "test",
    )
    actions.upsert(
        "W2.AT",
        pd.DataFrame(
            {"kind": ["split"], "value": [2.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-11-10")], name="date"),
        ),
        "test",
    )
    return store, actions, tmp_path / "state"


def test_fastsim_matches_live_engine(env, repo):
    store, actions, state = env
    universe = [f"W{i}.AT" for i in range(6)]
    days = cal.trading_days(START, END)
    # script: build-up buys on day 0, a stop-loss-free swap on the first day of December
    dec1 = days[0]
    dec_dec = cal.first_trading_day_of_month(2026, 12)
    script_live = {
        dec1: [Action(ticker=t, kind="buy") for t in ["W0.AT", "W1.AT", "W2.AT", "W3.AT"]],
        dec_dec: [Action(ticker="W3.AT", kind="sell"), Action(ticker="W4.AT", kind="buy")],
    }
    resolved = repo.resolve_arm("random")
    runner = ArmRunner(
        resolved, store, actions, state, decider=ScriptedDecider(script_live, universe)
    )
    live = simulate_arm(runner, None, START, END)
    live.loc[:, "date"] = live["date"].astype(str)

    frames = _Frames(store, actions)
    data = build_window_data(frames, universe, days)
    t_dec = days.index(dec_dec)
    script_fast = {
        0: ([], [(t, None) for t in ["W0.AT", "W1.AT", "W2.AT", "W3.AT"]]),
        t_dec: (["W3.AT"], [("W4.AT", None)]),
    }
    for book in resolved.books:
        params = SimParams(
            capital=book.capital_eur,
            fee_profile=resolved.fee_profile_for(book),
            slippage=resolved.slippage,
            limits=resolved.limits,
            min_order_eur=book.min_order_eur,
            fee_budget_pct_month=book.fee_budget_pct_month,
        )
        fast = simulate(data, params, ScriptedPolicy(script_fast))
        live_nav = live[live.book_id == book.id]["nav"].to_numpy()
        assert len(fast.nav_series) == len(live_nav) == len(days)
        diff = np.abs(np.array(fast.nav_series) - live_nav)
        assert diff.max() <= 0.011, (book.id, diff.argmax(), diff.max())
        lb = runner.load_books()[book.id]
        assert fast.fees_paid == pytest.approx(lb.fees_paid_eur, abs=0.011)
        assert fast.tax_paid == pytest.approx(lb.sales_tax_paid_eur, abs=0.011)
        assert fast.dividends == pytest.approx(lb.dividends_received_eur, abs=0.011)
        assert set(fast.positions) == set(lb.positions)
        assert {t: p.shares for t, p in fast.positions.items()} == {
            t: p.shares for t, p in lb.positions.items()
        }
    assert fast.n_trades >= 5  # 4 build-up buys + the December swap (both legs if allowed)


def test_random_policy_respects_limits(env, repo):
    store, actions, _ = env
    universe = [f"W{i}.AT" for i in range(6)]
    days = cal.trading_days(START, END)
    data = build_window_data(_Frames(store, actions), universe, days)
    resolved = repo.resolve_arm("random")
    book_cfg = resolved.books[0]
    params = SimParams(
        capital=10_000,
        fee_profile=resolved.fee_profile_for(book_cfg),
        slippage=resolved.slippage,
        limits=resolved.limits,
        min_order_eur=150,
        fee_budget_pct_month=0.015,
    )
    for seed in range(5):
        fast = simulate(data, params, RandomPolicy(p_swap=0.5), seed=seed)
        assert 1 <= len(fast.positions) <= 5
        for (y, m), mm in fast.months.items():
            if (y, m) != (2026, 10):
                assert mm.used <= 2, (seed, y, m, mm.used)
        assert fast.nav_series[0] == 10_000 and fast.cash >= 0
    a = simulate(data, params, RandomPolicy(p_swap=0.5), seed=1)
    b = simulate(data, params, RandomPolicy(p_swap=0.5), seed=1)
    assert a.nav_series == b.nav_series
