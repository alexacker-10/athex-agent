from __future__ import annotations

from datetime import date

import pytest

from athex_agent.portfolio.accounting import (
    AccountingError,
    BookState,
    InsufficientCash,
    InsufficientShares,
    MissingPrice,
)
from tests.conftest import build_fill, build_order


@pytest.fixture
def degiro(profiles):
    return profiles["degiro"]


@pytest.fixture
def book() -> BookState:
    return BookState.new("10k", "A", 10_000, date(2026, 9, 21))


def test_buy_then_partial_sell_fifo(book, degiro):
    buy = build_fill(
        build_order("ETE.AT", "BUY", 100, 20.0), open_price=20.0, slippage=0.001, profile=degiro
    )
    rec = book.apply_fill(buy)
    assert buy.fill_price == 20.02 and buy.gross_value == 2002.00 and buy.cash_delta == -2006.90
    assert book.cash_eur == 7993.10
    assert book.shares("ETE.AT") == 100
    assert book.positions["ETE.AT"].cost_basis == 2006.90
    assert book.slippage_cost_eur == 2.00
    assert rec.realized_pnl is None
    assert book.nav({"ETE.AT": 21.0}) == 10093.10

    sell = build_fill(
        build_order("ETE.AT", "SELL", 60, 22.0), open_price=22.0, slippage=0.001, profile=degiro
    )
    rec = book.apply_fill(sell)
    assert sell.gross_value == 1318.68 and sell.fees.total == 6.22 and sell.cash_delta == 1312.46
    assert rec.cost_basis_sold == 1204.14
    assert rec.realized_pnl == 108.32
    assert book.cash_eur == 9305.56
    assert book.shares("ETE.AT") == 40
    assert book.positions["ETE.AT"].cost_basis == 802.76
    assert book.fees_paid_eur == 9.80
    assert book.sales_tax_paid_eur == 1.32
    assert book.slippage_cost_eur == 3.32
    assert len(book.trades) == 2


def test_full_sell_removes_position(book, degiro):
    book.apply_fill(build_fill(build_order("PPC.AT", "BUY", 10, 25.0), 25.0, 0.0, degiro))
    book.apply_fill(build_fill(build_order("PPC.AT", "SELL", 10, 26.0), 26.0, 0.0, degiro))
    assert "PPC.AT" not in book.positions


def test_guards(book, degiro):
    with pytest.raises(InsufficientCash):
        book.apply_fill(build_fill(build_order("ETE.AT", "BUY", 1000, 20.0), 20.0, 0.0, degiro))
    with pytest.raises(InsufficientShares):
        book.apply_fill(build_fill(build_order("ETE.AT", "SELL", 1, 20.0), 20.0, 0.0, degiro))
    book.apply_fill(build_fill(build_order("ETE.AT", "BUY", 10, 20.0), 20.0, 0.0, degiro))
    with pytest.raises(MissingPrice):
        book.nav({})
    other = build_fill(build_order("ETE.AT", "BUY", 1, 20.0, book_id="1k"), 20.0, 0.0, degiro)
    with pytest.raises(AccountingError):
        book.apply_fill(other)


def test_dividend_is_net_and_paid_later(book, degiro):
    book.apply_fill(build_fill(build_order("HTO.AT", "BUY", 40, 19.0), 19.0, 0.0, degiro))
    ev = book.record_dividend(
        "HTO.AT", 0.50, ex_date=date(2026, 10, 1), pay_date=date(2026, 10, 15)
    )
    assert ev is not None and ev.amount_eur == 19.00
    cash_before = book.cash_eur
    assert book.apply_due_cash_events(date(2026, 10, 14)) == []
    assert book.cash_eur == cash_before
    applied = book.apply_due_cash_events(date(2026, 10, 15))
    assert len(applied) == 1 and book.cash_eur == cash_before + 19.00
    assert book.dividends_received_eur == 19.00
    assert book.apply_due_cash_events(date(2026, 10, 16)) == []  # not applied twice
    assert book.record_dividend("ZZZ.AT", 1.0, date(2026, 10, 1), date(2026, 10, 15)) is None


def test_forward_split_preserves_cost_basis(book, degiro):
    book.apply_fill(build_fill(build_order("BELA.AT", "BUY", 40, 25.0), 25.0, 0.0, degiro))
    basis = book.positions["BELA.AT"].cost_basis
    old, new = book.apply_split("BELA.AT", 2.0, date(2026, 11, 2), price_after=12.5)
    assert (old, new) == (40, 80)
    assert book.positions["BELA.AT"].cost_basis == basis
    assert book.positions["BELA.AT"].avg_cost == pytest.approx(basis / 80)
    assert not any(e.kind == "CASH_IN_LIEU" for e in book.cash_events)


def test_reverse_split_pays_cash_in_lieu(book, degiro):
    book.apply_fill(build_fill(build_order("CREDIA.AT", "BUY", 45, 1.0), 1.0, 0.0, degiro))
    old, new = book.apply_split("CREDIA.AT", 0.1, date(2026, 11, 2), price_after=10.0)
    assert (old, new) == (45, 4)
    lieu = [e for e in book.cash_events if e.kind == "CASH_IN_LIEU"]
    assert len(lieu) == 1 and lieu[0].amount_eur == 5.00  # 0.5 share x EUR 10
    cash = book.cash_eur
    book.apply_due_cash_events(date(2026, 11, 2))
    assert book.cash_eur == cash + 5.00
    assert book.apply_split("NOPE.AT", 2.0, date(2026, 11, 2), 1.0) == (0, 0)


def test_account_fee_and_divergence_log(book):
    book.schedule_account_fee(4.00, date(2026, 9, 30), "Piraeus quarterly account fee")
    book.apply_due_cash_events(date(2026, 9, 30))
    assert book.cash_eur == 9996.00 and book.account_fees_paid_eur == 4.00
    d = book.log_divergence(
        date(2026, 9, 22),
        "CREDIA.AT",
        "FEE_ECONOMICS_EXCLUSION",
        "per-share fee 20.8% of order",
        250,
        0,
    )
    assert book.divergences == [d]


def test_save_load_round_trip(tmp_path, book, degiro):
    book.apply_fill(build_fill(build_order("ETE.AT", "BUY", 10, 20.0), 20.0, 0.001, degiro))
    book.record_dividend("ETE.AT", 0.3, date(2026, 10, 1), date(2026, 10, 15))
    path = tmp_path / "book.json"
    book.save(path)
    loaded = BookState.load(path)
    assert loaded.model_dump() == book.model_dump()
    assert loaded.nav({"ETE.AT": 20.0}) == book.nav({"ETE.AT": 20.0})
