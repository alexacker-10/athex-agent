"""Day-by-day driver over stored prices: fill -> corporate actions -> decide -> record NAV.

Used for plumbing dry runs and tests. The live jobs (stage 6) perform the same steps, one day at
a time, with real clocks and git commits in between.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from athex_agent.arms.runner import ArmRunner
from athex_agent.config.models import UniverseConfig
from athex_agent.data import calendar as cal
from athex_agent.data.universe import select_universe


def simulate_arm(
    runner: ArmRunner,
    universe_cfg: UniverseConfig | None,
    start: date,
    end: date,
    extra: dict[str, Any] | None = None,
) -> pd.DataFrame:
    days = cal.trading_days(start, end)
    if not days:
        raise ValueError("no trading days in range")
    if not runner.started():
        runner.start(days[0])
    members: list[str] = []
    month: tuple[int, int] | None = None
    for d in days:
        runner.fill(now_utc=cal.session_open_utc(d) + timedelta(hours=1), today=d)
        runner.apply_corporate_actions(d)
        if universe_cfg is not None and (d.year, d.month) != month:
            members = select_universe(universe_cfg, runner.store.as_of(d)).members
            month = (d.year, d.month)
        runner.decide(d, cal.session_close_utc(d) + timedelta(hours=1), members, extra)
        runner.record_nav(d)
    frames = []
    for b in runner.resolved.books:
        df = pd.DataFrame(runner.nav_series(b.id))
        df["book_id"] = b.id
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    for c in ("nav", "cash", "invested", "fees_cum", "tax_cum", "slippage_cum", "dividends_cum"):
        out[c] = out[c].astype(float)
    out["positions"] = out["positions"].astype(int)
    return out
