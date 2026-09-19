"""Historical rolling-window study for the rule-based strategies (never the LLM arms).

One command:
    python -m athex_agent.analysis.historical --start 2011-01-01 --seeds 200
Writes docs/data/historical.json: per window and pooled distributions of excess return vs the
S&P 500 in EUR (SXR8.DE, bought under the DEGIRO ETF fee) for the random picker, momentum, equal
weight and the Greek ETF, at both capital levels, with the same fee and slippage model as live.

Caveats carried into the output: survivorship bias (Yahoo drops delisted names), the ALPHA.AT
history gap, GD.AT being a price index.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from athex_agent.analysis.fastsim import (
    BuyHoldPolicy,
    EqualWeightPolicy,
    MomentumPolicy,
    RandomPolicy,
    SimParams,
    WindowData,
    simulate,
)
from athex_agent.config.loader import ConfigRepo
from athex_agent.config.models import UniverseConfig
from athex_agent.data import calendar as cal
from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import PriceStore
from athex_agent.data.universe import select_universe

log = logging.getLogger(__name__)

SP500_TICKER = "SXR8.DE"
GD_TICKER = "GD.AT"
AETF_TICKER = "AETF.AT"
ADV_WINDOW = 20
MOM_LOOKBACK = 126
MOM_SKIP = 21
PCTS = (5, 25, 50, 75, 95)


class _Frames:
    """Per-ticker derived series, loaded once."""

    def __init__(self, store: PriceStore, actions: ActionsStore) -> None:
        self.store, self.actions = store, actions
        self._cache: dict[str, dict[str, pd.Series]] = {}

    def get(self, ticker: str) -> dict[str, pd.Series]:
        if ticker not in self._cache:
            df = self.store.load(ticker)
            acts = self.actions.load(ticker)
            if df.empty:
                self._cache[ticker] = {}
            else:
                close = df["close"].astype(float)
                self._cache[ticker] = {
                    "open": df["open"].astype(float),
                    "close": close,
                    "adv": (close * df["volume"].astype(float))
                    .rolling(ADV_WINDOW, min_periods=ADV_WINDOW // 2)
                    .mean(),
                    "mom": close.shift(MOM_SKIP) / close.shift(MOM_LOOKBACK) - 1.0,
                    "div": acts[acts["kind"] == "dividend"]["value"].astype(float)
                    if not acts.empty
                    else pd.Series(dtype=float),
                    "split": acts[acts["kind"] == "split"]["value"].astype(float)
                    if not acts.empty
                    else pd.Series(dtype=float),
                }
        return self._cache[ticker]


def build_window_data(
    frames: _Frames, tickers: list[str], dates: list[date], with_scores: bool = True
) -> WindowData:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    T, N = len(dates), len(tickers)
    opens = np.full((T, N), np.nan)
    closes = np.full((T, N), np.nan)
    adv = np.full((T, N), np.nan)
    divs = np.zeros((T, N))
    splits = np.ones((T, N))
    scores = np.full((T, N), np.nan)
    for j, tk in enumerate(tickers):
        f = frames.get(tk)
        if not f:
            continue
        opens[:, j] = f["open"].reindex(idx).to_numpy()
        closes[:, j] = f["close"].reindex(idx, method="ffill").to_numpy()
        adv[:, j] = f["adv"].reindex(idx, method="ffill").to_numpy()
        if with_scores:
            scores[:, j] = f["mom"].reindex(idx, method="ffill").to_numpy()
        if len(f["div"]):
            divs[:, j] = f["div"].reindex(idx).fillna(0.0).to_numpy()
        if len(f["split"]):
            splits[:, j] = f["split"].reindex(idx).fillna(1.0).to_numpy()
    return WindowData(
        dates=dates,
        tickers=tickers,
        opens=opens,
        closes=closes,
        adv=adv,
        dividends=divs,
        splits=splits,
        scores=scores if with_scores else None,
    )


@dataclass
class StudyConfig:
    start: date
    end: date
    window_days: int = 63
    step_days: int = 5
    n_seeds: int = 200
    p_swap: float = 1.0 / 21.0


def _ret(book, capital: float) -> float:
    return book.nav_series[-1] / capital - 1.0


def _stats(returns: list[float], sp500: float | None) -> dict[str, Any]:
    arr = np.array(returns, dtype=float)
    out: dict[str, Any] = {"n": int(arr.size), "mean": float(arr.mean()) if arr.size else None}
    for p in PCTS:
        out[f"p{p:02d}"] = float(np.percentile(arr, p)) if arr.size else None
    if sp500 is not None and arr.size:
        out["p_beat_sp500"] = float((arr > sp500).mean())
    return out


def run_historical(
    repo: ConfigRepo,
    store: PriceStore,
    actions: ActionsStore,
    cfg: StudyConfig,
    universe_cfg: UniverseConfig | None = None,
) -> dict[str, Any]:
    universe_cfg = universe_cfg or repo.universe()
    profiles = repo.fee_profiles()
    books = repo.books().books
    base_lim = repo.limits("base")
    div_lim = repo.limits("diversified")
    bh_lim = repo.limits("buy_hold")
    slippage = repo.slippage()
    frames = _Frames(store, actions)
    days = cal.trading_days(cfg.start, cfg.end)
    starts = list(range(0, max(0, len(days) - cfg.window_days + 1), cfg.step_days))
    windows: list[dict[str, Any]] = []
    pooled: dict[str, dict[str, list[float]]] = {
        b.id: {"random": [], "momentum": [], "equal_weight": [], "bh_aetf": []} for b in books
    }
    for w_i, s in enumerate(starts):
        dates = days[s : s + cfg.window_days]
        snap = select_universe(universe_cfg, store.as_of(dates[0]))
        members = [t for t in snap.members if t != AETF_TICKER]
        if len(members) < base_lim.target_positions:
            log.warning("window %s: only %d members, skipped", dates[0], len(members))
            continue
        data = build_window_data(frames, members, dates)
        bench_data = build_window_data(
            frames, [SP500_TICKER, AETF_TICKER, GD_TICKER], dates, with_scores=False
        )
        gd = bench_data.closes[:, 2]
        gd_ret = float(gd[-1] / gd[0] - 1.0) if not math.isnan(gd[0]) and gd[0] > 0 else None
        win: dict[str, Any] = {
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
            "days": len(dates),
            "universe_size": len(members),
            "gd_price_return": gd_ret,
            "books": {},
        }
        for b in books:

            def params(profile_id: str, limits, _b=b) -> SimParams:
                return SimParams(
                    capital=_b.capital_eur,
                    fee_profile=profiles[profile_id],
                    slippage=slippage,
                    limits=limits,
                    min_order_eur=_b.min_order_eur,
                    fee_budget_pct_month=_b.fee_budget_pct_month,
                )

            sp = simulate(
                bench_data, params("degiro_etf_core", bh_lim), BuyHoldPolicy(SP500_TICKER)
            )
            sp_ret = _ret(sp, b.capital_eur)
            aetf = simulate(bench_data, params(b.fee_profile, bh_lim), BuyHoldPolicy(AETF_TICKER))
            rnd = [
                simulate(data, params(b.fee_profile, base_lim), RandomPolicy(cfg.p_swap), seed=k)
                for k in range(cfg.n_seeds)
            ]
            rnd_ret = [_ret(r, b.capital_eur) for r in rnd]
            mom = simulate(data, params(b.fee_profile, base_lim), MomentumPolicy())
            eq = simulate(data, params(b.fee_profile, div_lim), EqualWeightPolicy())
            entry = {
                "sp500_return": sp_ret,
                "bh_aetf": {
                    "return": _ret(aetf, b.capital_eur),
                    "fees": aetf.fees_paid + aetf.tax_paid,
                },
                "random": {
                    **_stats(rnd_ret, sp_ret),
                    "mean_fees": float(np.mean([r.fees_paid + r.tax_paid for r in rnd])),
                    "mean_trades": float(np.mean([r.n_trades for r in rnd])),
                },
                "momentum": {
                    "return": _ret(mom, b.capital_eur),
                    "fees": mom.fees_paid + mom.tax_paid,
                    "trades": mom.n_trades,
                },
                "equal_weight": {
                    "return": _ret(eq, b.capital_eur),
                    "fees": eq.fees_paid + eq.tax_paid,
                    "positions": len(eq.positions),
                    "blocked": eq.n_blocked,
                },
            }
            win["books"][b.id] = entry
            pooled[b.id]["random"].extend(r - sp_ret for r in rnd_ret)
            pooled[b.id]["momentum"].append(entry["momentum"]["return"] - sp_ret)
            pooled[b.id]["equal_weight"].append(entry["equal_weight"]["return"] - sp_ret)
            pooled[b.id]["bh_aetf"].append(entry["bh_aetf"]["return"] - sp_ret)
        windows.append(win)
        if w_i % 10 == 0:
            log.info("window %d/%d done (%s)", w_i + 1, len(starts), dates[0])
    summary = {
        b.id: {
            k: {**_stats(v, 0.0), "p_beat_sp500": float(np.mean(np.array(v) > 0)) if v else None}
            for k, v in pooled[b.id].items()
        }
        for b in books
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "params": {
            "start": cfg.start.isoformat(),
            "end": cfg.end.isoformat(),
            "window_days": cfg.window_days,
            "step_days": cfg.step_days,
            "n_seeds": cfg.n_seeds,
            "p_swap": cfg.p_swap,
            "benchmark": SP500_TICKER,
            "fee_profiles": {b.id: b.fee_profile for b in books},
        },
        "caveats": [
            "Survivorship bias: Yahoo drops delisted names; the random-picker null is optimistic.",
            "ALPHA.AT history on Yahoo starts 2025-07; it is absent from earlier windows.",
            "GD.AT is a price index (no dividends); AETF.AT is the investable comparison.",
            "Rule-based strategies only; LLM arms are never backtested on history.",
        ],
        "n_windows": len(windows),
        "windows": windows,
        "pooled_excess_vs_sp500": summary,
        "random_excess_samples": {k: _downsample(v["random"]) for k, v in pooled.items()},
    }


def _downsample(values: list[float], cap: int = 5000) -> list[float]:
    if len(values) <= cap:
        return [round(v, 5) for v in values]
    step = len(values) / cap
    return [round(values[int(i * step)], 5) for i in range(cap)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2011-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--window", type=int, default=63)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--prices", default="data/prices")
    ap.add_argument("--actions", default="data/actions")
    ap.add_argument("--out", default="docs/data/historical.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repo = ConfigRepo()
    end = (
        date.fromisoformat(args.end) if args.end else cal.last_completed_session(datetime.now(UTC))
    )
    cfg = StudyConfig(
        start=date.fromisoformat(args.start),
        end=end,
        window_days=args.window,
        step_days=args.step,
        n_seeds=args.seeds,
    )
    result = run_historical(
        repo, PriceStore(Path(args.prices)), ActionsStore(Path(args.actions)), cfg
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"{result['n_windows']} windows -> {out}")
    for book_id, s in result["pooled_excess_vs_sp500"].items():
        r = s["random"]
        print(
            f"  {book_id}: random picker beats S&P in {r['p_beat_sp500']:.1%} of windows; "
            f"median excess {r['p50']:+.2%}; momentum median {s['momentum']['p50']:+.2%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
