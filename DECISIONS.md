# DECISIONS.md — dated log of every change to the experiment

Rule: live arm configs are frozen once started. A change to a running arm is a new arm with a new id; this file
records why. Entries are append-only.

## 2026-09-19 — Design rev 1 proposed
- Architecture, arm list (A/B/C/D/R1/R2 + rule baselines), risk limits and cost estimate presented (DESIGN.md rev 1).
- Flagged: at EUR 1,000 with a EUR 5 minimum commission, round trips cost 4-4.6% of a position.

## 2026-09-20 — Design rev 2 approved (fee rework)
- Every LLM arm runs two books off identical decisions: EUR 10,000 experiment book (primary result) and EUR 1,000
  personal book (secondary). Zero-fee shadow ledger dropped. Baselines run at both sizes.
- Limits: 4 target / 5 max positions; min hold 30 trading days; <= 2 orders/month; one-sided turnover <= 25% NAV/month;
  fee budget 1.5% NAV/month per book; build-up exemption (first 5 trading days) and stop-loss exemption as written.
  Rationale for 4 positions: a 25% turnover cap cannot accommodate swapping a 33% position.
- View-level scoring (5/10/20/30-day forward excess returns) is the primary statistical instrument; the books test
  whether views survive execution. Effective n from the block bootstrap is the headline, raw view count secondary.
- Fee drag (fees / average NAV, annualized, per book) is a first-class dashboard metric, re-costed under every profile.
- Broker profiles: DEGIRO default; Freedom24 Smart, Piraeus online (UNVERIFIED MINIMUM badge on the dashboard),
  Eurobank Trader, NBG Securities for re-costing. IBKR (no ATHEX) and Saxo (Greek shares disabled) excluded.
- Book divergence rule: if fee economics exclude a stock from the EUR 1k book that the EUR 10k book holds, it is logged
  as a divergence (FEE_ECONOMICS_EXCLUSION) and surfaced; a silently different universe would break the comparison.
- Pre-registered possible outcome: edge at 5 days but none at 20/30 means the approach does not fit a
  fee-constrained small account.
