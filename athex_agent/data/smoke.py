"""Network smoke test for the free price feed. Run locally: python -m athex_agent.data.smoke

Writes to .cache/smoke/ (git-ignored) and prints coverage: history depth, settled/provisional bars,
whether the latest session has a daily bar, a fresh quote, and an intraday opening print.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

from athex_agent.data import calendar as cal
from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import PriceStore
from athex_agent.data.sources import YahooSource
from athex_agent.data.update import update_prices

DEFAULT = ["ETE.AT", "PPC.AT", "MTLN.AT", "ALWN.AT", "AETF.AT", "GD.AT", "SXR8.DE", "EURUSD=X"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=DEFAULT)
    ap.add_argument("--out", default=".cache/smoke")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = Path(args.out)
    store, actions = PriceStore(out / "prices"), ActionsStore(out / "actions")
    src = YahooSource()
    as_of = cal.last_completed_session(datetime.now(UTC))
    report = update_prices(store, actions, src, args.tickers, as_of)
    print(f"\nas_of session: {as_of}   {report.summary()}")
    print(
        f"{'ticker':10s} {'ok':>3s} {'first':>10s} {'last':>10s} {'bars':>6s} {'prov':>4s} "
        f"{'quote':>5s} {'acts':>4s} {'intraday_open':>13s}  error"
    )
    for r in report.results:
        df = store.load(r.ticker)
        first = df.index[0].date() if not df.empty else None
        prov = int((~df["settled"]).sum()) if not df.empty else 0
        io = src.fetch_intraday_open(r.ticker, as_of) if r.ok else None
        n_acts = len(actions.load(r.ticker))
        print(
            f"{r.ticker:10s} {str(r.ok):>3s} {str(first):>10s} {str(r.latest_bar):>10s} "
            f"{len(df):6d} {prov:4d} {str(r.quote_used):>5s} {n_acts:4d} {str(io):>13s}  "
            f"{r.error or ''}"
        )
    rev = store.revisions()
    print(f"\nrevisions logged: {len(rev)}")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
