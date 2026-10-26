"""Strategy entry point: decide(observation) -> response.

v1 "high-price theta" (docs/MICRO_24H.md, zones 0.94-0.969: about -$0.02 to -$0.05 per cycle net of
fees at base on 24 h of real books, 200+ markets/day). The objective is P(qualify), not EV:
- Entry: flat, target not reached, reference final and fresh. The favoured outcome (by BTC mid vs
  target) has its best ask in [min_ask, max_ask] and passes the +2c sizing-headroom rule. BUY
  min(cash, $5) fee-inclusive.
- Exit: SELL once secondsToClose <= exit_s2c (hold to the deadline), or at once if the position
  belongs to another market. Optional stop: SELL when the best bid falls below stop_bid.
- v1-E sell-side depth gate (min_bid_depth): the favourite's best bid must hold at least as many shares as
  we would buy.
v2 "edge" additions (analysis/roll.py stages B-C, owner lifted the stateless rule 2026-10-26):
- Optional take profit: SELL once the held outcome's best bid >= tp_bid; re-entry in the same market is allowed.
- Optional vol-model edge (edge_min > -9): enter only if Phi(gap / (sigma_1s * sqrt(secondsToClose))) - ask
  >= edge_min, sigma_1s = stdev of 1 s BTC mid changes over the last 300 s. That history lives in a Memory object
  (RAM only, keyed by the request timestamp, never the wall clock). With < 60 s of history (fresh start or after a
  restart) the bot does not enter. Exits never depend on memory or the reference block; only entries do.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

from bot.guards import _finite, buy_cap, final_check, hold, levels, reference_ok, sizing_ok, target_reached

SIGMA_LOOKBACK_S = 300   # same as analysis/fastsim._sigma
SIGMA_MIN_POINTS = 60
TIME_REWIND_S = 5.0      # a request this far in the past = a new run: forget the history


@dataclass(frozen=True)
class Params:
    min_ask: float = 0.94
    max_ask: float = 0.969          # 0.97+ fails the +2c headroom rule (CONTRACT §11)
    max_entry_s2c: float = 300.0    # enter at any time in the window
    exit_s2c: float = 50.0          # SELL from 50 s before close (exits at 18-35 s got stuck on day 1)
    stop_bid: float = 0.80          # SELL at once if the best bid falls below this
    min_bid_depth: float = 0.0      # > 0: enter only if the favourite's best bid holds >= this x the shares we would buy
    tp_bid: float = 2.0             # take profit once the held outcome's best bid >= tp_bid (2 = off)
    edge_min: float = -9.0          # > -9: vol-model edge gate (needs Memory history)
    edge_twap: bool = False         # edge prices the 60 s settlement TWAP (seen part fixed) instead of spot at close


V1E = Params(min_bid_depth=1.0)     # v1-E = v1-D + a 1x sell-side depth gate (Builderr hint, 2026-10-24)
# v2-E = stage-C finalist d299dc9596: zone 0.94-0.969, entry <= 300 s, exit 20 s, no stop, take profit 0.99, edge 0.02
V2E = Params(exit_s2c=20.0, stop_bid=0.0, tp_bid=0.99, edge_min=0.02)
# v2-T = stage-D 0f9f721116: v2-E with the edge priced on the settlement TWAP and edge_min 0.03
V2T = Params(exit_s2c=20.0, stop_bid=0.0, tp_bid=0.99, edge_min=0.03, edge_twap=True)
DEFAULT = V2T


class Memory:
    """BTC mid history on a 1 s grid (second of the reference's observedAt -> last mid seen). RAM only."""

    def __init__(self) -> None:
        self.mid: dict[int, float] = {}
        self.last_ts: float | None = None
        self.lock = threading.Lock()

    def observe(self, obs: dict[str, Any]) -> None:
        now = _finite(obs.get("timestamp"))
        if now is None:
            return
        if self.last_ts is not None and now < self.last_ts - TIME_REWIND_S:
            self.mid.clear()
            self.last_ts = None
        self.last_ts = now if self.last_ts is None else max(self.last_ts, now)
        if not reference_ok(obs):
            return
        ref = obs["reference"]
        sec = int(math.floor(float(ref["observedAt"])))
        self.mid[sec] = float(ref["btcMidUsd"])
        if len(self.mid) > 3 * SIGMA_LOOKBACK_S:
            old = sec - 2 * SIGMA_LOOKBACK_S
            for s in [s for s in self.mid if s < old]:
                del self.mid[s]

    def sigma(self, now: float) -> float | None:
        """stdev of consecutive 1 s mid changes over [now - 300 s, now] (missing seconds skipped, as fastsim)."""
        end = int(math.floor(now))
        vals = [self.mid[s] for s in range(end - SIGMA_LOOKBACK_S, end + 1) if s in self.mid]
        if len(vals) < SIGMA_MIN_POINTS:
            return None
        d = [b - a for a, b in zip(vals, vals[1:])]
        mu = sum(d) / len(d)
        sd = math.sqrt(max(sum((x - mu) ** 2 for x in d) / len(d), 0.0))
        return sd if sd > 0 else None

    def seen(self, now: float, close: float) -> list[float]:
        """Mids already inside the 60 s settlement window [close - 60, now] (1 s grid, missing seconds skipped)."""
        return [self.mid[s] for s in range(int(round(close)) - 60, int(math.floor(now)) + 1) if s in self.mid]


_MEMORY = Memory()                  # the server's process-wide history


def _best(book: dict[str, Any], side: str) -> float | None:
    lv = levels(book.get(side), reverse=(side == "bids"))
    return lv[0][0] if lv else None


def model_edge(mid: float, target: float, ask: float, sigma: float, s2c: float,
               seen: list[float] | None = None) -> float:
    """Brownian win probability of the favourite minus its price.

    seen=None: Phi(|mid - target| / (sigma_1s * sqrt(s2c))). Otherwise the settlement is the 60 s TWAP: inside the last
    minute the seen mids are fixed and the rest is projected at mid (var sigma^2 s2c^3 / 10800); before it the TWAP's
    variance is sigma^2 (s2c - 40). Same maths as analysis/fastsim._model_p.
    """
    if seen is None:
        gap = abs(mid - target)
        return 0.5 * (1.0 + math.erf(gap / (sigma * math.sqrt(s2c)) / math.sqrt(2.0))) - ask
    if s2c < 60.0:
        proj = (sum(seen) + mid * s2c) / (len(seen) + s2c) if seen else mid
        sd = sigma * math.sqrt(s2c ** 3 / 10800.0)
    else:
        proj, sd = mid, sigma * math.sqrt(s2c - 40.0)
    signed = (proj - target) if mid >= target else (target - proj)
    return 0.5 * (1.0 + math.erf(signed / sd / math.sqrt(2.0))) - ask


def propose(obs: dict[str, Any], p: Params = DEFAULT, mem: Memory | None = None) -> dict[str, Any]:
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
        if bid is not None and bid >= p.tp_bid - 1e-9:
            return {"action": "SELL"}
        return hold()

    if target_reached(obs) or s2c is None or not reference_ok(obs):
        return hold()
    if not (p.exit_s2c + 2.0 < s2c <= p.max_entry_s2c):
        return hold()
    ref = obs["reference"]
    mid, target = float(ref["btcMidUsd"]), float(ref["openingTargetUsd"])
    outcome = "YES" if mid >= target else "NO"
    book = books.get(outcome) or {}
    ask = _best(book, "asks")
    if ask is None or not (p.min_ask - 1e-9 <= ask <= p.max_ask + 1e-9):
        return hold()
    amount = buy_cap(obs)
    min_size = _finite(book.get("minOrderSize"))
    if amount <= 0 or min_size is None or not sizing_ok(levels(book.get("asks"), reverse=False), amount, min_size):
        return hold()
    if p.min_bid_depth > 0:
        bids = levels(book.get("bids"), reverse=True)
        if not bids or bids[0][1] < p.min_bid_depth * amount / ask - 1e-9:
            return hold()
    if p.edge_min > -9.0:
        now = float(obs["timestamp"])
        sg = mem.sigma(now) if mem is not None else None
        seen = mem.seen(now, now + s2c) if p.edge_twap and sg is not None else None
        if sg is None or model_edge(mid, target, ask, sg, s2c, seen) < p.edge_min - 1e-12:
            return hold()
    return {"action": "BUY", "outcome": outcome, "maxCashUsd": amount}


def decide(obs: Any, p: Params = DEFAULT, mem: Memory | None = None) -> dict[str, Any]:
    """mem=None uses the process-wide history; pass a fresh Memory() per independent run (replay, tests)."""
    if not isinstance(obs, dict):
        return hold()
    m = _MEMORY if mem is None else mem
    try:
        with m.lock:
            m.observe(obs)
            return final_check(propose(obs, p, m), obs)
    except Exception:  # never let a strategy bug surface as an HTTP error or an illegal order
        return hold()
