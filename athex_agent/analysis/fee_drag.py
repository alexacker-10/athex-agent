"""Fee-drag tables (DESIGN.md section 1) and the per-book fee-drag metric for the dashboard.

Run: python -m athex_agent.analysis.fee_drag
"""

from __future__ import annotations

import math
from typing import Any

from athex_agent.config.loader import ConfigRepo
from athex_agent.portfolio.fees import compute_fees, quarterly_account_fee
from athex_agent.portfolio.money import round_cents

TRADING_DAYS_PER_YEAR = 252


def per_order_table(
    repo: ConfigRepo, n_positions: int = 4, median_price: float = 12.0
) -> list[dict]:
    rows: list[dict[str, Any]] = []
    profiles = repo.fee_profiles()
    for book in repo.books().books:
        order = book.capital_eur / n_positions
        shares = math.floor(order / median_price)
        for pid in repo.books().recost_profiles:
            p = profiles[pid]
            buy = compute_fees(p, "BUY", order, shares).total
            sell = compute_fees(p, "SELL", order, shares).total
            rt = round_cents(buy + sell)
            rows.append(
                {
                    "profile": pid,
                    "badge": p.badge,
                    "book": book.id,
                    "capital": book.capital_eur,
                    "order": order,
                    "buy": buy,
                    "sell": sell,
                    "round_trip": rt,
                    "pct_of_position": rt / order,
                    "pct_of_nav": rt / book.capital_eur,
                }
            )
    return rows


def annual_drag_table(
    repo: ConfigRepo,
    swaps_per_year: tuple[int, ...] = (6, 12),
    n_positions: int = 4,
    median_price: float = 12.0,
    slippage_per_side: float = 0.0015,
) -> list[dict]:
    rows: list[dict[str, Any]] = []
    profiles = repo.fee_profiles()
    for book in repo.books().books:
        order = book.capital_eur / n_positions
        shares = math.floor(order / median_price)
        for pid in repo.books().recost_profiles:
            p = profiles[pid]
            buy = compute_fees(p, "BUY", order, shares).total
            sell = compute_fees(p, "SELL", order, shares).total
            fixed = 4 * quarterly_account_fee(p, book.capital_eur)
            row: dict[str, Any] = {
                "profile": pid,
                "badge": p.badge,
                "book": book.id,
                "fixed_fees_year": fixed,
            }
            for s in swaps_per_year:
                fees = n_positions * buy + s * (buy + sell) + fixed
                slip = (n_positions * order + 2 * s * order) * slippage_per_side
                row[f"drag_{s}_swaps"] = fees / book.capital_eur
                row[f"drag_{s}_swaps_with_slippage"] = (fees + slip) / book.capital_eur
            rows.append(row)
    return rows


def fee_drag_metric(nav_rows: list[dict[str, Any]]) -> dict[str, float]:
    """Annualized cost drag from a book's nav.csv rows: fees+tax and slippage over average NAV."""
    if not nav_rows:
        return {"fees_pct_annualized": 0.0, "slippage_pct_annualized": 0.0, "days": 0}
    navs = [float(r["nav"]) for r in nav_rows]
    avg_nav = sum(navs) / len(navs)
    days = len(nav_rows)
    first, last = nav_rows[0], nav_rows[-1]
    fees = (
        float(last["fees_cum"])
        + float(last["tax_cum"])
        - float(first["fees_cum"])
        - float(first["tax_cum"])
    )
    slip = float(last["slippage_cum"]) - float(first["slippage_cum"])
    scale = TRADING_DAYS_PER_YEAR / max(days, 1)
    return {
        "fees_pct_annualized": fees / avg_nav * scale,
        "slippage_pct_annualized": slip / avg_nav * scale,
        "days": days,
    }


def _fmt_table(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    head = "| " + " | ".join(c[1] for c in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = []
    for r in rows:
        cells = []
        for key, _, fmt in cols:
            v = r.get(key)
            cells.append(fmt.format(v) if v is not None and v != "" else "")
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body])


def main() -> int:
    repo = ConfigRepo()
    print("Per order (4 equal positions, share count at median price EUR 12)\n")
    rows = per_order_table(repo)
    for r in rows:
        if r["badge"]:
            r["profile"] = f"{r['profile']} [{r['badge']}]"
    print(
        _fmt_table(
            rows,
            [
                ("profile", "profile", "{}"),
                ("book", "book", "{}"),
                ("order", "order", "{:.0f}"),
                ("buy", "buy", "{:.2f}"),
                ("sell", "sell", "{:.2f}"),
                ("round_trip", "round trip", "{:.2f}"),
                ("pct_of_position", "% of position", "{:.2%}"),
                ("pct_of_nav", "% of NAV", "{:.2%}"),
            ],
        )
    )
    print("\nAnnual drag, % of starting NAV (year 1 incl. 4 initial buys; a swap = sell + buy)\n")
    rows = annual_drag_table(repo)
    for r in rows:
        if r["badge"]:
            r["profile"] = f"{r['profile']} [{r['badge']}]"
    print(
        _fmt_table(
            rows,
            [
                ("profile", "profile", "{}"),
                ("book", "book", "{}"),
                ("drag_6_swaps", "6 swaps", "{:.2%}"),
                ("drag_12_swaps", "12 swaps (cap)", "{:.2%}"),
                ("drag_12_swaps_with_slippage", "cap + slippage", "{:.2%}"),
                ("fixed_fees_year", "fixed fees/yr", "{:.0f}"),
            ],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
