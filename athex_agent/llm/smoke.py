"""One-call live check of the model integration: python -m athex_agent.llm.smoke

Needs ANTHROPIC_API_KEY. Makes one small Haiku structured call and one Sonnet call with effort,
prints usage and cost, and appends to .cache/smoke/llm_cost.jsonl (not the live ledger).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from athex_agent.llm.client import CostLedger, LLMClient, Pricing

REPO = Path(__file__).resolve().parents[2]


class Ping(BaseModel):
    ticker: str
    view: str
    reason: str


def main() -> int:
    llm = LLMClient(
        CostLedger(REPO / ".cache" / "smoke" / "llm_cost.jsonl"),
        Pricing.load(REPO / "configs" / "llm_pricing.yaml"),
        monthly_cap_eur=1.0,
        dry_run=False,
    )
    for model, effort in (("claude-haiku-4-5", None), ("claude-sonnet-5", "low")):
        res = llm.parse(
            model=model,
            system="Answer in the requested JSON only.",
            user='Give a neutral view on ticker "ETE.AT" with a one-sentence reason.',
            output_model=Ping,
            max_tokens=400,
            purpose="smoke",
            effort=effort,
        )
        print(
            f"{model}: {res.parsed.model_dump()} | tokens in/out {res.usage.input_tokens}/"
            f"{res.usage.output_tokens} | ${res.cost_usd:.5f} | stop={res.stop_reason}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
