"""Bring the price and actions stores up to date for a list of tickers, with per-ticker health."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pandas as pd

from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import PriceStore
from athex_agent.data.sources import PriceSource, SourceError, refetch_start

log = logging.getLogger(__name__)


@dataclass
class TickerUpdate:
    ticker: str
    ok: bool
    bars_written: int = 0
    actions_written: int = 0
    latest_bar: date | None = None
    quote_used: bool = False
    error: str | None = None


@dataclass
class UpdateReport:
    as_of: date
    started_at: datetime
    results: list[TickerUpdate] = field(default_factory=list)

    @property
    def failed(self) -> list[TickerUpdate]:
        return [r for r in self.results if not r.ok]

    @property
    def stale(self) -> list[TickerUpdate]:
        return [
            r for r in self.results if r.ok and (r.latest_bar is None or r.latest_bar < self.as_of)
        ]

    def summary(self) -> str:
        return (
            f"{len(self.results)} tickers, {len(self.failed)} failed, {len(self.stale)} stale "
            f"(latest bar before {self.as_of})"
        )


def update_prices(
    store: PriceStore,
    actions: ActionsStore,
    source: PriceSource,
    tickers: list[str],
    as_of: date,
    use_quote_for_latest: bool = True,
) -> UpdateReport:
    """One broken ticker never stops the others; failures are recorded in the report."""
    report = UpdateReport(as_of=as_of, started_at=datetime.now(UTC))
    for ticker in tickers:
        res = TickerUpdate(ticker=ticker, ok=False)
        try:
            start = refetch_start(store.latest_date(ticker))
            bars, acts = source.fetch_daily(ticker, start)
            res.bars_written = store.upsert(ticker, bars, source.name, datetime.now(UTC))
            res.actions_written = actions.upsert(ticker, acts, source.name)
            res.latest_bar = store.latest_date(ticker)
            if use_quote_for_latest and (res.latest_bar is None or res.latest_bar < as_of):
                res.quote_used = _complete_from_quote(store, source, ticker, as_of)
                res.latest_bar = store.latest_date(ticker)
            res.ok = True
        except SourceError as exc:
            res.error = str(exc)
            log.error("price update failed for %s: %s", ticker, exc)
        except Exception as exc:  # noqa: BLE001 - a parser surprise must not kill the run
            res.error = f"{type(exc).__name__}: {exc}"
            log.exception("unexpected error updating %s", ticker)
        report.results.append(res)
    return report


def _complete_from_quote(store: PriceStore, source: PriceSource, ticker: str, as_of: date) -> bool:
    """When the daily bar for `as_of` is missing (Yahoo lag), synthesise a provisional bar from the
    quote endpoint if it reports a trade on that date."""
    q = source.fetch_quote(ticker)
    if q is None or q.ts.date() != as_of or q.price <= 0:
        return False
    row = pd.DataFrame(
        [
            {
                "open": q.open or q.price,
                "high": q.day_high or q.price,
                "low": q.day_low or q.price,
                "close": q.price,
                "volume": q.volume or 0.0,
            }
        ],
        index=pd.DatetimeIndex([pd.Timestamp(as_of)], name="date"),
    )
    store.upsert(ticker, row, f"{source.name}_quote", datetime.now(UTC))
    return True
