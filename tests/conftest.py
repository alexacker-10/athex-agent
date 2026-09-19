from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from athex_agent.config.loader import ConfigRepo
from athex_agent.portfolio.fees import compute_fees
from athex_agent.portfolio.orders import Fill, Order, make_fill


@pytest.fixture(scope="session")
def repo() -> ConfigRepo:
    return ConfigRepo()


@pytest.fixture(scope="session")
def profiles(repo):
    return repo.fee_profiles()


def build_order(
    ticker: str, side: str, shares: int, ref_price: float, book_id: str = "10k", arm_id: str = "A"
) -> Order:
    return Order(
        order_id=f"{arm_id}-{book_id}-{ticker}-{side}-{shares}",
        arm_id=arm_id,
        book_id=book_id,
        ticker=ticker,
        side=side,
        shares=shares,
        reference_price=ref_price,
        decision_date=date(2026, 9, 21),
        target_fill_date=date(2026, 9, 22),
        decision_ts=datetime(2026, 9, 21, 18, 40, tzinfo=UTC),
    )


def build_fill(order: Order, open_price: float, slippage: float, profile) -> Fill:
    sign = 1.0 if order.side == "BUY" else -1.0
    fill_price = round(open_price * (1 + sign * slippage), 4)
    gross = round(order.shares * fill_price, 2)
    fees = compute_fees(profile, order.side, gross, order.shares)
    return make_fill(
        order,
        fill_date=order.target_fill_date,
        open_price=open_price,
        fill_price=fill_price,
        slippage_pct=slippage,
        fees=fees,
        fill_ts=datetime(2026, 9, 22, 7, 45, tzinfo=UTC),
    )


def synthetic_store(
    root,
    prices: dict[str, float],
    end: date = date(2026, 9, 22),
    n: int = 200,
    volume: float = 200_000.0,
):
    """A PriceStore with `n` flat-ish daily bars per ticker ending on `end` (calendar days)."""
    import pandas as pd

    from athex_agent.data.prices import PriceStore

    store = PriceStore(root)
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    for ticker, px in prices.items():
        df = pd.DataFrame(
            {
                "open": [px] * n,
                "high": [px * 1.01] * n,
                "low": [px * 0.99] * n,
                "close": [px] * n,
                "volume": [volume] * n,
            },
            index=pd.DatetimeIndex(days, name="date"),
        )
        store.upsert(ticker, df, "test", datetime(2026, 9, 22, 18, 0, tzinfo=UTC))
    return store
