from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import LookaheadError, NoPriceData, PriceStore


def bars(start: date, n: int, base: float = 10.0) -> pd.DataFrame:
    days = [start + timedelta(days=i) for i in range(n)]
    df = pd.DataFrame(
        {
            "open": [base + i for i in range(n)],
            "high": [base + i + 0.5 for i in range(n)],
            "low": [base + i - 0.5 for i in range(n)],
            "close": [base + i + 0.2 for i in range(n)],
            "volume": [1000.0 * (i + 1) for i in range(n)],
        },
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="date"),
    )
    return df


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> PriceStore:
    return PriceStore(tmp_path / "prices")


def test_upsert_marks_newest_bars_provisional(store):
    n = store.upsert("ETE.AT", bars(date(2026, 9, 14), 10), "test", NOW)
    assert n == 10
    df = store.load("ETE.AT")
    assert len(df) == 10
    assert df["settled"].tolist() == [True] * 7 + [False] * 3
    assert store.latest_date("ETE.AT") == date(2026, 9, 23)
    assert store.tickers() == ["ETE.AT"]


def test_provisional_replaced_settled_immutable(store):
    store.upsert("ETE.AT", bars(date(2026, 9, 14), 10), "test", NOW)
    again = bars(date(2026, 9, 14), 10)
    again.loc[pd.Timestamp(date(2026, 9, 23)), "close"] = 99.0  # provisional bar revised
    again.loc[pd.Timestamp(date(2026, 9, 14)), "close"] = 1.0  # settled bar "revised" upstream
    n = store.upsert("ETE.AT", again, "test", NOW)
    df = store.load("ETE.AT")
    assert df.at[pd.Timestamp(date(2026, 9, 23)), "close"] == 99.0
    assert df.at[pd.Timestamp(date(2026, 9, 14)), "close"] == pytest.approx(10.2)
    rev = store.revisions()
    assert len(rev) == 2
    applied = dict(zip(rev["date"], rev["applied"], strict=True))
    assert applied["2026-09-23"] and not applied["2026-09-14"]
    assert n >= 1


def test_unchanged_refetch_writes_nothing_new_except_settling(store):
    store.upsert("ETE.AT", bars(date(2026, 9, 14), 10), "test", NOW)
    n = store.upsert("ETE.AT", bars(date(2026, 9, 14), 10), "test", NOW)
    assert n == 0
    # three more days arrive: the old provisional bars become settled
    n = store.upsert("ETE.AT", bars(date(2026, 9, 14), 13), "test", NOW)
    df = store.load("ETE.AT")
    assert n == 6  # 3 new + 3 newly settled
    assert df["settled"].tolist() == [True] * 10 + [False] * 3
    assert store.revisions().empty


def test_nan_or_zero_close_rows_are_dropped(store):
    df = bars(date(2026, 9, 14), 5)
    df.loc[pd.Timestamp(date(2026, 9, 18)), "close"] = float("nan")
    df.loc[pd.Timestamp(date(2026, 9, 17)), "close"] = 0.0
    assert store.upsert("ETE.AT", df, "test", NOW) == 3
    assert store.latest_date("ETE.AT") == date(2026, 9, 16)


def test_as_of_view_cannot_see_the_future(store):
    store.upsert("ETE.AT", bars(date(2026, 9, 14), 12), "test", NOW)  # through 2026-09-25
    view = store.as_of(date(2026, 9, 22))
    df = view.bars("ETE.AT")
    assert df.index[-1].date() == date(2026, 9, 22)
    assert view.last_bar("ETE.AT").date == date(2026, 9, 22)
    assert view.close("ETE.AT") == pytest.approx(10.2 + 8)
    assert view.close("ETE.AT", date(2026, 9, 20)) == pytest.approx(10.2 + 6)
    with pytest.raises(LookaheadError):
        view.close("ETE.AT", date(2026, 9, 23))
    with pytest.raises(LookaheadError):
        view.bars("ETE.AT", end=date(2026, 9, 23))
    with pytest.raises(LookaheadError):
        view.has_bar("ETE.AT", date(2026, 9, 23))
    assert view.has_bar("ETE.AT", date(2026, 9, 22))
    assert view.history_length("ETE.AT") == 9
    # the fill engine's accessor is on the store, not the view, and returns the next open
    assert store.session_open("ETE.AT", date(2026, 9, 23)) == pytest.approx(10.0 + 9)
    assert store.session_open("ETE.AT", date(2026, 10, 30)) is None
    with pytest.raises(NoPriceData):
        view.close("ZZZ.AT")
    assert view.closes(["ETE.AT"]) == {"ETE.AT": pytest.approx(18.2)}


def test_adv_metrics(store):
    store.upsert("ETE.AT", bars(date(2026, 9, 1), 30), "test", NOW)
    view = store.as_of(date(2026, 9, 30))
    df = view.bars("ETE.AT").tail(20)
    assert view.adv_eur("ETE.AT", 20) == pytest.approx(float((df["close"] * df["volume"]).mean()))
    assert view.median_adv_eur("ETE.AT", 60) == pytest.approx(
        float((view.bars("ETE.AT")["close"] * view.bars("ETE.AT")["volume"]).median())
    )
    assert pd.isna(store.as_of(date(2026, 9, 3)).adv_eur("ETE.AT", 20))


def test_actions_store(tmp_path):
    acts = ActionsStore(tmp_path / "actions")
    df = pd.DataFrame(
        {"kind": ["dividend", "split", "dividend"], "value": [0.5, 2.0, 0.0]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-10-01"), pd.Timestamp("2026-11-02"), pd.Timestamp("2026-12-01")],
            name="date",
        ),
    )
    assert acts.upsert("BELA.AT", df, "test") == 2  # zero-value dividend ignored
    assert acts.upsert("BELA.AT", df, "test") == 0  # duplicates ignored
    ev = acts.between("BELA.AT", date(2026, 9, 30), date(2026, 11, 2))
    assert [(e["date"], e["kind"], e["value"]) for e in ev] == [
        (date(2026, 10, 1), "dividend", 0.5),
        (date(2026, 11, 2), "split", 2.0),
    ]
    assert acts.between("BELA.AT", date(2026, 10, 1), date(2026, 11, 1)) == []
    assert (
        acts.between("BELA.AT", date(2026, 9, 1), date(2026, 12, 31), kind="split")[0]["value"]
        == 2.0
    )
    assert acts.between("NOPE.AT", date(2026, 9, 1), date(2026, 12, 31)) == []
