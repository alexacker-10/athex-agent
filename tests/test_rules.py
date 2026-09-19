"""Rules layer: the hard limits, per-book sizing and divergence logging."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from athex_agent.arms.proposal import Action, Proposal
from athex_agent.config.models import RiskLimits
from athex_agent.portfolio.accounting import BookState, TradeRecord
from athex_agent.portfolio.fees import compute_fees
from athex_agent.portfolio.rules import PlanContext, RulesLayer
from tests.conftest import build_fill, build_order, synthetic_store

PRICES = {
    "A.AT": 10.0,
    "B.AT": 20.0,
    "C.AT": 5.0,
    "D.AT": 0.55,
    "E.AT": 50.0,
    "F.AT": 12.0,
    "G.AT": 30.0,
    "H.AT": 8.0,
}
DECISION = date(2026, 9, 21)
TS = datetime(2026, 9, 21, 18, 40, tzinfo=UTC)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return synthetic_store(
        tmp_path_factory.mktemp("prices"), PRICES, end=DECISION, volume=200_000.0
    )


@pytest.fixture
def ctx_factory(repo, store):
    def make(book_id="10k", profile="degiro", limits=None):
        books = {b.id: b for b in repo.books().books}
        return PlanContext(
            decision_date=DECISION,
            decision_ts=TS,
            prices=store.as_of(DECISION),
            tradable=set(PRICES),
            limits=limits or repo.limits("base"),
            book_cfg=books[book_id],
            fee_profile=repo.fee_profile(profile),
            slippage=repo.slippage(),
        )

    return make


def proposal(*actions: Action) -> Proposal:
    return Proposal(arm_id="A", decision_date=DECISION, actions=list(actions))


def buy(t, w=None):
    return Action(ticker=t, kind="buy", weight=w, reason="test")


def sell(t):
    return Action(ticker=t, kind="sell", reason="test")


def add_trade(book: BookState, ticker: str, side: str, gross: float, decision: date) -> None:
    book.trades.append(
        TradeRecord(
            order_id=f"seed-{ticker}-{side}-{decision}",
            ticker=ticker,
            side=side,
            shares=1,
            decision_date=decision,
            fill_date=decision,
            open_price=gross,
            fill_price=gross,
            gross_value=gross,
            commission=0.0,
            handling=0.0,
            exchange=0.0,
            sales_tax=0.0,
            slippage_cost=0.0,
            cash_delta=gross if side == "SELL" else -gross,
        )
    )


def seeded_book(book_id, capital, holdings, started=date(2026, 8, 3), profile=None):
    """Book with positions given as ticker -> (shares, cost price, acquired date)."""
    b = BookState.new(book_id, "A", capital, started)
    for t, (n, px, acquired) in holdings.items():
        o = build_order(t, "BUY", n, px, book_id=book_id).model_copy(
            update={
                "decision_date": acquired,
                "target_fill_date": acquired,
                "decision_ts": datetime.combine(acquired, datetime.min.time(), tzinfo=UTC),
            }
        )
        b.apply_fill(build_fill(o, px, 0.0, profile).model_copy(update={"fill_date": acquired}))
    return b


def test_buildup_deploys_four_equal_positions_in_both_books(ctx_factory, repo):
    degiro = repo.fee_profile("degiro")
    for book_id, capital in (("10k", 10_000), ("1k", 1_000)):
        book = BookState.new(book_id, "A", capital, DECISION)
        res = RulesLayer().plan(
            proposal(buy("A.AT"), buy("B.AT"), buy("E.AT"), buy("F.AT")), book, ctx_factory(book_id)
        )
        assert res.buildup and not res.blocked and book.divergences == []
        assert [o.ticker for o in res.orders] == ["A.AT", "B.AT", "E.AT", "F.AT"]
        for o in res.orders:
            target = 0.25 * capital
            cost = o.shares * o.reference_price
            fee = compute_fees(degiro, "BUY", cost, o.shares).total
            assert cost + fee <= target + 1e-6
            assert (o.shares + 1) * o.reference_price * 1.0061 > target - 5  # tight sizing
            assert o.target_fill_date == date(2026, 9, 22) and o.decision_ts == TS
            assert o.side == "BUY" and o.decision_date == DECISION
    # identical decisions: the 1k book holds the same names at ~1/10 the size
    b10 = BookState.new("10k", "A", 10_000, DECISION)
    b1 = BookState.new("1k", "A", 1_000, DECISION)
    o10 = RulesLayer().plan(proposal(buy("A.AT")), b10, ctx_factory("10k")).orders[0]
    o1 = RulesLayer().plan(proposal(buy("A.AT")), b1, ctx_factory("1k")).orders[0]
    assert o10.shares == 248 and o1.shares == 24  # 249 x 10.0254 + 4.90 > 2500


def test_order_cap_outside_buildup(ctx_factory, repo):
    book = seeded_book("10k", 10_000, {}, started=date(2026, 8, 3))
    res = RulesLayer().plan(
        proposal(buy("A.AT"), buy("B.AT"), buy("E.AT")), book, ctx_factory("10k")
    )
    assert not res.buildup
    assert [o.ticker for o in res.orders] == ["A.AT", "B.AT"]
    assert [d.kind for d in book.divergences] == ["ORDER_CAP_BLOCK"]
    assert book.divergences[0].ticker == "E.AT"


def test_turnover_is_replacement_churn(ctx_factory, repo):
    degiro = repo.fee_profile("degiro")
    loose = repo.limits("base").model_copy(update={"max_orders_per_month": 10, "id": "loose"})
    # buys alone are not churn: 3000 bought this month, two more buys are allowed
    book = seeded_book("10k", 10_000, {"G.AT": (100, 30.0, date(2026, 9, 10))}, profile=degiro)
    res = RulesLayer().plan(
        proposal(buy("A.AT"), buy("B.AT")), book, ctx_factory("10k", limits=loose)
    )
    assert [o.ticker for o in res.orders] == ["A.AT", "B.AT"] and book.divergences == []
    # with 5000 of sells this month, min(buys, sells) caps replacement at 25% of NAV
    book = seeded_book("10k", 10_000, {"G.AT": (100, 30.0, date(2026, 9, 10))}, profile=degiro)
    add_trade(book, "H.AT", "SELL", 2_500.0, date(2026, 9, 4))
    add_trade(book, "F.AT", "SELL", 2_500.0, date(2026, 9, 7))
    res = RulesLayer().plan(
        proposal(buy("A.AT"), buy("B.AT")), book, ctx_factory("10k", limits=loose)
    )
    assert res.orders == []  # buys so far 3000: A -> min(5500, 5000) = 50% of NAV, blocked
    assert {d.kind for d in book.divergences} == {"TURNOVER_BLOCK"}


def test_hold_period_and_stop_loss(ctx_factory, repo, tmp_path):
    degiro = repo.fee_profile("degiro")
    loose = repo.limits("base").model_copy(update={"max_orders_per_month": 10, "id": "loose"})
    book = seeded_book("10k", 10_000, {"G.AT": (100, 30.0, date(2026, 9, 10))}, profile=degiro)
    # G was bought 7 trading days ago -> sell blocked
    res = RulesLayer().plan(proposal(sell("G.AT")), book, ctx_factory("10k", limits=loose))
    assert res.orders == [] and book.divergences[-1].kind == "HOLD_PERIOD_BLOCK"
    # stop-loss: cost ~30.05, close 25 (< 25.5) -> forced sell despite the hold period
    low = synthetic_store(tmp_path / "low", {"G.AT": 25.0}, end=DECISION)
    ctx = ctx_factory("10k", limits=loose)
    ctx.prices = low.as_of(DECISION)
    res = RulesLayer().plan(proposal(), book, ctx)
    assert len(res.orders) == 1 and res.orders[0].side == "SELL"
    assert "stop_loss" in res.orders[0].tags
    assert book.divergences[-1].kind == "STOP_LOSS_FORCED"


def test_sell_after_hold_funds_the_buy_and_swaps_survive_drift(ctx_factory, repo):
    degiro = repo.fee_profile("degiro")
    book = seeded_book("10k", 10_000, {"G.AT": (80, 30.0, date(2026, 7, 1))}, profile=degiro)
    book.cash_eur = 50.0  # nearly fully invested elsewhere (simulated)
    res = RulesLayer().plan(proposal(sell("G.AT"), buy("A.AT")), book, ctx_factory("10k"))
    assert [(o.side, o.ticker) for o in res.orders] == [("SELL", "G.AT"), ("BUY", "A.AT")]
    assert res.orders[1].shares > 0  # sized from the sale proceeds
    # a swap of a position that drifted above 25% of NAV is still one swap, not a breach
    book = seeded_book("10k", 10_000, {"G.AT": (100, 30.0, date(2026, 7, 1))}, profile=degiro)
    book.cash_eur = 6_000.0  # NAV 9000, G is 33%
    res = RulesLayer().plan(proposal(sell("G.AT"), buy("A.AT")), book, ctx_factory("10k"))
    assert [(o.side, o.ticker) for o in res.orders] == [("SELL", "G.AT"), ("BUY", "A.AT")]
    assert book.divergences == []


def test_fee_budget_block_1k_outside_buildup(ctx_factory, repo):
    degiro = repo.fee_profile("degiro")
    loose = repo.limits("base").model_copy(
        update={"max_orders_per_month": 10, "max_turnover_pct_month": 1.0, "id": "loose"}
    )
    book = seeded_book(
        "1k",
        1_000,
        {"A.AT": (12, 10.0, date(2026, 9, 2)), "B.AT": (6, 20.0, date(2026, 9, 3))},
        profile=degiro,
    )
    assert book.fees_in_month(2026, 9) == pytest.approx(9.80)
    res = RulesLayer().plan(
        proposal(buy("E.AT"), buy("F.AT")), book, ctx_factory("1k", limits=loose)
    )
    # budget 1.5% x NAV ~ 15.0: one more EUR 4.90 order fits (14.70), the next does not
    assert [o.ticker for o in res.orders] == ["E.AT"]
    assert book.divergences[-1].kind == "FEE_BUDGET_BLOCK" and book.divergences[-1].ticker == "F.AT"


def test_min_order_and_fee_economics_divergences(ctx_factory, repo):
    small = BookState.new("1k", "A", 500.0, DECISION)  # 25% = 125 < min order 150
    RulesLayer().plan(proposal(buy("A.AT")), small, ctx_factory("1k"))
    assert small.divergences[0].kind == "ORDER_SKIPPED_MIN_SIZE"
    # Freedom24 per-share fee on a EUR 0.55 stock: 1k book excluded, 10k book not
    b1 = BookState.new("1k", "A", 1_000, DECISION)
    b10 = BookState.new("10k", "A", 10_000, DECISION)
    r1 = RulesLayer().plan(proposal(buy("D.AT")), b1, ctx_factory("1k", profile="freedom24_smart"))
    r10 = RulesLayer().plan(
        proposal(buy("D.AT")), b10, ctx_factory("10k", profile="freedom24_smart")
    )
    assert r1.orders == [] and b1.divergences[0].kind == "FEE_ECONOMICS_EXCLUSION"
    assert "freedom24_smart" in b1.divergences[0].detail
    assert len(r10.orders) == 1 and b10.divergences == []


def test_universe_and_position_cap(ctx_factory, repo):
    degiro = repo.fee_profile("degiro")
    book = BookState.new("10k", "A", 10_000, DECISION)
    RulesLayer().plan(proposal(buy("ZZZ.AT")), book, ctx_factory("10k"))
    assert book.divergences[-1].kind == "NOT_IN_UNIVERSE"
    full = seeded_book(
        "10k",
        10_000,
        {t: (10, PRICES[t], date(2026, 7, 1)) for t in ["A.AT", "B.AT", "C.AT", "E.AT", "F.AT"]},
        profile=degiro,
    )
    RulesLayer().plan(proposal(buy("G.AT")), full, ctx_factory("10k"))
    assert full.divergences[-1].kind == "POSITION_CAP_BLOCK"


def test_liquidity_shrinks_to_adv_cap(ctx_factory, repo, tmp_path):
    thin = synthetic_store(tmp_path / "thin", {"A.AT": 10.0}, end=DECISION, volume=12_000.0)
    ctx = ctx_factory("10k")
    ctx.prices = thin.as_of(DECISION)  # ADV = 120,000 -> cap 1% = 1,200 per order
    book = BookState.new("10k", "A", 10_000, DECISION)
    res = RulesLayer().plan(proposal(buy("A.AT")), book, ctx)
    assert len(res.orders) == 1 and res.orders[0].shares * 10.0 <= 1_200


def test_limits_model_has_fee_economics_cap(repo):
    assert repo.limits("base").max_fee_pct_per_order == 0.04
    assert isinstance(repo.limits("buy_hold"), RiskLimits)
