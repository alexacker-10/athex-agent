# Project: Autonomous LLM paper-trading experiment on the Athens Stock Exchange

## Goal
Build working, deployable code for a system that autonomously paper-trades
Greek equities (ATHEX) with fake money, runs unattended, and tells me
whether it's worth putting real money behind. The real question: would
€1,000 do better in this agent than in an S&P 500 index fund? This is a
stepping stone to real money, so realism and statistical honesty matter
more than impressive paper numbers.

## Fixed decisions
- Runtime: GitHub Actions on a cron schedule. State persisted in the repo
  (SQLite or JSON/Parquet committed by the workflow). Dashboard served by
  GitHub Pages. No paid infrastructure.
- Language: Python.
- Decision frequency: once per trading day after ATHEX close; orders fill
  at next day's open. You may argue for a different cadence, but justify
  it against the cost budget and the liquidity of Greek stocks.
- Data: free sources only (e.g. yfinance .AT tickers for prices; RSS and
  public pages for news). Isolate every data source behind an interface so
  a paid/live feed can be swapped in later.
- Capital: €1,000 per agent. Long-only, no shorting, no leverage, no
  derivatives. Stocks and any ATHEX-listed ETFs. Cash is allowed.
- Fully autonomous: no human approval step.
- LLM budget: ~€25/month total across all arms. Hard monthly spending cap
  in code; log token cost per run and per arm. Give me a cost estimate.

## Simulation realism (non-negotiable)
- Whole shares only. Realistic Greek retail broker fees including the
  MINIMUM commission per trade, plus the transaction tax on sales and
  exchange fees. Research current typical values; expose them as config.
- Slippage model scaled to average daily volume; refuse or penalize orders
  that are large relative to volume.
- Liquidity filter on the universe. You choose the base universe (suggest
  FTSE/ATHEX Large Cap + liquid mid caps) and justify it.
- Dividends and splits handled.
- NO LOOKAHEAD: every decision is timestamped and committed to the log
  before the fill price exists. Make this structurally impossible to
  violate, and test it.
- Missed runs (Actions outage, data source down) must be detected, logged,
  and shown on the dashboard, never silently skipped.

## Inputs (be creative, but tag everything by source and reliability)
- Official: ATHEX company announcements, financial calendar, results dates.
- News: Greek-language (capital.gr, euro2day.gr, naftemporiki.gr, etc.)
  and English (Reuters, etc.). The model should read Greek natively.
- Macro: ECB decisions, Greek bond spreads, rating actions, bank-sector
  news, index reclassification flows, anything else you judge relevant.
- Rumors: public forums and social media. Treat as low-reliability and
  potentially manipulative (pump attempts in thin stocks). Public sources
  only. Respect robots.txt and terms of service.
- Each source must fail gracefully; one broken scraper must not kill a run.

## Experiment framework: A/B testing as a first-class feature
Build a general arm-based experiment framework, not hardcoded agents.
- An "arm" is a config file: input sources enabled, universe, risk limits,
  prompt variant, model, random seed. Adding an arm = adding a config.
- Every arm has its own €1,000 book under identical fees and constraints.
- COST DESIGN: the expensive step (ingesting, deduplicating, and
  summarizing the day's news with a cheap model) runs ONCE per day and is
  shared. Arms differ only in the final decision call, which sees a
  filtered view of the shared digest. This is what makes many arms
  affordable.
- One-factor-at-a-time: define a base LLM arm; every other LLM arm changes
  exactly one thing from it, so differences are attributable.

Initial arms (propose changes if something is more informative):
  LLM arms
  A. Base: all inputs including rumors
  B. Base without rumors
  C. Base with official announcements + prices only (does news help at all?)
  D. One variant of your choice (universe, risk posture, or prompt style)
  R. Replicas: 2 extra identical copies of the base arm (different seeds /
     sampling). Their divergence measures the agent's own noise.
  Rule-based baselines (no LLM cost)
  1. Buy-and-hold S&P 500 in EUR, total return (PRIMARY benchmark:
     my real alternative for this money)
  2. Buy-and-hold ATHEX Composite or GD.AT ETF (diagnostic: separates
     agent skill from "Greece had a good quarter")
  3. Random picker under identical constraints and turnover, many seeds
  4. Simple momentum rule

- Staggered cohorts: support starting a fresh copy of any arm on a later
  date (e.g. monthly), so that over time I accumulate several overlapping
  3-month windows rather than one.

## Historical-window analysis (rule-based strategies ONLY)
- Run baselines 1-4 over every rolling 3-month window in as much history
  as the free data allows (target 10+ years), with the same fee model.
- Output the null distribution: how often, and by how much, does a random
  constrained Greek portfolio beat the S&P 500 (EUR) over 3 months? Same
  for momentum and the ATHEX index.
- The dashboard places each live arm's result on this distribution (e.g.
  as a percentile), so I can see whether a result is distinguishable from
  luck.
- Do NOT backtest the LLM arms on historical windows as evidence of edge:
  the model's training data contains those periods, so it would be
  trading with hindsight. A dry-run over recent history is allowed for
  plumbing validation only, and must be labeled as such. If you see a
  defensible use of post-knowledge-cutoff data, propose it, with caveats.

## Decision architecture
- The LLM produces per-stock views/signals with written reasoning; a
  rules-based portfolio layer turns them into orders under hard
  constraints (max position size, max positions ~4-6 given fees, max
  monthly turnover, minimum holding period). Propose the exact limits.
- Memory: each arm keeps its own persistent journal: a running thesis per
  holding/watchlist stock, past calls and their outcomes, lessons learned.
  Fed back in on each run, within a size budget.
- Structured (JSON) outputs, validated; on invalid output, retry once,
  then hold positions.

## Dashboard (interactive, static site on GitHub Pages)
- Equity curves of all arms and baselines, in EUR, after fees; toggle
  arms on/off; filter by cohort.
- Headline metric: raw return vs S&P 500 (EUR). Alongside: return vs
  ATHEX, percentile vs the historical random-picker distribution, spread
  between replicas, max drawdown, volatility, turnover, fees paid, LLM
  cost per arm.
- A/B view: each arm vs the base arm, with the single factor that differs.
- Per arm: current holdings, full trade log, the model's reasoning for
  each decision, links to the news items it cited.
- System health: last run time, missed runs, failed data sources, budget
  used this month.

## Go-live criteria (pre-registered before the experiment starts)
Propose concrete criteria for moving to real money, written into the repo
before the first live run. For example: base arm beats S&P 500 (EUR) AND
lands above a stated percentile of the random-picker distribution, after
fees, with replicas agreeing in sign and max drawdown below a threshold,
ideally across more than one cohort. Also sketch what the real-money
version needs: broker API options available to Greek retail investors,
position limits, a kill switch. Design only; don't build it.

## Deliverables
1. Brief architecture overview and the key choices you made, with reasons.
2. Complete working repo: code, GitHub Actions workflows, arm configs,
   README with step-by-step setup (I'm an engineer comfortable with
   Python, new to GitHub Actions). Secrets via GitHub Secrets.
3. Tests, including the no-lookahead guarantee, the fee model, and arm
   isolation (no arm can see another's book or journal).
4. If you have network access, verify every live data source yourself and
   report coverage gaps (e.g. tickers missing from the free feed). If not,
   test against mock data and give me a smoke-test script to run locally.
5. The historical-window analysis, runnable as one command, with results
   rendered on the dashboard.
6. Cost estimate per month and an honest list of known weaknesses.

Be skeptical throughout. If any of my choices undermines the validity of
the experiment, say so and propose the fix rather than silently complying.
## Working process
Start by giving me the architecture overview, the proposed arm list, the
risk limits, and the cost estimate. Then STOP and wait for my approval
before writing any code. After approval, build in stages, running tests
as you go, and commit to git at each working milestone.
