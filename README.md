# athex-agent

Autonomous LLM paper-trading experiment on the Athens Stock Exchange (ATHEX), run unattended on GitHub
Actions, with a static dashboard on GitHub Pages. The question it answers: would money do better in this
agent than in an S&P 500 index fund (in EUR), after realistic Greek broker fees, and is any edge
distinguishable from luck?

Status: stage 1 of 7 (skeleton, configs, fee/slippage/accounting models, tests, CI). Not trading yet.

- `DESIGN.md` — the approved design (architecture, arms, risk limits, fee analysis, cost estimate).
- `DECISIONS.md` — dated log of every change to the experiment.
- `CLAUDE.md` — architecture summary, layout, conventions and the invariants that must never be broken.

## Setup (local)
Requires Python 3.12.
```
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/pytest
cp .env.example .env     # fill in secrets locally; never commit .env
```
GitHub Actions setup (secrets, schedules, Pages) is documented in a later stage.
