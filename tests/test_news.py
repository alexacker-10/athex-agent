"""News layer, offline: RSS parsing via a stubbed HTTP layer, dedup, ticker matching, storage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from athex_agent.data import news as news_mod
from athex_agent.data.news import (
    NewsItem,
    NewsStore,
    canonical_url,
    ingest,
    item_id,
    load_sources,
)
from athex_agent.digest.dedup import dedup, fold, jaccard, shingles
from athex_agent.digest.tickers import TickerMatcher

REPO = Path(__file__).resolve().parents[1]

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Εθνική Τράπεζα: Διαγράφει 11,58 εκατ. ίδιες μετοχές</title>
<link>https://example.gr/a?utm_source=x</link>
<description>&lt;p&gt;Η ΕΤΕ ανακοίνωσε...&lt;/p&gt;</description>
<pubDate>Fri, 18 Sep 2026 10:00:00 +0300</pubDate></item>
<item><title>ΔΕΗ: νέα επένδυση σε ΑΠΕ</title><link>https://example.gr/b</link>
<description>Η ΔΕΗ...</description><pubDate>Fri, 18 Sep 2026 11:00:00 +0300</pubDate></item>
<item><title></title><link>https://example.gr/empty</link></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200, text: str = ""):
        self.content, self.status_code, self.text = content, status, text or content.decode("utf-8")
        self.headers = {"content-type": "application/rss+xml"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise news_mod.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return {}


def test_canonical_url_and_id():
    assert (
        canonical_url("https://Example.gr/a/?utm_source=x&id=2#frag") == "https://example.gr/a?id=2"
    )
    assert item_id("https://example.gr/a?utm_source=x", "t") == item_id("https://example.gr/a", "t")
    assert item_id("", "Same  title ") == item_id("", "same title")


def test_sources_config_loads_and_google_is_off(repo):
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    ids = {s.id: s for s in cfg.sources}
    assert ids["google_news_el"].enabled is False
    assert ids["bankingnews"].category == "rumor" and ids["ecb_press"].category == "macro"
    assert (
        ids["athex_announcements"].category == "official"
        and ids["athex_announcements"].reliability == 5
    )


def test_ingest_isolates_failures_and_respects_robots(monkeypatch, tmp_path):
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    calls = []

    def fake_get(url, headers=None, timeout=None, params=None, **kw):
        calls.append(url)
        if url.endswith("robots.txt"):
            host = url.split("/")[2]
            if host == "www.ot.gr":
                return FakeResponse(b"User-agent: *\nDisallow: /feed/\n")
            return FakeResponse(b"User-agent: *\nAllow: /\n")
        if "naftemporiki" in url:
            return FakeResponse(RSS.encode("utf-8"))
        if "euro2day" in url:
            return FakeResponse(b"", status=503)
        return FakeResponse(b"<rss><channel></channel></rss>")

    monkeypatch.setattr(news_mod.requests, "get", fake_get)
    monkeypatch.setattr(news_mod.time, "sleep", lambda s: None)
    rep = ingest(cfg, enabled_ids={"naftemporiki", "euro2day_banks", "ot_gr", "reddit_greekstocks"})
    st = {s.source_id: s for s in rep.statuses}
    assert st["naftemporiki"].ok and st["naftemporiki"].items == 2
    assert not st["euro2day_banks"].ok and "HTTPError" in st["euro2day_banks"].error
    assert st["ot_gr"].skipped == "disallowed by robots.txt"
    assert st["reddit_greekstocks"].skipped == "no Reddit API credentials"
    assert [s.source_id for s in rep.failed] == ["euro2day_banks"]
    it = rep.items[0]
    assert it.title.startswith("Εθνική Τράπεζα") and it.summary == "Η ΕΤΕ ανακοίνωσε..."
    assert it.published == datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
    assert it.url == "https://example.gr/a?utm_source=x" and it.category == "news"


def test_dedup_folds_accents_and_keeps_most_reliable():
    now = datetime.now(UTC)
    mk = lambda src, rel, title, url: NewsItem(  # noqa: E731
        id=item_id(url, title),
        source_id=src,
        category="news",
        reliability=rel,
        url=url,
        title=title,
        fetched_at=now,
    )
    a = mk(
        "naftemporiki", 4, "Εθνική Τράπεζα: διαγράφει 11,58 εκατ. ίδιες μετοχές", "https://a.gr/1"
    )
    b = mk(
        "mononews",
        3,
        "Εθνικη Τραπεζα διαγραφει 11,58 εκατ. ιδιες μετοχες κόστους",
        "https://b.gr/2",
    )
    c = mk("insider", 3, "ΔΕΗ: νέα επένδυση σε ΑΠΕ στη Ρουμανία", "https://c.gr/3")
    d = mk("insider", 3, "Same URL other title", "https://a.gr/1?utm_medium=rss")
    kept, removed = dedup([b, a, c, d])
    assert [k.source_id for k in kept] == ["naftemporiki", "insider"]
    assert kept[1].title.startswith("ΔΕΗ") and removed == 2
    assert fold("Εθνική Τράπεζα") == "εθνικη τραπεζα"
    assert jaccard(shingles("a b c d"), shingles("a b c d")) == 1.0


def test_ticker_matcher():
    m = TickerMatcher.from_yaml(REPO / "configs" / "companies.yaml")
    assert m.match("Η Εθνική Τράπεζα και η ΔΕΗ ανακοίνωσαν αποτελέσματα") == ["ETE.AT", "PPC.AT"]
    assert m.match("national bank of greece raises guidance") == ["ETE.AT"]
    assert m.match("Metlen και Μυτιληναίος") == ["MTLN.AT"]
    assert m.match("Ο ΟΠΑΠ έγινε Allwyn") == ["ALWN.AT"]
    assert m.match("Nothing about companies") == []
    assert m.match("Optimalisation") == []  # whole-word match: no false hit for Optima
    assert m.is_macro("Η ΕΚΤ διατήρησε τα επιτόκια") and not m.is_macro("Καιρός αύριο")
    now = datetime.now(UTC)
    it = NewsItem(
        id="x",
        source_id="s",
        category="news",
        reliability=3,
        url="https://x.gr/1",
        title="Jumbo: αύξηση πωλήσεων",
        fetched_at=now,
    )
    assert m.tag([it])[0].tickers == ["BELA.AT"]


def test_news_store_rolls_seen_ids(tmp_path):
    store = NewsStore(tmp_path / "news", keep_days=30)
    now = datetime.now(UTC)
    it = NewsItem(
        id="abc",
        source_id="s",
        category="news",
        reliability=3,
        url="https://x.gr/1",
        title="t",
        fetched_at=now,
    )
    assert len(store.append("2026-09-21", [it])) == 1
    assert store.append("2026-09-22", [it]) == []  # seen yesterday
    assert store.load("2026-09-21")[0].id == "abc" and store.load("2026-09-22") == []
    assert store.append("2026-11-30", [it]) and store.seen()["abc"] == "2026-11-30"


def test_pydantic_item_roundtrip():
    now = datetime.now(UTC)
    it = NewsItem(
        id="a",
        source_id="s",
        category="rumor",
        reliability=1,
        url="",
        title="t",
        fetched_at=now,
        tickers=["ETE.AT"],
    )
    assert NewsItem.model_validate_json(it.model_dump_json()) == it
    with pytest.raises(ValueError):
        NewsItem(
            id="a",
            source_id="s",
            category="rumor",
            reliability=9,
            url="",
            title="t",
            fetched_at=now,
        )
