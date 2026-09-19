"""ArmRunner: one arm (optionally one seed instance), its books, decisions, fills and journal files.

State layout (all committed by the workflow):
  state/arms/<arm_id>[/instances/<instance>]/
    config.lock.json            frozen resolved config (see config.freeze)
    books/<book_id>/book.json   BookState
    books/<book_id>/nav.csv     daily NAV series
    decisions/<date>.json       proposal, per-book orders and blocks, NAVs
    views/<date>.json           the day's views (scored later)
An ArmRunner is constructed only from its own directory plus read-only shared data (prices,
actions, digest). It has no handle to any other arm's state.
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from athex_agent.arms.deciders import Decider, DecisionInputs, make_decider
from athex_agent.arms.journal import ClosedTrade, Journal
from athex_agent.arms.llm_decider import apply_journal_updates
from athex_agent.config.freeze import assert_frozen, write_lock
from athex_agent.config.models import BookConfig, ResolvedArm
from athex_agent.data import calendar as cal
from athex_agent.data.actions import ActionsStore
from athex_agent.data.prices import NoPriceData, PriceStore
from athex_agent.portfolio.accounting import BookState
from athex_agent.portfolio.fill_engine import FillEngine, FillOutcome
from athex_agent.portfolio.money import round_cents
from athex_agent.portfolio.rules import PlanContext, RulesLayer

DIVIDEND_PAY_LAG_SESSIONS = 10
DIVIDEND_WITHHOLDING = 0.05
NAV_COLUMNS = [
    "date",
    "nav",
    "cash",
    "invested",
    "fees_cum",
    "tax_cum",
    "slippage_cum",
    "dividends_cum",
    "positions",
]


class ArmPaths:
    def __init__(self, state_root: Path, arm_id: str, instance: str | None = None) -> None:
        self.root = Path(state_root) / "arms" / arm_id
        if instance:
            self.root = self.root / "instances" / instance

    def book(self, book_id: str) -> Path:
        return self.root / "books" / book_id / "book.json"

    def nav(self, book_id: str) -> Path:
        return self.root / "books" / book_id / "nav.csv"

    def decision(self, d: date) -> Path:
        return self.root / "decisions" / f"{d.isoformat()}.json"

    def views(self, d: date) -> Path:
        return self.root / "views" / f"{d.isoformat()}.json"

    def prompt(self, d: date) -> Path:
        return self.root / "decisions" / f"{d.isoformat()}.prompt.json"

    @property
    def journal(self) -> Path:
        return self.root / "journal.json"


class NotStarted(RuntimeError):
    pass


class ArmRunner:
    def __init__(
        self,
        resolved: ResolvedArm,
        store: PriceStore,
        actions: ActionsStore,
        state_root: Path,
        instance: str | None = None,
        decider: Decider | None = None,
    ) -> None:
        self.resolved = resolved
        self.arm = resolved.arm
        self.store = store
        self.actions = actions
        self.instance = instance
        self.paths = ArmPaths(state_root, self.arm.id, instance)
        # Rule arms build their own decider; LLM arms get one injected by the orchestrator.
        # The fill job needs no decider at all.
        self.decider = decider or (make_decider(self.arm) if self.arm.kind == "rule" else None)
        self.rules = RulesLayer()

    # ---- lifecycle
    def started(self) -> bool:
        return all(self.paths.book(b.id).exists() for b in self.resolved.books)

    def start(self, on: date) -> None:
        if self.started():
            raise RuntimeError(f"arm {self.arm.id} already started")
        write_lock(self.paths.root, self.resolved, on)
        for b in self.resolved.books:
            BookState.new(b.id, self.arm.id, b.capital_eur, on).save(self.paths.book(b.id))

    def load_books(self) -> dict[str, BookState]:
        if not self.started():
            raise NotStarted(f"arm {self.arm.id} has no books; call start() first")
        return {b.id: BookState.load(self.paths.book(b.id)) for b in self.resolved.books}

    def save_books(self, books: Mapping[str, BookState]) -> None:
        for book_id, book in books.items():
            book.save(self.paths.book(book_id))

    def book_cfg(self, book_id: str) -> BookConfig:
        return next(b for b in self.resolved.books if b.id == book_id)

    # ---- daily steps
    def fill(
        self,
        now_utc: datetime,
        today: date,
        committed_ts: Mapping[str, datetime] | None = None,
    ) -> dict[str, list[FillOutcome]]:
        books = self.load_books()
        engine = FillEngine(self.store, self.resolved.slippage)
        out: dict[str, list[FillOutcome]] = {}
        for book_id, book in books.items():
            adv = {}
            for o in book.pending_orders:
                view = self.store.as_of(o.decision_date)
                adv[o.ticker] = view.adv_eur(o.ticker, 20)
            profile = self.resolved.fee_profile_for(self.book_cfg(book_id))
            out[book_id] = engine.process(book, profile, adv, now_utc, committed_ts, today)
        self.save_books(books)
        self._record_closed_trades(out, books)
        return out

    def _journal(self) -> Journal:
        """The arm's journal: the decider's live object when it holds one, else from disk."""
        j = getattr(self.decider, "journal", None)
        if isinstance(j, Journal):
            return j
        return Journal.load(self.paths.journal, self.arm.id)

    def _record_closed_trades(self, outcomes, books) -> None:
        """Fully closed positions of the primary book become journal entries (LLM arms)."""
        if self.arm.kind != "llm":
            return
        primary = next((b.id for b in self.resolved.books if b.primary), self.resolved.books[0].id)
        book = books[primary]
        journal = self._journal()
        changed = False
        for o in outcomes.get(primary, []):
            if o.status != "filled" or o.fill is None or o.fill.side != "SELL":
                continue
            if book.shares(o.fill.ticker) != 0:
                continue
            rec = next((t for t in reversed(book.trades) if t.order_id == o.order.order_id), None)
            buys = [
                t
                for t in book.trades
                if t.ticker == o.fill.ticker and t.side == "BUY" and t.fill_date <= o.fill.fill_date
            ]
            if rec is None or not buys or not rec.cost_basis_sold:
                continue
            opened = buys[-1].fill_date
            journal.add_closed_trade(
                ClosedTrade(
                    ticker=o.fill.ticker,
                    opened=opened,
                    closed=o.fill.fill_date,
                    return_pct=round((rec.realized_pnl or 0.0) / rec.cost_basis_sold, 4),
                    hold_trading_days=cal.trading_days_between(opened, o.fill.fill_date),
                    reason=o.order.reason[:200],
                )
            )
            changed = True
        if changed:
            journal.save(self.paths.journal)

    def apply_corporate_actions(self, upto: date) -> dict[str, int]:
        books = self.load_books()
        counts: dict[str, int] = {}
        prices = self.store.as_of(upto)
        for book_id, book in books.items():
            since = book.actions_applied_through or cal.prev_trading_day(book.started_on)
            n = 0
            for ticker in list(book.positions):
                for ev in self.actions.between(ticker, since, upto):
                    if ev["kind"] == "dividend":
                        pay = cal.add_trading_days(ev["date"], DIVIDEND_PAY_LAG_SESSIONS)
                        if book.record_dividend(
                            ticker, ev["value"], ev["date"], pay, DIVIDEND_WITHHOLDING
                        ):
                            n += 1
                    elif ev["kind"] == "split":
                        try:
                            px = prices.close(ticker, ev["date"])
                        except NoPriceData:
                            px = prices.close(ticker)
                        book.apply_split(ticker, ev["value"], ev["date"], px)
                        n += 1
            book.apply_due_cash_events(upto)
            book.actions_applied_through = upto
            counts[book_id] = n
        self.save_books(books)
        return counts

    def decide(
        self,
        decision_date: date,
        decision_ts: datetime,
        universe_members: list[str],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_frozen(self.paths.root, self.resolved)
        if self.decider is None:
            raise RuntimeError(f"arm {self.arm.id}: no decider injected for an LLM arm")
        books = self.load_books()
        prices = self.store.as_of(decision_date)
        primary = next((books[b.id] for b in self.resolved.books if b.primary), None) or next(
            iter(books.values())
        )
        in_buildup = self.rules._in_buildup(primary, decision_date, self.resolved.limits)
        rng = random.Random(f"{self.arm.id}:{self.instance or ''}:{self.arm.seed}:{decision_date}")
        inp = DecisionInputs(
            arm=self.resolved,
            decision_date=decision_date,
            decision_ts=decision_ts,
            prices=prices,
            universe=list(universe_members),
            books=books,
            rng=rng,
            in_buildup=in_buildup,
            extra=extra or {},
        )
        proposal = self.decider.decide(inp)
        tradable = self.decider.tradable(inp)
        record: dict[str, Any] = {
            "arm_id": self.arm.id,
            "instance": self.instance,
            "decision_date": decision_date.isoformat(),
            "decision_ts": decision_ts.isoformat(),
            "buildup": in_buildup,
            "universe_size": len(universe_members),
            "proposal": proposal.model_dump(mode="json"),
            "books": {},
        }
        for book_id, book in books.items():
            cfg = self.book_cfg(book_id)
            ctx = PlanContext(
                decision_date=decision_date,
                decision_ts=decision_ts,
                prices=prices,
                tradable=tradable,
                limits=self.resolved.limits,
                book_cfg=cfg,
                fee_profile=self.resolved.fee_profile_for(cfg),
                slippage=self.resolved.slippage,
            )
            res = self.rules.plan(proposal, book, ctx)
            for o in res.orders:
                book.add_pending(o)
            record["books"][book_id] = {
                "nav": res.nav,
                "orders": [o.model_dump(mode="json") for o in res.orders],
                "blocked": res.blocked,
            }
        self.save_books(books)
        if getattr(self.decider, "last_prompt", None) is not None:
            self._write_json(
                self.paths.prompt(decision_date),
                {
                    "system_prompt_version": getattr(self.decider, "prompt_version", None),
                    "user_payload": self.decider.last_prompt,
                    "raw_output": getattr(self.decider, "last_raw", None),
                },
            )
        if self.arm.kind == "llm":
            journal = self._journal()
            apply_journal_updates(journal, proposal, decision_date, set(universe_members))
            journal.save(self.paths.journal)
        self._write_json(self.paths.decision(decision_date), record)
        self._write_json(
            self.paths.views(decision_date),
            {
                "arm_id": self.arm.id,
                "instance": self.instance,
                "decision_date": decision_date.isoformat(),
                "views": [v.model_dump(mode="json") for v in proposal.views],
            },
        )
        return record

    def decide_hold(
        self, decision_date: date, decision_ts: datetime, universe_members: list[str], reason: str
    ) -> dict[str, Any]:
        """Record a no-action decision (e.g. budget shed) without calling the decider."""
        assert_frozen(self.paths.root, self.resolved)
        books = self.load_books()
        record: dict[str, Any] = {
            "arm_id": self.arm.id,
            "instance": self.instance,
            "decision_date": decision_date.isoformat(),
            "decision_ts": decision_ts.isoformat(),
            "buildup": False,
            "universe_size": len(universe_members),
            "proposal": {
                "arm_id": self.arm.id,
                "decision_date": decision_date.isoformat(),
                "views": [],
                "actions": [],
                "meta": {"hold_reason": reason},
            },
            "books": {},
        }
        prices = self.store.as_of(decision_date)
        for book_id, book in books.items():
            closes = {t: prices.close(t) for t in book.positions}
            record["books"][book_id] = {"nav": book.nav(closes), "orders": [], "blocked": []}
        self._write_json(self.paths.decision(decision_date), record)
        self._write_json(
            self.paths.views(decision_date),
            {
                "arm_id": self.arm.id,
                "instance": self.instance,
                "decision_date": decision_date.isoformat(),
                "views": [],
            },
        )
        return record

    def record_nav(self, d: date) -> dict[str, float]:
        books = self.load_books()
        prices = self.store.as_of(d)
        out: dict[str, float] = {}
        for book_id, book in books.items():
            closes = {t: prices.close(t) for t in book.positions}
            nav = book.nav(closes)
            row = {
                "date": d.isoformat(),
                "nav": nav,
                "cash": book.cash_eur,
                "invested": round_cents(nav - book.cash_eur),
                "fees_cum": book.fees_paid_eur,
                "tax_cum": book.sales_tax_paid_eur,
                "slippage_cum": book.slippage_cost_eur,
                "dividends_cum": book.dividends_received_eur,
                "positions": len(book.positions),
            }
            self._append_nav(self.paths.nav(book_id), row)
            out[book_id] = nav
        return out

    def nav_series(self, book_id: str) -> list[dict[str, Any]]:
        p = self.paths.nav(book_id)
        if not p.exists():
            return []
        with p.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    # ---- io helpers
    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _append_nav(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        if not new:
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if rows and rows[-1]["date"] == row["date"]:
                rows[-1] = row  # idempotent: re-recording the same day replaces it
                with path.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=NAV_COLUMNS)
                    w.writeheader()
                    w.writerows(rows)
                return
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=NAV_COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
