from __future__ import annotations

import pytest
from pydantic import ValidationError

from athex_agent.config.loader import ConfigError, ConfigRepo, arm_diff
from athex_agent.config.models import BookConfig, BooksConfig, RiskLimits, UniverseConfig


def test_all_configs_resolve(repo):
    ids = repo.validate_all()
    assert {
        "A",
        "B",
        "C",
        "D",
        "R1",
        "R2",
        "bh_sp500",
        "bh_athex",
        "random",
        "momentum",
        "equal_weight",
    } <= set(ids)


def test_one_factor_at_a_time(repo):
    arms = repo.arms()
    base = arms["A"]
    for arm in arms.values():
        if arm.kind != "llm" or arm.id == "A":
            continue
        diff = arm_diff(base, arm)
        assert set(diff) == set(arm.factor), arm.id
        groups = {k.split(".")[0] for k in arm.factor}
        assert len(groups) <= 1, f"{arm.id} changes more than one factor group: {groups}"
    assert arm_diff(base, arms["R1"]) == {}
    assert arm_diff(base, arms["B"]) == {"sources.rumors": (True, False)}
    assert set(arm_diff(base, arms["D"])) == {"llm.model"}


def test_declared_factor_must_match(repo, tmp_path):
    import shutil

    root = tmp_path / "configs"
    shutil.copytree(repo.root, root)
    b = root / "arms" / "B.yaml"
    b.write_text(b.read_text().replace("factor: [sources.rumors]", "factor: [llm.model]"))
    with pytest.raises(ConfigError, match="declared factor"):
        ConfigRepo(root).resolve_arm("B")


def test_books_and_fee_profiles(repo):
    books = repo.books()
    assert [b.id for b in books.books] == ["10k", "1k"]
    assert [b.capital_eur for b in books.books] == [10000, 1000]
    assert [b.primary for b in books.books] == [True, False]
    resolved = repo.resolve_arm("bh_sp500")
    assert resolved.fee_profile_for(resolved.books[0]).id == "degiro_etf_core"
    resolved = repo.resolve_arm("A")
    assert resolved.fee_profile_for(resolved.books[1]).id == "degiro"


def test_exactly_one_primary_book():
    kw = dict(
        label="x", capital_eur=1, fee_profile="degiro", min_order_eur=0, fee_budget_pct_month=0.01
    )
    with pytest.raises(ValidationError):
        BooksConfig(
            books=[BookConfig(id="a", primary=True, **kw), BookConfig(id="b", primary=True, **kw)]
        )
    with pytest.raises(ValidationError):
        BooksConfig(books=[BookConfig(id="a", primary=True, **kw), BookConfig(id="a", **kw)])


def test_turnover_cap_must_allow_one_swap():
    kw = dict(
        id="x",
        max_positions=5,
        max_weight=0.4,
        min_weight=0.1,
        min_hold_trading_days=30,
        max_orders_per_month=2,
        stop_loss_pct=0.15,
        buildup_trading_days=5,
        max_adv_fraction=0.01,
    )
    with pytest.raises(ValidationError, match="never swap"):
        RiskLimits(target_positions=3, max_turnover_pct_month=0.25, **kw)
    RiskLimits(target_positions=3, max_turnover_pct_month=0.34, **kw)
    RiskLimits(target_positions=4, max_turnover_pct_month=0.25, **kw)


def test_base_limits_are_the_approved_ones(repo):
    lim = repo.limits("base")
    assert (lim.target_positions, lim.max_positions) == (4, 5)
    assert lim.min_hold_trading_days == 30
    assert lim.max_orders_per_month == 2
    assert lim.max_turnover_pct_month == 0.25
    assert lim.buildup_trading_days == 5


def test_universe_aliases_resolve(repo):
    u = repo.universe()
    assert u.canonical("OPAP.AT") == "ALWN.AT"
    assert u.canonical("ETE.AT") == "ETE.AT"
    with pytest.raises(ValidationError):
        UniverseConfig(
            id="u",
            description="d",
            min_adv_eur=1,
            min_price_eur=1,
            min_history_trading_days=1,
            adv_window_days=1,
            candidates=["A.AT"],
            aliases={"B.AT": "C.AT"},
        )


def test_unknown_references_are_errors(repo, tmp_path):
    import shutil

    root = tmp_path / "configs"
    shutil.copytree(repo.root, root)
    a = root / "arms" / "A.yaml"
    a.write_text(a.read_text().replace('books: ["10k", "1k"]', 'books: ["10k", "5k"]'))
    with pytest.raises(ConfigError, match="unknown books"):
        ConfigRepo(root).resolve_arm("A")
