# ATHEX paper-trading agent — design proposal (rev 2)

Status: PROPOSAL rev 2 (2026-09-20), awaiting approval. Nothing is built yet.
Rev 2 changes (from your fee feedback): two books per arm at EUR 10,000 and EUR 1,000 off identical decisions; lower
trading frequency; view-level scoring as the primary instrument; baselines at both sizes; fee drag as a headline metric;
broker survey with three extra re-costing profiles; pre-registered "signal half-life" outcome.
This document becomes the basis for CLAUDE.md, PREREGISTRATION.md and README.md after approval.

## 0. What I verified before designing (live probes, 2026-09-19/20)

**Prices (yfinance 1.7.0, Yahoo Finance)**
- 60+ ATHEX tickers resolve. Most large/mid caps have 10-25 years of daily bars with dividends and splits.
- Renamed/merged names need a mapping table: OPAP -> `ALWN.AT` (Allwyn, history back to 2001), Intralot -> `BYLOT.AT`,
  Metlen -> `MTLN.AT` (new PLC listing, history only from 2025-08), Intracom -> `INTRK.AT`, Attica Bank -> `CREDIA.AT`,
  EYATH -> `EYAPS.AT`. Terna Energy and Epsilon Net are delisted (no data).
- `ALPHA.AT` history was reset on Yahoo in 2025-07 (only 14 months available). Gap for the historical analysis.
- **Daily-bar lag**: for some of the most liquid tickers (ETE, PPC, OPTIMA) the daily bar for Sep 17 was missing and the
  Sep 18 row had no close, while other tickers (MTLN, AETF) and the index (GD.AT) were complete. Yahoo's *quote* endpoint
  and 1-minute intraday bars were fresh for all of them (Sep 18 open at the 10:30 auction, close at 17:2x). The price layer
  treats the last 1-3 daily bars as provisional and derives open/close from quote + intraday until the daily bar settles.
- Benchmarks: `SXR8.DE` (iShares Core S&P 500 UCITS ETF, accumulating, EUR, Xetra) since 2010-05; `^SP500TR` (since 1988)
  x `EURUSD=X` (since 2003) for longer history; `GD.AT` (Athens Composite, price index) since 1997; `AETF.AT`
  (Alpha ETF FTSE/ATHEX Large Cap, investable Greek ETF) since 2016.
- Yahoo rate-limits shared IPs (GitHub Actions runners are shared). Mitigation: full history cached in the repo,
  incremental fetches only, retries with backoff, second endpoint for the latest bar. Stooq is behind a JS challenge.

**Taxes**
- Sales duty on listed shares: **0.10% of sale value** (Law 5073/2023 art. 50). Capital gains exempt below 0.5% holding.
  Dividend withholding 5%. Not modelled: nothing else applies to a resident individual at this size.

**Broker survey (who can a Greek resident actually use for ATHEX, and what does an order cost)**

| Broker | ATHEX access | Per-order pricing (verified source) | Role in design |
|---|---|---|---|
| DEGIRO | yes | EUR 3.90 + EUR 1.00 handling = **EUR 4.90 flat**, third-party costs included; FTT passed through (fee schedule 1 Jan 2026) | **default profile** |
| Freedom24 (Smart plan) | yes, direct member since 2025 (clearing via Piraeus Bank) | **EUR 2.00 + EUR 0.02/share**, no monthly fee (Appendix 11 fee schedule; 2026 reviews quote the same) | re-costing profile |
| Piraeus Securities online | yes | **0.35% of value**, no minimum published, + third-party costs and taxes; account fee EUR 4/quarter (portfolio EUR 100-10k) or EUR 5/quarter (10-50k) (price list PDF) | re-costing profile, flagged: minimum not published |
| Eurobank Equities (Eurobank Trader) | yes | 0.35% min EUR 6.00 + 0.0325% + 0.040% + EUR 0.50/security + EUR 0.06/order (charges page) | re-costing profile (Greek bank broker) |
| NBG Securities | yes | 1.00% up to EUR 3k (0.75%/0.50% above), min EUR 5.00; + 0.02% + 0.03% + 0.0125% + EUR 0.50 (schedule 12 May 2025) | re-costing profile |
| Interactive Brokers | **no** | Europe stock-commission table lists 16 markets; Greece/Athens absent | excluded |
| Saxo | effectively no | Greek shares disabled for new clients (needs a DSS/SAT account Saxo no longer opens) | excluded |
| Trade Republic, Scalable, Revolut, XTB, Lightyear, Trading212, eToro | no ATHEX | | excluded |

Answer to "is anything materially below DEGIRO's EUR 4.90": for the EUR 1,000 book, yes: Freedom24 (about EUR 2.40 on a
EUR 250 order at the universe's median share price) and Piraeus online (about EUR 1.50 if the unpublished minimum is
really zero). For the EUR 10,000 book, no: DEGIRO's flat fee is the cheapest at EUR 2,500 orders. Freedom24 is
price-sensitive: on a EUR 2,500 order it costs EUR 3.00 at a EUR 50 share price but EUR 52 at EUR 1 (CREDIA, BYLOT).

**News / official / rumor sources**
- Working RSS: naftemporiki.gr, euro2day.gr (12 sector feeds), bankingnews.gr, newmoney.gr, mononews.gr, ot.gr,
  insider.gr, ECB press releases. All allow crawling in robots.txt.
- capital.gr: no working RSS (legacy RSS index empty; `/api/` disallowed). HTML section pages crawlable; optional adapter.
- kathimerini/ekathimerini and Reuters: 401/403 to non-browser clients. Excluded.
- ATHEX/Euronext Athens announcements: `athexgroup.gr` returns 403 to non-browser clients; `athens.euronext.com` has no
  announcements RSS. Tested from a GitHub runner with browser headers at build time; fallback is the announcements
  coverage in euro2day/naftemporiki. Coverage risk for arm C.
- Reddit: robots.txt disallows crawlers; official Data API (free tier, OAuth) is the compliant path (two secrets).
  Google News RSS works for Greek queries but robots.txt disallows `/rss` -> shipped, **off by default**.
- Greek bond spreads: no verified free daily feed; ECB Data Portal API gives policy rates. Spread news via feeds.

## 1. Fees: the reframing

The fee is fixed per order, so it is the book size that decides whether fees dominate. Every LLM arm therefore runs
**two books off identical decisions**:

- **Experiment book, EUR 10,000**, full realistic fees (DEGIRO default). Primary result: does the approach have an edge
  at a size where fees don't dominate?
- **Personal book, EUR 1,000**, full realistic fees. Secondary: can that edge be harvested today at your size?

Book mechanics: the rules layer emits *actions* (sell X; buy Y at target weight w). Each book turns an action into
whole-share orders against its own NAV, cash and per-book limits (min order size, fee budget). The EUR 1,000 book may
skip an order below its minimum (EUR 150) or shrink it to available cash; every divergence between the books is logged
with a reason and the dashboard shows the divergence count and the resulting holding differences. No zero-fee shadow
ledger. Rule-based baselines run at both sizes under the same constraints.

### Fee drag, revised (4 equal positions; share counts at the universe's median share price of EUR 12)

Per order, EUR:

| Profile | Book | Order | Buy | Sell (incl. 0.10% duty) | Round trip | % of position | % of NAV |
|---|---|---|---|---|---|---|---|
| DEGIRO | 10,000 | 2,500 | 4.90 | 7.40 | 12.30 | 0.49% | 0.12% |
| Freedom24 Smart | 10,000 | 2,500 | 6.16 | 8.66 | 14.82 | 0.59% | 0.15% |
| Piraeus online* | 10,000 | 2,500 | 10.81 | 13.31 | 24.12 | 0.96% | 0.24% |
| Eurobank Trader | 10,000 | 2,500 | 11.12 | 13.62 | 24.75 | 0.99% | 0.25% |
| NBG Securities | 10,000 | 2,500 | 27.06 | 29.56 | 56.62 | 2.27% | 0.57% |
| DEGIRO | 1,000 | 250 | 4.90 | 5.15 | 10.05 | 4.02% | 1.01% |
| Freedom24 Smart | 1,000 | 250 | 2.40 | 2.65 | 5.05 | 2.02% | 0.51% |
| Piraeus online* | 1,000 | 250 | 1.53 | 1.78 | 3.31 | 1.32% | 0.33% |
| Eurobank Trader | 1,000 | 250 | 6.74 | 6.99 | 13.73 | 5.49% | 1.37% |
| NBG Securities | 1,000 | 250 | 5.66 | 5.91 | 11.56 | 4.62% | 1.16% |

Annual fee drag, % of starting NAV, year 1 including the initial 4 buys; a swap is one sell plus one buy:

| Profile | Book | 6 swaps/yr | 12 swaps/yr (the cap) | cap + slippage (0.15%/side) |
|---|---|---|---|---|
| DEGIRO | 10,000 | 0.93% | 1.67% | 2.72% |
| Freedom24 Smart | 10,000 | 1.14% | 2.02% | 3.07% |
| Piraeus online* (+EUR 20/yr account fee) | 10,000 | 2.08% | 3.53% | 4.58% |
| Eurobank Trader | 10,000 | 1.93% | 3.41% | 4.46% |
| NBG Securities | 10,000 | 4.48% | 7.88% | 8.93% |
| DEGIRO | 1,000 | 7.99% | 14.02% | 15.07% |
| Freedom24 Smart | 1,000 | 3.99% | 7.02% | 8.07% |
| Piraeus online* (+EUR 16/yr account fee) | 1,000 | 4.20% | 6.19% | 7.24% |
| Eurobank Trader | 1,000 | 10.94% | 19.18% | 20.23% |
| NBG Securities | 1,000 | 9.20% | 16.14% | 17.19% |

\* Piraeus online: the published price list states 0.35% with the "minimum" column blank; modelled as no minimum and
flagged. Confirm with the broker before relying on it.

Reading: at EUR 10,000 with DEGIRO, fees plus spread cost 1-3% a year, which a real edge can clear. At EUR 1,000 the
same decisions cost 8-15% a year with DEGIRO, or 4-8% with Freedom24/Piraeus. Slippage (spread) is book-size
independent and is reported separately from fees.

## 2. Architecture

Python 3.12 package `athex_agent/`; state and data are plain files committed by the workflow; dashboard is static.

```
athex_agent/
  data/        PriceSource (yahoo_chart, yahoo_quote_intraday), PriceStore (CSV per ticker, settled flag,
               as_of(date) view), CorporateActions, Calendar (ATHEX holidays), FX, NewsSource adapters
               (rss_generic, euro2day_sectors, athex_announcements, reddit_api, google_news[off]), SourceHealth
  digest/      dedup (canonical URL + title shingles), ticker aliasing (Greek/English names), relevance filter,
               Haiku summarization -> data/digests/YYYY-MM-DD.json (shared, run once per day)
  arms/        ArmConfig (YAML), ArmState (state/arms/<id>/), Decider interface: LLMDecider, RandomDecider,
               MomentumDecider, BuyHoldDecider, EqualWeightDecider
  portfolio/   RulesLayer (views + limits -> actions), Book (actions -> whole-share orders per capital level),
               FeeModel (profiles), SlippageModel, FillEngine (lookahead guard), Accounting (NAV, dividends, splits)
  llm/         Anthropic client wrapper: structured outputs (JSON schema), prompt builder (cache-friendly order),
               cost meter, monthly budget guard with priority shedding
  runs/        jobs: fill, digest, decide, reconcile, score_views, build_dashboard; run ledger (expected vs actual per
               trading day), idempotency per (date, job), git commit/push helper with rebase-retry
  analysis/    rolling-window historical study (both capital levels), null distributions, metrics, view scoring,
               fee-drag and re-costing, cohort aggregation
  dashboard/   generator -> docs/ (JSON + single-page app, Plotly from CDN, no build step)
configs/arms/*.yaml  configs/books.yaml  configs/fees/*.yaml  configs/universe.yaml  configs/cohorts.yaml
data/prices/ data/digests/ data/news/    state/arms/<id>/{views/,journal.json,decisions/,books/<10k|1k>/{book.json,trades.csv}}
state/ledger/ (runs, missed runs, source health, llm_cost)   docs/ (GitHub Pages)
tests/        .github/workflows/{ci,daily,historical,pages}.yml
```

**Daily cycle (UTC crons; ATHEX: pre-open 10:00-10:30, continuous 10:30-17:00, close 17:20 Athens)**
- 09:00 UTC `fill`: fills last evening's pending orders for every book at today's 10:30 opening-auction price (intraday
  1-min bar or quote `open`), applies slippage and the book's fee profile, credits dividends due, records fills.
  Idempotent; also runs at the start of the evening job so a missed morning run is caught up.
- 18:30 UTC `decide`: ingest -> shared digest (once) -> per active arm: prompt from filtered digest + prices + both
  books + journal -> structured decision (views for the universe + actions) -> rules layer -> per-book orders for next
  open -> decision record with UTC timestamp and digest hash -> commit + push. Then reconcile provisional bars, score
  matured views, update ledger and dashboard.
- Every job first checks the ledger: if this (trading date, job) already succeeded it exits. Workflow-level
  `concurrency: group: trading, cancel-in-progress: false`. Missed runs are detected against the ATHEX calendar; a missed
  `decide` is logged as MISSED and never back-filled (back-filling would be lookahead).

**No-lookahead, structurally**
- The decision job receives a `PriceStore.as_of(T)` view; any request for a bar dated after T raises. Tested.
- Orders carry `decision_ts` (UTC) and must be committed to git before the fill can exist. The FillEngine refuses any
  order whose `decision_ts` or git commit time is not strictly before the target session's 10:30 Athens open
  (`LOOKAHEAD_GUARD`). Tested with a fake clock and with a store that contains T+1 bars.
- Decision records store: prompt template version, digest id + included item ids, book and journal snapshots, raw model
  output, token counts and cost. Full input is reproducible byte-for-byte from committed artifacts.

**Arm isolation**: an ArmRunner is constructed only from its own state directory and the read-only shared digest; tests
run an arm in a temp dir containing only its own state and assert that no other arm's holdings/journal text ever appears
in any prompt (mock LLM captures prompts). The two books of one arm share the arm's journal; books of different arms
share nothing.

**LLM output contract** (structured outputs, validated with pydantic; invalid -> retry once -> hold, logged):
- `views`: for every universe ticker the model has a non-neutral opinion on, and at least its top 5 and bottom 5:
  {ticker, view in {strong_sell, sell, neutral, buy, strong_buy}, conviction 0-1, horizon_days, thesis <= 60 words,
  catalysts, risks, cited_item_ids}. "Opine more": views are cheap; orders are not.
- `actions`: proposed sells/buys (the rules layer decides what is actually allowed).
- `journal_updates` (bounded) and an optional regime note.

**Memory/journal** per arm: running thesis per held/watched ticker, open-position rationale, last 20 closed trades with
outcome, up to 10 "lessons"; rendered into the prompt within a ~3k-token budget. The journal also receives the arm's own
view-scoring summary (hit rates by horizon), so the model sees how its past calls did.

**Data realism**: whole shares; opening-auction fills; slippage = tiered half-spread by 20-day ADV (>= EUR 5M: 0.10%,
1-5M: 0.25%, 0.3-1M: 0.60%) + impact 0.10% x sqrt(order/ADV); orders above 1% of ADV are refused (the EUR 2,500 order
is 0.8% of ADV at the universe floor, so the floor is set accordingly). Dividends credited net of 5% withholding 10
trading days after ex-date; splits adjust shares and cost basis. Cash earns 0% (config).

## 3. Universe

Base rule (frozen; membership re-evaluated on the first trading day of each month, changes logged):
FTSE/ATHEX Large Cap + Mid Cap constituents with Yahoo coverage, 60-day median ADV >= EUR 300k, price >= EUR 0.30,
>= 120 trading days of history, plus `AETF.AT`. Existing positions in names that drop out may be held but not added to.
Draft membership (~36 names): ALPHA, ETE, EUROB, TPEIR, CREDIA, OPTIMA, BOCHGR, PPC, MTLN, ALWN, MOH, GEKTERNA, HTO,
BELA, ELPE, CENER, ELHA, TITC, ADMIE, BYLOT, VIO, AIA, LAMDA, AVAX, EYDAP, AEGN, KRI, AEM, PROF, EKTER, SAR, PPA,
INTRK, FOYRK, INTEK, AETF. (Yahoo volume for a few mid caps looks understated; verified at build time.)

## 4. Arms

LLM arms (one factor each vs. A). Base decision model Sonnet 5; digest model Haiku 4.5. Each runs both books.

| Arm | Differs from A by | Purpose |
|---|---|---|
| A base | - | all inputs incl. rumors, base prompt, base limits, Sonnet 5 |
| B | rumors off | does the rumor feed help or hurt |
| C | official announcements + prices only | does news help at all (depends on ATHEX feed access; else "prices only", flagged) |
| D | decision model = Opus 5 | is the expensive model worth it |
| R1, R2 | identical to A | replica spread = the agent's own noise |

Sonnet 5/Opus 5 accept no temperature/seed, so replicas measure inherent sampling noise; `seed` drives only the rules
layer and random baselines.

Rule-based baselines, free, **each at both EUR 10,000 and EUR 1,000 under the same constraints** (4 positions,
1 swap/month, 30-day hold, same fee profile):

| Baseline | Definition |
|---|---|
| 1 S&P 500 EUR (PRIMARY) | buy-and-hold `SXR8.DE`, DEGIRO Core Selection fee EUR 1; cross-check `^SP500TR` x EURUSD frictionless |
| 2 Greece | buy-and-hold `AETF.AT` under the Greek fee model; `GD.AT` price index as frictionless diagnostic (understates by ~3-5%/yr dividends; noted) |
| 3 Random picker | random views into the same rules layer, trade rate matched to A's trailing realized rate; 200 seeds live per book, 500 per historical window |
| 4 Momentum | 6-1 month momentum, top-4 equal weight; at most one swap/month (weakest holding vs strongest candidate) |
| 5 Equal-weight universe | quarterly rebalance, capped at the same order limits (naive-diversified Greek diagnostic) |

Cohorts: `configs/cohorts.yaml` starts a fresh copy of arm A (both books) and all free baselines on the first trading
day of each month; ids like `A@2026-11`. Other LLM arms get cohorts only if budget allows.

## 5. Risk limits (per arm config; per-book values in `configs/books.yaml`)

| Limit | Value | Note |
|---|---|---|
| Books | EUR 10,000 experiment; EUR 1,000 personal; long-only; no leverage; cash allowed | identical decisions |
| Positions | **target 4, max 5**, equal weight at purchase, max 30% of NAV, min 15% | see turnover note below |
| Min order | EUR 150 per book (final sells exempt); below it: skip or shrink, logged as divergence | binds only the EUR 1k book |
| Min holding | **30 trading days** | hard stop-loss exempt |
| Order cap | **<= 2 orders per calendar month** (one swap) | build-up exemption: first 5 trading days of a cohort allow up to max_positions buys |
| Turnover | **<= 25% of NAV per calendar month**, measured as replacement churn min(buys, sells)/NAV (see DECISIONS.md 2026-09-20 build clarifications) | waived during build-up |
| Fee budget | **<= 1.5% of NAV per calendar month, per book**; breaching orders rejected and logged; waived during build-up | calibrated so two DEGIRO orders fit at EUR 1k (EUR 10.05 vs EUR 15) |
| Hard stop-loss | -15% vs cost at close, sold next open; exempt from min hold and order cap; counts toward turnover and fee budget | tail protection |
| Liquidity | order <= 1% of 20-day ADV; universe floor EUR 300k ADV | binding for EUR 2,500 orders at the floor |
| LLM failure | invalid output -> retry once -> hold | spec |
| LLM budget | EUR 25/month hard cap; at 85% shed cohorts, then D, then B/C; at 100% all LLM arms hold; every hold logged | spec |

Turnover note: your 25% cap and 3 positions cannot coexist. One swap of a 33% position is 33% one-sided turnover, so a
3-position book could never replace a holding. With 4 positions at 25% each, one swap per month is exactly the cap, which
matches "<= 2 orders a month". Hence target 4, max 5. If you prefer 3 positions, the cap needs to be 35%.

## 6. Cost estimate (unchanged by the two-book change; books are accounting only)

USD at EUR/USD 1.15; 22 trading days. Per decision call ~13k input tokens (2.5k system, 1.5k price table, ~6k filtered
digest, ~3k books+journal incl. view-score summary), ~5.5k output incl. adaptive thinking (more views than rev 1).

| Component | Model | Calls/mo | $/call | $/mo |
|---|---|---|---|---|
| Shared digest (~75k in / 6k out) | Haiku 4.5 | 22 | 0.10 | 2.3 |
| Decisions A, B, C, R1, R2 | Sonnet 5 | 110 | 0.08 | 8.8 |
| Decision D | Opus 5 | 22 | 0.20 | 4.4 |
| Retries, schema failures, headroom (15%) | | | | 2.3 |
| **Total** | | | | **~$18 = ~EUR 16** |
| Each extra monthly cohort of A | Sonnet 5 | +22 | | +1.8 |

Three cohorts of A by month 3: ~EUR 19/month. All-Opus-5 variant: ~EUR 28 (over cap, no cohorts).

## 7. View-level scoring: the primary statistical instrument

- Every view is stored with its date, ticker, direction, conviction and horizon.
- Scoring: forward excess return of the ticker vs. the universe equal-weight return over 5, 10 and 20 trading days
  (and 30, matching the hold period), computed from open-to-open prices starting at the next session's open (the earliest
  price at which the view could have been acted on). Long views scored as-is, short views sign-flipped.
- Metrics per arm and horizon: hit rate, mean excess return, conviction-weighted mean, long-minus-short spread,
  and a 95% interval. Independence: consecutive daily views on the same ticker overlap, so the interval uses a
  block bootstrap over non-overlapping horizon-length blocks; n reported as effective (non-overlapping) views, not raw.
- Decay curve: excess return by horizon (5 -> 10 -> 20 -> 30). This is what decides the pre-registered outcome below.
- The books then test whether views survive execution: fees, whole shares, the order cap and the hold period.

## 8. Historical-window analysis (rule-based only)

One command: `python -m athex_agent.analysis.historical --start 2011-01-01`. Rolling 3-month windows (63 trading days)
starting every 5 trading days, same fee/slippage model, **both capital levels**, universe per window = names with data
and ADV filter at window start. Outputs: distribution of excess return vs SXR8 for random picker (500 seeds/window),
momentum, equal-weight, AETF/GD; P(beat S&P), median and percentiles; rendered on the dashboard with each live
arm/book placed as a percentile. Caveats on the dashboard: survivorship bias (Yahoo drops delisted names; the null is
optimistic by roughly 1-3%/yr), ALPHA.AT history gap, GD.AT price-only.

Contemporaneous null: the 200 live random-picker seeds per book run over the identical dates as each cohort; this is
the primary "is it luck" comparison. Post-cutoff LLM dry run: only arm C can be replayed faithfully; a 4-week labeled
plumbing dry run is planned and is not evidence of edge.

## 9. Dashboard additions (rev 2)
- Per arm: two equity curves (10k, 1k) with the divergence markers; per book: **fee drag = fees paid / average NAV,
  annualized**, plus slippage drag, plus re-costed fee drag under every profile in `configs/fees/`.
- View-scoring panel: decay curve by horizon, hit rate with intervals, per arm and vs. the random-view null.
- Everything else as rev 1 (A/B view, holdings, trade log, reasoning with cited items, health, budget).

## 10. Go-live criteria (pre-registered in PREREGISTRATION.md before the first live run)

After >= 6 months and >= 2 completed 3-month cohorts of arm A:
1. **Signal**: A's 20-day view excess return is positive with the block-bootstrap 95% interval excluding zero, on
   >= 300 effective views; and the 30-day horizon is not materially worse than the 20-day one.
2. **Experiment book (EUR 10k)**: after-fee return exceeds SXR8.DE over the full period and in >= 2 of 3 completed
   cohorts; excess return above the 90th percentile of the contemporaneous random-picker distribution at EUR 10k;
   also beats AETF.AT.
3. **Replicas**: A, R1, R2 agree in sign vs SXR8 and their return spread is < 5 percentage points.
4. **Personal book (EUR 1k)**: after-fee return exceeds SXR8.DE under the DEGIRO profile, or under the cheapest
   verified profile you would actually open an account with (named in PREREGISTRATION.md before go-live).
5. **Risk/health**: max drawdown < 15% (10k book); fees < 3% of starting capital per quarter on the book that would
   go live; > 95% of scheduled runs executed; LLM cost < EUR 25/month.

**Pre-registered possible outcome (the half-life tension).** A 30-trading-day hold assumes the signal persists well
beyond the days-long half-life typical of news-driven information. If view-level scoring shows edge at 5 days but
none at 20 or 30 (decay curve falls to zero inside the hold period), the conclusion is: *this approach does not fit a
fee-constrained small account*, because harvesting a 5-day signal needs turnover the fees forbid. That is a valid,
complete result of the experiment, not a failure to explain away, and it is written into PREREGISTRATION.md as such.
Explicitly: all criteria are a "not obviously luck" bar, not proof of edge; book-level power is low by construction,
which is why the signal criterion comes first.

Real-money sketch (design only): no retail broker reachable from Greece with a public API offers ATHEX (IBKR: no
ATHEX; Saxo: Greek shares disabled; DEGIRO/Freedom24/Greek brokers: no public API). Practical path: assisted execution
(agent posts orders as a GitHub issue/Telegram message; human executes at the open). Kill switch: repo variable
`TRADING_ENABLED`, a `HALT` file, automatic halts on -15% drawdown, 3 consecutive data failures or budget breach,
per-order notional cap, allow-listed universe, dry-run diff mode.

## 11. Known weaknesses (so far)
- The EUR 1k book loses 8-15%/yr to fees at the order cap with DEGIRO; that is the point of measuring it, not a bug.
- Book-level statistical power is low; view-level scoring is the compensating instrument, and its effective n is
  smaller than the raw view count because daily views on the same name overlap.
- Yahoo as sole free price feed: lagging bars, rate limits, understated volumes, no delisted names.
- Official announcements feed may be blocked from CI; arm C could degrade to prices-only.
- Rumor sources are thin (Reddit is small for Greek stocks; X has no free API).
- GitHub cron can be late by up to ~30 minutes; occasionally skipped under load. Detected and shown, not silent.
- Replica noise cannot be controlled via temperature on current models.
- Piraeus online minimum commission unverified; Freedom24 schedule dated 2023 (reviews in 2026 quote the same numbers).

## 12. Build stages after approval
1. Skeleton, configs (arms, books, fees), fee/slippage/accounting + tests, .gitignore/.env.example, CI.
2. Price layer, calendar, store, lookahead guard + tests. 3. Rules layer, Book, fill engine, rule-based arms at both
sizes, historical analysis. 4. News adapters, digest, source health. 5. LLM arms, journal, view scoring, budget guard,
isolation tests. 6. Jobs, ledger, workflows, Pages, dashboard. 7. Labeled dry run, README, PREREGISTRATION.md,
DECISIONS.md, CLAUDE.md. Commit at each milestone.

## 13. Approval notes (2026-09-20)
Approved as rev 2 with 4 target / 5 max positions and the 25% one-sided turnover cap, build-up and stop-loss exemptions
as written. Three build requirements added:
1. The Piraeus profile is shown on the dashboard with an **UNVERIFIED MINIMUM** badge (carried in the fee-profile
   config as `badge`), not only in code comments.
2. If fee economics ever exclude a stock from the EUR 1k book that the EUR 10k book holds (per-share fees on
   low-priced names, min order, fee budget), it is logged as a book divergence of kind `FEE_ECONOMICS_EXCLUSION`
   and surfaced on the dashboard.
3. The view-scoring panel reports **effective n** from the block bootstrap as the headline, raw view count secondary.
