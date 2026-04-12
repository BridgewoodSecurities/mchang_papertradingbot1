# Paper Trading Guide

## Warning

This workflow is for Alpaca paper trading only.
It does not support live trading, margin, short selling, options, or crypto.
Broker submission stays disabled unless both of these are true:
- the CLI command includes `--execute`
- `PAPER_TRADING_ENABLED=true` is set in the environment

For unattended 15-minute automation, see [daemon.md](./daemon.md).

## Architecture

The existing `TradingAgentsGraph` remains the research and signal-generation engine.
The new execution stack wraps that output with the following stages:

1. `TradingAgentsGraph` generates the raw portfolio-manager decision text.
2. `DecisionParser` converts the raw text into one or more structured `OrderIntent` objects.
3. `ExecutionPolicy` applies conservative sizing defaults when the decision is directionally clear but position size is omitted.
4. `RiskEngine` evaluates each intent against account state, exposure caps, market-hours checks, confidence thresholds, and symbol cooldowns.
5. `AlpacaPaperBroker` submits approved orders to the Alpaca paper endpoint only.
6. `SQLitePersistence` stores runs, raw decisions, parsed decisions, risk decisions, broker events, orders, fills, position snapshots, equity snapshots, and daily PnL summaries.
7. Structured logs and one audit JSONL file per run are written locally for review and replay.

## Installation

```bash
git clone <your-repo-url>
cd <your-repo-directory>
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

## Environment Variables

Required for analysis:

```bash
OPENAI_API_KEY=...
```

You can use another supported provider instead:

```bash
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...
XAI_API_KEY=...
OPENROUTER_API_KEY=...
```

Optional non-interactive CLI overrides:

```bash
TRADINGAGENTS_LLM_PROVIDER=openai
TRADINGAGENTS_DEEP_THINK_LLM=gpt-5.4
TRADINGAGENTS_QUICK_THINK_LLM=gpt-5.4-mini
TRADINGAGENTS_BACKEND_URL=https://api.openai.com/v1
TRADINGAGENTS_OPENAI_REASONING_EFFORT=medium
TRADINGAGENTS_GOOGLE_THINKING_LEVEL=high
TRADINGAGENTS_ANTHROPIC_EFFORT=medium
```

You can also pass these as CLI flags on `dry-run`, `paper-run`, and `replay`:

```bash
tradingagents dry-run \
  --symbols NVDA \
  --date 2026-04-10 \
  --llm-provider google \
  --deep-model gemini-2.5-pro \
  --quick-model gemini-2.5-flash
```

Required for Alpaca paper trading:

```bash
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
PAPER_TRADING_ENABLED=false
```

Runtime locations:

```bash
EXECUTION_DB_PATH=./runtime/tradingagents.db
TRADINGAGENTS_RESULTS_DIR=./results
LOG_LEVEL=INFO
DAILY_SUMMARY_DIR=./results/daily
```

Optional risk controls:

```bash
ALLOWED_SYMBOLS=NVDA,AAPL
MAX_POSITION_NOTIONAL_PCT_PER_SYMBOL=0.20
MAX_TOTAL_GROSS_EXPOSURE_PCT=0.50
MAX_NEW_POSITIONS_PER_DAY=3
MAX_OPEN_POSITIONS=5
MAX_DAILY_LOSS_PCT=0.03
MIN_CONFIDENCE_THRESHOLD=0.55
REJECT_SHORT_SELLING=true
REJECT_FRACTIONAL_SHARES=true
ALLOW_POSITION_SCALING=false
MARKET_HOURS_ONLY=true
SYMBOL_COOLDOWN_MINUTES=60
MAX_ORDER_NOTIONAL_USD=1000
```

## Commands

Dry-run analysis with no broker submission:

```bash
tradingagents dry-run --symbols NVDA,AAPL --date 2026-04-11
```

Paper-run with broker connectivity but still no submission:

```bash
tradingagents paper-run --symbols NVDA,AAPL --date 2026-04-11
```

Paper-run with actual Alpaca paper order submission:

```bash
PAPER_TRADING_ENABLED=true \
tradingagents paper-run --symbols NVDA,AAPL --date 2026-04-11 --execute
```

Inspect broker state:

```bash
tradingagents account
tradingagents positions
tradingagents orders
tradingagents pnl
```

Replay historical days with explicit assumptions:

```bash
tradingagents replay --symbols NVDA,AAPL --from 2026-03-01 --to 2026-03-31
```

## Logging, Results, and Database

Default runtime outputs:
- database: `./runtime/tradingagents.db`
- logs: `./runtime/logs/<run_id>.log`
- audit: `./runtime/audit/<run_id>.jsonl`
- result summaries: `./results/<run_id>.json`

Each run prints these locations at the end.

## Replay Assumptions

Replay mode is intentionally simple and transparent, not a full institutional backtester.

Current assumptions:
- fill at `next_open` by default, or `same_day_close` if configured
- static `slippage_bps`
- flat `commission_per_order`
- long-only US equities
- no intraday portfolio rebalancing
- no borrow, leverage, options, or crypto support
- market-hours checks are approximate and do not include exchange holiday calendars

## Known Limitations

- The parser is deterministic and conservative; ambiguous text is rejected or interpreted as `HOLD`.
- Sizing defaults are applied only when direction is clear; ambiguous size instructions remain blocked by risk checks.
- The market-hours filter uses regular US market hours and does not currently integrate a full exchange holiday calendar.
- Replay mode re-runs the LLM analysis across historical dates, which can be slow and token-intensive.
- Dry-run mode uses a simulated account when Alpaca credentials are absent.
