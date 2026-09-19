# PREREGISTRATION.md — go-live criteria, written before the first live run

Status: pre-registered on 2026-09-20, before any live decision. This file is not edited after the
first live run; any later change to the experiment is a new arm and a DECISIONS.md entry.

## The question
Would EUR 10,000 (the experiment book) and EUR 1,000 (my actual size, the personal book) do better in
this agent than in an S&P 500 index fund bought in EUR (iShares Core S&P 500 UCITS ETF, SXR8.DE,
DEGIRO ETF Core Selection fee), after realistic Greek broker fees, taxes, whole shares, next-open
fills and slippage? And is any difference distinguishable from luck?

## Primary instrument: view-level scoring
Every day each LLM arm states views on the universe. Views are scored on forward signed excess
return versus the equal-weight universe from the next session's open at 5, 10, 20 and 30 sessions.
The headline sample size is the EFFECTIVE n (non-overlapping windows per name), never the raw count;
the 95% interval comes from a block bootstrap over date blocks of the horizon length. The books test
whether views survive execution (fees, whole shares, the 2-orders-a-month cap, the 30-session hold).

## Go-live criteria (all must hold), evaluated after >= 6 months and >= 2 completed 3-month cohorts of A
1. Signal. Arm A's 20-session signed excess return is positive with the block-bootstrap 95%
   interval excluding zero, on >= 300 effective views, and the 30-session result is not materially
   worse than the 20-session one (point estimate within one standard error).
2. Experiment book (EUR 10k). A's after-fee return exceeds SXR8.DE over the full live period and in
   at least 2 of 3 completed cohorts; A's return sits above the 90th percentile of the
   contemporaneous random-picker distribution (200 seeds, identical rules and dates, EUR 10k); and
   A also beats the Greek ETF (AETF.AT) over the full period.
3. Replicas. A, R1 and R2 agree in sign versus SXR8.DE and their return spread is below 5
   percentage points on the EUR 10k book.
4. Personal book (EUR 1k). A's after-fee return exceeds SXR8.DE under the DEGIRO profile. If I
   would instead open an account with a cheaper verified broker (Freedom24 or a Greek broker whose
   minimum commission I have confirmed in writing), that profile is named here before go-live and
   used instead: __________________ (blank until confirmed).
5. Risk and health. Maximum drawdown below 15% on the EUR 10k book; fees plus tax below 3% of
   starting capital per quarter on the book that would go live; more than 95% of scheduled runs
   executed; LLM cost below EUR 25 per month.

## Pre-registered possible outcomes
- Edge at 5 sessions but none at 20 or 30 (the decay curve falls to zero inside the holding
  period): the approach does not fit a fee-constrained small account. Harvesting a days-long
  signal needs turnover the fees forbid. This is a complete, valid result, not a failure to be
  explained away, and it ends the experiment in its current form.
- Signal but no book edge (criterion 1 holds, 2 fails): execution constraints, not information,
  are the binding limit; a larger book or cheaper broker is the only lever, and only the EUR 10k
  book result speaks to that.
- Book edge without signal (2 holds, 1 fails): treated as luck. Three to four bets per quarter
  cannot establish skill; no go-live.
- Replica disagreement (3 fails): the agent's own sampling noise exceeds its edge; no go-live.
- The digest degrades (official announcements unavailable, or a major feed dead for more than 10%
  of sessions): arms B, C and A are still comparable to each other, but any go-live decision waits
  for a full cohort with healthy sources.

## What these criteria are and are not
They are a "not obviously luck" bar. At EUR 10,000 with four positions and one swap a month, a
3-month cohort is a handful of bets; six months of cohorts give little power at the book level.
The view-level criterion (1) carries the statistical weight. Passing every criterion is consistent
with a modest real edge and with a moderately lucky half-year; it is not proof.

## What the real-money version would need (design only; not built)
- Broker: no retail broker reachable from Greece with a public trading API offers ATHEX (IBKR: no
  ATHEX; Saxo: Greek shares disabled for new clients). DEGIRO, Freedom24 and the Greek bank brokers
  have no public API. The practical path is assisted execution: the agent posts the day's orders
  (GitHub issue or Telegram) at 18:30 UTC and I execute them at the next opening auction. The
  paper books keep running in parallel as the control.
- Position limits: EUR 10k total, 4-5 names, max 30% per name, universe allow-list only, no name
  below EUR 300k median ADV, no order above 1% of ADV.
- Kill switch: repository variable TRADING_ENABLED=false stops the workflow; a HALT file in the repo
  root stops decisions; automatic halts on -15% drawdown from peak, three consecutive data failures,
  a missed decide run, or a budget breach.
- Dry-run diff: every live day also produces the paper decision, and any divergence between what
  was executed and what the paper book did is logged.
