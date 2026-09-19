"""What a decider produces for one decision date: per-stock views and proposed actions.

Views are the primary statistical instrument (scored on forward excess returns); actions are what
the rules layer turns into per-book orders under the hard limits.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field

from athex_agent.config.models import StrictModel

ViewLabel = Literal["strong_sell", "sell", "neutral", "buy", "strong_buy"]
VIEW_SCORE: dict[str, int] = {
    "strong_sell": -2,
    "sell": -1,
    "neutral": 0,
    "buy": 1,
    "strong_buy": 2,
}


class View(StrictModel):
    ticker: str
    view: ViewLabel
    conviction: float = Field(ge=0, le=1)
    horizon_days: int = Field(default=20, ge=1, le=250)
    thesis: str = ""
    catalysts: list[str] = []
    risks: list[str] = []
    cited_item_ids: list[str] = []

    @property
    def score(self) -> int:
        return VIEW_SCORE[self.view]


class Action(StrictModel):
    ticker: str
    kind: Literal["buy", "sell"]
    reason: str = ""
    weight: float | None = Field(default=None, gt=0, le=1)  # target NAV weight for buys


class Proposal(StrictModel):
    arm_id: str
    decision_date: date
    views: list[View] = []
    actions: list[Action] = []
    regime_note: str = ""
    journal_updates: dict[str, Any] = {}
    meta: dict[str, Any] = {}

    @property
    def buys(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "buy"]

    @property
    def sells(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "sell"]
