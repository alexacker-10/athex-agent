from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from athex_agent.config.models import SourceToggles
from athex_agent.data.news import IngestReport, NewsItem, NewsStore, SourceStatus, load_sources
from athex_agent.digest.build import DigestBuildConfig, build_digest, load_digest
from athex_agent.digest.models import DigestItemOut, DigestOutput
from athex_agent.digest.tickers import TickerMatcher
from athex_agent.llm.client import CostLedger, LLMClient, Pricing

REPO = Path(__file__).resolve().parents[1]
DAY = "2026-09-21"
NOW = datetime(2026, 9, 21, 18, 30, tzinfo=UTC)


def item(i, src, cat, rel, title, summary="", published=NOW):
    return NewsItem(
        id=f"id{i}",
        source_id=src,
        category=cat,
        reliability=rel,
        url=f"https://{src}.gr/{i}",
        title=title,
        summary=summary,
        published=published,
        fetched_at=NOW,
    )


def report():
    items = [
        item(1, "naftemporiki", "news", 4, "Εθνική Τράπεζα: ισχυρά αποτελέσματα τριμήνου"),
        item(2, "mononews", "news", 3, "Εθνικη Τραπεζα ισχυρα αποτελεσματα τριμηνου"),  # duplicate
        item(3, "ecb_press", "macro", 5, "ECB keeps rates unchanged"),
        item(4, "bankingnews", "rumor", 2, "Φήμες για deal της ΔΕΗ στα Βαλκάνια"),
        item(5, "insider_gr", "news", 3, "Καιρός: καύσωνας το Σαββατοκύριακο"),  # irrelevant
        item(
            6,
            "naftemporiki",
            "news",
            4,
            "Old story about Jumbo",
            published=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    ]
    return IngestReport(
        fetched_at=NOW,
        statuses=[
            SourceStatus("naftemporiki", True, 3),
            SourceStatus("euro2day_banks", False, 0, "HTTPError"),
        ],
        items=items,
    )


class FakeLLM:
    """Stands in for the model: echoes ids with a tell-tale summary; can inject bad ids."""

    def __init__(self, extra_bad_id=True):
        self.calls = 0
        self.extra_bad_id = extra_bad_id

    def parse(self, **kwargs):
        self.calls += 1
        import json

        payload = json.loads(kwargs["user"])
        outs = [
            DigestItemOut(
                id=r["id"],
                tickers=["ETE.AT", "NOT.AT"] if r["id"] == "id1" else [],
                summary_en=f"S:{r['title'][:20]}",
                sentiment=1,
                event_type="results",
                importance=4,
            )
            for r in payload["items"]
        ]
        if self.extra_bad_id:
            outs.append(
                DigestItemOut(
                    id="ghost", summary_en="x", sentiment=0, event_type="other", importance=1
                )
            )
        return SimpleNamespace(
            parsed=DigestOutput(items=outs, macro_summary="M", market_summary="K"),
            cost_usd=0.05,
            cost_eur=0.0435,
            usage=SimpleNamespace(input_tokens=5000, output_tokens=800),
            dry_run=False,
        )

    dry_run = False


def test_build_digest_pipeline(tmp_path):
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    store = NewsStore(tmp_path / "news")
    matcher = TickerMatcher.from_yaml(REPO / "configs" / "companies.yaml")
    llm = FakeLLM()
    d = build_digest(
        DAY,
        cfg,
        store,
        matcher,
        llm,
        universe=["ETE.AT", "PPC.AT"],
        out_dir=tmp_path / "digests",
        cfg=DigestBuildConfig(fetch_bodies=False),
        report=report(),
    )
    assert llm.calls == 1
    ids = [it.id for it in d.items]
    assert ids == ["id3", "id1", "id4"]  # ranked by reliability; id2 dup, id5 irrelevant, id6 stale
    by = {it.id: it for it in d.items}
    assert by["id1"].tickers == ["ETE.AT"]  # model's NOT.AT rejected, matcher's ETE kept
    assert by["id1"].summary_en.startswith("S:") and by["id1"].reliability == 4
    assert by["id4"].category == "rumor" and by["id4"].tickers == ["PPC.AT"]
    assert d.macro_summary == "M" and d.model == "claude-haiku-4-5"
    assert d.stats["n_duplicates"] == 1 and d.stats["n_new"] == 5 and d.stats["n_relevant"] == 3
    assert any("euro2day_banks failed" in w for w in d.warnings)
    assert d.cost["eur"] > 0
    # views
    assert [it.id for it in d.view(SourceToggles(rumors=False))] == ["id3", "id1"]
    assert [it.id for it in d.view(SourceToggles(news=False, macro=False, rumors=False))] == []
    assert load_digest(tmp_path / "digests", DAY).items == d.items
    # a second run the same day sees nothing new (already stored) and calls no model
    llm2 = FakeLLM()
    d2 = build_digest(
        DAY,
        cfg,
        store,
        matcher,
        llm2,
        universe=["ETE.AT"],
        out_dir=tmp_path / "d2",
        cfg=DigestBuildConfig(fetch_bodies=False),
        report=report(),
    )
    assert llm2.calls == 0 and d2.items == []


def test_dry_run_digest_is_mechanical(tmp_path):
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    store = NewsStore(tmp_path / "news")
    matcher = TickerMatcher.from_yaml(REPO / "configs" / "companies.yaml")
    llm = LLMClient(
        CostLedger(tmp_path / "c.jsonl"),
        Pricing.load(REPO / "configs" / "llm_pricing.yaml"),
        monthly_cap_eur=25,
        dry_run=True,
    )
    d = build_digest(
        DAY,
        cfg,
        store,
        matcher,
        llm,
        universe=["ETE.AT"],
        out_dir=tmp_path / "dg",
        cfg=DigestBuildConfig(fetch_bodies=False),
        report=report(),
    )
    assert d.model == "dry-run" and len(d.items) == 3 and d.cost["eur"] == 0
    assert d.items[1].summary_en.startswith("Εθνική Τράπεζα")


def test_items_published_after_the_digest_day_are_excluded(tmp_path):
    """Structural no-lookahead for news: a replay of a past day never sees later items."""
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    store = NewsStore(tmp_path / "news")
    matcher = TickerMatcher.from_yaml(REPO / "configs" / "companies.yaml")
    future = item(
        9,
        "naftemporiki",
        "news",
        4,
        "Εθνική Τράπεζα: μελλοντική είδηση",
        published=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
    )
    rep = IngestReport(fetched_at=NOW, statuses=[], items=[future])
    d = build_digest(
        DAY,
        cfg,
        store,
        matcher,
        FakeLLM(),
        universe=["ETE.AT"],
        out_dir=tmp_path / "d",
        cfg=DigestBuildConfig(fetch_bodies=False),
        report=rep,
    )
    assert d.items == [] and d.model == "no-items" and store.load(DAY) == []
