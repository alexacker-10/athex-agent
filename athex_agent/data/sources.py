"""Price source interface and the Yahoo Finance implementation (free, via yfinance).

Any source can be swapped in by implementing PriceSource. Every network call is retried with
backoff and every failure is reported, never raised through a run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import pandas as pd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    open: float | None
    prev_close: float | None
    day_high: float | None
    day_low: float | None
    volume: float | None
    ts: datetime  # UTC time of the last trade Yahoo reports


class PriceSource(Protocol):
    name: str

    def fetch_daily(self, ticker: str, start: date | None) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return (bars, actions). bars: index date, columns open,high,low,close,volume.
        actions: index date, columns kind ('dividend'|'split'), value."""
        ...

    def fetch_quote(self, ticker: str) -> Quote | None: ...

    def fetch_intraday_open(self, ticker: str, d: date) -> float | None: ...


class SourceError(RuntimeError):
    pass


def _retry(fn, *, attempts: int, backoff_s: float, what: str):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - any transport/parse error is retryable here
            last = exc
            wait = backoff_s * (2**i)
            log.warning(
                "%s failed (%s: %s); retry %d/%d in %.0fs",
                what,
                type(exc).__name__,
                exc,
                i + 1,
                attempts,
                wait,
            )
            time.sleep(wait)
    raise SourceError(f"{what}: {type(last).__name__}: {last}")


class YahooSource:
    name = "yahoo"

    def __init__(self, pause_s: float = 0.6, attempts: int = 3, backoff_s: float = 3.0) -> None:
        self.pause_s = pause_s
        self.attempts = attempts
        self.backoff_s = backoff_s

    def _ticker(self, ticker: str):
        import yfinance as yf  # imported lazily so tests never need the network stack

        return yf.Ticker(ticker)

    def fetch_daily(self, ticker: str, start: date | None) -> tuple[pd.DataFrame, pd.DataFrame]:
        def go():
            tk = self._ticker(ticker)
            if start is None:
                h = tk.history(period="max", auto_adjust=False, actions=True)
            else:
                h = tk.history(start=start.isoformat(), auto_adjust=False, actions=True)
            return h

        h = _retry(
            go, attempts=self.attempts, backoff_s=self.backoff_s, what=f"yahoo daily {ticker}"
        )
        time.sleep(self.pause_s)
        if h is None or h.empty:
            return _empty_bars(), _empty_actions()
        h = h.copy()
        h.index = pd.DatetimeIndex([ts.date() for ts in h.index], name="date")
        bars = h.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]]
        bars = bars[bars["close"].notna()]
        acts = []
        if "Dividends" in h.columns:
            for ts, v in h["Dividends"].items():
                if v and v > 0:
                    acts.append({"date": ts, "kind": "dividend", "value": float(v)})
        if "Stock Splits" in h.columns:
            for ts, v in h["Stock Splits"].items():
                if v and v > 0:
                    acts.append({"date": ts, "kind": "split", "value": float(v)})
        actions = pd.DataFrame(acts).set_index("date") if acts else _empty_actions()
        return bars, actions

    def fetch_quote(self, ticker: str) -> Quote | None:
        def go():
            info = self._ticker(ticker).info or {}
            price = info.get("regularMarketPrice")
            ts = info.get("regularMarketTime")
            if price is None or ts is None:
                return None
            return Quote(
                ticker=ticker,
                price=float(price),
                open=_f(info.get("regularMarketOpen")),
                prev_close=_f(info.get("regularMarketPreviousClose")),
                day_high=_f(info.get("regularMarketDayHigh")),
                day_low=_f(info.get("regularMarketDayLow")),
                volume=_f(info.get("regularMarketVolume")),
                ts=datetime.fromtimestamp(int(ts), tz=UTC),
            )

        try:
            q = _retry(
                go, attempts=self.attempts, backoff_s=self.backoff_s, what=f"yahoo quote {ticker}"
            )
        except SourceError as exc:
            log.warning("%s", exc)
            return None
        time.sleep(self.pause_s)
        return q

    def fetch_intraday_open(self, ticker: str, d: date) -> float | None:
        def go():
            h = self._ticker(ticker).history(period="5d", interval="1m", auto_adjust=False)
            if h is None or h.empty:
                return None
            day = h[[ts.date() == d for ts in h.index]]
            day = day[day["Open"].notna() & (day["Open"] > 0)]
            return float(day["Open"].iloc[0]) if not day.empty else None

        try:
            v = _retry(
                go,
                attempts=self.attempts,
                backoff_s=self.backoff_s,
                what=f"yahoo intraday {ticker} {d}",
            )
        except SourceError as exc:
            log.warning("%s", exc)
            return None
        time.sleep(self.pause_s)
        return v


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _empty_bars() -> pd.DataFrame:
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([], name="date")
    return df


def _empty_actions() -> pd.DataFrame:
    df = pd.DataFrame(columns=["kind", "value"])
    df.index = pd.DatetimeIndex([], name="date")
    return df


REFETCH_WINDOW_DAYS = 14  # re-fetch this many calendar days so provisional bars get replaced


def refetch_start(latest: date | None) -> date | None:
    return None if latest is None else latest - timedelta(days=REFETCH_WINDOW_DAYS)
