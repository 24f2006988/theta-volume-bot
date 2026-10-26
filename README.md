# Theta v2-T: YeNo × Builderr volume bot

Paper-trading `POST /decide` bot for the YeNo BTC five-minute Up/Down challenge.
Python 3.12, standard library only. No network, wall clock, randomness or disk I/O inside the decision.
v2-T keeps state in RAM only: the BTC reference mid it has seen, one value per second of `reference.observedAt`,
for the last 10 minutes. Time comes only from the request `timestamp`. A request more than 5 s earlier than the
last one starts a new history.

## Run

```bash
python -m bot.server                 # POST http://127.0.0.1:8080/decide
YENO_HOST=0.0.0.0 YENO_PORT=8080 python -m bot.server
```

Container (non-root, base image pinned by digest, copies `bot/` only):

```bash
docker build -t theta-v2-t .
docker run --rm -p 8080:8080 theta-v2-t
```

## Strategy (v2-T)

- Entry: buy the favourite (BTC reference mid vs opening target) when its best ask is in [0.94, 0.969],
  between 300 s and 22 s before close. Size min(cash, $5) fee-inclusive, and only if the BUY would still get
  at least `minOrderSize` shares after a +2 c adverse move.
- Settlement-aware volatility edge gate. sigma is the standard deviation of 1 s BTC mid changes over the last 300 s
  (at least 60 points). The market settles on the 60 s average price before close, so the model prices that average.
  Inside the last minute, the mids already seen are fixed and the rest is projected at the current mid, with variance
  sigma² · s³ / 10800. Before the last minute, the variance is sigma² · (s − 40), where s = secondsToClose.
  Enter only if P(favourite wins) − ask ≥ 0.03. Without enough history (fresh start or restart) the bot doesn't enter.
- Exit: SELL once the held outcome's best bid is ≥ 0.99 (take profit; re-entry in the same market is allowed),
  once 20 s or less remain, or at once if the position belongs to another market. There is no price stop.
  Exits never depend on the remembered history or the reference block.
- No entry on missing, provisional, stale or future reference data. HOLD forever after $1,000.
- Every response passes a last-line rule check (`bot/guards.py`): $5 fee-inclusive cap, cash cap, one position,
  no BUYs in the final 15 s, valid response shape.

Fees mirror the evaluator contract: `0.07 × shares × p × (1 − p)` plus the 1 % overlay on each leg (`bot/fees.py`).
