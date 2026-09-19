"""CLI: python -m athex_agent.runs <fill|decide|bootstrap|dashboard> [--as-of DATE] [--dry-run]"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date

from athex_agent.runs.jobs import Env, run_job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["fill", "decide", "bootstrap", "dashboard"])
    ap.add_argument("--as-of", default=None, help="trading date (default: last completed session)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="no model calls; separate state under .cache/dryrun; labelled on the dashboard",
    )
    ap.add_argument(
        "--simulate-clock",
        action="store_true",
        help="dry runs only: pretend the job runs at the session's own time so past "
        "dates can be replayed for plumbing checks (decisions still cannot see "
        "later data)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.simulate_clock and not (args.dry_run and args.as_of):
        ap.error("--simulate-clock requires --dry-run and --as-of")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    env = Env.from_args(args.dry_run)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    if args.simulate_clock and as_of is not None:
        from datetime import timedelta

        from athex_agent.data import calendar as cal

        env.now = (
            cal.session_open_utc(as_of) if args.job == "fill" else cal.session_close_utc(as_of)
        ) + timedelta(hours=1)
    out = run_job(args.job, env, as_of)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
