"""Fee and book-walk helpers that MIRROR the kit's formula (yeno-house-bot-v1.py `_fees`).

Used for sizing decisions only. The evaluator owns real fees, fills and cash.
Per leg: taker = round(0.07 * sum(shares * p * (1 - p)), 5); overlay = round(0.01 * gross, 5).
"""

from __future__ import annotations

from typing import Any

TAKER_RATE = 0.07
OVERLAY_RATE = 0.01
EPS = 1e-9

Level = tuple[float, float]


def levels(rows: Any, *, reverse: bool) -> list[Level]:
    """Parse [[price, size], ...] into sorted valid levels (asks ascending, bids descending with reverse=True)."""
    out: list[Level] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    return sorted(out, reverse=reverse)


def leg_fees(fills: list[Level]) -> float:
    """Total fees for one leg made of (price, shares) fills, rounded exactly like the kit."""
    gross = sum(p * s for p, s in fills)
    taker = sum(TAKER_RATE * s * p * (1.0 - p) for p, s in fills)
    return round(taker + 1e-12, 5) + round(gross * OVERLAY_RATE + 1e-12, 5)


def unit_cost(price: float) -> float:
    """Fee-inclusive cash per share at a single price (before 5-dp rounding)."""
    return price * (1.0 + OVERLAY_RATE + TAKER_RATE * (1.0 - price))


def max_shares_for_cash(price: float, cash: float) -> float:
    """Max shares affordable at one price level with `cash`, fee-inclusive: cash / (p * (1.01 + 0.07 * (1 - p)))."""
    if not 0.0 < price < 1.0 or cash <= 0.0:
        return 0.0
    return cash / unit_cost(price)


def walk(book_levels: list[Level], shares: float) -> list[Level] | None:
    """Fills for `shares` against sorted levels; None if depth is insufficient."""
    remaining = shares
    fills: list[Level] = []
    for price, available in book_levels:
        qty = min(remaining, available)
        fills.append((price, qty))
        remaining -= qty
        if remaining <= EPS:
            return fills
    return None


def buy_cost(asks: Any, shares: float) -> tuple[float, float] | None:
    """(gross, fees) to buy `shares` by walking the asks, or None if too thin."""
    fills = walk(levels(asks, reverse=False), shares)
    if fills is None:
        return None
    return sum(p * s for p, s in fills), leg_fees(fills)


def sell_proceeds(bids: Any, shares: float) -> tuple[float, float] | None:
    """(gross, fees) to sell `shares` by walking the bids, or None if too thin."""
    fills = walk(levels(bids, reverse=True), shares)
    if fills is None:
        return None
    return sum(p * s for p, s in fills), leg_fees(fills)


def cycle_pnl(obs: dict[str, Any]) -> float | None:
    """Net $ P&L of selling the remaining position now against visible bids, including prior partial exits."""
    if not isinstance(obs, dict):
        return None
    pos = (obs.get("account") or {}).get("position")
    if not isinstance(pos, dict):
        return None
    try:
        side = str(pos["outcome"])
        shares = float(pos["shares"])
        paid = float(pos["buy_gross_usd"]) + float(pos["buy_fees_usd"])
        received = float(pos.get("sell_gross_usd", 0.0)) - float(pos.get("sell_fees_usd", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    if side not in ("YES", "NO") or shares <= 0.0:
        return None
    bids = ((obs.get("books") or {}).get(side) or {}).get("bids")
    executable = sell_proceeds(bids, shares)
    if executable is None:
        return None
    sell_gross, sell_fees = executable
    return received + sell_gross - sell_fees - paid
