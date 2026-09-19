# CLAUDE.md — working notes for this repository

Read DESIGN.md (approved rev 2, 2026-09-20) for the full design and DECISIONS.md for every change since.
Keep this file current as the project evolves.

## What this is
An autonomous paper-trading experiment on the Athens Stock Exchange. LLM "arms" produce daily per-stock views
and actions; a deterministic rules layer turns them into whole-share orders that fill at the next session's
opening auction under realistic Greek fees and slippage. Every arm runs two books off identical decisions:
EUR 10,000 (experiment book, primary result) and EUR 1,000 (personal book). Rule-based baselines (S&P 500 in
EUR via SXR8.DE, Greek ETF, random picker, momentum, equal weight) run at both sizes. View-level scoring
(forward 5/10/20/30-day excess returns, effective n from a block bootstrap) is the primary statistical
instrument; the books test whether views survive execution.

## Architecture (see DESIGN.md §2)
- `athex_agent/config/`   strict pydantic models for every YAML in `configs/`, loader with reference checks,
                          `freeze.py` = frozen-live-config invariant (fingerprint lock per arm state dir).
- `athex_agent/portfolio/` `fees.py` (itemised broker fees per profile), `slippage.py` (half-spread tiers +
                          impact, ADV cap), `orders.py` (Order/Fill), `accounting.py` (BookState: cash, FIFO
                          lots, dividends, splits, periodic fees, divergences).
- `athex_agent/data/`     [stage 2] price sources (Yahoo chart + quote/intraday), PriceStore with `as_of()`,
                          ATHEX calendar, corporate actions, news adapters, source health.
- `athex_agent/digest/`   [stage 4] dedup + Haiku summarisation, shared once per day.
- `athex_agent/arms/`     [stage 3/5] Decider interface: LLM, random, momentum, buy-and-hold, equal-weight.
- `athex_agent/portfolio/rules.py`, `book.py`, `fill_engine.py` [stage 3] rules layer, per-book sizing,
                          fills with the lookahead guard.
- `athex_agent/llm/`      [stage 5] Anthropic client wrapper, structured outputs, cost meter, budget guard.
- `athex_agent/runs/`     [stage 6] jobs (fill, digest, decide, reconcile, score_views, build_dashboard),
                          run ledger, idempotency, git helper.
- `athex_agent/analysis/` [stage 3/5] historical windows, null distributions, view scoring, fee drag.
- `athex_agent/dashboard/` [stage 6] static site generator -> `docs/` (GitHub Pages).

## Repo layout
```
configs/   arms/*.yaml  books.yaml  fees/*.yaml  limits/*.yaml  slippage.yaml  universe.yaml  cohorts.yaml
data/      prices/, digests/, news/ (committed by the daily workflow)
state/     arms/<id>/{config.lock.json, journal.json, views/, decisions/, books/<book>/book.json}, ledger/
docs/      GitHub Pages site (generated)
tests/     pytest suite
.github/workflows/  ci.yml (tests on every push), daily.yml, historical.yml, pages.yml [stages 6-7]
```

## How to run
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/pytest          # lint + tests (CI runs the same)
cp .env.example .env                                # local secrets; never commit .env
# local dry run (available from stage 6): python -m athex_agent.runs.decide --as-of 2026-10-01 --dry-run
```
Fee-drag table used in DESIGN.md §1: `python -m athex_agent.analysis.fee_drag` [stage 3].

## Coding conventions
- Python 3.12, ruff (line length 100, rules E/F/I/B/UP/W), `ruff format`. Pinned dependencies in
  `requirements*.txt` / `pyproject.toml`; add a pin when adding a dependency.
- Configs are strict pydantic models (`extra="forbid"`, frozen). Unknown keys are errors.
- Money: floats rounded to cents with `portfolio.money.round_cents` (half-up) at every ledger write; prices
  to 4 decimals. Never compare money with `==` in tests without rounding.
- Dates are `datetime.date` (Athens trading date); timestamps are timezone-aware UTC `datetime`s.
- Every data source sits behind an interface and must fail gracefully (SourceHealth), never kill a run.
- Tests accompany every module; run them before committing. Commit at each working milestone.
- Model ids: `claude-sonnet-5` (base arms), `claude-opus-5` (arm D), `claude-haiku-4-5` (digest).

## Invariants that must never be broken
1. **No lookahead.** A decision for date T sees only data up to T (`PriceStore.as_of(T)` raises beyond it).
   Orders carry a UTC `decision_ts` and are committed to git before the next open; the fill engine refuses
   any order not strictly decided and committed before the target session's 10:30 Athens open. Missed
   decision runs are logged as MISSED and never back-filled.
2. **Arm isolation.** An arm runner is built only from its own `state/arms/<id>/` directory plus the shared
   read-only digest. No arm ever sees another arm's book, journal or views. Tests assert it.
3. **Frozen live configs.** Once an arm has a `config.lock.json`, its resolved config must reproduce the same
   fingerprint; a change is refused. To change an experiment, add a new arm id and log it in DECISIONS.md.
4. **Secrets never in the repo.** API keys only via environment variables / GitHub Secrets; `.env` is ignored.
5. **Idempotent, non-concurrent daily runs.** One successful run per (trading date, job); workflow
   concurrency group `trading`; re-running is a no-op.
6. **Identical decisions across books.** Both books of an arm consume the same actions; any place a book
   cannot follow (min order, cash, fee budget, fee economics excluding a low-priced name) is logged as a
   `Divergence` and surfaced on the dashboard. A silently different universe would break the comparison.
7. **One factor at a time.** Every LLM arm with a `base_arm` differs from it only in the declared `factor`
   paths (enforced by the loader and tests). Replicas differ in nothing.
8. **Full decision logging.** Prompt template version, digest id and included item ids, book and journal
   snapshots, raw model output, token counts and cost, for every decision.
9. **Fee profiles are cited and honest.** Unverified terms carry a `badge` (e.g. Piraeus: UNVERIFIED
   MINIMUM) that the dashboard shows verbatim.

## Process
- DECISIONS.md is append-only and dated. PREREGISTRATION.md is written before the first live run and not
  edited afterwards.
- The daily workflow commits state to `main`; CI runs on every push; bot commits use `[skip ci]`.
