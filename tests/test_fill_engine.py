"""Fill engine: next-open fills, slippage and fees, and the structural lookahead guard."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from athex_agent.portfolio.accounting import BookState
from athex_agent.portfolio.fill_engine import FillEngine, LookaheadGuard, check_no_lookahead
from tests.conftest import build_order, synthetic_store

DECISION = date(2026, 9, 21)
TARGET = date(2026, 9, 22)
OPEN_UTC = datetime(2026, 9, 22, 7, 30, tzinfo=UTC)  # 10:30 Athens in September


@pytest.fixture
def env(tmp_path, repo):
    store = synthetic_store(tmp_path / "p", {"A.AT": 10.0, "B.AT": 20.0}, end=date(2026, 9, 23))
    engine = FillEngine(store, repo.slippage())
    return store, engine, repo.fee_profile("degiro")


def pending(book: BookState, ticker="A.AT", side="BUY", shares=100, ref=10.0, ts=None):
    o = build_order(ticker, side, shares, ref, book_id=book.book_id)
    if ts is not None:
        o = o.model_copy(update={"decision_ts": ts})
    book.add_pending(o)
    return o


def test_fills_at_next_open_with_slippage_and_fees(env):
    store, engine, degiro = env
    book = BookState.new("10k", "A", 10_000, DECISION)
    o = pending(book)
    out = engine.process(book, degiro, {"A.AT": 2_000_000.0}, OPEN_UTC + timedelta(hours=1))
    assert [x.status for x in out] == ["filled"]
    f = out[0].fill
    assert f.open_price == 10.0 and f.fill_price > 10.0 and f.fill_date == TARGET
    assert f.fees.total == 4.90 and f.decision_date == DECISION
    assert book.pending_orders == [] and book.shares("A.AT") == 100
    assert book.cash_eur == pytest.approx(10_000 - f.gross_value - 4.90)
    assert book.trades[0].order_id == o.order_id


def test_lookahead_guard_rejects_orders_decided_or_committed_after_the_open(env):
    store, engine, degiro = env
    late = OPEN_UTC + timedelta(minutes=1)
    book = BookState.new("10k", "A", 10_000, DECISION)
    pending(book, ts=late)
    with pytest.raises(LookaheadGuard, match="decided at"):
        engine.process(book, degiro, {"A.AT": 2e6}, OPEN_UTC + timedelta(hours=2))
    book2 = BookState.new("10k", "A", 10_000, DECISION)
    o = pending(book2)
    with pytest.raises(LookaheadGuard, match="committed at"):
        engine.process(
            book2,
            degiro,
            {"A.AT": 2e6},
            OPEN_UTC + timedelta(hours=2),
            committed_ts={o.order_id: late},
        )
    # committed one second before the open is fine
    assert check_no_lookahead(o, OPEN_UTC - timedelta(seconds=1)) == OPEN_UTC


def test_pending_before_open_and_when_bar_missing_then_stale_cancel(env, tmp_path, repo):
    store, engine, degiro = env
    book = BookState.new("10k", "A", 10_000, DECISION)
    pending(book)
    out = engine.process(book, degiro, {"A.AT": 2e6}, OPEN_UTC - timedelta(minutes=5))
    assert out[0].status == "pending" and book.pending_orders
    # a ticker with no bar for the target date stays pending, then is cancelled after 3 sessions
    book = BookState.new("10k", "A", 10_000, DECISION)
    pending(book, ticker="C.AT")
    out = engine.process(book, degiro, {"C.AT": 2e6}, OPEN_UTC + timedelta(hours=1), today=TARGET)
    assert out[0].status == "pending"
    out = engine.process(
        book, degiro, {"C.AT": 2e6}, OPEN_UTC + timedelta(days=5), today=date(2026, 9, 25)
    )
    assert out[0].status == "cancelled"
    assert book.cancelled_orders[0].reason.startswith("no opening price")
    assert book.divergences[-1].kind == "ORDER_STALE_CANCELLED"


def test_liquidity_refusal_and_cash_shrink(env):
    store, engine, degiro = env
    book = BookState.new("10k", "A", 10_000, DECISION)
    pending(book)
    out = engine.process(book, degiro, {"A.AT": 5_000.0}, OPEN_UTC + timedelta(hours=1))
    assert out[0].status == "cancelled" and book.divergences[-1].kind == "LIQUIDITY_REFUSED"
    book = BookState.new("10k", "A", 1_000, DECISION)
    pending(book, shares=100, ref=10.0)  # 100 x 10 = 1000 leaves no room for fees
    out = engine.process(book, degiro, {"A.AT": 2e6}, OPEN_UTC + timedelta(hours=1))
    assert out[0].status == "filled" and out[0].fill.shares < 100
    assert book.divergences[-1].kind == "ORDER_SHRUNK_CASH"
    assert book.cash_eur >= 0


def test_sell_fill_reduces_position(env):
    store, engine, degiro = env
    book = BookState.new("10k", "A", 10_000, DECISION)
    pending(book, shares=50, ref=10.0)
    engine.process(book, degiro, {"A.AT": 2e6}, OPEN_UTC + timedelta(hours=1))
    o = pending(book, side="SELL", shares=20, ref=10.0)
    o2 = o.model_copy(
        update={
            "decision_date": TARGET,
            "target_fill_date": date(2026, 9, 23),
            "order_id": "sell-later",
        }
    )
    book.remove_pending(o.order_id)
    book.add_pending(o2)
    out = engine.process(book, degiro, {"A.AT": 2e6}, datetime(2026, 9, 23, 9, 0, tzinfo=UTC))
    assert out[0].status == "filled" and book.shares("A.AT") == 30
    assert out[0].fill.fees.sales_tax > 0
