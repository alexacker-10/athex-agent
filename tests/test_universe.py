from __future__ import annotations

from datetime import date

from athex_agent.config.models import UniverseConfig
from athex_agent.data.universe import select_universe
from tests.conftest import synthetic_store


def test_select_universe_applies_every_filter(tmp_path):
    end = date(2026, 9, 21)
    store = synthetic_store(
        tmp_path / "p", {"BIG.AT": 10.0, "PENNY.AT": 0.2, "ETF.AT": 60.0}, end=end, volume=100_000.0
    )
    # a thin name and a young name
    synthetic_store(tmp_path / "p", {"THIN.AT": 10.0}, end=end, volume=1_000.0)
    synthetic_store(tmp_path / "p", {"YOUNG.AT": 10.0}, end=end, n=50, volume=100_000.0)
    cfg = UniverseConfig(
        id="base",
        description="t",
        min_adv_eur=300_000,
        min_price_eur=0.30,
        min_history_trading_days=120,
        adv_window_days=60,
        include_always=["ETF.AT"],
        candidates=["BIG.AT", "PENNY.AT", "THIN.AT", "YOUNG.AT", "OLD.AT", "MISSING.AT"],
        aliases={"OLD.AT": "BIG.AT"},
    )
    snap = select_universe(cfg, store.as_of(end))
    assert snap.members == ["BIG.AT", "ETF.AT"]
    assert snap.excluded["PENNY.AT"].startswith("price")
    assert snap.excluded["THIN.AT"].startswith("median ADV")
    assert snap.excluded["YOUNG.AT"].startswith("history")
    assert snap.excluded["MISSING.AT"] == "no price data"
    assert "OLD.AT" not in snap.excluded  # alias resolved to BIG.AT
    assert snap.adv_eur["BIG.AT"] == 1_000_000.0
