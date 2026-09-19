from __future__ import annotations

import pytest

from athex_agent.analysis.fee_drag import annual_drag_table, fee_drag_metric, per_order_table


def test_per_order_table_matches_design(repo):
    rows = {(r["profile"], r["book"]): r for r in per_order_table(repo)}
    assert rows[("degiro", "10k")]["round_trip"] == pytest.approx(12.30)
    assert rows[("degiro", "1k")]["round_trip"] == pytest.approx(10.05)
    assert rows[("nbg_securities", "10k")]["round_trip"] == pytest.approx(56.62)
    assert rows[("freedom24_smart", "1k")]["round_trip"] == pytest.approx(5.05)
    assert rows[("eurobank_trader", "1k")]["round_trip"] == pytest.approx(13.73)
    assert rows[("piraeus_online", "1k")]["badge"] == "UNVERIFIED MINIMUM"
    assert rows[("degiro", "1k")]["pct_of_position"] == pytest.approx(0.0402)


def test_annual_drag_matches_design(repo):
    rows = {(r["profile"], r["book"]): r for r in annual_drag_table(repo)}
    assert rows[("degiro", "10k")]["drag_12_swaps"] == pytest.approx(0.01672, abs=1e-4)
    assert rows[("degiro", "1k")]["drag_12_swaps"] == pytest.approx(0.1402, abs=1e-3)
    assert rows[("degiro", "1k")]["drag_6_swaps"] == pytest.approx(0.0799, abs=1e-3)
    assert rows[("piraeus_online", "1k")]["fixed_fees_year"] == 16.0
    assert rows[("piraeus_online", "10k")]["fixed_fees_year"] == 16.0  # EUR 4/quarter up to 10,000


def test_fee_drag_metric_annualizes():
    rows = [
        {"nav": 10000, "fees_cum": 0, "tax_cum": 0, "slippage_cum": 0},
        {"nav": 10000, "fees_cum": 9.8, "tax_cum": 2.5, "slippage_cum": 5.0},
    ]
    rows = rows + [rows[-1]] * 124  # 126 trading days = half a year
    m = fee_drag_metric(rows)
    assert m["days"] == 126
    assert m["fees_pct_annualized"] == pytest.approx(12.3 / 10000 * 2)
    assert m["slippage_pct_annualized"] == pytest.approx(5.0 / 10000 * 2)
    assert fee_drag_metric([])["days"] == 0
