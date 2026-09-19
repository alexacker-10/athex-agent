"""Which arm instances run today, when cohorts start, and which LLM calls the budget allows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from athex_agent.arms.runner import ArmPaths
from athex_agent.config.loader import ConfigRepo
from athex_agent.config.models import ResolvedArm
from athex_agent.data import calendar as cal

COHORT_RANDOM_SEEDS = 50
FULL_RECORD_SEEDS = 5  # random seeds beyond this keep NAV only (no per-day decision/view files)
ESTIMATE_EUR = {"claude-opus-5": 0.20, "claude-sonnet-5": 0.08, "claude-haiku-4-5": 0.03}
SHED_PRIORITY = ["cohort", "D", "C", "B", "R2", "R1", "A"]  # first entries are shed first


@dataclass(frozen=True)
class ArmInstance:
    arm_id: str
    instance: str | None
    cohort: str | None  # "YYYY-MM" for staggered copies, None for the base instance
    start_on: date
    resolved: ResolvedArm
    light: bool = False  # NAV series only; no per-day decision, view or prompt files

    @property
    def key(self) -> str:
        return f"{self.arm_id}@{self.instance}" if self.instance else self.arm_id

    @property
    def is_llm(self) -> bool:
        return self.resolved.arm.kind == "llm"

    def paths(self, state_root: Path) -> ArmPaths:
        return ArmPaths(state_root, self.arm_id, self.instance)


def _months_between(start: date, end: date) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def active_instances(repo: ConfigRepo, first_live: date | None, today: date) -> list[ArmInstance]:
    """Base instances for every active arm (random: one per seed) plus monthly cohorts."""
    first_live = first_live or today
    cohorts = repo.cohorts()
    out: list[ArmInstance] = []
    for arm_id, arm in repo.arms().items():
        if not arm.active:
            continue
        resolved = repo.resolve_arm(arm_id)
        start_on = arm.cohort_start or first_live
        if arm.kind == "rule" and arm.rule and arm.rule.kind == "random":
            n = int(arm.rule.params.get("n_seeds", 1))
            out.extend(
                ArmInstance(
                    arm_id, f"seed-{k:03d}", None, start_on, resolved, k >= FULL_RECORD_SEEDS
                )
                for k in range(n)
            )
        else:
            out.append(ArmInstance(arm_id, None, None, start_on, resolved))
        if arm_id in cohorts.arms:
            months = _months_between(first_live, today)[1 : cohorts.months + 1]
            for month in months:
                y, m = (int(x) for x in month.split("-"))
                start = cal.first_trading_day_of_month(y, m)
                if start > today:
                    continue
                if arm.kind == "rule" and arm.rule and arm.rule.kind == "random":
                    out.extend(
                        ArmInstance(
                            arm_id,
                            f"cohort-{month}-seed-{k:03d}",
                            month,
                            start,
                            resolved,
                            k >= FULL_RECORD_SEEDS,
                        )
                        for k in range(COHORT_RANDOM_SEEDS)
                    )
                else:
                    out.append(ArmInstance(arm_id, f"cohort-{month}", month, start, resolved))
    return out


def shed_order(inst: ArmInstance) -> int:
    if inst.cohort is not None:
        return 0
    try:
        return SHED_PRIORITY.index(inst.arm_id)
    except ValueError:
        return 1


def plan_llm_calls(
    instances: list[ArmInstance], remaining_eur: float, reserve_eur: float = 0.0
) -> dict[str, bool]:
    """Decide which LLM instances may call today given the remaining monthly budget.

    Highest-priority arms (A, replicas) are funded first; cohorts are shed first."""
    llm = sorted((i for i in instances if i.is_llm), key=shed_order, reverse=True)
    budget = remaining_eur - reserve_eur
    allowed: dict[str, bool] = {}
    for inst in llm:
        est = ESTIMATE_EUR.get(inst.resolved.arm.llm.model if inst.resolved.arm.llm else "", 0.10)
        if budget - est >= 0:
            allowed[inst.key] = True
            budget -= est
        else:
            allowed[inst.key] = False
    return allowed


def random_p_swap_from(orders_last_n_sessions: int, n_sessions: int) -> float:
    """Trade-rate matching: one swap = two orders; probability of a swap per decision day."""
    if n_sessions <= 0:
        return 1.0 / 21.0
    return max(0.0, min(1.0, (orders_last_n_sessions / 2.0) / n_sessions))
