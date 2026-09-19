from __future__ import annotations

from datetime import date

import pytest

from athex_agent.config.freeze import (
    FrozenConfigViolation,
    assert_frozen,
    fingerprint,
    read_lock,
    write_lock,
)


def test_fingerprint_is_deterministic(repo):
    a1 = repo.resolve_arm("A")
    a2 = repo.resolve_arm("A")
    assert fingerprint(a1) == fingerprint(a2)
    assert fingerprint(a1) != fingerprint(repo.resolve_arm("B"))


def test_lock_then_change_is_refused(repo, tmp_path):
    resolved = repo.resolve_arm("A")
    state_dir = tmp_path / "state" / "arms" / "A"
    assert read_lock(state_dir) is None
    write_lock(state_dir, resolved, date(2026, 10, 1))
    lock = read_lock(state_dir)
    assert lock["arm_id"] == "A" and lock["started_on"] == "2026-10-01"
    assert_frozen(state_dir, resolved)
    write_lock(state_dir, resolved, date(2026, 12, 1))  # idempotent, keeps the original date
    assert read_lock(state_dir)["started_on"] == "2026-10-01"

    changed_limits = resolved.limits.model_copy(update={"min_hold_trading_days": 31})
    changed = resolved.model_copy(update={"limits": changed_limits})
    with pytest.raises(FrozenConfigViolation, match="frozen"):
        assert_frozen(state_dir, changed)
    with pytest.raises(FrozenConfigViolation):
        write_lock(state_dir, changed, date(2026, 12, 1))
