from __future__ import annotations

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from athex_agent.analysis.views import (
    block_bootstrap_ci,
    effective_n,
    load_views,
    score_views,
    summarize,
)
from athex_agent.data import calendar as cal
from athex_agent.data.prices import PriceStore

START, END = date(2026, 9, 1), date(2026, 12, 18)


def trend_store(root) -> PriceStore:
    """UP.AT rises 0.5%/day, DOWN.AT falls 0.5%/day, FLAT.AT is flat; opens == closes."""
    store = PriceStore(root)
    days = pd.bdate_range(start=pd.Timestamp(START), end=pd.Timestamp(END))
    n = len(days)
    for tk, g in (("UP.AT", 1.005), ("DOWN.AT", 0.995), ("FLAT.AT", 1.0)):
        px = 10.0 * g ** np.arange(n)
        df = pd.DataFrame(
            {"open": px, "high": px, "low": px, "close": px, "volume": [1e5] * n},
            index=pd.DatetimeIndex(days, name="date"),
        )
        store.upsert(tk, df, "test", datetime(2026, 12, 19, tzinfo=UTC))
    return store


def write_views(root, rows):
    (root / "views").mkdir(parents=True, exist_ok=True)
    by_date = {}
    for d, tk, view, conv in rows:
        by_date.setdefault(d, []).append(
            {"ticker": tk, "view": view, "conviction": conv, "horizon_days": 20}
        )
    for d, views in by_date.items():
        (root / "views" / f"{d.isoformat()}.json").write_text(
            json.dumps(
                {"arm_id": "A", "instance": None, "decision_date": d.isoformat(), "views": views}
            )
        )


def test_scoring_signs_and_effective_n(tmp_path):
    store = trend_store(tmp_path / "p")
    root = tmp_path / "arm"
    days = cal.trading_days(date(2026, 9, 1), date(2026, 10, 15))
    rows = []
    for d in days:  # a view every day on both names: heavily overlapping
        rows.append((d, "UP.AT", "buy", 0.8))
        rows.append((d, "DOWN.AT", "sell", 0.6))
        rows.append((d, "FLAT.AT", "neutral", 0.5))
    write_views(root, rows)
    views = load_views(root)
    assert len(views) == 3 * len(days)
    scored = score_views(views, store, lambda d: ["UP.AT", "DOWN.AT", "FLAT.AT"], as_of=END)
    up = scored[scored.ticker == "UP.AT"]
    down = scored[scored.ticker == "DOWN.AT"]
    assert (up["excess_5"] > 0).all() and (down["excess_5"] < 0).all()
    assert (down["signed_5"] > 0).all()  # a correct short view scores positive
    assert (scored[scored.ticker == "FLAT.AT"]["signed_20"] == 0).all()
    # universe-relative: UP's 5-day raw return minus the mean of the three
    raw = 1.005**5 - 1
    assert up["excess_5"].iloc[0] == pytest.approx(
        raw - np.mean([raw, 0.995**5 - 1, 0.0]), rel=1e-6
    )
    assert effective_n(scored, 20) == 2 * ((len(days) + 19) // 20) + ((len(days) + 19) // 20)
    assert effective_n(scored, 5) > effective_n(scored, 20)
    s = summarize(scored)
    h20 = s["horizons"]["20"]
    assert h20["n_raw"] == 2 * len(days) and h20["n_effective"] < h20["n_raw"]
    assert h20["hit_rate"] == 1.0 and h20["significant"] is True
    assert h20["ci95"][0] > 0 and h20["long_minus_short"] > 0
    mean, lo, hi = block_bootstrap_ci(scored, 5)
    assert lo <= mean <= hi


def test_unmatured_and_missing_prices_are_nan(tmp_path):
    store = trend_store(tmp_path / "p")
    root = tmp_path / "arm"
    write_views(
        root, [(date(2026, 12, 15), "UP.AT", "buy", 0.9), (date(2026, 9, 2), "ZZZ.AT", "buy", 0.9)]
    )
    scored = score_views(load_views(root), store, lambda d: ["UP.AT"], as_of=date(2026, 12, 18))
    assert scored.loc[scored.ticker == "UP.AT", "excess_5"].isna().all()  # 5 sessions not elapsed
    assert scored.loc[scored.ticker == "ZZZ.AT", "excess_5"].isna().all()
    assert summarize(scored)["horizons"]["5"]["n_effective"] == 0
    assert load_views(tmp_path / "empty").empty
