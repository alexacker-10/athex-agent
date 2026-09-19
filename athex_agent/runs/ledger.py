"""Run ledger: one JSON per trading date under state/ledger/runs/, plus missed-run detection.

A job is idempotent per (trading date, job): a second invocation finds the success record and
exits. A decision run that never happened for a past trading day is reported as MISSED and is
never back-filled (that would be lookahead)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from athex_agent.data import calendar as cal

JobName = Literal["fill", "decide", "digest", "dashboard", "historical", "bootstrap"]
Status = Literal["running", "success", "failed", "skipped"]


@dataclass
class RunRecord:
    job: str
    status: Status
    started: str
    finished: str | None = None
    note: str = ""
    dry_run: bool = False


class RunLedger:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def path(self, d: date) -> Path:
        return self.root / "runs" / f"{d.isoformat()}.json"

    def load(self, d: date) -> dict[str, Any]:
        p = self.path(d)
        return (
            json.loads(p.read_text(encoding="utf-8"))
            if p.exists()
            else {"date": d.isoformat(), "jobs": {}}
        )

    def save(self, d: date, payload: dict[str, Any]) -> None:
        self.path(d).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def status(self, d: date, job: str) -> str | None:
        return self.load(d)["jobs"].get(job, {}).get("status")

    def already_done(self, d: date, job: str) -> bool:
        return self.status(d, job) == "success"

    def start(self, d: date, job: str, dry_run: bool = False) -> None:
        payload = self.load(d)
        payload["jobs"][job] = {
            "status": "running",
            "started": datetime.now(UTC).isoformat(),
            "finished": None,
            "note": "",
            "dry_run": dry_run,
        }
        self.save(d, payload)

    def finish(self, d: date, job: str, status: Status, note: str = "") -> None:
        payload = self.load(d)
        rec = payload["jobs"].setdefault(job, {"started": datetime.now(UTC).isoformat()})
        rec.update(
            {"status": status, "finished": datetime.now(UTC).isoformat(), "note": note[:2000]}
        )
        self.save(d, payload)

    # ---- first live date and missed runs
    def first_live_path(self) -> Path:
        return self.root / "first_live.json"

    def first_live(self) -> date | None:
        p = self.first_live_path()
        return date.fromisoformat(json.loads(p.read_text())["date"]) if p.exists() else None

    def mark_first_live(self, d: date) -> None:
        if self.first_live() is None:
            self.first_live_path().write_text(json.dumps({"date": d.isoformat()}) + "\n")

    def missed(self, upto: date, jobs: tuple[str, ...] = ("decide",)) -> list[dict[str, str]]:
        """Trading days since the first live decide with no successful record for `jobs`."""
        start = self.first_live()
        if start is None:
            return []
        out = []
        for d in cal.trading_days(start, upto):
            payload = self.load(d)
            for job in jobs:
                st = payload["jobs"].get(job, {}).get("status")
                if st != "success":
                    out.append({"date": d.isoformat(), "job": job, "status": st or "missing"})
        return out

    def recent(self, upto: date, n: int = 30) -> list[dict[str, Any]]:
        days = cal.trading_days(cal.add_trading_days(upto, -n), upto)
        return [self.load(d) for d in days if self.path(d).exists()]
