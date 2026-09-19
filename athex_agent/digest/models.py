"""The shared daily digest and the schema the cheap model fills in."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from athex_agent.config.models import SourceToggles, StrictModel
from athex_agent.data.news import Category

EventType = Literal[
    "results",
    "guidance",
    "dividend",
    "capital_action",
    "m_and_a",
    "contract",
    "regulatory",
    "management",
    "macro",
    "sector",
    "rumor",
    "other",
]


class DigestItemOut(BaseModel):
    """What the model returns per item (provenance fields are filled in by us, never by it)."""

    id: str
    tickers: list[str] = []
    summary_en: str = Field(description="<= 60 words, English, keep Greek proper names")
    sentiment: int = Field(ge=-2, le=2)
    event_type: EventType
    importance: int = Field(ge=1, le=5)
    red_flags: str = ""


class DigestOutput(BaseModel):
    items: list[DigestItemOut]
    macro_summary: str = Field(description="<= 120 words on ECB, rates, spreads, ratings, flows")
    market_summary: str = Field(description="<= 80 words on the Athens market as a whole")


class DigestItem(StrictModel):
    id: str
    source_id: str
    category: Category
    reliability: int = Field(ge=1, le=5)
    url: str
    title: str
    published: datetime | None = None
    tickers: list[str] = []
    summary_en: str = ""
    sentiment: int = 0
    event_type: EventType = "other"
    importance: int = 2
    red_flags: str = ""
    has_body: bool = False


class Digest(StrictModel):
    date: str
    generated_at: datetime
    model: str
    items: list[DigestItem] = []
    macro_summary: str = ""
    market_summary: str = ""
    stats: dict[str, Any] = {}
    cost: dict[str, float] = {}
    warnings: list[str] = []

    def view(self, toggles: SourceToggles) -> list[DigestItem]:
        allowed: set[str] = set()
        if toggles.official:
            allowed.add("official")
        if toggles.news:
            allowed.add("news")
        if toggles.macro:
            allowed.add("macro")
        if toggles.rumors:
            allowed.add("rumor")
        return [it for it in self.items if it.category in allowed]

    @property
    def digest_id(self) -> str:
        return f"digest:{self.date}:{self.generated_at.strftime('%H%M%S')}"
