"""Ledger, orchestration, git timestamps and an end-to-end dry run of the daily jobs."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from athex_agent.config.loader import ConfigRepo
from athex_agent.data import calendar as cal
from athex_agent.data.sources import Quote
from athex_agent.digest.models import Digest, DigestItem
from athex_agent.runs import jobs as jobs_mod
from athex_agent.runs.git import commit_and_push, committed_ts
from athex_agent.runs.jobs import Env, run_job
from athex_agent.runs.ledger import RunLedger
from athex_agent.runs.orchestrate import (
    active_instances,
    plan_llm_calls,
    random_p_swap_from,
    shed_order,
)
from tests.conftest import synthetic_store

REPO = Path(__file__).resolve().parents[1]


def test_ledger_idempotency_and_missed_runs(tmp_path):
    led = RunLedger(tmp_path / "ledger")
    d = date(2026, 10, 5)
    assert not led.already_done(d, "decide") and led.missed(d) == []
    led.mark_first_live(d)
    led.start(d, "decide")
    assert led.status(d, "decide") == "running"
    led.finish(d, "decide", "success", "ok")
    assert led.already_done(d, "decide")
    led.start(date(2026, 10, 6), "decide")
    led.finish(date(2026, 10, 6), "decide", "failed", "boom")
    missed = led.missed(date(2026, 10, 8))
    assert [(m["date"], m["status"]) for m in missed] == [
        ("2026-10-06", "failed"),
        ("2026-10-07", "missing"),
        ("2026-10-08", "missing"),
    ]
    led.mark_first_live(date(2026, 1, 1))  # never overwritten
    assert led.first_live() == d
    assert len(led.recent(date(2026, 10, 8), 10)) == 2


def test_orchestrator_instances_and_shedding(repo):
    first = date(2026, 10, 1)
    inst = active_instances(repo, first, date(2026, 10, 15))
    keys = {i.key for i in inst}
    assert {
        "A",
        "B",
        "C",
        "D",
        "R1",
        "R2",
        "bh_sp500",
        "bh_athex",
        "momentum",
        "equal_weight",
    } <= keys
    assert sum(1 for i in inst if i.arm_id == "random") == 200
    assert not any(i.cohort for i in inst)  # no cohort before the next month starts
    later = active_instances(repo, first, date(2026, 12, 3))
    cohorts = sorted({i.cohort for i in later if i.cohort})
    assert cohorts == ["2026-11", "2026-12"]
    a_cohorts = [i for i in later if i.arm_id == "A" and i.cohort]
    assert [i.start_on for i in a_cohorts] == [date(2026, 11, 2), date(2026, 12, 1)]
    assert all(i.key == f"A@cohort-{i.cohort}" for i in a_cohorts)
    assert sum(1 for i in later if i.arm_id == "random" and i.cohort == "2026-11") == 50
    # shedding: cohorts first, then D, C, B, replicas; A last
    order = sorted((i for i in later if i.is_llm), key=shed_order)
    assert order[0].cohort is not None and order[-1].arm_id == "A"
    allowed = plan_llm_calls([i for i in later if i.is_llm], remaining_eur=0.30)
    assert allowed["A"] and allowed["R1"] and not allowed["D"] and not allowed["A@cohort-2026-11"]
    assert all(plan_llm_calls([i for i in later if i.is_llm], remaining_eur=100).values())
    assert random_p_swap_from(4, 60) == pytest.approx(2 / 60) and random_p_swap_from(0, 0) == 1 / 21


def test_git_committed_ts(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    f = repo / "state" / "x.json"
    f.parent.mkdir()
    f.write_text("{}")
    assert committed_ts(f, repo) is None
    assert commit_and_push(repo, "first", ["state"], push=False)
    ts = committed_ts(f, repo)
    assert ts is not None and abs((datetime.now(UTC) - ts).total_seconds()) < 60
    assert not commit_and_push(repo, "nothing", ["state"], push=False)
    assert committed_ts(tmp_path / "nowhere.json", tmp_path) is None


class EmptySource:
    name = "fake"

    def fetch_daily(self, ticker, start):

        e = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        e.index = pd.DatetimeIndex([], name="date")
        a = pd.DataFrame(columns=["kind", "value"])
        a.index = pd.DatetimeIndex([], name="date")
        return e, a

    def fetch_quote(self, ticker):
        return None

    def fetch_intraday_open(self, ticker, d):
        return None


@pytest.fixture
def small_repo(tmp_path, repo):
    """Configs copied with a 3-seed random arm and a tiny candidate list."""
    root = tmp_path / "configs"
    shutil.copytree(repo.root, root)
    r = root / "arms" / "random.yaml"
    r.write_text(r.read_text().replace("n_seeds: 200", "n_seeds: 3"))
    u = root / "universe.yaml"
    text = u.read_text()
    head = text.split("candidates:")[0]
    u.write_text(
        head
        + "candidates:\n  - ETE.AT\n  - PPC.AT\n  - MOH.AT\n  - BELA.AT\n  - HTO.AT\n  - ALWN.AT\n"
        "aliases:\n  OPAP.AT: ALWN.AT\n"
    )
    return ConfigRepo(root)


def test_decide_job_end_to_end_dry_run(tmp_path, small_repo, monkeypatch):
    universe = ["ETE.AT", "PPC.AT", "MOH.AT", "BELA.AT", "HTO.AT", "ALWN.AT"]
    env = Env(
        repo_root=REPO,
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        docs_root=tmp_path / "docs",
        dry_run=True,
        source=EmptySource(),
    )
    synthetic_store(
        env.data_root / "prices",
        {
            **dict.fromkeys(universe, 12.0),
            "AETF.AT": 60.0,
            "SXR8.DE": 700.0,
            "GD.AT": 2600.0,
            "EURUSD=X": 1.15,
        },
        end=date(2026, 10, 9),
        n=250,
    )

    # no network: canned digest
    def fake_digest(repo, env_, today, uni):
        return Digest(
            date=today.isoformat(),
            generated_at=datetime.now(UTC),
            model="fake",
            items=[
                DigestItem(
                    id="i1",
                    source_id="naftemporiki",
                    category="news",
                    reliability=4,
                    url="https://x.gr/1",
                    title="ETE results",
                    tickers=["ETE.AT"],
                    summary_en="strong quarter",
                    importance=4,
                )
            ],
        )

    monkeypatch.setattr(jobs_mod, "job_digest", fake_digest)
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    env.now = cal.session_close_utc(d1) + timedelta(hours=1)
    out = run_job("decide", env, as_of=d1, repo=small_repo)
    assert out["universe_size"] == 7 and out["digest"]["items"] == 1  # 6 names + AETF.AT
    assert set(out["arms"]) >= {"A", "B", "C", "D", "R1", "R2", "bh_sp500", "random@seed-000"}
    assert out["arms"]["A"]["orders"] == {"10k": 4, "1k": 4}  # dry-run build-up
    assert out["arms"]["bh_sp500"]["orders"] == {"10k": 1, "1k": 1}
    # next morning: fills at the open
    env.now = cal.session_open_utc(d2) + timedelta(hours=1)
    out = run_job("fill", env, as_of=d2, repo=small_repo)
    assert out["filled"] >= 20 and out["pending"] == 0
    # second decide: nothing new to buy for A (already 4 positions), NAV recorded, dashboard built
    env.now = cal.session_close_utc(d2) + timedelta(hours=1)
    out = run_job("decide", env, as_of=d2, repo=small_repo)
    assert out["arms"]["A"]["orders"] == {"10k": 0, "1k": 0}
    summary = json.loads((env.docs_root / "data" / "summary.json").read_text())
    assert summary["dry_run"] is True and summary["as_of"] == "2026-10-02"
    a = next(x for x in summary["arms"] if x["key"] == "A")
    assert len(a["books"]["10k"]["nav"]) == 2 and len(a["books"]["10k"]["holdings"]) == 4
    assert (
        a["books"]["10k"]["metrics"]["n_trades"] == 4
        and a["books"]["10k"]["fee_profile"] == "degiro"
    )
    assert (
        a["books"]["10k"]["recost_fees_eur"]["nbg_securities"]
        > a["books"]["10k"]["recost_fees_eur"]["degiro"]
    )
    assert "replica_spread" in a["books"]["10k"]["metrics"]
    piraeus = next(p for p in summary["fee_profiles"] if p["id"] == "piraeus_online")
    assert piraeus["badge"] == "UNVERIFIED MINIMUM"
    assert (env.docs_root / "index.html").exists() and (
        env.docs_root / "data" / "arms" / "A.json"
    ).exists()
    detail = json.loads((env.docs_root / "data" / "arms" / "A.json").read_text())
    assert len(detail["decisions"]) == 2 and detail["decisions"][0]["books"]["10k"]["orders"]
    # ledger: both days succeeded; dry runs never set first_live
    assert env.ledger.status(d1, "decide") == "success" and env.ledger.first_live() is None
    # the universe snapshot is cached per month
    assert (env.state_root / "universe" / "2026-10.json").exists()


def test_fill_before_open_is_skipped(tmp_path, small_repo):
    env = Env(
        repo_root=REPO,
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        docs_root=tmp_path / "docs",
        dry_run=True,
        source=EmptySource(),
    )
    env.now = cal.session_open_utc(date(2026, 10, 2)) - timedelta(minutes=30)
    out = run_job("fill", env, repo=small_repo)
    assert out["skipped"] and "before the opening" in out["reason"]


def test_quote_dataclass_is_frozen():
    q = Quote("ETE.AT", 17.5, 17.4, 17.3, 17.6, 17.2, 1.0, datetime.now(UTC))
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.price = 1.0  # type: ignore[misc]
