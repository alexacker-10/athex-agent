"""Daily digest pipeline: ingest -> store -> dedup -> tag -> filter -> summarise once -> save.

Runs once per day and is shared by every arm; arms only ever see `Digest.view(toggles)`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from athex_agent.data.articles import fetch_article
from athex_agent.data.news import (
    IngestReport,
    NewsItem,
    NewsStore,
    RobotsCache,
    SourcesConfig,
    ingest,
)
from athex_agent.digest.dedup import dedup
from athex_agent.digest.models import Digest, DigestItem, DigestItemOut, DigestOutput
from athex_agent.digest.tickers import TickerMatcher
from athex_agent.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

DIGEST_MODEL = "claude-haiku-4-5"
PROMPT_VERSION = "digest-v1"

SYSTEM_PROMPT = """You are the news desk for a paper-trading research system on the Athens Stock
Exchange.
You read Greek natively. You receive today's raw items (title, summary, sometimes body) from Greek
and English sources, each tagged with a source id, a reliability tier (5 = official/primary, 4 =
established financial press, 3 = general press, 2 = opinionated/aggregator, 1 = anonymous social
media) and a category (official, news, macro, rumor).

For every item that is relevant to a listed Greek company in the provided universe, to Greek
banks, to the Athens market or to Greek/euro-area macro conditions, return one entry:
- id: copy the item id exactly.
- tickers: only symbols from the provided universe list that the item is about (empty if none).
- summary_en: at most 60 words in English; keep Greek proper names; state facts, numbers and dates;
  never add information that is not in the item.
- sentiment: -2 (clearly negative for the company/market) to +2 (clearly positive), 0 if neutral.
- event_type: one of results, guidance, dividend, capital_action, m_and_a, contract, regulatory,
  management, macro, sector, rumor, other.
- importance: 1 (noise) to 5 (moves the stock or the market).
- red_flags: for reliability 1-2 items, note signs of promotion or manipulation (anonymous
  claims, urgency, price targets without sources, thin stocks); otherwise empty.
Skip items that are irrelevant (sport, lifestyle, foreign companies with no Greek angle).
Then write macro_summary (<= 120 words: ECB, rates, Greek bond spreads, ratings, index flows,
bank sector) and market_summary (<= 80 words on the Athens market as a whole today).
Be terse and factual. Do not speculate beyond the sources."""


@dataclass
class DigestBuildConfig:
    max_items_to_model: int = 250
    max_bodies: int = 20
    fetch_bodies: bool = True
    lookback_hours: int = 36
    model: str = DIGEST_MODEL
    max_output_tokens: int = 16000
    estimate_eur: float = 0.15


def _relevant(items: list[NewsItem], matcher: TickerMatcher) -> list[NewsItem]:
    out = []
    for it in items:
        text = f"{it.title} {it.summary}"
        if it.category == "macro" or it.tickers or matcher.is_macro(text):
            out.append(it)
    return out


def _rank(items: list[NewsItem]) -> list[NewsItem]:
    def key(it: NewsItem):
        return (-it.reliability, -len(it.tickers), -(it.published or it.fetched_at).timestamp())

    return sorted(items, key=key)


def _user_payload(items: list[NewsItem], universe: list[str]) -> str:
    rows = []
    for it in items:
        row: dict[str, Any] = {
            "id": it.id,
            "source": it.source_id,
            "reliability": it.reliability,
            "category": it.category,
            "published": it.published.isoformat() if it.published else None,
            "title": it.title,
            "summary": it.summary[:600],
            "matched_tickers": it.tickers,
        }
        if it.body:
            row["body"] = it.body[:4000]
        rows.append(row)
    return json.dumps({"universe": universe, "items": rows}, ensure_ascii=False)


def _mechanical_output(items: list[NewsItem]) -> DigestOutput:
    """Stand-in when the model is unavailable (dry run or failure): titles only, no judgement."""
    return DigestOutput(
        items=[
            DigestItemOut(
                id=it.id,
                tickers=it.tickers,
                summary_en=it.title,
                sentiment=0,
                event_type="macro" if it.category == "macro" else "other",
                importance=2,
            )
            for it in items
        ],
        macro_summary="(no model summary)",
        market_summary="(no model summary)",
    )


def build_digest(
    day: str,
    sources_cfg: SourcesConfig,
    news_store: NewsStore,
    matcher: TickerMatcher,
    llm: LLMClient,
    universe: list[str],
    out_dir: Path,
    cfg: DigestBuildConfig | None = None,
    reddit_credentials: tuple[str | None, str | None] = (None, None),
    google_queries: list[str] | None = None,
    report: IngestReport | None = None,
    robots: RobotsCache | None = None,
) -> Digest:
    cfg = cfg or DigestBuildConfig()
    robots = robots or RobotsCache(sources_cfg.user_agent)
    warnings: list[str] = []
    if report is None:
        report = ingest(
            sources_cfg,
            reddit_credentials=reddit_credentials,
            google_queries=google_queries,
            robots=robots,
        )
    for s in report.failed:
        warnings.append(f"source {s.source_id} failed: {s.error}")
    cutoff = (
        datetime.fromisoformat(day).replace(tzinfo=UTC)
        + timedelta(days=1)
        - timedelta(hours=cfg.lookback_hours)
    )
    fresh = [it for it in report.items if it.published is None or it.published >= cutoff]
    new = news_store.append(day, fresh)
    deduped, n_dup = dedup(new)
    tagged = matcher.tag(deduped)
    relevant = _rank(_relevant(tagged, matcher))[: cfg.max_items_to_model]
    if cfg.fetch_bodies and not llm.dry_run:
        for i, it in enumerate(relevant[: cfg.max_bodies]):
            body = fetch_article(it.url, sources_cfg.user_agent, robots) if it.url else None
            if body:
                relevant[i] = it.model_copy(update={"body": body})
    by_id = {it.id: it for it in relevant}
    model_used = cfg.model
    cost: dict[str, float] = {"usd": 0.0, "eur": 0.0, "input_tokens": 0, "output_tokens": 0}
    output: DigestOutput | None = None
    if relevant:
        for attempt in (1, 2):
            try:
                res = llm.parse(
                    model=cfg.model,
                    system=SYSTEM_PROMPT,
                    user=_user_payload(relevant, universe),
                    output_model=DigestOutput,
                    max_tokens=cfg.max_output_tokens,
                    purpose="digest",
                    estimate_eur=cfg.estimate_eur,
                    dry_run_factory=lambda: _mechanical_output(relevant),
                )
                output = res.parsed
                cost = {
                    "usd": round(cost["usd"] + res.cost_usd, 6),
                    "eur": round(cost["eur"] + res.cost_eur, 6),
                    "input_tokens": cost["input_tokens"] + res.usage.input_tokens,
                    "output_tokens": cost["output_tokens"] + res.usage.output_tokens,
                }
                if res.dry_run:
                    model_used = "dry-run"
                break
            except LLMError as exc:
                warnings.append(f"digest model attempt {attempt} failed: {exc}")
                log.warning("digest model attempt %d failed: %s", attempt, exc)
        if output is None:
            output = _mechanical_output(relevant)
            model_used = "mechanical-fallback"
            warnings.append("LLM digest unavailable; items carry titles only")
    else:
        output = DigestOutput(items=[], macro_summary="", market_summary="")
    universe_set = set(universe)
    items: list[DigestItem] = []
    seen: set[str] = set()
    for o in output.items:
        src = by_id.get(o.id)
        if src is None or o.id in seen:
            continue
        seen.add(o.id)
        tickers = sorted(set(src.tickers) | {t for t in o.tickers if t in universe_set})
        items.append(
            DigestItem(
                id=src.id,
                source_id=src.source_id,
                category=src.category,
                reliability=src.reliability,
                url=src.url,
                title=src.title,
                published=src.published,
                tickers=tickers,
                summary_en=o.summary_en[:600],
                sentiment=o.sentiment,
                event_type=o.event_type,
                importance=o.importance,
                red_flags=o.red_flags[:300],
                has_body=bool(src.body),
            )
        )
    digest = Digest(
        date=day,
        generated_at=datetime.now(UTC),
        model=model_used,
        items=items,
        macro_summary=output.macro_summary[:1200],
        market_summary=output.market_summary[:800],
        stats={
            "n_fetched": len(report.items),
            "n_fresh": len(fresh),
            "n_new": len(new),
            "n_duplicates": n_dup,
            "n_relevant": len(relevant),
            "n_summarized": len(items),
            "sources": [s.__dict__ for s in report.statuses],
            "prompt_version": PROMPT_VERSION,
        },
        cost=cost,
        warnings=warnings,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day}.json").write_text(digest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return digest


def load_digest(out_dir: Path, day: str) -> Digest | None:
    p = Path(out_dir) / f"{day}.json"
    return Digest.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None
