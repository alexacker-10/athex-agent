"""Per-arm persistent journal: running theses, lessons, closed trades. Fed back within a budget."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Stance = Literal["strong_sell", "sell", "neutral", "buy", "strong_buy"]
MAX_THESES = 12
MAX_LESSONS = 10
MAX_CLOSED = 20
MAX_WORDS_THESIS = 80


class ThesisEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    thesis: str
    stance: Stance = "neutral"
    since: date
    updated: date


class ClosedTrade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    opened: date
    closed: date
    return_pct: float
    hold_trading_days: int
    reason: str = ""
    lesson: str = ""


class ThesisUpdate(BaseModel):
    ticker: str
    thesis: str = Field(max_length=800)
    stance: Stance = "neutral"


class JournalUpdates(BaseModel):
    """What the model may change each day. Everything is bounded and truncated on apply."""

    theses: list[ThesisUpdate] = []
    drop_theses: list[str] = []
    lessons_add: list[str] = []
    lessons_drop: list[str] = []
    notes: str | None = Field(default=None, max_length=1500)
    closed_trade_lessons: dict[str, str] = {}  # "TICKER@closed_date" -> lesson


class Journal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arm_id: str
    theses: dict[str, ThesisEntry] = {}
    lessons: list[str] = []
    closed_trades: list[ClosedTrade] = []
    notes: str = ""
    updated: date | None = None

    @classmethod
    def load(cls, path: Path, arm_id: str) -> Journal:
        p = Path(path)
        if p.exists():
            return cls.model_validate_json(p.read_text(encoding="utf-8"))
        return cls(arm_id=arm_id)

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def add_closed_trade(self, trade: ClosedTrade) -> None:
        self.closed_trades.append(trade)
        self.closed_trades = self.closed_trades[-MAX_CLOSED:]

    def apply_updates(self, upd: JournalUpdates, on: date, allowed_tickers: set[str]) -> None:
        for t in upd.theses:
            if t.ticker not in allowed_tickers:
                continue
            words = t.thesis.split()
            thesis = " ".join(words[:MAX_WORDS_THESIS])
            prev = self.theses.get(t.ticker)
            self.theses[t.ticker] = ThesisEntry(
                ticker=t.ticker,
                thesis=thesis,
                stance=t.stance,
                since=prev.since if prev else on,
                updated=on,
            )
        for tk in upd.drop_theses:
            self.theses.pop(tk, None)
        if len(self.theses) > MAX_THESES:  # keep the most recently updated
            keep = sorted(self.theses.values(), key=lambda e: e.updated, reverse=True)[:MAX_THESES]
            self.theses = {e.ticker: e for e in keep}
        for lesson in upd.lessons_drop:
            self.lessons = [ls for ls in self.lessons if ls != lesson]
        for lesson in upd.lessons_add:
            lesson = " ".join(lesson.split()[:40])
            if lesson and lesson not in self.lessons:
                self.lessons.append(lesson)
        self.lessons = self.lessons[-MAX_LESSONS:]
        if upd.notes is not None:
            self.notes = " ".join(upd.notes.split()[:200])
        for key, lesson in upd.closed_trade_lessons.items():
            for ct in self.closed_trades:
                if f"{ct.ticker}@{ct.closed.isoformat()}" == key:
                    ct.lesson = " ".join(lesson.split()[:40])
        self.updated = on

    def render(self, max_chars: int = 9000) -> str:
        parts = []
        if self.notes:
            parts.append(f"NOTES: {self.notes}")
        if self.lessons:
            parts.append("LESSONS:\n" + "\n".join(f"- {ls}" for ls in self.lessons))
        if self.theses:
            parts.append(
                "THESES:\n"
                + "\n".join(
                    f"- {e.ticker} [{e.stance}, since {e.since.isoformat()}]: {e.thesis}"
                    for e in sorted(self.theses.values(), key=lambda e: e.ticker)
                )
            )
        if self.closed_trades:
            parts.append(
                "CLOSED TRADES (most recent last):\n"
                + "\n".join(
                    f"- {c.ticker} {c.opened.isoformat()}->{c.closed.isoformat()} "
                    f"{c.return_pct:+.1%} after {c.hold_trading_days}d; {c.reason}"
                    + (f"; lesson: {c.lesson}" if c.lesson else "")
                    for c in self.closed_trades[-MAX_CLOSED:]
                )
            )
        text = "\n\n".join(parts) if parts else "(empty journal)"
        return text[-max_chars:] if len(text) > max_chars else text
