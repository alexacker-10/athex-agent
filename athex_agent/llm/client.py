"""Anthropic client wrapper used by the digest and the LLM arms.

- Structured outputs via `client.messages.parse(output_format=<pydantic model>)`.
- Every call is metered (tokens, USD, EUR) and appended to state/ledger/llm_cost.jsonl.
- A hard monthly cap (EUR) is enforced before each call: BudgetExceeded is raised, never a call.
- DRY_RUN returns a caller-supplied stand-in output without touching the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
DEFAULT_CAP_EUR = 25.0


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMRefused(LLMError):
    pass


class LLMTruncated(LLMError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float
    cache_write: float


class Pricing:
    def __init__(self, models: dict[str, ModelPrice], eur_per_usd: float) -> None:
        self.models = models
        self.eur_per_usd = eur_per_usd

    @classmethod
    def load(cls, path: Path, eur_per_usd: float | None = None) -> Pricing:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        models = {k: ModelPrice(**v) for k, v in data["models"].items()}
        return cls(models, eur_per_usd or float(data["eur_per_usd"]))

    def cost_usd(self, model: str, usage: Usage) -> float:
        p = self.models.get(model)
        if p is None:
            raise LLMError(f"no pricing for model {model}")
        return (
            usage.input_tokens * p.input
            + usage.output_tokens * p.output
            + usage.cache_read_tokens * p.cache_read
            + usage.cache_write_tokens * p.cache_write
        ) / 1_000_000


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> Usage:
        return cls(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )


@dataclass
class LLMResult[T: BaseModel]:
    parsed: T
    usage: Usage
    cost_usd: float
    cost_eur: float
    model: str
    stop_reason: str
    dry_run: bool = False


class CostLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def month_total_eur(self, year: int, month: int) -> float:
        prefix = f"{year:04d}-{month:02d}"
        return round(sum(r["cost_eur"] for r in self.records() if r["ts"].startswith(prefix)), 6)

    def month_by_arm(self, year: int, month: int) -> dict[str, float]:
        prefix = f"{year:04d}-{month:02d}"
        out: dict[str, float] = {}
        for r in self.records():
            if r["ts"].startswith(prefix):
                key = r.get("arm_id") or r.get("purpose") or "?"
                out[key] = round(out.get(key, 0.0) + r["cost_eur"], 6)
        return out


class LLMClient:
    def __init__(
        self,
        ledger: CostLedger,
        pricing: Pricing,
        monthly_cap_eur: float | None = None,
        dry_run: bool | None = None,
        client: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.pricing = pricing
        self.cap = (
            monthly_cap_eur
            if monthly_cap_eur is not None
            else float(os.environ.get("LLM_MONTHLY_CAP_EUR", DEFAULT_CAP_EUR))
        )
        self.dry_run = dry_run if dry_run is not None else os.environ.get("DRY_RUN", "0") == "1"
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def spent_eur(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return self.ledger.month_total_eur(now.year, now.month)

    def remaining_eur(self, now: datetime | None = None) -> float:
        return round(self.cap - self.spent_eur(now), 6)

    def can_spend(self, estimate_eur: float, now: datetime | None = None) -> bool:
        return self.spent_eur(now) + estimate_eur <= self.cap + 1e-9

    def parse(
        self,
        *,
        model: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int,
        purpose: str,
        arm_id: str | None = None,
        effort: str | None = None,
        estimate_eur: float = 0.0,
        dry_run_factory: Callable[[], T] | None = None,
        cache_system: bool = True,
        now: datetime | None = None,
    ) -> LLMResult[T]:
        now = now or datetime.now(UTC)
        if not self.can_spend(estimate_eur, now):
            raise BudgetExceeded(
                f"monthly LLM cap EUR {self.cap:.2f} would be exceeded "
                f"(spent {self.spent_eur(now):.2f}, estimate {estimate_eur:.2f})"
            )
        if self.dry_run:
            if dry_run_factory is None:
                raise LLMError("dry run requires a dry_run_factory")
            return LLMResult(dry_run_factory(), Usage(), 0.0, 0.0, model, "dry_run", dry_run=True)
        system_blocks: Any = system
        if cache_system:
            system_blocks = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
            output_format=output_model,
        )
        if effort:
            kwargs["output_config"] = {"effort": effort}
        try:
            import anthropic

            errors: tuple[type[Exception], ...] = (anthropic.APIError,)
        except ImportError:  # pragma: no cover - the SDK is a pinned dependency
            errors = (Exception,)
        try:
            resp = self.client.messages.parse(**kwargs)
        except errors as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        usage = Usage.from_response(getattr(resp, "usage", None))
        cost_usd = self.pricing.cost_usd(model, usage)
        cost_eur = round(cost_usd * self.pricing.eur_per_usd, 6)
        stop = str(getattr(resp, "stop_reason", "") or "")
        self.ledger.append(
            {
                "ts": now.isoformat(),
                "model": model,
                "purpose": purpose,
                "arm_id": arm_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost_usd": round(cost_usd, 6),
                "cost_eur": cost_eur,
                "stop_reason": stop,
            }
        )
        if stop == "refusal":
            raise LLMRefused(f"{purpose}: model declined the request")
        if stop == "max_tokens":
            raise LLMTruncated(f"{purpose}: output hit max_tokens={max_tokens}")
        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            raise LLMError(f"{purpose}: no parsed output (stop_reason={stop})")
        return LLMResult(parsed, usage, cost_usd, cost_eur, model, stop)
