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
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    env = Env.from_args(args.dry_run)
    out = run_job(args.job, env, date.fromisoformat(args.as_of) if args.as_of else None)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
