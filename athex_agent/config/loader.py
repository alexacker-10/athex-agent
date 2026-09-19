"""Load and cross-validate the YAML configs under configs/."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from athex_agent.config.models import (
    ArmConfig,
    BooksConfig,
    CohortSchedule,
    FeeProfile,
    ResolvedArm,
    RiskLimits,
    SlippageConfig,
    UniverseConfig,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "configs"

# Fields that are allowed to differ between an arm and its base without counting as a factor.
_IDENTITY_FIELDS = {"id", "description", "base_arm", "factor", "cohort_start", "active"}


class ConfigError(ValueError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def arm_diff(a: ArmConfig, b: ArmConfig) -> dict[str, tuple[Any, Any]]:
    """Dotted paths where two arm configs differ, ignoring identity fields."""
    fa = flatten(a.model_dump(mode="json"))
    fb = flatten(b.model_dump(mode="json"))
    keys = (set(fa) | set(fb)) - _IDENTITY_FIELDS
    return {k: (fa.get(k), fb.get(k)) for k in sorted(keys) if fa.get(k) != fb.get(k)}


class ConfigRepo:
    """Typed access to the config directory, with reference checks."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_CONFIG_DIR
        if not self.root.is_dir():
            raise ConfigError(f"config directory not found: {self.root}")

    # ---- single files
    def fee_profile(self, profile_id: str) -> FeeProfile:
        return FeeProfile.model_validate(load_yaml(self.root / "fees" / f"{profile_id}.yaml"))

    def fee_profiles(self) -> dict[str, FeeProfile]:
        out = {}
        for p in sorted((self.root / "fees").glob("*.yaml")):
            prof = FeeProfile.model_validate(load_yaml(p))
            if prof.id != p.stem:
                raise ConfigError(f"{p}: id '{prof.id}' does not match filename")
            out[prof.id] = prof
        return out

    def books(self) -> BooksConfig:
        return BooksConfig.model_validate(load_yaml(self.root / "books.yaml"))

    def limits(self, limits_id: str) -> RiskLimits:
        return RiskLimits.model_validate(load_yaml(self.root / "limits" / f"{limits_id}.yaml"))

    def slippage(self) -> SlippageConfig:
        return SlippageConfig.model_validate(load_yaml(self.root / "slippage.yaml"))

    def universe(self) -> UniverseConfig:
        return UniverseConfig.model_validate(load_yaml(self.root / "universe.yaml"))

    def cohorts(self) -> CohortSchedule:
        return CohortSchedule.model_validate(load_yaml(self.root / "cohorts.yaml"))

    def arm(self, arm_id: str) -> ArmConfig:
        path = self.root / "arms" / f"{arm_id}.yaml"
        arm = ArmConfig.model_validate(load_yaml(path))
        if arm.id != arm_id:
            raise ConfigError(f"{path}: id '{arm.id}' does not match filename")
        return arm

    def arms(self) -> dict[str, ArmConfig]:
        return {p.stem: self.arm(p.stem) for p in sorted((self.root / "arms").glob("*.yaml"))}

    # ---- resolution with reference checks
    def resolve_arm(self, arm_id: str) -> ResolvedArm:
        arm = self.arm(arm_id)
        limits = self.limits(arm.limits)
        books_cfg = self.books()
        by_id = {b.id: b for b in books_cfg.books}
        missing = [b for b in arm.books if b not in by_id]
        if missing:
            raise ConfigError(f"arm {arm_id}: unknown books {missing}")
        books = [by_id[b] for b in arm.books]
        profiles = self.fee_profiles()
        needed = {arm.fee_profile_override or b.fee_profile for b in books}
        unknown = sorted(p for p in needed if p not in profiles)
        if unknown:
            raise ConfigError(f"arm {arm_id}: unknown fee profiles {unknown}")
        universe = self.universe()
        if arm.universe != universe.id:
            raise ConfigError(f"arm {arm_id}: unknown universe '{arm.universe}'")
        if arm.base_arm is not None:
            base = self.arm(arm.base_arm)
            diff = arm_diff(base, arm)
            if set(diff) != set(arm.factor):
                raise ConfigError(
                    f"arm {arm_id}: declared factor {sorted(arm.factor)} but differs from "
                    f"{arm.base_arm} in {sorted(diff)}"
                )
        return ResolvedArm(
            arm=arm,
            limits=limits,
            books=books,
            fee_profiles={p: profiles[p] for p in sorted(needed)},
            slippage=self.slippage(),
            universe=universe,
        )

    def validate_all(self) -> list[str]:
        """Resolve every arm and the cohort schedule; returns the list of arm ids checked."""
        arms = self.arms()
        for arm_id in arms:
            self.resolve_arm(arm_id)
        cohorts = self.cohorts()
        unknown = [a for a in cohorts.arms if a not in arms]
        if unknown:
            raise ConfigError(f"cohorts.yaml references unknown arms {unknown}")
        recost = self.books().recost_profiles
        profiles = self.fee_profiles()
        unknown = [p for p in recost if p not in profiles]
        if unknown:
            raise ConfigError(f"books.yaml recost_profiles references unknown profiles {unknown}")
        return sorted(arms)
