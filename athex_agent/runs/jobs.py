"""The daily jobs. Each is idempotent per (trading date, job) via the run ledger.

  fill      09:00 UTC  fill pending orders at today's opening auction, apply corporate actions
  decide    18:30 UTC  prices -> digest (once) -> universe -> every arm decides -> NAV -> dashboard
  bootstrap once       download full price history for the universe and benchmarks
  dashboard any        rebuild docs/ from state

`decide` also runs `fill` first (catch-up) and starts cohorts due today. Dry runs never call the
model, use a separate state root, and are labelled on the dashboard.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from athex_agent.analysis.views import load_views, score_views, summarize
from athex_agent.arms.journal import Journal
from athex_agent.arms.llm_decider import LLMDecider
from athex_agent.arms.runner import ArmRunner, NotStarted
from athex_agent.config.loader import REPO_ROOT, ConfigRepo
from athex_agent.data import calendar as cal
from athex_agent.data.actions import ActionsStore
from athex_agent.data.news import NewsStore, load_sources
from athex_agent.data.prices import PriceStore
from athex_agent.data.sources import PriceSource, YahooSource
from athex_agent.data.universe import select_universe
from athex_agent.data.update import update_prices
from athex_agent.digest.build import DigestBuildConfig, build_digest, load_digest
from athex_agent.digest.models import Digest
from athex_agent.digest.tickers import TickerMatcher
from athex_agent.llm.client import CostLedger, LLMClient, Pricing
from athex_agent.runs.git import committed_ts, is_repo
from athex_agent.runs.ledger import RunLedger
from athex_agent.runs.orchestrate import (
    ArmInstance,
    active_instances,
    plan_llm_calls,
    random_p_swap_from,
)

log = logging.getLogger(__name__)
BENCHMARKS = ["SXR8.DE", "AETF.AT", "GD.AT", "EURUSD=X"]


@dataclass
class Env:
    """Where a run reads and writes. Live: repo data/ and state/. Dry run: .cache/dryrun/."""

    repo_root: Path
    data_root: Path
    state_root: Path
    docs_root: Path
    dry_run: bool
    source: PriceSource | None = None
    now: datetime | None = None

    @classmethod
    def from_args(cls, dry_run: bool, root: Path | None = None) -> Env:
        root = root or REPO_ROOT
        if dry_run:
            base = root / ".cache" / "dryrun"
            return cls(root, base / "data", base / "state", base / "docs", True)
        return cls(root, root / "data", root / "state", root / "docs", False)

    @property
    def prices(self) -> PriceStore:
        return PriceStore(self.data_root / "prices")

    @property
    def actions(self) -> ActionsStore:
        return ActionsStore(self.data_root / "actions")

    @property
    def ledger(self) -> RunLedger:
        return RunLedger(self.state_root / "ledger")

    @property
    def llm(self) -> LLMClient:
        return LLMClient(
            CostLedger(self.state_root / "ledger" / "llm_cost.jsonl"),
            Pricing.load(self.repo_root / "configs" / "llm_pricing.yaml"),
            dry_run=self.dry_run or None,
        )

    def clock(self) -> datetime:
        return self.now or datetime.now(UTC)


def _tickers(repo: ConfigRepo, env: Env) -> list[str]:
    u = repo.universe()
    names = {u.canonical(t) for t in u.candidates} | set(u.include_always) | set(BENCHMARKS)
    for p in (env.state_root / "arms").glob("**/books/*/book.json"):
        try:
            import json

            for t in json.loads(p.read_text())["positions"]:
                names.add(t)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(names)


def refresh_prices(repo: ConfigRepo, env: Env, as_of: date) -> dict[str, Any]:
    src = env.source or YahooSource()
    rep = update_prices(env.prices, env.actions, src, _tickers(repo, env), as_of)
    log.info("prices: %s", rep.summary())
    return {
        "summary": rep.summary(),
        "failed": [r.ticker for r in rep.failed],
        "stale": [r.ticker for r in rep.stale],
    }


def universe_for(repo: ConfigRepo, env: Env, d: date) -> list[str]:
    """Monthly membership snapshot, computed on the first decision of the month and cached."""
    p = env.state_root / "universe" / f"{d.year:04d}-{d.month:02d}.json"
    if p.exists():
        import json

        return json.loads(p.read_text())["members"]
    snap = select_universe(repo.universe(), env.prices.as_of(d))
    p.parent.mkdir(parents=True, exist_ok=True)
    import json

    p.write_text(
        json.dumps(
            {
                "as_of": snap.as_of,
                "members": snap.members,
                "excluded": snap.excluded,
                "adv_eur": snap.adv_eur,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return snap.members


def _runner(inst: ArmInstance, env: Env, decider=None) -> ArmRunner:
    return ArmRunner(inst.resolved, env.prices, env.actions, env.state_root, inst.instance, decider)


def _committed(env: Env, runner: ArmRunner, book) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    if env.dry_run or not is_repo(env.repo_root):
        return out
    for o in book.pending_orders:
        ts = committed_ts(runner.paths.decision(o.decision_date), env.repo_root)
        if ts is not None:
            out[o.order_id] = ts
    return out


def job_fill(repo: ConfigRepo, env: Env, today: date) -> dict[str, Any]:
    """Fill every started instance's pending orders at today's open; apply corporate actions."""
    filled = cancelled = pending = 0
    for inst in active_instances(repo, env.ledger.first_live(), today):
        runner = _runner(inst, env)
        if not runner.started():
            continue
        books = runner.load_books()
        committed: dict[str, datetime] = {}
        for book in books.values():
            committed.update(_committed(env, runner, book))
        out = runner.fill(env.clock(), today, committed)
        for res in out.values():
            filled += sum(o.status == "filled" for o in res)
            cancelled += sum(o.status == "cancelled" for o in res)
            pending += sum(o.status == "pending" for o in res)
        runner.apply_corporate_actions(today)
    return {"filled": filled, "cancelled": cancelled, "pending": pending}


def job_digest(repo: ConfigRepo, env: Env, today: date, universe: list[str]) -> Digest:
    existing = load_digest(env.data_root / "digests", today.isoformat())
    if existing is not None:
        return existing
    sources = load_sources(env.repo_root / "configs" / "sources.yaml")
    matcher = TickerMatcher.from_yaml(env.repo_root / "configs" / "companies.yaml")
    creds = (os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET"))
    return build_digest(
        today.isoformat(),
        sources,
        NewsStore(env.data_root / "news"),
        matcher,
        env.llm,
        universe,
        env.data_root / "digests",
        DigestBuildConfig(),
        reddit_credentials=creds,
    )


def _view_scores(inst: ArmInstance, env: Env, universe: list[str], as_of: date) -> dict[str, Any]:
    views = load_views(inst.paths(env.state_root).root)
    if views.empty:
        return {}
    scored = score_views(views, env.prices, lambda d: universe, as_of)
    return summarize(scored)


def _a_order_rate(env: Env, today: date) -> float:
    """Arm A's realized swap rate over the trailing 60 sessions, for the random baseline."""
    p = env.state_root / "arms" / "A" / "books" / "10k" / "book.json"
    if not p.exists():
        return 1.0 / 21.0
    import json

    trades = json.loads(p.read_text()).get("trades", [])
    since = cal.add_trading_days(today, -60)
    n = sum(1 for t in trades if date.fromisoformat(t["decision_date"]) > since)
    return random_p_swap_from(n, 60)


def job_decide(repo: ConfigRepo, env: Env, today: date) -> dict[str, Any]:
    ledger = env.ledger
    summary: dict[str, Any] = {"date": today.isoformat(), "dry_run": env.dry_run}
    summary["prices"] = refresh_prices(repo, env, today)
    summary["fill"] = job_fill(repo, env, today)
    if not env.dry_run:
        ledger.mark_first_live(today)
    universe = universe_for(repo, env, today)
    summary["universe_size"] = len(universe)
    digest = job_digest(repo, env, today, universe)
    summary["digest"] = {
        "items": len(digest.items),
        "model": digest.model,
        "cost_eur": digest.cost.get("eur", 0.0),
        "warnings": digest.warnings[:5],
    }
    llm = env.llm
    instances = active_instances(repo, ledger.first_live(), today)
    allowed = plan_llm_calls(instances, llm.remaining_eur(env.clock()), reserve_eur=0.5)
    p_swap = _a_order_rate(env, today)
    decision_ts = env.clock()
    per_arm: dict[str, Any] = {}
    for inst in instances:
        if inst.start_on > today:
            continue
        decider = None
        extra: dict[str, Any] = {}
        if inst.is_llm:
            paths = inst.paths(env.state_root)
            journal = Journal.load(paths.journal, inst.arm_id)
            decider = LLMDecider(
                llm,
                inst.resolved.arm.llm,
                inst.resolved.arm.sources,
                digest,
                journal,
                view_scores=_view_scores(inst, env, universe, today),
            )
            if not allowed.get(inst.key, False):
                extra["budget_shed"] = True
        elif inst.resolved.arm.rule and inst.resolved.arm.rule.kind == "random":
            extra["p_swap"] = p_swap
        runner = _runner(inst, env, decider)
        if not runner.started():
            runner.start(today)
        if extra.get("budget_shed"):
            rec = runner.decide_hold(today, decision_ts, universe, "budget-shed")
        else:
            rec = runner.decide(today, decision_ts, universe, extra)
        runner.record_nav(today)
        per_arm[inst.key] = {
            "orders": {b: len(v["orders"]) for b, v in rec["books"].items()},
            "hold_reason": rec["proposal"].get("meta", {}).get("hold_reason"),
            "cost_eur": rec["proposal"].get("meta", {}).get("cost", {}).get("eur", 0.0),
        }
    summary["arms"] = per_arm
    summary["llm_spent_month_eur"] = llm.spent_eur(env.clock())
    return summary


def job_bootstrap(repo: ConfigRepo, env: Env, today: date) -> dict[str, Any]:
    return refresh_prices(repo, env, today)


def fill_session(now: datetime) -> date | None:
    """The session whose opening auction the fill job targets: today if it is a trading day and
    the auction has printed; None before the auction; the previous session on non-trading days."""
    local = now.astimezone(cal.ATHENS).date()
    if cal.is_trading_day(local):
        return local if now >= cal.session_open_utc(local) + timedelta(minutes=5) else None
    return cal.prev_trading_day(local)


def run_job(
    job: str, env: Env, as_of: date | None = None, repo: ConfigRepo | None = None
) -> dict[str, Any]:
    repo = repo or ConfigRepo(env.repo_root / "configs")
    if job == "fill":
        today = as_of or fill_session(env.clock())
        if today is None:
            return {"skipped": True, "reason": "before the opening auction"}
    else:
        today = as_of or cal.last_completed_session(env.clock())
    ledger = env.ledger
    if job in ("fill", "decide") and ledger.already_done(today, job) and not env.dry_run:
        log.info("%s already succeeded for %s; nothing to do", job, today)
        return {"skipped": True, "date": today.isoformat()}
    ledger.start(today, job, env.dry_run)
    try:
        if job == "fill":
            out = job_fill(repo, env, today)
        elif job == "decide":
            out = job_decide(repo, env, today)
        elif job == "bootstrap":
            out = job_bootstrap(repo, env, today)
        elif job == "dashboard":
            from athex_agent.dashboard.build import build_dashboard

            out = build_dashboard(repo, env, today)
        else:
            raise ValueError(f"unknown job {job}")
    except NotStarted as exc:
        ledger.finish(today, job, "failed", str(exc))
        raise
    except Exception as exc:
        ledger.finish(today, job, "failed", f"{type(exc).__name__}: {exc}")
        raise
    ledger.finish(today, job, "success", str(out)[:1500])
    if job == "decide":
        from athex_agent.dashboard.build import build_dashboard

        build_dashboard(repo, env, today)
    return out
