"""Network smoke test of the news sources: python -m athex_agent.digest.smoke

Fetches every enabled source (respecting robots.txt), reports per-source health, dedup and ticker
coverage. Never calls the model; writes to .cache/smoke/news (git-ignored).
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from athex_agent.data.news import NewsStore, ingest, load_sources
from athex_agent.digest.dedup import dedup
from athex_agent.digest.tickers import TickerMatcher

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".cache/smoke/news")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_sources(REPO / "configs" / "sources.yaml")
    creds = (os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET"))
    rep = ingest(cfg, reddit_credentials=creds)
    print(f"{'source':34s} {'ok':>3s} {'items':>5s} {'secs':>5s}  note")
    for s in rep.statuses:
        print(
            f"{s.source_id:34s} {str(s.ok):>3s} {s.items:5d} {s.seconds:5.1f}  "
            f"{s.skipped or s.error or ''}"
        )
    deduped, n_dup = dedup(rep.items)
    matcher = TickerMatcher.from_yaml(REPO / "configs" / "companies.yaml")
    tagged = matcher.tag(deduped)
    with_t = [it for it in tagged if it.tickers]
    macro = [it for it in tagged if not it.tickers and matcher.is_macro(f"{it.title} {it.summary}")]
    print(
        f"\nfetched {len(rep.items)}, duplicates {n_dup}, unique {len(deduped)}, "
        f"with tickers {len(with_t)}, macro-only {len(macro)}, failed sources {len(rep.failed)}"
    )
    counts = Counter(t for it in with_t for t in it.tickers)
    print("top tickers:", ", ".join(f"{t}:{n}" for t, n in counts.most_common(12)))
    for it in with_t[:6]:
        print(f"  [{it.source_id} r{it.reliability}] {it.tickers} {it.title[:90]}")
    store = NewsStore(Path(args.out))
    new = store.append(datetime.now(UTC).date().isoformat(), tagged)
    print(f"stored {len(new)} new items under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
