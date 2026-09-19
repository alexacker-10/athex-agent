from __future__ import annotations

from datetime import date

from athex_agent.arms.journal import ClosedTrade, Journal, JournalUpdates, ThesisUpdate


def test_journal_updates_are_bounded_and_filtered(tmp_path):
    j = Journal(arm_id="A")
    upd = JournalUpdates(
        theses=[
            ThesisUpdate(ticker="ETE.AT", thesis=" ".join(["word"] * 120), stance="buy"),
            ThesisUpdate(ticker="NOPE.AT", thesis="not in universe"),
        ],
        lessons_add=["Fees dominate at 1k"] * 3 + [f"lesson {i}" for i in range(12)],
        notes="n " * 400,
    )
    j.apply_updates(upd, date(2026, 10, 5), {"ETE.AT", "PPC.AT"})
    assert list(j.theses) == ["ETE.AT"] and len(j.theses["ETE.AT"].thesis.split()) == 80
    assert j.theses["ETE.AT"].since == date(2026, 10, 5)
    assert len(j.lessons) == 10 and j.lessons.count("Fees dominate at 1k") == 0  # dropped by cap
    assert len(j.notes.split()) == 200
    j.apply_updates(
        JournalUpdates(theses=[ThesisUpdate(ticker="ETE.AT", thesis="v2", stance="sell")]),
        date(2026, 10, 6),
        {"ETE.AT"},
    )
    assert j.theses["ETE.AT"].since == date(2026, 10, 5) and j.theses["ETE.AT"].updated == date(
        2026, 10, 6
    )
    j.apply_updates(
        JournalUpdates(drop_theses=["ETE.AT"], lessons_drop=["lesson 11"]), date(2026, 10, 7), set()
    )
    assert j.theses == {} and "lesson 11" not in j.lessons
    j.add_closed_trade(
        ClosedTrade(
            ticker="PPC.AT",
            opened=date(2026, 8, 3),
            closed=date(2026, 10, 1),
            return_pct=0.052,
            hold_trading_days=42,
            reason="thesis played out",
        )
    )
    j.apply_updates(
        JournalUpdates(closed_trade_lessons={"PPC.AT@2026-10-01": "size winners"}),
        date(2026, 10, 8),
        set(),
    )
    assert j.closed_trades[0].lesson == "size winners"
    text = j.render()
    assert "PPC.AT 2026-08-03->2026-10-01 +5.2% after 42d" in text and "LESSONS" in text
    assert len(j.render(max_chars=50)) == 50
    j.save(tmp_path / "journal.json")
    assert Journal.load(tmp_path / "journal.json", "A") == j
    assert Journal.load(tmp_path / "missing.json", "B").arm_id == "B"
