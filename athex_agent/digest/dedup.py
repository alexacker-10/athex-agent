"""Near-duplicate removal for headlines syndicated across Greek outlets."""

from __future__ import annotations

import re
import unicodedata

from athex_agent.data.news import NewsItem, canonical_url

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def fold(text: str) -> str:
    """Lowercase, strip accents (Greek tonos included), keep letters only."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def shingles(text: str, n: int = 3) -> set[str]:
    words = _WORD.findall(fold(text))
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup(items: list[NewsItem], threshold: float = 0.6) -> tuple[list[NewsItem], int]:
    """Keep one item per story; prefer higher reliability, then earlier publication."""
    ordered = sorted(items, key=lambda it: (-it.reliability, it.published or it.fetched_at))
    kept: list[NewsItem] = []
    kept_urls: set[str] = set()
    kept_shingles: list[set[str]] = []
    removed = 0
    for it in ordered:
        cu = canonical_url(it.url) if it.url else ""
        sh = shingles(it.title)
        if (cu and cu in kept_urls) or any(jaccard(sh, k) >= threshold for k in kept_shingles):
            removed += 1
            continue
        kept.append(it)
        if cu:
            kept_urls.add(cu)
        kept_shingles.append(sh)
    return kept, removed
