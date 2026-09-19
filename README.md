# athex-agent

An autonomous LLM paper-trading experiment on the Athens Stock Exchange (ATHEX), run unattended by
GitHub Actions, with a static dashboard on GitHub Pages. The question it answers: would money do
better in this agent than in an S&P 500 index fund bought in EUR, after realistic Greek broker
fees, and is any difference distinguishable from luck?

- `DESIGN.md` — the approved design: architecture, arms, risk limits, fee analysis, cost estimate.
- `PREREGISTRATION.md` — go-live criteria, written before the first live run.
- `DECISIONS.md` — dated, append-only log of every change to the experiment.
- `CLAUDE.md` — architecture summary, layout, conventions and the invariants that must never break.

## How it works, in one paragraph
Every trading day at 18:30 UTC the `decide` job refreshes prices (free Yahoo Finance feed), ingests
Greek and English news, official announcements, macro releases and rumour sources (RSS and the
Reddit API; robots.txt respected), deduplicates them and summarises them once with a cheap model
(Claude Haiku 4.5) into a shared digest. Each LLM arm (Claude Sonnet 5; arm D on Claude Opus 5)
then sees a filtered view of that digest, a price table, its own two books and its own journal, and
returns per-stock views plus proposed actions as structured JSON. A deterministic rules layer turns
actions into whole-share orders under hard limits (4 target positions, 30-session hold, 2 orders a
month, one 25% swap a month, per-book fee budget, stop-loss), for a EUR 10,000 experiment book and a
EUR 1,000 personal book off identical decisions. Orders are committed to git before the next open;
at 09:00 UTC the `fill` job fills them at the opening auction with slippage and the broker's fees.
Rule-based baselines (S&P 500 ETF, Greek ETF, random picker with 200 seeds, momentum, equal weight)
run under the same rules at both sizes. Views are scored on forward excess returns; the dashboard
shows everything, including fee drag, replica spread, budget and missed runs.

## Local setup (Python 3.12)
```
git clone <your fork> athex-agent && cd athex-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/pytest          # 150+ tests, all offline
cp .env.example .env                                 # local secrets only; never committed
```
Smoke tests that touch the network (run them once before going live):
```
.venv/bin/python -m athex_agent.data.smoke           # price feed coverage and freshness
.venv/bin/python -m athex_agent.digest.smoke         # every news source, robots.txt, ticker tagging
ANTHROPIC_API_KEY=... .venv/bin/python -m athex_agent.llm.smoke   # one Haiku + one Sonnet call
```
Labelled dry run (no model calls, separate state under `.cache/dryrun/`, banner on the dashboard):
```
.venv/bin/python -m athex_agent.runs decide --dry-run --as-of 2026-09-15 -v
.venv/bin/python -m athex_agent.runs fill   --dry-run --as-of 2026-09-16 -v
.venv/bin/python -m athex_agent.runs dashboard --dry-run   # then open .cache/dryrun/docs/index.html
```
Fee-drag tables and the historical study (rule-based strategies only, never the LLM arms):
```
.venv/bin/python -m athex_agent.analysis.fee_drag
.venv/bin/python -m athex_agent.analysis.historical --start 2011-01-01 --seeds 200
```

## GitHub setup, step by step (new to Actions)
1. Create an empty repository on GitHub and push this code to its `main` branch.
2. Secrets. Repository → Settings → Secrets and variables → Actions → "New repository secret":
   - `ANTHROPIC_API_KEY` (required). Get it from console.anthropic.com.
   - `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (optional; the rumour source is skipped without
     them). Create a "script" app at reddit.com/prefs/apps.
   Secrets are never written to the repository; the workflows read them as environment variables.
3. Variables (same page, "Variables" tab): `LLM_MONTHLY_CAP_EUR` = `25` (the hard cap; the code
   refuses calls beyond it) and `TRADING_ENABLED` = `true` (set to `false` to stop everything).
4. Pages. Settings → Pages → "Build and deployment" → Source: **GitHub Actions**. The
   `Deploy dashboard` workflow publishes `docs/` after every state commit.
5. Actions. Open the Actions tab and enable workflows if GitHub asks. Then:
   - run "Daily trading run" manually with job `bootstrap` once (downloads the full price history
     into `data/` and commits it), unless `data/prices/` is already populated;
   - run "Historical study" once (about 30-90 minutes; writes `docs/data/historical.json`);
   - optionally run "Daily trading run" with job `decide` once to start the arms today; otherwise
     the schedule starts them at the next 18:30 UTC.
6. Schedule. `fill` runs at 09:00 UTC (11:00/12:00 Athens, after the 10:30 opening auction) and
   `decide` at 18:30 UTC (20:30/21:30 Athens, after the 17:20 close), Monday to Friday; ATHEX
   holidays are skipped by the calendar. GitHub may delay cron by up to about 30 minutes; a run that
   never happens shows up on the dashboard as MISSED and is never back-filled.
7. The dashboard URL is `https://<user>.github.io/<repo>/`.

## Adding or changing an arm
Add a YAML under `configs/arms/`. A live arm's resolved config is frozen by a lock file on its first
run; editing it is refused. To change an experiment, add a new arm id, keep the old one, and write a
dated entry in `DECISIONS.md`. Every LLM arm with a `base_arm` must differ from it only in the
declared `factor` paths (the loader and tests enforce this).

## Reading the results honestly
- The EUR 10k book is the primary result; the EUR 1k book shows whether that result survives your
  actual size. Divergences between the two are listed per arm.
- "pct vs random" is the share of random-picker books (same dates, same rules) this arm beat; below
  90% is indistinguishable from luck under the pre-registered criteria.
- View scoring shows the effective sample size first; overlapping daily views on correlated Greek
  names are far from independent.
- The Piraeus fee profile carries an UNVERIFIED MINIMUM badge until the broker confirms in writing
  that online orders have no minimum commission.

## Known weaknesses
See DESIGN.md section 11 and the "caveats" embedded in `docs/data/historical.json`: single free
price feed with lagging bars and no delisted names (survivorship bias in the null), official ATHEX
announcements page not reachable by a crawler (arm C degrades to prices-only), thin rumour sources,
low statistical power at the book level, replica noise not controllable on current models.
