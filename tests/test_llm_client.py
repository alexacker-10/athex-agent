from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from athex_agent.llm.client import (
    BudgetExceeded,
    CostLedger,
    LLMClient,
    LLMRefused,
    LLMTruncated,
    Pricing,
    Usage,
)

REPO = Path(__file__).resolve().parents[1]


class Out(BaseModel):
    answer: str


class FakeMessages:
    def __init__(self, parsed=None, stop="end_turn", usage=None):
        self.parsed, self.stop, self.usage = parsed, stop, usage
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self.parsed, stop_reason=self.stop, usage=self.usage)


def fake_client(**kw):
    m = FakeMessages(**kw)
    return SimpleNamespace(messages=m), m


def test_pricing_and_cost_ledger(tmp_path):
    pricing = Pricing.load(REPO / "configs" / "llm_pricing.yaml")
    usage = Usage(input_tokens=12_500, output_tokens=5_000, cache_read_tokens=2_000)
    assert pricing.cost_usd("claude-sonnet-5", usage) == pytest.approx(0.025 + 0.05 + 0.0004)
    assert pricing.cost_usd("claude-haiku-4-5", Usage(75_000, 6_000)) == pytest.approx(0.105)
    ledger = CostLedger(tmp_path / "llm_cost.jsonl")
    ledger.append({"ts": "2026-10-03T18:30:00+00:00", "cost_eur": 0.1, "arm_id": "A"})
    ledger.append({"ts": "2026-10-04T18:30:00+00:00", "cost_eur": 0.2, "arm_id": "B"})
    ledger.append({"ts": "2026-11-01T18:30:00+00:00", "cost_eur": 5.0, "arm_id": "A"})
    assert ledger.month_total_eur(2026, 10) == pytest.approx(0.3)
    assert ledger.month_by_arm(2026, 10) == {"A": 0.1, "B": 0.2}


def test_parse_meters_and_caps(tmp_path):
    pricing = Pricing.load(REPO / "configs" / "llm_pricing.yaml")
    ledger = CostLedger(tmp_path / "llm_cost.jsonl")
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=300,
    )
    client, msgs = fake_client(parsed=Out(answer="ok"), usage=usage)
    llm = LLMClient(ledger, pricing, monthly_cap_eur=1.0, dry_run=False, client=client)
    now = datetime(2026, 10, 5, 18, 30, tzinfo=UTC)
    res = llm.parse(
        model="claude-sonnet-5",
        system="sys",
        user="u",
        output_model=Out,
        max_tokens=100,
        purpose="test",
        arm_id="A",
        effort="medium",
        now=now,
    )
    assert res.parsed.answer == "ok" and res.stop_reason == "end_turn"
    assert res.cost_usd == pytest.approx((1000 * 2 + 500 * 10 + 300 * 2.5) / 1e6)
    assert ledger.records()[0]["arm_id"] == "A" and llm.spent_eur(now) == pytest.approx(
        res.cost_eur
    )
    call = msgs.calls[0]
    assert call["output_format"] is Out and call["output_config"] == {"effort": "medium"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # cap: an estimate that would exceed the remaining budget is refused before any call
    with pytest.raises(BudgetExceeded):
        llm.parse(
            model="claude-sonnet-5",
            system="s",
            user="u",
            output_model=Out,
            max_tokens=10,
            purpose="test",
            estimate_eur=2.0,
            now=now,
        )
    assert len(msgs.calls) == 1
    assert llm.remaining_eur(now) == pytest.approx(1.0 - res.cost_eur)


def test_dry_run_refusal_and_truncation(tmp_path):
    pricing = Pricing.load(REPO / "configs" / "llm_pricing.yaml")
    ledger = CostLedger(tmp_path / "llm_cost.jsonl")
    llm = LLMClient(ledger, pricing, monthly_cap_eur=25, dry_run=True, client=None)
    res = llm.parse(
        model="claude-haiku-4-5",
        system="s",
        user="u",
        output_model=Out,
        max_tokens=10,
        purpose="p",
        dry_run_factory=lambda: Out(answer="canned"),
    )
    assert res.dry_run and res.parsed.answer == "canned" and ledger.records() == []
    client, _ = fake_client(
        parsed=None, stop="refusal", usage=SimpleNamespace(input_tokens=1, output_tokens=0)
    )
    llm = LLMClient(ledger, pricing, monthly_cap_eur=25, dry_run=False, client=client)
    with pytest.raises(LLMRefused):
        llm.parse(
            model="claude-haiku-4-5",
            system="s",
            user="u",
            output_model=Out,
            max_tokens=10,
            purpose="p",
        )
    client, _ = fake_client(
        parsed=None, stop="max_tokens", usage=SimpleNamespace(input_tokens=1, output_tokens=10)
    )
    llm = LLMClient(ledger, pricing, monthly_cap_eur=25, dry_run=False, client=client)
    with pytest.raises(LLMTruncated):
        llm.parse(
            model="claude-haiku-4-5",
            system="s",
            user="u",
            output_model=Out,
            max_tokens=10,
            purpose="p",
        )
    assert len(ledger.records()) == 2  # refused and truncated calls are still metered
