"""update_prices with a fake source: failures are isolated, lag is completed from the quote."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd

from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import PriceStore
from athex_agent.data.sources import Quote, SourceError
from athex_agent.data.update import update_prices
from tests.test_prices import bars


class FakeSource:
    name = "fake"

    def __init__(self, lag_tickers=(), broken=()):
        self.lag = set(lag_tickers)
        self.broken = set(broken)
        self.calls = []

    def fetch_daily(self, ticker, start):
        self.calls.append((ticker, start))
        if ticker in self.broken:
            raise SourceError("boom")
        n = 10 if ticker not in self.lag else 8  # lagging ticker misses the last two sessions
        acts = pd.DataFrame(
            {"kind": ["dividend"], "value": [0.4]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-09-16")], name="date"),
        )
        return bars(date(2026, 9, 14), n), acts

    def fetch_quote(self, ticker):
        if ticker in self.lag:
            return Quote(
                ticker,
                price=50.0,
                open=49.0,
                prev_close=48.0,
                day_high=51.0,
                day_low=48.5,
                volume=12345.0,
                ts=datetime(2026, 9, 23, 14, 25, tzinfo=UTC),
            )
        return None

    def fetch_intraday_open(self, ticker, d):
        return None


def test_update_isolates_failures_and_completes_lag(tmp_path):
    store, acts = PriceStore(tmp_path / "p"), ActionsStore(tmp_path / "a")
    src = FakeSource(lag_tickers={"PPC.AT"}, broken={"BAD.AT"})
    rep = update_prices(store, acts, src, ["ETE.AT", "PPC.AT", "BAD.AT"], as_of=date(2026, 9, 23))
    by = {r.ticker: r for r in rep.results}
    assert by["ETE.AT"].ok and by["ETE.AT"].latest_bar == date(2026, 9, 23)
    assert not by["ETE.AT"].quote_used
    assert by["PPC.AT"].ok and by["PPC.AT"].quote_used
    assert by["PPC.AT"].latest_bar == date(2026, 9, 23)
    row = store.load("PPC.AT").iloc[-1]
    assert row["close"] == 50.0 and row["open"] == 49.0 and row["source"] == "fake_quote"
    assert not row["settled"]
    assert not by["BAD.AT"].ok and "boom" in by["BAD.AT"].error
    assert [r.ticker for r in rep.failed] == ["BAD.AT"]
    assert rep.stale == []
    assert acts.load("ETE.AT").iloc[0]["value"] == 0.4
    assert "1 failed" in rep.summary()
    # second run re-fetches only a short window
    update_prices(store, acts, src, ["ETE.AT"], as_of=date(2026, 9, 23))
    assert src.calls[-1] == ("ETE.AT", date(2026, 9, 23) - timedelta(days=14))
