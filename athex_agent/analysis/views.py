"""View-level scoring: forward excess returns of every view at 5/10/20/30 sessions.

Entry price is the next session's open (the earliest price the view could have been acted on);
exit is the open h sessions later; excess is versus the equal-weight universe over the same
window; short views are sign-flipped. Daily views on the same name overlap, so the headline
sample size is the EFFECTIVE n (non-overlapping windows) and the confidence interval comes from
a block bootstrap over date blocks of the horizon length. The raw view count is secondary.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from athex_agent.arms.proposal import VIEW_SCORE
from athex_agent.data import calendar as cal
from athex_agent.data.prices import PriceStore

HORIZONS = (5, 10, 20, 30)


def load_views(arm_root: Path) -> pd.DataFrame:
    rows = []
    for p in sorted((Path(arm_root) / "views").glob("*.json")):
        payload = json.loads(p.read_text(encoding="utf-8"))
        d = payload["decision_date"]
        for v in payload.get("views", []):
            rows.append(
                {
                    "date": d,
                    "ticker": v["ticker"],
                    "view": v["view"],
                    "score": VIEW_SCORE[v["view"]],
                    "conviction": float(v["conviction"]),
                    "horizon_days": int(v.get("horizon_days", 20)),
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=["date", "ticker", "view", "score", "conviction", "horizon_days"]
        )
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _open_on(store: PriceStore, ticker: str, d: date) -> float | None:
    return store.session_open(ticker, d)


def score_views(
    views: pd.DataFrame,
    store: PriceStore,
    universe_on: Callable[[date], list[str]],
    as_of: date,
    horizons: tuple[int, ...] = HORIZONS,
) -> pd.DataFrame:
    """Add excess_{h} and signed_{h} columns; NaN where the horizon has not matured by as_of."""
    if views.empty:
        return views.copy()
    out = views.copy()
    cache: dict[tuple[str, date], float | None] = {}

    def opn(t: str, d: date) -> float | None:
        key = (t, d)
        if key not in cache:
            cache[key] = _open_on(store, t, d)
        return cache[key]

    uni_cache: dict[tuple[date, int], float] = {}
    for h in horizons:
        exc, sgn = [], []
        for _, r in out.iterrows():
            d = r["date"]
            entry_d = cal.next_trading_day(d)
            exit_d = cal.add_trading_days(entry_d, h)
            if exit_d > as_of:
                exc.append(np.nan)
                sgn.append(np.nan)
                continue
            e, x = opn(r["ticker"], entry_d), opn(r["ticker"], exit_d)
            if not e or not x:
                exc.append(np.nan)
                sgn.append(np.nan)
                continue
            key = (d, h)
            if key not in uni_cache:
                rets = []
                for t in universe_on(d):
                    ue, ux = opn(t, entry_d), opn(t, exit_d)
                    if ue and ux:
                        rets.append(ux / ue - 1.0)
                uni_cache[key] = float(np.mean(rets)) if rets else 0.0
            ret = x / e - 1.0 - uni_cache[key]
            exc.append(ret)
            sgn.append(ret * (1 if r["score"] > 0 else -1 if r["score"] < 0 else 0))
        out[f"excess_{h}"] = exc
        out[f"signed_{h}"] = sgn
    return out


def effective_n(scored: pd.DataFrame, h: int) -> int:
    """Count views per ticker that are at least h trading days apart (greedy, non-overlapping)."""
    col = f"signed_{h}"
    n = 0
    for _, grp in scored.dropna(subset=[col]).sort_values("date").groupby("ticker"):
        last: date | None = None
        for d in grp["date"]:
            if last is None or cal.trading_days_between(last, d) >= h:
                n += 1
                last = d
    return n


def block_bootstrap_ci(
    scored: pd.DataFrame, h: int, n_boot: int = 1000, seed: int = 0, weighted: bool = False
) -> tuple[float, float, float]:
    """(mean, lo95, hi95) of the signed excess return; blocks of h consecutive decision dates."""
    col = f"signed_{h}"
    df = scored.dropna(subset=[col])
    df = df[df["score"] != 0]
    if df.empty:
        return (math.nan, math.nan, math.nan)
    dates = sorted(df["date"].unique())
    blocks = [dates[i : i + h] for i in range(0, len(dates), h)]
    by_date = {d: g for d, g in df.groupby("date")}
    rng = np.random.default_rng(seed)

    def mean_of(sample_blocks) -> float:
        vals, wts = [], []
        for blk in sample_blocks:
            for d in blk:
                g = by_date[d]
                vals.extend(g[col].tolist())
                wts.extend(
                    (g["conviction"] if weighted else pd.Series(1.0, index=g.index)).tolist()
                )
        return float(np.average(vals, weights=wts)) if vals else math.nan

    point = mean_of(blocks)
    if len(blocks) < 2:
        return (point, math.nan, math.nan)
    boots = [
        mean_of([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        for _ in range(n_boot)
    ]
    return (point, float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))


def summarize(scored: pd.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> dict[str, Any]:
    out: dict[str, Any] = {"n_views_raw": int(len(scored)), "horizons": {}}
    for h in horizons:
        col = f"signed_{h}"
        if col not in scored.columns:
            continue
        df = scored.dropna(subset=[col])
        df = df[df["score"] != 0]
        mean, lo, hi = block_bootstrap_ci(scored, h)
        wmean, wlo, whi = block_bootstrap_ci(scored, h, weighted=True)
        longs = df[df["score"] > 0][f"excess_{h}"]
        shorts = df[df["score"] < 0][f"excess_{h}"]
        out["horizons"][str(h)] = {
            "n_effective": effective_n(scored, h),
            "n_raw": int(len(df)),
            "hit_rate": float((df[col] > 0).mean()) if len(df) else None,
            "mean_signed_excess": None if math.isnan(mean) else mean,
            "ci95": [None if math.isnan(lo) else lo, None if math.isnan(hi) else hi],
            "conviction_weighted_mean": None if math.isnan(wmean) else wmean,
            "long_minus_short": (
                float(longs.mean() - shorts.mean()) if len(longs) and len(shorts) else None
            ),
            "significant": bool(not math.isnan(lo) and lo > 0),
        }
    return out
