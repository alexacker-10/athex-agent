"""Price store with a structural no-lookahead guard.

Layout: one CSV per ticker under data/prices/<TICKER>.csv with columns
date,open,high,low,close,volume,settled,source. Bars are unadjusted; dividends and splits live in
the actions store and are applied explicitly by accounting.

Provisional bars: Yahoo's daily bars for some ATHEX tickers arrive 1-2 sessions late and the
latest row can be partial. The newest PROVISIONAL_BARS bars of every upsert are stored as
provisional and are replaced on the next fetch; settled bars are immutable (a differing refetch
is logged to data/prices/_revisions.csv, never applied).

Lookahead: consumers get an `AsOfPrices` view bound to a date T. The view filters every frame
to dates <= T and raises LookaheadError on any explicit request beyond T. The only accessor that
returns a price after a view's T is `PriceStore.session_open`, which exists for the fill engine
alone and takes no view.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

PROVISIONAL_BARS = 3
COLUMNS = ["open", "high", "low", "close", "volume", "settled", "source"]
REVISIONS_FILE = "_revisions.csv"


class LookaheadError(RuntimeError):
    """A consumer asked for data after its as-of date. This must never be caught and ignored."""


class NoPriceData(RuntimeError):
    pass


@dataclass(frozen=True)
class Bar:
    ticker: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    settled: bool
    source: str


def _empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    df.index = pd.DatetimeIndex([], name="date")
    return df.astype(
        {
            "open": float,
            "high": float,
            "low": float,
            "close": float,
            "volume": float,
            "settled": bool,
            "source": str,
        }
    )


def frame_from_bars(bars: Iterable[Bar]) -> pd.DataFrame:
    rows = [
        {
            "date": pd.Timestamp(b.date),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "settled": b.settled,
            "source": b.source,
        }
        for b in bars
    ]
    if not rows:
        return _empty_frame()
    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df[COLUMNS]


class PriceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- files
    def path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.csv"

    def tickers(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.csv") if not p.stem.startswith("_"))

    def load(self, ticker: str) -> pd.DataFrame:
        p = self.path(ticker)
        if not p.exists():
            return _empty_frame()
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
        df["settled"] = df["settled"].astype(bool)
        df["source"] = df["source"].astype(str)
        return df[COLUMNS].sort_index()

    def save(self, ticker: str, df: pd.DataFrame) -> None:
        out = df[COLUMNS].sort_index().copy()
        out.index.name = "date"
        out.to_csv(self.path(ticker), date_format="%Y-%m-%d", float_format="%.6f")

    def latest_date(self, ticker: str) -> date | None:
        df = self.load(ticker)
        return None if df.empty else df.index[-1].date()

    # ---- ingestion
    def upsert(self, ticker: str, incoming: pd.DataFrame, source: str, fetched_at: datetime) -> int:
        """Merge fetched bars. Returns the number of rows written (new or replaced provisional)."""
        if incoming.empty:
            return 0
        new = incoming.copy()
        new = new[new["close"].notna() & (new["close"] > 0)]
        if new.empty:
            return 0
        new.index = pd.DatetimeIndex(pd.to_datetime(new.index).date, name="date")
        new = new[~new.index.duplicated(keep="last")].sort_index()
        new["source"] = source
        new["settled"] = False
        if len(new) > PROVISIONAL_BARS:
            new.iloc[:-PROVISIONAL_BARS, new.columns.get_loc("settled")] = True
        existing = self.load(ticker)
        written = 0
        revisions: list[dict[str, object]] = []
        rows: dict[pd.Timestamp, pd.Series] = {ts: row for ts, row in existing.iterrows()}
        for ts, row in new.iterrows():
            old = rows.get(ts)
            if old is None:
                rows[ts] = row[COLUMNS]
                written += 1
                continue
            changed = any(
                not _close_enough(old[c], row[c])
                for c in ("open", "high", "low", "close", "volume")
            )
            if old["settled"]:
                if changed:
                    revisions.append(_revision(ticker, ts, old, row, applied=False, at=fetched_at))
                continue  # settled bars are immutable
            newly_settled = bool(row["settled"]) and not bool(old["settled"])
            if not changed and not newly_settled:
                continue
            if changed:
                revisions.append(_revision(ticker, ts, old, row, applied=True, at=fetched_at))
            merged = row[COLUMNS].copy()
            merged["settled"] = bool(row["settled"]) or bool(old["settled"])
            rows[ts] = merged
            written += 1
        df = pd.DataFrame.from_dict(rows, orient="index")[COLUMNS]
        df.index = pd.DatetimeIndex(df.index, name="date")
        self.save(ticker, df.sort_index())
        if revisions:
            self._log_revisions(revisions)
        return written

    def _log_revisions(self, revisions: list[dict[str, object]]) -> None:
        p = self.root / REVISIONS_FILE
        new_file = not p.exists()
        with p.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(revisions[0].keys()))
            if new_file:
                w.writeheader()
            w.writerows(revisions)

    def revisions(self) -> pd.DataFrame:
        p = self.root / REVISIONS_FILE
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    # ---- fill-engine-only accessor (deliberately not on the as-of view)
    def session_open(self, ticker: str, d: date) -> float | None:
        """Opening-auction price of session d, or None if the bar is not stored yet."""
        df = self.load(ticker)
        ts = pd.Timestamp(d)
        if ts not in df.index:
            return None
        v = float(df.at[ts, "open"])
        return None if math.isnan(v) or v <= 0 else v

    # ---- views
    def as_of(self, as_of: date) -> AsOfPrices:
        return AsOfPrices(self, as_of)


def _close_enough(a: object, b: object) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(fa) and math.isnan(fb):
        return True
    return math.isclose(fa, fb, rel_tol=1e-6, abs_tol=1e-6)


def _revision(
    ticker: str, ts: pd.Timestamp, old: pd.Series, new: pd.Series, applied: bool, at: datetime
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "date": ts.date().isoformat(),
        "applied": applied,
        "old_open": old["open"],
        "new_open": new["open"],
        "old_close": old["close"],
        "new_close": new["close"],
        "old_volume": old["volume"],
        "new_volume": new["volume"],
        "fetched_at": at.isoformat(),
    }


class AsOfPrices:
    """Read-only price access bound to an as-of date. Cannot return anything after that date."""

    def __init__(self, store: PriceStore, as_of: date) -> None:
        self._store = store
        self.as_of = as_of
        self._cache: dict[str, pd.DataFrame] = {}

    def _guard(self, d: date | None) -> None:
        if d is not None and d > self.as_of:
            raise LookaheadError(f"requested {d} but the view is as of {self.as_of}")

    def bars(self, ticker: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        self._guard(end)
        df = self._cache.get(ticker)
        if df is None:
            df = self._store.load(ticker)
            df = df[df.index <= pd.Timestamp(self.as_of)]
            self._cache[ticker] = df
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        assert df.empty or df.index[-1].date() <= self.as_of  # structural guarantee
        return df

    def last_bar(self, ticker: str) -> Bar:
        df = self.bars(ticker)
        if df.empty:
            raise NoPriceData(f"no bars for {ticker} as of {self.as_of}")
        ts = df.index[-1]
        r = df.iloc[-1]
        return Bar(
            ticker,
            ts.date(),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r["volume"]),
            bool(r["settled"]),
            str(r["source"]),
        )

    def close(self, ticker: str, d: date | None = None) -> float:
        self._guard(d)
        df = self.bars(ticker, end=d)
        if df.empty:
            raise NoPriceData(f"no close for {ticker} as of {d or self.as_of}")
        return float(df["close"].iloc[-1])

    def closes(self, tickers: Iterable[str]) -> dict[str, float]:
        return {t: self.close(t) for t in tickers}

    def adv_eur(self, ticker: str, window: int = 20) -> float:
        """Mean of close x volume over the last `window` bars (NaN if fewer than window//2 bars)."""
        df = self.bars(ticker).tail(window)
        if len(df) < max(1, window // 2):
            return float("nan")
        return float((df["close"] * df["volume"]).mean())

    def median_adv_eur(self, ticker: str, window: int = 60) -> float:
        df = self.bars(ticker).tail(window)
        if len(df) < max(1, window // 2):
            return float("nan")
        return float((df["close"] * df["volume"]).median())

    def history_length(self, ticker: str) -> int:
        return len(self.bars(ticker))

    def has_bar(self, ticker: str, d: date) -> bool:
        self._guard(d)
        return pd.Timestamp(d) in self.bars(ticker).index
