# Automated Paper Trading Daemon

## Warning

This daemon is paper trading only.
It does not support live trading, margin, shorting, options, or crypto.

## What It Does

The daemon wraps the existing `TradingAgentsGraph` workflow with a durable 15-minute loop that:

1. aligns to market buckets
2. loads the configured watchlist
3. fetches and deduplicates fresh news/context
4. runs TradingAgents analysis
5. reviews portfolio, recent PnL, recent wins/losses, and prior reasoning
6. runs a strict final agent decision prompt that must output BUY, SELL, or HOLD with edge, supporting signals, risks, and sizing rationale
7. applies conservative risk checks
8. writes a reflection step and updates persistent learning memory
9. submits Alpaca paper orders when allowed
10. writes heartbeats, logs, sqlite state, and summaries
11. survives restarts without reprocessing completed symbol buckets

## Agent Cycle

Each 15-minute cycle now follows a fixed protocol:

1. `RECEIVE`: timestamp, fresh market/news context, account state, positions, recent PnL
2. `REVIEW`: open positions, recent closed trades, recent winning/losing trades, prior reasoning
3. `ANALYZE`: run the TradingAgents research graph
4. `DECIDE`: produce structured `BUY` / `SELL` / `HOLD` output with confidence, expected edge, supporting signals, risks, and sizing rationale
5. `EXECUTE`: pass the structured intent through the risk engine and broker layer
6. `REFLECT`: compare with the previous cycle and store one lesson in persistent memory

## Commands

```bash
tradingagents daemon run
tradingagents daemon start
tradingagents daemon status
tradingagents daemon memory
tradingagents daemon heartbeat
tradingagents daemon pause
tradingagents daemon resume
tradingagents daemon stop
tradingagents dashboard run
```

## Core Environment Variables

```bash
PAPER_TRADING_ENABLED=true
AGENT_ID=primary
ARENA_ENABLED=true
AGENT_MEMORY_LIMIT=10
WATCHLIST=SPY,QQQ,AAPL,MSFT,NVDA
MAX_SYMBOLS_PER_CYCLE=5
SCHEDULER_INTERVAL_MINUTES=15
MAX_TRADES_PER_CYCLE=2
MARKET_OPEN_SLEEP_SECONDS=15
MARKET_CLOSED_SLEEP_SECONDS=300
KILL_SWITCH_PATH=./runtime/KILL_SWITCH
CONSERVATIVE_SPREAD_BPS=10
DEFAULT_POSITION_SIZE_PCT=0.02
MIN_ORDER_NOTIONAL_USD=100
MAX_ORDER_NOTIONAL_USD=1000
MIN_CONFIDENCE_THRESHOLD=0.65
DEFAULT_TO_HOLD=true
REQUIRE_MULTIPLE_SIGNALS=true
MIN_SIGNALS_REQUIRED=2
MAX_POSITION_NOTIONAL_PCT=0.20
MAX_TOTAL_EXPOSURE_PCT=0.50
MAX_DAILY_TRADES=0
MAX_DAILY_TRADES_PER_SYMBOL=0
MAX_OPEN_POSITIONS=5
SYMBOL_COOLDOWN_MINUTES=60
TRADE_FREQUENCY_PENALTY_ENABLED=true
RECENT_TRADE_LOOKBACK_MINUTES=120
EXTRA_CONFIDENCE_THRESHOLD_AFTER_RECENT_TRADE=0.1
BLOCK_REENTRY_WHILE_POSITION_OPEN=true
ALLOW_SCALING_IN=false
ALLOW_REVERSAL=false
POSITION_REENTRY_COOLDOWN_HOURS=4
REVERSAL_COOLDOWN_HOURS=8
ALLOW_POSITION_SCALING=false
REJECT_FRACTIONAL_SHARES=true
MARKET_HOURS_ONLY=true
REQUIRE_EXPECTED_EDGE=true
REQUIRE_MARKET_MISPRICING_REASON=true
REQUIRE_NEW_INFORMATION_CHECK=true
REQUIRE_POSITION_SIZING_RATIONALE=true
REJECT_IF_CONTRADICTS_RECENT_FAILURES=true
```

Daemon runtime paths:

```bash
DAEMON_PID_PATH=./runtime/daemon.pid
DAEMON_LOCK_PATH=./runtime/daemon.lock
DAEMON_HEARTBEAT_PATH=./runtime/daemon-heartbeat.json
DAILY_SUMMARY_DIR=./results/daily
```

## Market Hours Behavior

The daemon now behaves differently depending on the US regular session:

- during market hours (`09:30` to `16:00` America/New_York), it runs normal 15-minute trading cycles and keeps the short open-session sleep interval
- outside market hours, it does not run trading cycles, stays alive quietly, and sleeps for the longer closed-session interval

Recommended defaults:

```bash
MARKET_OPEN_SLEEP_SECONDS=15
MARKET_CLOSED_SLEEP_SECONDS=300
```

Outside hours, the daemon throttles the `market closed — sleeping` log so it does not spam the log file while waiting for the next session.

## Fresh News Deduplication

Each cycle fetches symbol news and market-wide news via `yfinance`.
Every item is hashed and persisted in sqlite so repeated headlines are tracked instead of being treated as new on every 15-minute wake-up.

## Memory And Learning

The daemon now keeps a capped, persistent agent memory in sqlite:

- last 10 decisions
- last 10 closed trades
- last 10 losing trades
- last 10 winning trades
- recurring mistakes
- recurring successful patterns
- a rolling learning summary

Inspect it with:

```bash
tradingagents daemon memory
```

The latest learning summary is also shown in `tradingagents daemon status`.

## HOLD Bias And Multi-Signal Confirmation

The daemon now has a strong do-nothing bias:

- `HOLD` is the preferred action when evidence is mixed
- low-confidence decisions are forced to `HOLD` or rejected
- every `BUY` / `SELL` must include a clear expected edge, why-the-market-is-wrong thesis, non-generic reasoning, and explicit risks
- multiple independent supporting signals are required before a trade is even eligible

By default the system requires at least two supporting signals, such as:

- fresh news catalyst
- price/trend confirmation
- technical confirmation
- portfolio/risk alignment
- reflection/memory support
- strong edge explanation

If the signal count is below `MIN_SIGNALS_REQUIRED`, the system defaults to `HOLD`.

## Trade Frequency Limits

The daemon now emphasizes prudence and trade quality over arbitrary quotas:

- `MAX_DAILY_TRADES=0` disables the global hard daily trade cap
- `MAX_DAILY_TRADES_PER_SYMBOL=0` disables the per-symbol hard daily trade cap
- a recent-trade penalty that raises the effective confidence threshold after recent activity
- multi-signal confirmation, confidence thresholds, edge-quality checks, cooldowns, open-position protection, and anti-flip-flop logic still remain in force

`tradingagents daemon status` shows:

- trades today
- trades per symbol today
- whether a hard daily trade cap is active or disabled

The arena prompt also sees recent trade counts so it can bias toward patience, prudence, and higher-confidence decisions even without a hard trade quota.

## Existing Position And Anti-Churn Logic

To reduce flip-flopping and churn:

- open positions block re-entry unless scaling is explicitly enabled
- recent exits trigger a re-entry cooldown
- recent opposite-direction trades trigger a reversal cooldown
- repeated `BUY -> SELL -> BUY` style churn is detected and rejected

The agent prompt receives:

- whether the symbol already has an open position
- entry price
- current unrealized PnL
- time since entry when available
- last trade details
- whether re-entry or reversal cooldowns are active

This pushes the agent to wait for materially new information instead of reacting every 15 minutes.

## Restart Recovery

The daemon persists:

- processed symbol/time buckets
- cycle start/end records
- heartbeat state
- pause/stop flags
- last error

If the daemon restarts, completed symbol buckets in the current time window are skipped.

## Kill Switch

To halt submissions immediately:

```bash
touch runtime/KILL_SWITCH
```

To restore submission:

```bash
rm runtime/KILL_SWITCH
```

When the kill switch exists, analysis/logging continue but order submission is disabled.

## Monitoring

Useful paths:

- sqlite DB: `./runtime/tradingagents.db`
- heartbeat: `./runtime/daemon-heartbeat.json`
- rotating logs: `./runtime/logs/`
- run results: `./results/`
- daily summaries: `./results/daily/`

`tradingagents daemon status` shows running state, pid, last heartbeat, last cycle timing, last processed symbols, last error, broker account snapshots when available, trade counts, daily-cap state, the latest learning summary, and the latest performance snapshot.

For a browser-based monitor, run:

```bash
tradingagents dashboard run
```

See [dashboard.md](./dashboard.md) for the live web dashboard that exposes heartbeats, memory, positions, orders, decisions, reflections, cycles, news, errors, and runtime log tails.

## Performance Tracking

Each cycle records:

- conservative account value
- realized PnL
- unrealized PnL
- total PnL
- win rate
- average win / loss
- max drawdown
- trade frequency

Mark-to-market account value is calculated conservatively as cash plus the liquidation value of positions, using a bid price when available or a last-price discount based on `CONSERVATIVE_SPREAD_BPS`.

## Long-Run Operations

Recommended wrappers:

- `tmux`
- `systemd`
- Docker with a restart policy

Example:

```bash
tmux new -s tradingagents-daemon
source .venv/bin/activate
tradingagents daemon run
```

## Limitations

- Market calendar logic supports weekdays and configurable holiday dates, not a full exchange calendar.
- Fresh news is persisted and deduplicated, but the TradingAgents graph still performs its own internal research rather than consuming the cache as a first-class graph input.
- Daily summary winner/loser reporting is currently a simplified placeholder.
- The reflection and learning-summary logic is intentionally lightweight; it is a disciplined memory layer, not a full reinforcement-learning system.
- SQLite retention pruning is capped for the agent-memory tables, but broader historical retention is still simple and local.
- Supporting-signal detection is intentionally heuristic; it is designed to be conservative rather than exhaustive.
