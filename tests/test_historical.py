from __future__ import annotations

import json
from datetime import date

import numpy as np

from athex_agent.analysis.historical import StudyConfig, run_historical
from athex_agent.config.models import UniverseConfig
from athex_agent.data.actions import ActionsStore
from tests.test_fastsim import wavy_store


def test_run_historical_small(tmp_path, repo):
    store = wavy_store(tmp_path / "p")
    # benchmarks: flat S&P, rising Greek ETF, index level
    from tests.conftest import synthetic_store

    synthetic_store(
        tmp_path / "p",
        {"SXR8.DE": 700.0, "AETF.AT": 60.0, "GD.AT": 2600.0},
        end=date(2026, 12, 18),
        n=220,
    )
    actions = ActionsStore(tmp_path / "a")
    ucfg = UniverseConfig(
        id="base",
        description="t",
        min_adv_eur=100_000,
        min_price_eur=0.3,
        min_history_trading_days=100,
        adv_window_days=60,
        candidates=[f"W{i}.AT" for i in range(6)],
        include_always=["AETF.AT"],
    )
    cfg = StudyConfig(
        start=date(2026, 8, 3), end=date(2026, 12, 18), window_days=40, step_days=30, n_seeds=4
    )
    res = run_historical(repo, store, actions, cfg, universe_cfg=ucfg)
    assert res["n_windows"] == len(res["windows"]) >= 2
    w = res["windows"][0]
    assert w["universe_size"] == 6 and w["days"] == 40
    for book_id in ("10k", "1k"):
        e = w["books"][book_id]
        assert e["random"]["n"] == 4 and 0.0 <= e["random"]["p_beat_sp500"] <= 1.0
        assert np.isfinite(e["random"]["p50"]) and np.isfinite(e["momentum"]["return"])
        assert e["sp500_return"] < 0  # flat price, ETF fee and spread
        if book_id == "10k":
            assert e["equal_weight"]["positions"] == 6
        else:  # a 10% slice of EUR 1,000 is below the EUR 150 minimum order: unaffordable
            assert e["equal_weight"]["positions"] == 0 and e["equal_weight"]["blocked"] >= 6
    pooled = res["pooled_excess_vs_sp500"]["10k"]["random"]
    assert pooled["n"] == 4 * res["n_windows"] and "p_beat_sp500" in pooled
    assert len(res["random_excess_samples"]["1k"]) == pooled["n"]
    json.dumps(res)  # serialisable
    assert any("Survivorship" in c for c in res["caveats"])
