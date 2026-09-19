"""LLM decider with a fake model, plus the arm-isolation guarantee."""

from __future__ import annotations

import json
import random
import shutil
from datetime import UTC, date, datetime
from types import SimpleNamespace

from athex_agent.arms.deciders import DecisionInputs
from athex_agent.arms.journal import Journal, JournalUpdates, ThesisUpdate
from athex_agent.arms.llm_decider import ActionOut, DecisionOutput, LLMDecider, ViewOut
from athex_agent.arms.runner import ArmRunner
from athex_agent.data.actions import ActionsStore
from athex_agent.digest.models import Digest, DigestItem
from athex_agent.llm.client import BudgetExceeded, CostLedger, LLMClient, LLMError, Pricing
from athex_agent.portfolio.accounting import BookState
from tests.conftest import synthetic_store
from tests.test_llm_client import REPO

D = date(2026, 10, 5)
TS = datetime(2026, 10, 5, 18, 40, tzinfo=UTC)
UNIVERSE = ["ETE.AT", "PPC.AT", "MOH.AT", "BELA.AT", "HTO.AT", "AETF.AT"]


def digest() -> Digest:
    mk = lambda i, cat, rel, tickers: DigestItem(  # noqa: E731
        id=f"it{i}",
        source_id="s",
        category=cat,
        reliability=rel,
        url=f"https://x.gr/{i}",
        title=f"title {i}",
        tickers=tickers,
        summary_en=f"SUMMARY-{cat.upper()}-{i}",
        importance=3,
    )
    return Digest(
        date=D.isoformat(),
        generated_at=TS,
        model="fake",
        items=[
            mk(1, "official", 5, ["ETE.AT"]),
            mk(2, "news", 4, ["PPC.AT"]),
            mk(3, "macro", 5, []),
            mk(4, "rumor", 1, ["MOH.AT"]),
        ],
        macro_summary="MACRO-TEXT",
        market_summary="MARKET-TEXT",
    )


class FakeLLM:
    def __init__(self, output=None, error=None, dry_run=False):
        self.output, self.error, self.dry_run = output, error, dry_run
        self.prompts: list[str] = []

    def parse(self, **kwargs):
        self.prompts.append(kwargs["user"])
        if self.dry_run:
            return SimpleNamespace(
                parsed=kwargs["dry_run_factory"](),
                cost_usd=0,
                cost_eur=0,
                usage=SimpleNamespace(input_tokens=0, output_tokens=0),
                model=kwargs["model"],
                dry_run=True,
            )
        if self.error:
            raise self.error
        return SimpleNamespace(
            parsed=self.output,
            cost_usd=0.08,
            cost_eur=0.07,
            usage=SimpleNamespace(input_tokens=12000, output_tokens=4000),
            model=kwargs["model"],
            dry_run=False,
        )


def inputs(repo, store, arm_id="A", books=None, buildup=False):
    resolved = repo.resolve_arm(arm_id)
    books = books or {
        "10k": BookState.new("10k", arm_id, 10_000, D),
        "1k": BookState.new("1k", arm_id, 1_000, D),
    }
    return DecisionInputs(
        arm=resolved,
        decision_date=D,
        decision_ts=TS,
        prices=store.as_of(D),
        universe=UNIVERSE,
        books=books,
        rng=random.Random(1),
        in_buildup=buildup,
    )


def test_decider_filters_and_reports(repo, tmp_path):
    store = synthetic_store(tmp_path / "p", dict.fromkeys(UNIVERSE, 10.0), end=D)
    out = DecisionOutput(
        views=[
            ViewOut(
                ticker="ETE.AT",
                view="buy",
                conviction=0.7,
                thesis="t",
                cited_item_ids=["it1", "ghost"],
            ),
            ViewOut(ticker="ETE.AT", view="sell", conviction=0.2, thesis="dup"),
            ViewOut(ticker="ZZZ.AT", view="buy", conviction=0.9, thesis="not in universe"),
        ],
        actions=[
            ActionOut(ticker="PPC.AT", kind="buy"),
            ActionOut(ticker="ZZZ.AT", kind="buy"),
            ActionOut(ticker="MOH.AT", kind="sell", reason="not held"),
        ],
        journal_updates=JournalUpdates(
            theses=[ThesisUpdate(ticker="PPC.AT", thesis="cheap power")]
        ),
        regime_note="calm",
    )
    llm = FakeLLM(output=out)
    arm = repo.arm("A")
    dec = LLMDecider(
        llm,
        arm.llm,
        arm.sources,
        digest(),
        Journal(arm_id="A"),
        view_scores={"horizons": {"20": {"hit_rate": 0.55}}},
    )
    p = dec.decide(inputs(repo, store))
    assert [v.ticker for v in p.views] == ["ETE.AT"] and p.views[0].cited_item_ids == ["it1"]
    assert [(a.kind, a.ticker) for a in p.actions] == [("buy", "PPC.AT")]
    assert sorted(p.meta["dropped"]) == [
        "action buy ZZZ.AT: not in universe",
        "action sell MOH.AT: not held",
        "view ETE.AT",
        "view ZZZ.AT",
    ]
    assert p.meta["cost"]["eur"] == 0.07 and p.meta["prompt_version"] == "base-v1"
    assert p.meta["item_ids"] == ["it1", "it3", "it2", "it4"] and p.regime_note == "calm"
    payload = json.loads(llm.prompts[0])
    assert payload["your_view_scores_so_far"]["horizons"]["20"]["hit_rate"] == 0.55
    assert payload["digest"]["macro_summary"] == "MACRO-TEXT" and len(payload["prices"]) == 6
    assert dec.last_raw["regime_note"] == "calm"


def test_source_toggles_filter_the_digest(repo, tmp_path):
    store = synthetic_store(tmp_path / "p", dict.fromkeys(UNIVERSE, 10.0), end=D)
    for arm_id, expected in (
        ("A", {"official", "news", "macro", "rumor"}),
        ("B", {"official", "news", "macro"}),
        ("C", {"official"}),
    ):
        arm = repo.arm(arm_id)
        llm = FakeLLM(output=DecisionOutput(views=[]))
        LLMDecider(llm, arm.llm, arm.sources, digest(), Journal(arm_id=arm_id)).decide(
            inputs(repo, store, arm_id)
        )
        payload = json.loads(llm.prompts[0])
        assert {it["category"] for it in payload["digest"]["items"]} == expected, arm_id
        if arm_id == "C":
            assert "SUMMARY-NEWS" not in llm.prompts[0] and "SUMMARY-RUMOR" not in llm.prompts[0]
            assert payload["digest"]["macro_summary"] == "(macro hidden)"


def test_failure_modes_hold(repo, tmp_path):
    store = synthetic_store(tmp_path / "p", dict.fromkeys(UNIVERSE, 10.0), end=D)
    arm = repo.arm("A")
    llm = FakeLLM(error=LLMError("boom"))
    p = LLMDecider(llm, arm.llm, arm.sources, digest(), Journal(arm_id="A")).decide(
        inputs(repo, store)
    )
    assert p.actions == [] and p.views == [] and p.meta["hold_reason"] == "model_failure"
    assert len(llm.prompts) == 2  # retried once
    llm = FakeLLM(error=BudgetExceeded("cap"))
    p = LLMDecider(llm, arm.llm, arm.sources, digest(), Journal(arm_id="A")).decide(
        inputs(repo, store)
    )
    assert p.meta["hold_reason"] == "budget" and len(llm.prompts) == 1
    llm = FakeLLM(dry_run=True)
    p = LLMDecider(llm, arm.llm, arm.sources, None, Journal(arm_id="A")).decide(
        inputs(repo, store, buildup=True)
    )
    assert len(p.buys) == 4 and p.meta["dry_run"] is True


def test_real_client_dry_run_through_runner(repo, tmp_path):
    """LLMClient in DRY_RUN + ArmRunner: decisions, prompt log and journal files are written."""
    store = synthetic_store(tmp_path / "p", dict.fromkeys(UNIVERSE, 10.0), end=D)
    llm = LLMClient(
        CostLedger(tmp_path / "c.jsonl"),
        Pricing.load(REPO / "configs" / "llm_pricing.yaml"),
        monthly_cap_eur=25,
        dry_run=True,
    )
    resolved = repo.resolve_arm("A")
    runner = ArmRunner(
        resolved,
        store,
        ActionsStore(tmp_path / "a"),
        tmp_path / "state",
        decider=LLMDecider(
            llm, resolved.arm.llm, resolved.arm.sources, digest(), Journal(arm_id="A")
        ),
    )
    runner.start(D)
    rec = runner.decide(D, TS, UNIVERSE)
    assert len(rec["books"]["10k"]["orders"]) == 4 and rec["proposal"]["meta"]["dry_run"]
    prompt = json.loads(runner.paths.prompt(D).read_text())
    assert prompt["system_prompt_version"] == "base-v1" and "MACRO-TEXT" in prompt["user_payload"]
    assert runner.paths.journal.exists()


def test_arm_isolation(repo, tmp_path):
    """No arm may see another arm's journal or holdings; an arm runs from its own directory."""
    store = synthetic_store(tmp_path / "p", dict.fromkeys(UNIVERSE, 10.0), end=D)
    actions = ActionsStore(tmp_path / "a")
    state = tmp_path / "state"
    prompts: dict[str, str] = {}
    for arm_id, secret, ticker in (
        ("A", "ZEBRA-THESIS-A", "ETE.AT"),
        ("B", "GIRAFFE-THESIS-B", "PPC.AT"),
    ):
        resolved = repo.resolve_arm(arm_id)
        journal = Journal(arm_id=arm_id)
        journal.apply_updates(
            JournalUpdates(theses=[ThesisUpdate(ticker=ticker, thesis=secret)]), D, set(UNIVERSE)
        )
        llm = FakeLLM(
            output=DecisionOutput(
                views=[ViewOut(ticker=ticker, view="buy", conviction=0.6, thesis=secret)],
                actions=[ActionOut(ticker=ticker, kind="buy")],
            )
        )
        runner = ArmRunner(
            resolved,
            store,
            actions,
            state,
            decider=LLMDecider(llm, resolved.arm.llm, resolved.arm.sources, digest(), journal),
        )
        runner.start(D)
        runner.decide(D, TS, UNIVERSE)
        prompts[arm_id] = llm.prompts[0]
    assert "ZEBRA-THESIS-A" not in prompts["B"] and "GIRAFFE-THESIS-B" not in prompts["A"]
    assert "ZEBRA-THESIS-A" in json.loads(prompts["A"])["journal"]
    # B's pending buy of PPC must not appear in A's book view and vice versa
    assert "PPC.AT" not in json.loads(prompts["A"])["books"]["10k"]["pending_orders"]
    # an arm constructed from a state root holding only its own directory still runs
    lonely = tmp_path / "lonely"
    shutil.copytree(state / "arms" / "B", lonely / "arms" / "B")
    resolved = repo.resolve_arm("B")
    journal = Journal.load(lonely / "arms" / "B" / "journal.json", "B")
    llm = FakeLLM(output=DecisionOutput(views=[]))
    runner = ArmRunner(
        resolved,
        store,
        actions,
        lonely,
        decider=LLMDecider(llm, resolved.arm.llm, resolved.arm.sources, digest(), journal),
    )
    rec = runner.decide(date(2026, 10, 6), datetime(2026, 10, 6, 18, 40, tzinfo=UTC), UNIVERSE)
    assert rec["arm_id"] == "B" and "GIRAFFE-THESIS-B" in llm.prompts[0]
    assert not (lonely / "arms" / "A").exists()
