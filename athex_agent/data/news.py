"""News, official, macro and rumor sources behind one interface, with robots.txt checks and
per-source health. Every adapter returns NewsItems; nothing here calls an LLM."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml
from pydantic import Field

from athex_agent.config.models import StrictModel

log = logging.getLogger(__name__)

Category = Literal["official", "news", "macro", "rumor"]
_TRACKING = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


class NewsItem(StrictModel):
    id: str
    source_id: str
    category: Category
    reliability: int = Field(ge=1, le=5)
    url: str
    title: str
    summary: str = ""
    published: datetime | None = None
    fetched_at: datetime
    lang: str = "el"
    tickers: list[str] = []
    body: str | None = None


class SourceConfig(StrictModel):
    id: str
    kind: Literal["rss", "reddit", "google_news", "athex_html"]
    url: str
    category: Category
    reliability: int = Field(ge=1, le=5)
    lang: str = "el"
    enabled: bool = True
    note: str = ""


class SourcesConfig(StrictModel):
    user_agent: str
    sources: list[SourceConfig]


def load_sources(path: Path) -> SourcesConfig:
    return SourcesConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k not in _TRACKING])
    return urlunparse(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", query, "")
    )


def item_id(url: str, title: str) -> str:
    key = canonical_url(url) if url else re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ robots


class RobotsCache:
    def __init__(self, user_agent: str, timeout: float = 8.0) -> None:
        self.ua = user_agent
        self.timeout = timeout
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._parsers:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = requests.get(
                    f"https://{host}/robots.txt",
                    headers={"User-Agent": self.ua},
                    timeout=self.timeout,
                )
                if r.status_code >= 400:
                    rp = None  # no robots file: allowed
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._parsers[host] = rp
        rp = self._parsers[host]
        return True if rp is None else rp.can_fetch(self.ua, url)


# ------------------------------------------------------------------ health


@dataclass
class SourceStatus:
    source_id: str
    ok: bool
    items: int = 0
    error: str | None = None
    skipped: str | None = None
    seconds: float = 0.0


@dataclass
class IngestReport:
    fetched_at: datetime
    statuses: list[SourceStatus] = field(default_factory=list)
    items: list[NewsItem] = field(default_factory=list)

    @property
    def failed(self) -> list[SourceStatus]:
        return [s for s in self.statuses if not s.ok and s.skipped is None]

    def to_json(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "sources": [s.__dict__ for s in self.statuses],
            "n_items": len(self.items),
        }


# ------------------------------------------------------------------ adapters


def _parse_dt(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


class RssSource:
    def __init__(self, cfg: SourceConfig, ua: str, timeout: float = 15.0) -> None:
        self.cfg, self.ua, self.timeout = cfg, ua, timeout

    def fetch(self, url: str | None = None) -> list[NewsItem]:
        import feedparser

        r = requests.get(url or self.cfg.url, headers={"User-Agent": self.ua}, timeout=self.timeout)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        now = datetime.now(UTC)
        items = []
        for e in feed.entries:
            link = (e.get("link") or "").strip()
            title = _strip_html(e.get("title") or "")
            if not title:
                continue
            items.append(
                NewsItem(
                    id=item_id(link, title),
                    source_id=self.cfg.id,
                    category=self.cfg.category,
                    reliability=self.cfg.reliability,
                    url=link,
                    title=title,
                    summary=_strip_html(e.get("summary") or e.get("description") or "")[:1500],
                    published=_parse_dt(e),
                    fetched_at=now,
                    lang=self.cfg.lang,
                )
            )
        return items


class RedditSource:
    """Official Data API, client-credentials OAuth; skipped without credentials."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

    def __init__(
        self,
        cfg: SourceConfig,
        ua: str,
        client_id: str | None,
        client_secret: str | None,
        timeout: float = 15.0,
    ) -> None:
        self.cfg, self.ua, self.timeout = cfg, ua, timeout
        self.client_id, self.client_secret = client_id, client_secret

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def fetch(self) -> list[NewsItem]:
        tok = requests.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self.ua},
            timeout=self.timeout,
        )
        tok.raise_for_status()
        token = tok.json()["access_token"]
        r = requests.get(
            self.cfg.url,
            params={"limit": 50},
            headers={"User-Agent": self.ua, "Authorization": f"bearer {token}"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        now = datetime.now(UTC)
        items = []
        for child in r.json().get("data", {}).get("children", []):
            d = child.get("data", {})
            title = (d.get("title") or "").strip()
            if not title:
                continue
            url = "https://www.reddit.com" + d.get("permalink", "")
            items.append(
                NewsItem(
                    id=item_id(url, title),
                    source_id=self.cfg.id,
                    category=self.cfg.category,
                    reliability=self.cfg.reliability,
                    url=url,
                    title=title,
                    summary=(d.get("selftext") or "")[:1500],
                    published=datetime.fromtimestamp(float(d.get("created_utc", 0)), tz=UTC),
                    fetched_at=now,
                    lang=self.cfg.lang,
                )
            )
        return items


class AthexAnnouncementsSource:
    """Company announcements page (HTML). Best effort: the site blocks non-browser clients."""

    def __init__(self, cfg: SourceConfig, ua: str, timeout: float = 20.0) -> None:
        self.cfg, self.ua, self.timeout = cfg, ua, timeout

    def fetch(self) -> list[NewsItem]:
        from bs4 import BeautifulSoup

        r = requests.get(
            self.cfg.url,
            headers={"User-Agent": self.ua, "Accept-Language": "el,en"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        now = datetime.now(UTC)
        items = []
        for a in soup.select("a[href*='announcement'], a[href*='anakoinos'], table a"):
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            if len(title) < 12 or not href:
                continue
            url = href if href.startswith("http") else "https://www.athexgroup.gr" + href
            items.append(
                NewsItem(
                    id=item_id(url, title),
                    source_id=self.cfg.id,
                    category="official",
                    reliability=self.cfg.reliability,
                    url=url,
                    title=title,
                    fetched_at=now,
                    lang=self.cfg.lang,
                )
            )
        if not items:
            raise RuntimeError("no announcement links parsed (page layout changed or blocked)")
        return items


# ------------------------------------------------------------------ orchestration


def ingest(
    cfg: SourcesConfig,
    enabled_ids: set[str] | None = None,
    reddit_credentials: tuple[str | None, str | None] = (None, None),
    google_queries: list[str] | None = None,
    robots: RobotsCache | None = None,
    pause_s: float = 0.5,
) -> IngestReport:
    """Fetch every enabled source. Failures are isolated and recorded; robots.txt is respected."""
    robots = robots or RobotsCache(cfg.user_agent)
    report = IngestReport(fetched_at=datetime.now(UTC))
    seen: set[str] = set()
    for src in cfg.sources:
        if not src.enabled or (enabled_ids is not None and src.id not in enabled_ids):
            continue
        t0 = time.monotonic()
        status = SourceStatus(source_id=src.id, ok=False)
        try:
            urls = [src.url]
            if src.kind == "google_news":
                urls = [
                    src.url.format(query=requests.utils.quote(q)) for q in (google_queries or [])
                ]
            if src.kind != "reddit" and not all(robots.allowed(u) for u in urls):
                status.skipped = "disallowed by robots.txt"
                report.statuses.append(status)
                continue
            if src.kind == "rss":
                items = RssSource(src, cfg.user_agent).fetch()
            elif src.kind == "google_news":
                rss = RssSource(src, cfg.user_agent)
                items = [it for u in urls for it in rss.fetch(u)]
            elif src.kind == "reddit":
                rd = RedditSource(src, cfg.user_agent, *reddit_credentials)
                if not rd.configured:
                    status.skipped = "no Reddit API credentials"
                    report.statuses.append(status)
                    continue
                items = rd.fetch()
            elif src.kind == "athex_html":
                items = AthexAnnouncementsSource(src, cfg.user_agent).fetch()
            else:  # pragma: no cover
                raise RuntimeError(f"unknown source kind {src.kind}")
            fresh = [it for it in items if it.id not in seen]
            seen.update(it.id for it in fresh)
            report.items.extend(fresh)
            status.ok, status.items = True, len(fresh)
        except Exception as exc:  # noqa: BLE001 - one broken source must not kill the run
            status.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("source %s failed: %s", src.id, status.error)
        status.seconds = round(time.monotonic() - t0, 2)
        report.statuses.append(status)
        time.sleep(pause_s)
    return report


# ------------------------------------------------------------------ storage


class NewsStore:
    """data/news/<date>.jsonl of raw items per ingestion date plus a rolling seen-id index."""

    def __init__(self, root: Path, keep_days: int = 30) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days

    def _seen_path(self) -> Path:
        return self.root / "_seen.json"

    def seen(self) -> dict[str, str]:
        p = self._seen_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def append(self, day: str, items: list[NewsItem]) -> list[NewsItem]:
        """Store items not seen in the last keep_days; returns the new ones."""
        seen = self.seen()
        cutoff = (
            (datetime.fromisoformat(day) - __import__("datetime").timedelta(days=self.keep_days))
            .date()
            .isoformat()
        )
        seen = {k: v for k, v in seen.items() if v >= cutoff}
        new = [it for it in items if it.id not in seen]
        with (self.root / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            for it in new:
                fh.write(it.model_dump_json() + "\n")
                seen[it.id] = day
        self._seen_path().write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
        return new

    def load(self, day: str) -> list[NewsItem]:
        p = self.root / f"{day}.jsonl"
        if not p.exists():
            return []
        return [
            NewsItem.model_validate_json(line)
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
