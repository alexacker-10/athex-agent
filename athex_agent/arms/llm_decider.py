"""LLM decider: builds the prompt from the filtered digest, prices, books and journal; parses a
structured decision; validates it; returns a Proposal. On model failure it holds.

Everything the model saw is reproducible: the decision record stores the prompt version, the
digest id, the included item ids, the book/journal snapshots and the raw parsed output.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from athex_agent.arms.deciders import DecisionInputs
from athex_agent.arms.journal import Journal, JournalUpdates
from athex_agent.arms.prompts import BASE_SYSTEM_PROMPT, PROMPT_VERSIONS
from athex_agent.arms.proposal import Action, Proposal, View, ViewLabel
from athex_agent.config.models import LLMSettings, SourceToggles
from athex_agent.data import calendar as cal
from athex_agent.digest.models import Digest
from athex_agent.llm.client import BudgetExceeded, LLMClient, LLMError

log = logging.getLogger(__name__)

SYSTEM_PROMPTS = {"base": BASE_SYSTEM_PROMPT}
MAX_DIGEST_ITEMS = 60
MAX_JOURNAL_CHARS = 9000


class ViewOut(BaseModel):
    ticker: str
    view: ViewLabel
    conviction: float = Field(ge=0, le=1)
    horizon_days: int = Field(default=20, ge=1, le=250)
    thesis: str = Field(max_length=600)
    catalysts: list[str] = []
    risks: list[str] = []
    cited_item_ids: list[str] = []


class ActionOut(BaseModel):
    ticker: str
    kind: str = Field(pattern="^(buy|sell)$")
    reason: str = Field(default="", max_length=400)


class DecisionOutput(BaseModel):
    views: list[ViewOut]
    actions: list[ActionOut] = []
    journal_updates: JournalUpdates = JournalUpdates()
    regime_note: str = Field(default="", max_length=600)


class LLMDecider:
    def __init__(
        self,
        llm: LLMClient,
        settings: LLMSettings,
        sources: SourceToggles,
        digest: Digest | None,
        journal: Journal,
        view_scores: dict[str, Any] | None = None,
        estimate_eur: float = 0.10,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.sources = sources
        self.digest = digest
        self.journal = journal
        self.view_scores = view_scores or {}
        self.estimate_eur = estimate_eur
        self.last_prompt: str | None = None
        self.last_raw: dict[str, Any] | None = None

    def tradable(self, inp: DecisionInputs) -> set[str]:
        return set(inp.universe)

    # ---- prompt
    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSIONS[self.settings.prompt_variant]

    def system_prompt(self) -> str:
        return SYSTEM_PROMPTS[self.settings.prompt_variant]

    def _price_table(self, inp: DecisionInputs) -> list[dict[str, Any]]:
        held = inp.held()
        rows = []
        for t in sorted(inp.universe):
            df = inp.prices.bars(t)
            if df.empty:
                continue
            c = df["close"]
            last = float(c.iloc[-1])

            def ret(n: int, c=c, last=last) -> float | None:
                return round(last / float(c.iloc[-1 - n]) - 1.0, 4) if len(c) > n else None

            rows.append(
                {
                    "ticker": t,
                    "close": round(last, 3),
                    "r1d": ret(1),
                    "r5d": ret(5),
                    "r20d": ret(20),
                    "r60d": ret(60),
                    "adv_eur_20d": round(inp.prices.adv_eur(t, 20)),
                    "held": t in held,
                }
            )
        return rows

    def _books(self, inp: DecisionInputs) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for book_id, book in inp.books.items():
            closes = {t: inp.prices.close(t) for t in book.positions}
            nav = book.nav(closes)
            positions = []
            for t, pos in book.positions.items():
                px = closes[t]
                positions.append(
                    {
                        "ticker": t,
                        "shares": pos.shares,
                        "avg_cost": round(pos.avg_cost, 4),
                        "last": round(px, 4),
                        "pnl_pct": round(px / pos.avg_cost - 1.0, 4),
                        "weight": round(pos.shares * px / nav, 4) if nav else None,
                        "held_trading_days": cal.trading_days_between(
                            pos.last_acquired, inp.decision_date
                        ),
                    }
                )
            d = inp.decision_date
            out[book_id] = {
                "nav": nav,
                "cash": book.cash_eur,
                "positions": positions,
                "pending_orders": [f"{o.side} {o.shares} {o.ticker}" for o in book.pending_orders],
                "orders_used_this_month": book.orders_placed_in_month(d.year, d.month),
                "fees_paid_total": book.fees_paid_eur + book.sales_tax_paid_eur,
                "recent_divergences": [
                    f"{x.date} {x.ticker} {x.kind}" for x in book.divergences[-5:]
                ],
            }
        return out

    def _digest_block(self) -> dict[str, Any]:
        if self.digest is None:
            return {"digest": None, "note": "no digest available today"}
        items = self.digest.view(self.sources)
        items = sorted(items, key=lambda it: (-it.importance, -it.reliability))[:MAX_DIGEST_ITEMS]
        return {
            "digest_id": self.digest.digest_id,
            "date": self.digest.date,
            "macro_summary": self.digest.macro_summary if self.sources.macro else "(macro hidden)",
            "market_summary": self.digest.market_summary,
            "items": [
                {
                    "id": it.id,
                    "category": it.category,
                    "reliability": it.reliability,
                    "source": it.source_id,
                    "tickers": it.tickers,
                    "event_type": it.event_type,
                    "importance": it.importance,
                    "sentiment": it.sentiment,
                    "published": it.published.isoformat() if it.published else None,
                    "summary": it.summary_en,
                    "red_flags": it.red_flags,
                    "url": it.url,
                }
                for it in items
            ],
        }

    def build_user_prompt(self, inp: DecisionInputs) -> tuple[str, dict[str, Any]]:
        digest_block = self._digest_block()
        payload = {
            "decision_date": inp.decision_date.isoformat(),
            "arm_id": inp.arm.arm.id,
            "buildup_phase": inp.in_buildup,
            "sources_enabled": self.sources.model_dump(),
            "universe": sorted(inp.universe),
            "prices": self._price_table(inp),
            "books": self._books(inp),
            "digest": digest_block,
            "journal": self.journal.render(MAX_JOURNAL_CHARS),
            "your_view_scores_so_far": self.view_scores,
            "closed_trades_awaiting_lesson": [
                f"{c.ticker}@{c.closed.isoformat()}: {c.return_pct:+.1%} ({c.reason})"
                for c in self.journal.closed_trades
                if not c.lesson
            ][-5:],
        }
        meta = {
            "digest_id": digest_block.get("digest_id"),
            "item_ids": [it["id"] for it in digest_block.get("items", [])],
            "prompt_version": self.prompt_version,
        }
        return json.dumps(payload, ensure_ascii=False), meta

    # ---- decision
    def _dry_output(self, inp: DecisionInputs) -> DecisionOutput:
        held = inp.held()
        actions = []
        if inp.in_buildup:
            for t in sorted(inp.universe):
                if len(held) + len(actions) >= inp.target_positions:
                    break
                if t not in held:
                    actions.append(ActionOut(ticker=t, kind="buy", reason="dry-run build-up"))
        views = [
            ViewOut(ticker=t, view="buy", conviction=0.5, thesis="dry-run placeholder")
            for t in sorted(inp.universe)[:5]
        ]
        return DecisionOutput(views=views, actions=actions, regime_note="dry run")

    def decide(self, inp: DecisionInputs) -> Proposal:
        user, meta = self.build_user_prompt(inp)
        self.last_prompt = user
        universe = set(inp.universe)
        held = inp.held()
        output: DecisionOutput | None = None
        errors: list[str] = []
        cost = {"usd": 0.0, "eur": 0.0, "input_tokens": 0, "output_tokens": 0}
        for attempt in (1, 2):
            try:
                res = self.llm.parse(
                    model=self.settings.model,
                    system=self.system_prompt(),
                    user=user,
                    output_model=DecisionOutput,
                    max_tokens=self.settings.max_output_tokens,
                    purpose="decision",
                    arm_id=inp.arm.arm.id,
                    effort=self.settings.effort,
                    estimate_eur=self.estimate_eur,
                    dry_run_factory=lambda: self._dry_output(inp),
                )
                cost = {
                    "usd": cost["usd"] + res.cost_usd,
                    "eur": cost["eur"] + res.cost_eur,
                    "input_tokens": cost["input_tokens"] + res.usage.input_tokens,
                    "output_tokens": cost["output_tokens"] + res.usage.output_tokens,
                }
                output = res.parsed
                meta["model"] = res.model
                meta["dry_run"] = res.dry_run
                break
            except BudgetExceeded as exc:
                errors.append(f"budget: {exc}")
                break
            except (LLMError, ValidationError) as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:200]}")
                log.warning("arm %s decision attempt %d failed: %s", inp.arm.arm.id, attempt, exc)
        meta["cost"] = cost
        meta["errors"] = errors
        if output is None:
            meta["hold_reason"] = (
                "budget" if errors and errors[0].startswith("budget") else "model_failure"
            )
            self.last_raw = None
            return Proposal(arm_id=inp.arm.arm.id, decision_date=inp.decision_date, meta=meta)
        self.last_raw = output.model_dump(mode="json")
        views, dropped = [], []
        seen: set[str] = set()
        for v in output.views:
            if v.ticker not in universe or v.ticker in seen:
                dropped.append(f"view {v.ticker}")
                continue
            seen.add(v.ticker)
            views.append(
                View(
                    ticker=v.ticker,
                    view=v.view,
                    conviction=v.conviction,
                    horizon_days=v.horizon_days,
                    thesis=v.thesis[:600],
                    catalysts=v.catalysts[:5],
                    risks=v.risks[:5],
                    cited_item_ids=[i for i in v.cited_item_ids if i in set(meta["item_ids"])][:8],
                )
            )
        actions = []
        for a in output.actions:
            if a.ticker not in universe:
                dropped.append(f"action {a.kind} {a.ticker}: not in universe")
                continue
            if a.kind == "sell" and a.ticker not in held:
                dropped.append(f"action sell {a.ticker}: not held")
                continue
            if a.kind == "buy" and a.ticker in held:
                dropped.append(f"action buy {a.ticker}: already held")
                continue
            actions.append(Action(ticker=a.ticker, kind=a.kind, reason=a.reason[:400]))
        meta["dropped"] = dropped
        return Proposal(
            arm_id=inp.arm.arm.id,
            decision_date=inp.decision_date,
            views=views,
            actions=actions,
            regime_note=output.regime_note,
            journal_updates=output.journal_updates.model_dump(mode="json"),
            meta=meta,
        )


def apply_journal_updates(
    journal: Journal, proposal: Proposal, on: date, universe: set[str]
) -> None:
    if not proposal.journal_updates:
        return
    try:
        upd = JournalUpdates.model_validate(proposal.journal_updates)
    except ValidationError as exc:
        log.warning("journal updates rejected: %s", exc)
        return
    journal.apply_updates(upd, on, universe)
