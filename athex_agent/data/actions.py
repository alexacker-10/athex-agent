"""Corporate actions store: dividends (gross per share, ex-date), splits (ratio, effective date)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd

ActionKind = Literal["dividend", "split"]
COLUMNS = ["kind", "value", "source"]


class ActionsStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.csv"

    def load(self, ticker: str) -> pd.DataFrame:
        p = self.path(ticker)
        if not p.exists():
            df = pd.DataFrame(columns=COLUMNS)
            df.index = pd.DatetimeIndex([], name="date")
            return df
        df = pd.read_csv(p, parse_dates=["date"], index_col="date")
        return df[COLUMNS].sort_index()

    def upsert(self, ticker: str, actions: pd.DataFrame, source: str) -> int:
        """`actions`: index=date, columns kind,value. Existing (date, kind) rows are kept."""
        if actions.empty:
            return 0
        existing = self.load(ticker)
        keys = {(ts.date(), k) for ts, k in zip(existing.index, existing["kind"], strict=True)}
        rows = []
        for ts, row in actions.iterrows():
            d = pd.Timestamp(ts).date()
            if (d, row["kind"]) in keys or not row["value"] or float(row["value"]) <= 0:
                continue
            rows.append(
                {
                    "date": pd.Timestamp(d),
                    "kind": row["kind"],
                    "value": float(row["value"]),
                    "source": source,
                }
            )
        if not rows:
            return 0
        new = pd.DataFrame(rows).set_index("date")
        out = pd.concat([existing, new]).sort_index()
        out.index.name = "date"
        out.to_csv(self.path(ticker), date_format="%Y-%m-%d")
        return len(rows)

    def between(
        self, ticker: str, start: date, end: date, kind: ActionKind | None = None
    ) -> list[dict]:
        """Actions with start < date <= end (the events that occur after a decision on `start`)."""
        df = self.load(ticker)
        df = df[(df.index > pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        if kind is not None:
            df = df[df["kind"] == kind]
        return [
            {"date": ts.date(), "kind": r["kind"], "value": float(r["value"])}
            for ts, r in df.iterrows()
        ]
