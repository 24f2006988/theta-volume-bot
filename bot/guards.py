"""Rule guards. Every response leaves the bot through final_check(), which turns anything illegal into HOLD.

All functions are pure functions of the observation (no clocks, no globals, no I/O).
"""

from __future__ import annotations

import math
from typing import Any

from bot.fees import Level, leg_fees, levels, max_shares_for_cash

HARD_MAX_BUY_USD = 5.0              # contract: $5 fee-inclusive per BUY, regardless of what rules{} says
FINAL_ENTRY_BLOCK_SECONDS = 15.0    # contract: no new entries inside the final 15 s
ENTRY_LATENCY_MARGIN_SECONDS = 1.0  # BUY executes >= 250 ms later; keep 1 s clear of the block
MAX_REFERENCE_AGE_SECONDS = 1.5     # same as House Bot v1
ADVERSE_HEADROOM = 0.02             # sizing must survive a +2c ask move during latency
PRICE_CEILING = 0.99                # highest tradable price on a 0.01 tick
OUTCOMES = ("YES", "NO")

HOLD: dict[str, Any] = {"action": "HOLD"}


def hold() -> dict[str, Any]:
    return {"action": "HOLD"}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def target_reached(obs: dict[str, Any]) -> bool:
    """True once eligible volume has reached the target: from then on we only ever HOLD."""
    acct = obs.get("account") or {}
    rules = obs.get("rules") or {}
    vol = _finite(acct.get("eligibleVolumeUsd"))
    target = _finite(rules.get("targetVolumeUsd"))
    if target is None:
        target = 1000.0
    return vol is not None and vol >= target - 1e-9


CASH_SAFETY_MARGIN_USD = 1e-6       # guard against evaluator rounding cashUsd up at 6 dp


def buy_cap(obs: dict[str, Any]) -> float:
    """Largest fee-inclusive BUY allowed now: min(cash - 1e-6, rules max, hard $5)."""
    acct = obs.get("account") or {}
    rules = obs.get("rules") or {}
    cash = (_finite(acct.get("cashUsd")) or 0.0) - CASH_SAFETY_MARGIN_USD
    rule_max = _finite(rules.get("maximumBuyCashUsd"))
    cap = min(cash, HARD_MAX_BUY_USD, rule_max if rule_max is not None else HARD_MAX_BUY_USD)
    return max(floor6(cap), 0.0)


def reference_ok(obs: dict[str, Any], max_age: float = MAX_REFERENCE_AGE_SECONDS) -> bool:
    """Reference must exist, be final (not provisional), fresh, causal and numeric."""
    ref = obs.get("reference")
    now = _finite(obs.get("timestamp"))
    if not isinstance(ref, dict) or now is None:
        return False
    if ref.get("targetProvisional") is not False:   # missing flag counts as provisional
        return False
    mid = _finite(ref.get("btcMidUsd"))
    target = _finite(ref.get("openingTargetUsd"))
    observed = _finite(ref.get("observedAt"))
    target_observed = _finite(ref.get("targetObservedAt"))
    if None in (mid, target, observed, target_observed):
        return False
    if mid <= 0 or target <= 0:  # type: ignore[operator]
        return False
    if now - observed > max_age + 1e-9:  # type: ignore[operator]
        return False
    if observed > now + 1e-9 or target_observed > now + 1e-9:  # type: ignore[operator]
        return False
    return True


def entry_time_ok(obs: dict[str, Any]) -> bool:
    secs = _finite((obs.get("market") or {}).get("secondsToClose"))
    return secs is not None and secs > FINAL_ENTRY_BLOCK_SECONDS + ENTRY_LATENCY_MARGIN_SECONDS


def min_order_size(obs: dict[str, Any], outcome: str) -> float | None:
    book = (obs.get("books") or {}).get(outcome) or {}
    m = _finite(book.get("minOrderSize"))
    return m if m is not None and m > 0 else None


def _fills_cost(fills: list[Level]) -> float:
    return sum(p * s for p, s in fills) + leg_fees(fills) if fills else 0.0


def headroom_shares(asks: Any, cash: float, headroom: float = ADVERSE_HEADROOM) -> float:
    """Shares `cash` buys by walking the sorted ask book with every level shifted up by `headroom`."""
    if cash <= 0.0:
        return 0.0
    fills: list[Level] = []
    for price, avail in levels(asks, reverse=False):
        if price + headroom > PRICE_CEILING + 1e-9:
            break
        p = min(price + headroom, PRICE_CEILING)
        spent = _fills_cost(fills)
        rem = cash - spent
        if rem <= 1e-12:
            break
        lo, hi = 0.0, min(avail, max_shares_for_cash(p, rem + 2e-5))
        if _fills_cost(fills + [(p, hi)]) <= cash + 1e-12:
            q = hi
        else:
            for _ in range(52):
                mid = 0.5 * (lo + hi)
                if _fills_cost(fills + [(p, mid)]) <= cash + 1e-12:
                    lo = mid
                else:
                    hi = mid
            q = lo
        if q <= 1e-12:
            break
        fills.append((p, q))
        if q < avail - 1e-9:
            break
    return sum(s for _, s in fills)


def sizing_ok(asks: Any, cash: float, min_size: float, headroom: float = ADVERSE_HEADROOM) -> bool:
    """CONTRACT.md sizing-headroom rule: still >= minOrderSize shares after a +2c adverse move."""
    return headroom_shares(asks, cash, headroom) >= min_size - 1e-9


def exit_depth_ok(obs: dict[str, Any], outcome: str, shares: float, coverage: float = 1.0,
                  max_slippage: float = 0.02) -> bool:
    """Visible bids within `max_slippage` of best_bid cover `coverage` x shares."""
    book = (obs.get("books") or {}).get(outcome) or {}
    bids = levels(book.get("bids"), reverse=True)
    if not bids:
        return False
    best_bid = bids[0][0]
    depth = sum(size for price, size in bids if price >= best_bid - max_slippage - 1e-9)
    return depth >= coverage * shares - 1e-9


def floor6(x: float) -> float:
    """Round DOWN to 6 decimals so a sized amount can never creep above a cap."""
    return math.floor(x * 1_000_000 + 1e-7) / 1_000_000


def final_check(resp: Any, obs: dict[str, Any]) -> dict[str, Any]:
    """Last line of defence. Returns a contract-valid response; anything doubtful becomes HOLD."""
    if not isinstance(resp, dict):
        return hold()
    action = resp.get("action")
    if action == "HOLD":
        return hold()
    if target_reached(obs):
        return hold()
    position = (obs.get("account") or {}).get("position")

    if action == "SELL":
        return {"action": "SELL"} if position is not None else hold()

    if action != "BUY":
        return hold()
    if position is not None:                       # one position / pending BUY at a time
        return hold()
    outcome = resp.get("outcome")
    if outcome not in OUTCOMES:
        return hold()
    if not entry_time_ok(obs) or not reference_ok(obs):
        return hold()
    amount = _finite(resp.get("maxCashUsd"))
    if amount is None or amount <= 0:
        return hold()
    amount = floor6(amount)
    if amount > buy_cap(obs) + 1e-12 or amount <= 0:
        return hold()
    min_size = min_order_size(obs, outcome)
    asks = levels(((obs.get("books") or {}).get(outcome) or {}).get("asks"), reverse=False)
    if min_size is None or not asks:
        return hold()
    if not sizing_ok(asks, amount, min_size):
        return hold()
    return {"action": "BUY", "outcome": outcome, "maxCashUsd": amount}
