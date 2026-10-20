"""Strategy entry point: decide(observation) -> response. Pure function of the request.

v1 "high-price theta" (docs/MICRO_24H.md, zones 0.94-0.969: about -$0.02 to -$0.05 per cycle net of
fees at base on 24 h of real books, 200+ markets/day). The objective is P(qualify), not EV:
- Entry: flat, target not reached, reference final and fresh. The favoured outcome (by BTC mid vs
  target) has its best ask in [min_ask, max_ask] and passes the +2c sizing-headroom rule. BUY
  min(cash, $5) fee-inclusive.
- Exit: SELL once secondsToClose <= exit_s2c (hold to the deadline), or at once if the position
  belongs to another market. Optional stop: SELL when the best bid falls below stop_bid.
- Stateless: entries need secondsToClose > exit_s2c + 2, so after the deadline exit the same market
  cannot be re-entered (one cycle per market without per-market memory).
Exits never depend on the reference block; only entries do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.guards import _finite, buy_cap, final_check, hold, levels, reference_ok, sizing_ok, target_reached


@dataclass(frozen=True)
class Params:
    min_ask: float = 0.94
    max_ask: float = 0.969          # 0.97+ fails the +2c headroom rule (CONTRACT §11)
    max_entry_s2c: float = 300.0    # enter at any time in the window
    exit_s2c: float = 50.0          # SELL from 50 s before close (exits at 18-35 s got stuck on day 1)
    stop_bid: float = 0.80          # SELL at once if the best bid falls below this


DEFAULT = Params()


def _best(book: dict[str, Any], side: str) -> float | None:
    lv = levels(book.get(side), reverse=(side == "bids"))
    return lv[0][0] if lv else None


def propose(obs: dict[str, Any], p: Params = DEFAULT) -> dict[str, Any]:
    market = obs.get("market") or {}
    s2c = _finite(market.get("secondsToClose"))
    books = obs.get("books") or {}
    position = (obs.get("account") or {}).get("position")

    if position is not None:
        if position.get("market_id") != market.get("id") or s2c is None or s2c <= p.exit_s2c:
            return {"action": "SELL"}
        bid = _best(books.get(position.get("outcome")) or {}, "bids")
        if p.stop_bid > 0 and bid is not None and bid < p.stop_bid:
            return {"action": "SELL"}
        return hold()

    if target_reached(obs) or s2c is None or not reference_ok(obs):
        return hold()
    if not (p.exit_s2c + 2.0 < s2c <= p.max_entry_s2c):
        return hold()
    ref = obs["reference"]
    outcome = "YES" if float(ref["btcMidUsd"]) >= float(ref["openingTargetUsd"]) else "NO"
    book = books.get(outcome) or {}
    ask = _best(book, "asks")
    if ask is None or not (p.min_ask - 1e-9 <= ask <= p.max_ask + 1e-9):
        return hold()
    amount = buy_cap(obs)
    min_size = _finite(book.get("minOrderSize"))
    if amount <= 0 or min_size is None or not sizing_ok(levels(book.get("asks"), reverse=False), amount, min_size):
        return hold()
    return {"action": "BUY", "outcome": outcome, "maxCashUsd": amount}


def decide(obs: Any, p: Params = DEFAULT) -> dict[str, Any]:
    if not isinstance(obs, dict):
        return hold()
    try:
        return final_check(propose(obs, p), obs)
    except Exception:  # never let a strategy bug surface as an HTTP error or an illegal order
        return hold()
