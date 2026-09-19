"""Frozen live configs.

Once an arm has started trading, its resolved config (arm + limits + books + fee profiles +
slippage + universe rule) is fingerprinted into `<state_dir>/config.lock.json`. Every later run
must reproduce the same fingerprint; otherwise it refuses to run. Changing an experiment means
adding a new arm, never editing a running one (see DECISIONS.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from athex_agent.config.models import ResolvedArm

LOCK_FILE = "config.lock.json"


class FrozenConfigViolation(RuntimeError):
    pass


def fingerprint(resolved: ResolvedArm) -> str:
    payload = json.dumps(resolved.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / LOCK_FILE


def read_lock(state_dir: Path) -> dict[str, Any] | None:
    p = lock_path(state_dir)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_lock(state_dir: Path, resolved: ResolvedArm, started_on: date) -> Path:
    """Create the lock on first run; on later runs verify instead of overwriting."""
    existing = read_lock(state_dir)
    fp = fingerprint(resolved)
    if existing is not None:
        assert_frozen(state_dir, resolved)
        return lock_path(state_dir)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    lock = {
        "arm_id": resolved.arm.id,
        "fingerprint": fp,
        "started_on": started_on.isoformat(),
        "resolved": resolved.model_dump(mode="json"),
    }
    p = lock_path(state_dir)
    p.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def assert_frozen(state_dir: Path, resolved: ResolvedArm) -> None:
    existing = read_lock(state_dir)
    if existing is None:
        return
    fp = fingerprint(resolved)
    if existing["fingerprint"] != fp:
        raise FrozenConfigViolation(
            f"arm {resolved.arm.id}: resolved config changed since it started on "
            f"{existing['started_on']} (locked {existing['fingerprint'][:12]}, now {fp[:12]}). "
            "Live arms are frozen; create a new arm id instead of editing this one."
        )
