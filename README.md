# Theta v1-E: YeNo × Builderr volume bot

Paper-trading `POST /decide` bot for the YeNo BTC five-minute Up/Down challenge.
Python 3.12, standard library only. No network, wall clock, randomness or I/O inside the decision:
`bot.strategy.decide(observation)` depends only on the request.

## Run

```bash
python -m bot.server                 # POST http://127.0.0.1:8080/decide
YENO_HOST=0.0.0.0 YENO_PORT=8080 python -m bot.server
```

Container (non-root, base image pinned by digest, copies `bot/` only):

```bash
docker build -t theta-v1-e .
docker run --rm -p 8080:8080 theta-v1-e
```

## Strategy (v1-E = v1-D + a sell-side depth gate)

- Entry: buy the favourite (BTC reference mid vs opening target) when its best ask is in [0.94, 0.969],
  between 300 s and 52 s before close, sized min(cash, $5) fee-inclusive, only if the BUY would still get
  at least `minOrderSize` shares after a +2 c adverse move, and only if the favourite's best bid holds at
  least as many shares as the BUY would get (so the exit has a buyer).
- Exit: SELL once 50 s or less remain, or at once if the best bid falls below 0.80, or if the position belongs to
  another market.
- No entry on missing, provisional, stale or future reference data (exits still run). HOLD forever after $1,000.
- Every response passes a last-line rule check (`bot/guards.py`): $5 fee-inclusive cap, cash cap, one position,
  no BUYs in the final 15 s, valid response shape.

Fees mirror the evaluator contract: `0.07 × shares × p × (1 − p)` plus the 1 % overlay on each leg (`bot/fees.py`).
