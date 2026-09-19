"""End-to-end: rule arms run day by day through fills, corporate actions, decisions and NAV."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from athex_agent.arms.deciders import RandomDecider
from athex_agent.arms.runner import ArmRunner
from athex_agent.arms.simulate import simulate_arm
from athex_agent.config.freeze import FrozenConfigViolation
from athex_agent.config.models import RuleParams, UniverseConfig
from athex_agent.data.actions import ActionsStore
from tests.conftest import synthetic_store

END = date(2026, 12, 18)
UNIVERSE = {f"U{i}.AT": 5.0 + 3 * i for i in range(8)}


@pytest.fixture
def env(tmp_path, repo):
    store = synthetic_store(tmp_path / "prices", {**UNIVERSE, "AETF.AT": 60.0}, end=END, n=260)
    actions = ActionsStore(tmp_path / "actions")
    div = pd.DataFrame(
        {"kind": ["dividend"], "value": [0.30]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-10-15")], name="date"),
    )
    actions.upsert("U0.AT", div, "test")
    actions.upsert("AETF.AT", div, "test")
    ucfg = UniverseConfig(
        id="base",
        description="t",
        min_adv_eur=100_000,
        min_price_eur=0.3,
        min_history_trading_days=120,
        adv_window_days=60,
        candidates=list(UNIVERSE),
        include_always=[],
    )
    return store, actions, ucfg, tmp_path / "state"


def test_buy_hold_arm_end_to_end(env, repo):
    store, actions, ucfg, state = env
    resolved = repo.resolve_arm("bh_athex")
    runner = ArmRunner(resolved, store, actions, state)
    nav = simulate_arm(runner, None, date(2026, 10, 1), date(2026, 10, 30))
    days = nav[nav.book_id == "10k"]
    assert len(days) == 21  # trading days in October 2026 (28 Oct is a holiday)
    for book_id, capital in (("10k", 10_000), ("1k", 1_000)):
        book = runner.load_books()[book_id]
        assert set(book.positions) == {"AETF.AT"}
        assert book.fees_paid_eur == 4.90 and book.pending_orders == []
        # dividend ex 15 Oct (0.30 gross, 5% withholding) is paid 10 sessions later, on 30 Oct
        assert book.dividends_received_eur == pytest.approx(
            round(book.shares("AETF.AT") * 0.30 * 0.95, 2)
        )
        assert book.cash_events[0].applied_on == date(2026, 10, 30)
        last = nav[nav.book_id == book_id]["nav"].iloc[
            -1
        ]  # flat prices: capital - costs + dividend
        assert last == pytest.approx(
            capital - book.fees_paid_eur - book.slippage_cost_eur + book.dividends_received_eur,
            abs=0.02,
        )
    assert (runner.paths.root / "config.lock.json").exists()
    rec = json.loads(runner.paths.decision(date(2026, 10, 1)).read_text())
    assert rec["books"]["10k"]["orders"][0]["ticker"] == "AETF.AT" and rec["buildup"]
    assert (
        json.loads(runner.paths.decision(date(2026, 10, 2)).read_text())["books"]["10k"]["orders"]
        == []
    )
    # frozen config: a changed limit is refused on the next decision
    changed = resolved.model_copy(
        update={"limits": resolved.limits.model_copy(update={"min_hold_trading_days": 1})}
    )
    with pytest.raises(FrozenConfigViolation):
        ArmRunner(changed, store, actions, state).decide(date(2026, 11, 2), None, [])


def test_random_arm_respects_limits_and_pays_dividends(env, repo):
    store, actions, ucfg, state = env
    resolved = repo.resolve_arm("random")
    runner = ArmRunner(
        resolved,
        store,
        actions,
        state,
        instance="seed-3",
        decider=RandomDecider(p_swap=0.5, n_views=4),
    )
    nav = simulate_arm(runner, ucfg, date(2026, 10, 1), date(2026, 12, 18))
    books = runner.load_books()
    for book_id, book in books.items():
        assert 1 <= len(book.positions) <= 5
        for y, m in [(2026, 11), (2026, 12)]:
            assert book.orders_placed_in_month(y, m) <= 2, (book_id, y, m)
        # no sell before the 30-trading-day hold except stop-loss (prices are flat: none)
        for t in book.trades:
            if t.side == "SELL":
                buys = [
                    b
                    for b in book.trades
                    if b.ticker == t.ticker and b.side == "BUY" and b.fill_date < t.fill_date
                ]
                assert (t.decision_date - buys[-1].fill_date).days >= 40
        assert book.fees_paid_eur > 0
    assert runner.paths.root.name == "seed-3"
    # the personal book logs where it could not follow (min order) or shows identical names
    b10, b1 = books["10k"], books["1k"]
    assert set(b1.positions) <= set(b10.positions) or b1.divergences
    assert len(runner.nav_series("10k")) == len(nav[nav.book_id == "10k"])
    views = json.loads(runner.paths.views(date(2026, 10, 1)).read_text())["views"]
    assert len(views) == 4


def test_record_nav_is_idempotent(env, repo):
    store, actions, ucfg, state = env
    runner = ArmRunner(repo.resolve_arm("bh_athex"), store, actions, state)
    runner.start(date(2026, 10, 1))
    runner.record_nav(date(2026, 10, 1))
    runner.record_nav(date(2026, 10, 1))
    runner.record_nav(date(2026, 10, 2))
    assert [r["date"] for r in runner.nav_series("10k")] == ["2026-10-01", "2026-10-02"]


def test_rule_params_model():
    assert RuleParams(kind="random", params={"n_seeds": 3}).params["n_seeds"] == 3
