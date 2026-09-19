"""Build docs/ from state: summary.json (all arms, metrics, health) and per-arm detail files."""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from athex_agent.analysis.fee_drag import fee_drag_metric
from athex_agent.analysis.views import load_views, score_views, summarize
from athex_agent.arms.runner import ArmPaths
from athex_agent.config.loader import ConfigRepo
from athex_agent.portfolio.fees import compute_fees
from athex_agent.runs.orchestrate import active_instances

TEMPLATE = Path(__file__).resolve().parent / "index.html"


def _nav_rows(paths: ArmPaths, book_id: str) -> list[dict[str, Any]]:
    p = paths.nav(book_id)
    if not p.exists():
        return []
    import csv

    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _series(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [[r["date"], round(float(r["nav"]), 2)] for r in rows]


def _return_between(store, ticker: str, start: date, end: date) -> float | None:
    view = store.as_of(end)
    try:
        a = view.close(ticker, start)
        b = view.close(ticker, end)
    except Exception:  # noqa: BLE001 - benchmark missing is reported as None
        return None
    return b / a - 1.0 if a else None


def _metrics(rows: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    if not rows:
        return {}
    navs = np.array([float(r["nav"]) for r in rows])
    rets = np.diff(navs) / navs[:-1] if len(navs) > 1 else np.array([])
    peak = np.maximum.accumulate(navs)
    mdd = float(((navs - peak) / peak).min()) if len(navs) else 0.0
    vol = float(rets.std(ddof=1) * math.sqrt(252)) if len(rets) > 2 else None
    drag = fee_drag_metric(rows)
    return {
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "days": len(rows),
        "nav": float(navs[-1]),
        "return": float(navs[-1] / capital - 1.0),
        "max_drawdown": mdd,
        "volatility_annualized": vol,
        "fees_pct_annualized": drag["fees_pct_annualized"],
        "slippage_pct_annualized": drag["slippage_pct_annualized"],
        "fees_eur": float(rows[-1]["fees_cum"]) + float(rows[-1]["tax_cum"]),
        "dividends_eur": float(rows[-1]["dividends_cum"]),
    }


def _recost(trades: list[dict[str, Any]], repo: ConfigRepo) -> dict[str, float]:
    profiles = repo.fee_profiles()
    out = {}
    for pid in repo.books().recost_profiles:
        p = profiles[pid]
        total = 0.0
        for t in trades:
            total += compute_fees(p, t["side"], float(t["gross_value"]), int(t["shares"])).total
        out[pid] = round(total, 2)
    return out


def _cited_items(decisions: list[dict[str, Any]], digests_dir: Path) -> dict[str, dict[str, str]]:
    """Resolve cited digest item ids to title/url/source for the decision viewer."""
    wanted: dict[str, set[str]] = {}
    for d in decisions:
        for v in d.get("views", []):
            for iid in v.get("cited_item_ids", []):
                wanted.setdefault(d["date"], set()).add(iid)
    out: dict[str, dict[str, str]] = {}
    for day, ids in wanted.items():
        p = digests_dir / f"{day}.json"
        if not p.exists():
            continue
        for it in json.loads(p.read_text()).get("items", []):
            if it["id"] in ids:
                out[it["id"]] = {
                    "title": it["title"],
                    "url": it["url"],
                    "source": it["source_id"],
                    "reliability": it["reliability"],
                    "date": day,
                }
    return out


def _percentile(value: float, samples: list[float]) -> float | None:
    if not samples or value is None:
        return None
    arr = np.array(samples)
    return float((arr < value).mean())


def build_dashboard(repo: ConfigRepo, env, today: date) -> dict[str, Any]:
    store = env.prices
    ledger = env.ledger
    docs = Path(env.docs_root)
    (docs / "data" / "arms").mkdir(parents=True, exist_ok=True)
    instances = active_instances(repo, ledger.first_live(), today)
    books_cfg = {b.id: b for b in repo.books().books}
    profiles = repo.fee_profiles()
    arms_out: list[dict[str, Any]] = []
    random_returns: dict[str, list[float]] = {b: [] for b in books_cfg}
    for inst in instances:
        paths = inst.paths(env.state_root)
        if not paths.root.exists():
            continue
        arm = inst.resolved.arm
        entry: dict[str, Any] = {
            "key": inst.key,
            "arm_id": inst.arm_id,
            "instance": inst.instance,
            "cohort": inst.cohort,
            "kind": arm.kind,
            "description": arm.description,
            "base_arm": arm.base_arm,
            "factor": arm.factor,
            "model": arm.llm.model if arm.llm else None,
            "rule": arm.rule.kind if arm.rule else None,
            "books": {},
        }
        detail: dict[str, Any] = {"key": inst.key, "books": {}, "decisions": []}
        for b in inst.resolved.books:
            rows = _nav_rows(paths, b.id)
            if not rows:
                continue
            profile = inst.resolved.fee_profile_for(b)
            book_json = (
                json.loads(paths.book(b.id).read_text()) if paths.book(b.id).exists() else {}
            )
            m = _metrics(rows, b.capital_eur)
            start, end = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
            bench = {
                t: _return_between(store, t, start, end) for t in ("SXR8.DE", "AETF.AT", "GD.AT")
            }
            m["vs_sp500"] = (
                (m["return"] - bench["SXR8.DE"]) if bench["SXR8.DE"] is not None else None
            )
            m["vs_aetf"] = (
                (m["return"] - bench["AETF.AT"]) if bench["AETF.AT"] is not None else None
            )
            m["benchmarks"] = bench
            trades = book_json.get("trades", [])
            m["turnover_gross_eur"] = round(sum(float(t["gross_value"]) for t in trades), 2)
            m["n_trades"] = len(trades)
            entry["books"][b.id] = {
                "capital": b.capital_eur,
                "fee_profile": profile.id,
                "fee_badge": profile.badge,
                "metrics": m,
                "nav": _series(rows),
                "holdings": [
                    {
                        "ticker": t,
                        "shares": p["lots"] and sum(lo["shares"] for lo in p["lots"]),
                        "cost_basis": round(sum(lo["cost_total"] for lo in p["lots"]), 2),
                    }
                    for t, p in book_json.get("positions", {}).items()
                ],
                "divergences": book_json.get("divergences", [])[-50:],
                "n_divergences": len(book_json.get("divergences", [])),
                "recost_fees_eur": _recost(trades, repo),
            }
            detail["books"][b.id] = {
                "trades": trades,
                "divergences": book_json.get("divergences", []),
                "cancelled": book_json.get("cancelled_orders", []),
            }
            if inst.arm_id == "random" and inst.cohort is None:
                random_returns[b.id].append(m["return"])
        if not entry["books"]:
            continue
        # decisions and views
        for p in sorted((paths.root / "decisions").glob("????-??-??.json"))[-90:]:
            rec = json.loads(p.read_text())
            prop = rec.get("proposal", {})
            detail["decisions"].append(
                {
                    "date": rec["decision_date"],
                    "regime_note": prop.get("regime_note", ""),
                    "actions": prop.get("actions", []),
                    "views": prop.get("views", []),
                    "meta": prop.get("meta", {}),
                    "books": {
                        bk: {
                            "orders": [
                                f"{o['side']} {o['shares']} {o['ticker']}" for o in v["orders"]
                            ],
                            "blocked": v["blocked"],
                        }
                        for bk, v in rec.get("books", {}).items()
                    },
                    "prompt_file": f"state/arms/{inst.arm_id}"
                    + (f"/instances/{inst.instance}" if inst.instance else "")
                    + f"/decisions/{rec['decision_date']}.prompt.json",
                }
            )
        detail["items"] = _cited_items(detail["decisions"], env.data_root / "digests")
        views = load_views(paths.root)
        if not views.empty:
            uni_p = env.state_root / "universe"
            members: list[str] = []
            for up in sorted(uni_p.glob("*.json"))[-1:]:
                members = json.loads(up.read_text())["members"]
            scored = score_views(views, store, lambda d, m=members: m, today)
            entry["view_scores"] = summarize(scored)
        if arm.kind == "llm":
            entry["llm_cost_month_eur"] = env.llm.ledger.month_by_arm(today.year, today.month).get(
                inst.arm_id, 0.0
            )
        if paths.journal.exists():
            detail["journal"] = json.loads(paths.journal.read_text())
        arms_out.append(entry)
        (docs / "data" / "arms" / f"{inst.key.replace('@', '__')}.json").write_text(
            json.dumps(detail, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )
    # contemporaneous random percentile and replica spread
    by_key = {a["key"]: a for a in arms_out}
    for a in arms_out:
        for book_id, bk in a["books"].items():
            bk["metrics"]["percentile_vs_random_live"] = _percentile(
                bk["metrics"]["return"], random_returns.get(book_id, [])
            )
    if "A" in by_key:
        for book_id in books_cfg:
            reps = [
                by_key[k]["books"][book_id]["metrics"]["return"]
                for k in ("A", "R1", "R2")
                if k in by_key and book_id in by_key[k]["books"]
            ]
            if len(reps) >= 2:
                by_key["A"]["books"][book_id]["metrics"]["replica_spread"] = max(reps) - min(reps)
                by_key["A"]["books"][book_id]["metrics"]["replica_returns"] = reps
    # historical null
    hist_p = docs / "data" / "historical.json"
    historical = json.loads(hist_p.read_text()) if hist_p.exists() else None
    if historical:
        for a in arms_out:
            for book_id, bk in a["books"].items():
                samples = historical.get("random_excess_samples", {}).get(book_id, [])
                bk["metrics"]["percentile_vs_random_historical"] = _percentile(
                    bk["metrics"].get("vs_sp500"), samples
                )
    # health
    cost = env.llm.ledger
    health = {
        "last_runs": ledger.recent(today, 10)[-5:],
        "missed": ledger.missed(today)[-30:],
        "n_missed": len(ledger.missed(today)),
        "llm_spent_month_eur": cost.month_total_eur(today.year, today.month),
        "llm_cap_eur": env.llm.cap,
        "first_live": ledger.first_live().isoformat() if ledger.first_live() else None,
    }
    digest_p = env.data_root / "digests" / f"{today.isoformat()}.json"
    if digest_p.exists():
        d = json.loads(digest_p.read_text())
        health["sources"] = d.get("stats", {}).get("sources", [])
        health["digest"] = {
            "items": len(d.get("items", [])),
            "model": d.get("model"),
            "warnings": d.get("warnings", []),
        }
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": today.isoformat(),
        "dry_run": bool(env.dry_run),
        "arms": [{k: v for k, v in a.items()} for a in arms_out],
        "fee_profiles": [
            {
                "id": p.id,
                "name": p.name,
                "badge": p.badge,
                "warnings": p.warnings,
                "source": p.source,
            }
            for p in profiles.values()
        ],
        "books": [
            {"id": b.id, "label": b.label, "capital": b.capital_eur, "primary": b.primary}
            for b in repo.books().books
        ],
        "health": health,
        "historical": {
            "n_windows": historical.get("n_windows"),
            "pooled": historical.get("pooled_excess_vs_sp500"),
            "caveats": historical.get("caveats"),
        }
        if historical
        else None,
        "limits": repo.limits("base").model_dump(),
    }
    (docs / "data" / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    shutil.copyfile(TEMPLATE, docs / "index.html")
    (docs / ".nojekyll").touch()
    return {"arms": len(arms_out), "docs": str(docs), "as_of": today.isoformat()}
