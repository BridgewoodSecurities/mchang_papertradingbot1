# TradingBot

Autonomous paper-trading system for US equities, built around the `TradingAgentsGraph` research workflow and extended with execution, daemon automation, persistent memory, risk controls, replay tools, and a live monitoring dashboard.

This repository is inspired by the original [TradingAgents](https://github.com/TauricResearch/TradingAgents) framework from Tauric Research, but it is documented and operated here as its own project: a stateful paper-trading stack designed for long-running simulation and monitoring.

<div align="center">

🚀 [Overview](#overview) | ⚡ [Installation & CLI](#installation-and-cli) | 🧠 [Research Engine](#research-engine) | 📈 [Paper Trading](#paper-trading-cli) | 🤖 [Daemon And Monitoring](#daemon-and-monitoring) | 📦 [Python Usage](#python-usage) | 🙌 [Acknowledgments](#acknowledgments)

</div>

## Overview

TradingBot uses the existing `tradingagents` package as the multi-agent analysis and debate engine, then layers on the missing operational pieces needed to run a safe paper-trading workflow:

- structured decision parsing
- conservative risk controls with a strong HOLD bias
- Alpaca paper broker execution
- sqlite-backed persistence
- 15-minute daemon scheduling
- persistent memory and reflection
- replay/backtest-lite mode
- a live web dashboard for status, heartbeat, memory, positions, orders, and logs

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> This repository is experimental software for paper trading and research workflows. It is not financial advice, it is not suitable for live capital, and safe no-op behavior is preferred over risky execution.

## Research Engine

The underlying analysis still follows the multi-agent structure from TradingAgents. Different agent roles debate the market from multiple angles before the final portfolio decision is parsed and passed into the execution stack.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Analyzes social media and public sentiment using sentiment scoring algorithms to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions. It determines the timing and magnitude of trades based on comprehensive market insights.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone your repo:
```bash
git clone <your-repo-url>
cd <your-repo-directory>
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp env.template .env  # add your API keys
docker compose run --rm tradingagents
```

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### Required APIs

This project supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For local models, configure Ollama with `llm_provider: "ollama"` in your config.

Alternatively, copy `env.template` to `.env` and fill in your keys:
```bash
cp env.template .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
The command name remains `tradingagents` for compatibility with the underlying package, but the repo itself is positioned here as `TradingBot`.

You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more.

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Paper Trading CLI

`TradingAgentsGraph` remains the research engine. This repository adds a safe paper-trading execution layer around it with:
- decision parsing into structured trade intents
- configurable risk guardrails
- Alpaca paper broker integration
- local sqlite persistence, logs, and audit trails
- replay/backtest-lite support

Important safety defaults:
- paper trading only
- no live Alpaca endpoint
- no margin, shorting, options, or crypto
- no broker submission unless `--execute` is passed and `PAPER_TRADING_ENABLED=true`

Create a root `.env` file from `env.template`, add your LLM provider key, and only add Alpaca paper keys when you are ready to connect paper execution.

Examples:

```bash
tradingagents dry-run --symbols NVDA,AAPL --date 2026-04-11
tradingagents paper-run --symbols NVDA,AAPL --date 2026-04-11
tradingagents paper-run --symbols NVDA,AAPL --date 2026-04-11 --execute
tradingagents account
tradingagents positions
tradingagents orders
tradingagents pnl
tradingagents replay --symbols NVDA,AAPL --from 2026-03-01 --to 2026-03-31
```

See [docs/paper_trading.md](docs/paper_trading.md) for setup, architecture, safety controls, replay assumptions, database paths, and paper-trading-only guidance.
See [docs/daemon.md](docs/daemon.md) for the unattended 15-minute paper-trading daemon, controls, and monitoring.
See [docs/dashboard.md](docs/dashboard.md) for the live monitoring web app.

## Daemon And Monitoring

For unattended operation:

```bash
tradingagents daemon run
```

For browser-based monitoring:

```bash
tradingagents dashboard run
```

The dashboard shows live daemon state, heartbeat updates, memory, positions, orders, reflections, cycles, news, and recent errors.

## Python Usage

If you want to use the underlying research graph directly, you can still import `TradingAgentsGraph` and work with it as a Python package:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # openai, google, anthropic, xai, openrouter, ollama
config["deep_think_llm"] = "gpt-5.4"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `tradingagents/default_config.py` for all configuration options.

## Contributing

Contributions that improve reliability, observability, safer execution, and paper-trading research workflows are all good fits for this repo.

## Acknowledgments

This project is inspired by the original TradingAgents paper and open-source framework:

```text
Xiao, Yijia and Sun, Edward and Luo, Di and Wang, Wei.
"TradingAgents: Multi-Agents LLM Financial Trading Framework."
arXiv:2412.20138
https://arxiv.org/abs/2412.20138
```

The underlying `tradingagents` package structure and research workflow come from that lineage, while this repository extends it into a stateful paper-trading and monitoring system.
