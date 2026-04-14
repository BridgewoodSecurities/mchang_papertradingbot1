from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.models import ExecutionConfig, RiskConfig
from tradingagents.universe.sp500 import load_sp500_symbols


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_float(value: str | None, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _parse_int(value: str | None, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    return items or None


def _parse_csv_preserve_case(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _load_watchlist(env: dict[str, str], *, project_dir: str) -> list[str]:
    watchlist_file = env.get("WATCHLIST_FILE")
    watchlist_preset = (env.get("WATCHLIST_PRESET") or "sp500").strip().lower()
    symbols: list[str] = []

    if watchlist_file:
        candidate = Path(watchlist_file)
        if not candidate.is_absolute():
            candidate = Path(project_dir) / candidate
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            raw_items: list[str] = []
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                raw_items.extend(part.strip() for part in stripped.split(","))
            symbols = [item.upper() for item in raw_items if item]

    if not symbols:
        env_symbols = _parse_csv(env.get("WATCHLIST"))
        if env_symbols:
            symbols = env_symbols

    if not symbols and watchlist_preset == "sp500":
        symbols = load_sp500_symbols(
            cache_path=Path(project_dir) / "runtime" / "sp500_constituents.json",
            refresh=True,
        )

    normalized = []
    seen = set()
    for symbol in (symbols or ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]):
        if symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


def load_execution_config(
    env: dict[str, str] | None = None,
    *,
    project_dir: str | None = None,
    execute: bool = False,
    llm_overrides: dict[str, Any] | None = None,
) -> ExecutionConfig:
    env = env or os.environ
    root_dir = project_dir or os.getcwd()

    runtime_dir = Path(root_dir) / "runtime"
    results_dir = env.get("TRADINGAGENTS_RESULTS_DIR", "./results")
    db_path = env.get("EXECUTION_DB_PATH", "./runtime/tradingagents.db")

    config = ExecutionConfig(
        project_dir=root_dir,
        results_dir=results_dir,
        db_path=db_path,
        log_dir=env.get("TRADINGAGENTS_LOG_DIR", str(runtime_dir / "logs")),
        audit_dir=env.get("TRADINGAGENTS_AUDIT_DIR", str(runtime_dir / "audit")),
        log_level=env.get("LOG_LEVEL", "INFO"),
        paper_trading_enabled=_parse_bool(env.get("PAPER_TRADING_ENABLED"), False),
        execute=execute,
        default_order_notional_usd=_parse_float(
            env.get("DEFAULT_ORDER_NOTIONAL_USD"), 1000.0
        ),
        time_in_force=env.get("ORDER_TIME_IN_FORCE", "day"),
        simulated_starting_equity=_parse_float(
            env.get("SIMULATED_STARTING_EQUITY"), 100000.0
        ),
        underweight_sell_fraction=_parse_float(
            env.get("UNDERWEIGHT_SELL_FRACTION"), 0.5
        ),
        sell_entire_position_on_sell=_parse_bool(
            env.get("SELL_ENTIRE_POSITION_ON_SELL"), True
        ),
        one_order_per_symbol_per_cycle=_parse_bool(
            env.get("ONE_ORDER_PER_SYMBOL_PER_CYCLE"), True
        ),
        slippage_bps=_parse_float(env.get("REPLAY_SLIPPAGE_BPS"), 5.0),
        commission_per_order=_parse_float(env.get("REPLAY_COMMISSION_PER_ORDER"), 0.0),
        replay_fill_assumption=env.get("REPLAY_FILL_ASSUMPTION", "next_open"),
        market_timezone=env.get("MARKET_TIMEZONE", "America/New_York"),
        market_open_time=env.get("MARKET_OPEN_TIME", "09:30"),
        market_close_time=env.get("MARKET_CLOSE_TIME", "16:00"),
        market_holidays=_parse_csv_preserve_case(env.get("MARKET_HOLIDAYS")) or [],
        watchlist=_load_watchlist(env, project_dir=root_dir),
        max_symbols_per_cycle=_parse_int(env.get("MAX_SYMBOLS_PER_CYCLE"), 5),
        scheduler_interval_minutes=_parse_int(
            env.get("SCHEDULER_INTERVAL_MINUTES"), 15
        ),
        daemon_poll_seconds=_parse_int(env.get("DAEMON_POLL_SECONDS"), 15),
        market_open_sleep_seconds=_parse_int(
            env.get("MARKET_OPEN_SLEEP_SECONDS") or env.get("DAEMON_POLL_SECONDS"),
            15,
        ),
        market_closed_sleep_seconds=_parse_int(
            env.get("MARKET_CLOSED_SLEEP_SECONDS"),
            300,
        ),
        daemon_pid_path=env.get("DAEMON_PID_PATH", "./runtime/daemon.pid"),
        daemon_lock_path=env.get("DAEMON_LOCK_PATH", "./runtime/daemon.lock"),
        daemon_heartbeat_path=env.get(
            "DAEMON_HEARTBEAT_PATH", "./runtime/daemon-heartbeat.json"
        ),
        kill_switch_path=env.get("KILL_SWITCH_PATH", "./runtime/KILL_SWITCH"),
        default_position_size_pct=_parse_float(
            env.get("DEFAULT_POSITION_SIZE_PCT"), 0.02
        ),
        max_order_notional_usd=_parse_float(
            env.get("MAX_ORDER_NOTIONAL_USD"), 1000.0
        ),
        min_order_notional_usd=_parse_float(
            env.get("MIN_ORDER_NOTIONAL_USD"), 100.0
        ),
        allow_fractional_shares=_parse_bool(
            env.get("ALLOW_FRACTIONAL_SHARES"), False
        ),
        daily_summary_dir=env.get("DAILY_SUMMARY_DIR", "./results/daily"),
        agent_id=env.get("AGENT_ID", "primary"),
        arena_enabled=_parse_bool(env.get("ARENA_ENABLED"), False),
        arena_model=env.get("ARENA_MODEL"),
        agent_memory_limit=_parse_int(env.get("AGENT_MEMORY_LIMIT"), 10),
        max_trades_per_cycle=_parse_int(env.get("MAX_TRADES_PER_CYCLE"), 0),
        conservative_spread_bps=_parse_float(env.get("CONSERVATIVE_SPREAD_BPS"), 10.0),
        symbol_analysis_timeout_seconds=_parse_int(
            env.get("SYMBOL_ANALYSIS_TIMEOUT_SECONDS"),
            180,
        ),
        cycle_context_timeout_seconds=_parse_int(
            env.get("CYCLE_CONTEXT_TIMEOUT_SECONDS"),
            45,
        ),
    )
    config.llm_config_overrides = _build_llm_overrides(env, cli_overrides=llm_overrides)
    return config


def load_risk_config(env: dict[str, str] | None = None) -> RiskConfig:
    env = env or os.environ
    allow_fractional = _parse_bool(env.get("ALLOW_FRACTIONAL_SHARES"), False)
    return RiskConfig(
        paper_only=_parse_bool(env.get("PAPER_ONLY"), True),
        allowed_symbols=_parse_csv(env.get("ALLOWED_SYMBOLS")),
        max_position_notional_pct_per_symbol=_parse_float(
            env.get("MAX_POSITION_NOTIONAL_PCT_PER_SYMBOL")
            or env.get("MAX_POSITION_NOTIONAL_PCT"),
            0.20,
        ),
        max_total_gross_exposure_pct=_parse_float(
            env.get("MAX_TOTAL_GROSS_EXPOSURE_PCT")
            or env.get("MAX_TOTAL_EXPOSURE_PCT"),
            0.50,
        ),
        max_new_positions_per_day=_parse_int(env.get("MAX_NEW_POSITIONS_PER_DAY"), 3),
        max_open_positions=_parse_int(env.get("MAX_OPEN_POSITIONS"), 5),
        max_daily_loss_pct=_parse_float(env.get("MAX_DAILY_LOSS_PCT"), 0.03),
        min_confidence_threshold=_parse_float(
            env.get("MIN_CONFIDENCE_THRESHOLD"), 0.65
        ),
        reject_short_selling=_parse_bool(env.get("REJECT_SHORT_SELLING"), True),
        reject_fractional_shares=_parse_bool(
            env.get("REJECT_FRACTIONAL_SHARES"),
            not allow_fractional,
        ),
        market_hours_only=_parse_bool(env.get("MARKET_HOURS_ONLY"), True),
        cooldown_minutes_per_symbol=_parse_int(
            env.get("COOLDOWN_MINUTES_PER_SYMBOL")
            or env.get("SYMBOL_COOLDOWN_MINUTES"),
            60,
        ),
        max_order_notional_usd=_parse_float(
            env.get("MAX_ORDER_NOTIONAL_USD"), 1000.0
        ),
        max_daily_trades=_parse_int(env.get("MAX_DAILY_TRADES"), 0),
        max_daily_trades_per_symbol=_parse_int(
            env.get("MAX_DAILY_TRADES_PER_SYMBOL"),
            0,
        ),
        allow_position_scaling=_parse_bool(
            env.get("ALLOW_POSITION_SCALING")
            if env.get("ALLOW_POSITION_SCALING") is not None
            else env.get("ALLOW_SCALING_IN"),
            False,
        ),
        max_trades_per_cycle=_parse_int(env.get("MAX_TRADES_PER_CYCLE"), 0),
        default_to_hold=_parse_bool(env.get("DEFAULT_TO_HOLD"), True),
        require_multiple_signals=_parse_bool(
            env.get("REQUIRE_MULTIPLE_SIGNALS"),
            True,
        ),
        min_signals_required=_parse_int(env.get("MIN_SIGNALS_REQUIRED"), 2),
        require_expected_edge=_parse_bool(env.get("REQUIRE_EXPECTED_EDGE"), True),
        require_market_mispricing_reason=_parse_bool(
            env.get("REQUIRE_MARKET_MISPRICING_REASON"), True
        ),
        require_new_information_check=_parse_bool(
            env.get("REQUIRE_NEW_INFORMATION_CHECK"), True
        ),
        require_position_sizing_rationale=_parse_bool(
            env.get("REQUIRE_POSITION_SIZING_RATIONALE"), True
        ),
        reject_if_contradicts_recent_failures=_parse_bool(
            env.get("REJECT_IF_CONTRADICTS_RECENT_FAILURES"), True
        ),
        trade_frequency_penalty_enabled=_parse_bool(
            env.get("TRADE_FREQUENCY_PENALTY_ENABLED"),
            True,
        ),
        recent_trade_lookback_minutes=_parse_int(
            env.get("RECENT_TRADE_LOOKBACK_MINUTES"),
            120,
        ),
        extra_confidence_threshold_after_recent_trade=_parse_float(
            env.get("EXTRA_CONFIDENCE_THRESHOLD_AFTER_RECENT_TRADE"),
            0.05,
        ),
        block_reentry_while_position_open=_parse_bool(
            env.get("BLOCK_REENTRY_WHILE_POSITION_OPEN"),
            True,
        ),
        allow_scaling_in=_parse_bool(env.get("ALLOW_SCALING_IN"), False),
        allow_reversal=_parse_bool(env.get("ALLOW_REVERSAL"), False),
        position_reentry_cooldown_hours=_parse_int(
            env.get("POSITION_REENTRY_COOLDOWN_HOURS"),
            4,
        ),
        reversal_cooldown_hours=_parse_int(
            env.get("REVERSAL_COOLDOWN_HOURS"),
            8,
        ),
        max_flip_flops_per_symbol_per_day=_parse_int(
            env.get("MAX_FLIP_FLOPS_PER_SYMBOL_PER_DAY"),
            1,
        ),
    )


def build_analysis_config(
    execution_config: ExecutionConfig,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    config["results_dir"] = execution_config.results_dir
    config["data_vendors"] = {
        "core_stock_apis": os.getenv("TRADINGAGENTS_CORE_STOCK_VENDOR", "alpaca"),
        "technical_indicators": os.getenv("TRADINGAGENTS_TECHNICAL_VENDOR", "alpaca"),
        "fundamental_data": os.getenv("TRADINGAGENTS_FUNDAMENTAL_VENDOR", "alpaca"),
        "news_data": os.getenv("TRADINGAGENTS_NEWS_VENDOR", "alpaca"),
    }
    config.update(execution_config.llm_config_overrides)
    if overrides:
        config.update(overrides)
    return config


def _build_llm_overrides(
    env: dict[str, str],
    *,
    cli_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    env_map = {
        "TRADINGAGENTS_LLM_PROVIDER": "llm_provider",
        "TRADINGAGENTS_DEEP_THINK_LLM": "deep_think_llm",
        "TRADINGAGENTS_QUICK_THINK_LLM": "quick_think_llm",
        "TRADINGAGENTS_BACKEND_URL": "backend_url",
        "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
        "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "google_thinking_level",
        "TRADINGAGENTS_ANTHROPIC_EFFORT": "anthropic_effort",
    }
    for env_key, config_key in env_map.items():
        value = env.get(env_key)
        if value not in (None, ""):
            overrides[config_key] = value

    if cli_overrides:
        overrides.update({key: value for key, value in cli_overrides.items() if value not in (None, "")})

    return overrides
