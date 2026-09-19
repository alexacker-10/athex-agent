"""Attach tickers to news items by company-name aliases (accent/case-insensitive, whole words)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from athex_agent.data.news import NewsItem
from athex_agent.digest.dedup import fold

MACRO_TERMS = [
    "εκτ",
    "ecb",
    "επιτόκι",
    "ομόλογ",
    "spread",
    "moody",
    "fitch",
    "s&p",
    "αξιολόγησ",
    "rating",
    "msci",
    "ftse russell",
    "αναβάθμισ",
    "υποβάθμισ",
    "πληθωρισμ",
    "inflation",
    "τράπεζ",
    "χρηματιστήρι",
    "γενικός δείκτης",
    "γδ",
    "athex",
    "euronext athens",
    "eurogroup",
    "δημοσιονομ",
    "ανάπτυξη",
    "gdp",
    "αεπ",
]


class TickerMatcher:
    def __init__(self, aliases: dict[str, list[str]]) -> None:
        self.patterns: list[tuple[str, re.Pattern[str]]] = []
        for ticker, names in aliases.items():
            for name in names:
                f = fold(str(name))
                if len(f) < 3:
                    continue
                self.patterns.append((ticker, re.compile(rf"(?<!\w){re.escape(f)}(?!\w)")))
        self.macro = [fold(t) for t in MACRO_TERMS]

    @classmethod
    def from_yaml(cls, path: Path) -> TickerMatcher:
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def match(self, text: str) -> list[str]:
        f = fold(text)
        found = {t for t, p in self.patterns if p.search(f)}
        return sorted(found)

    def is_macro(self, text: str) -> bool:
        f = fold(text)
        return any(term in f for term in self.macro)

    def tag(self, items: list[NewsItem]) -> list[NewsItem]:
        out = []
        for it in items:
            tickers = self.match(f"{it.title} {it.summary}")
            out.append(it.model_copy(update={"tickers": tickers}))
        return out
