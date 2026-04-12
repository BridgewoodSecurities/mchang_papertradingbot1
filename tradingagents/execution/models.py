from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class RunMode(str, Enum):
    DRY_RUN = "dry-run"
    PAPER = "paper-run"
    REPLAY = "replay"
    DAEMON = "daemon"


class OrderIntent(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    symbol: str
    action: TradeAction
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None
    quantity: float | None = Field(default=None, gt=0.0)
    notional_usd: float | None = Field(default=None, gt=0.0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(default=None, gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    take_profit: float | None = Field(default=None, gt=0.0)
    time_horizon: str | None = None
    expected_edge: str | None = None
    supporting_signals: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    position_sizing_rationale: str | None = None
    why_market_wrong: str | None = None
    is_new_information: bool | None = None
    fits_success_patterns: bool | None = None
    contradicts_recent_failures: bool | None = None
    previous_reasoning_change: str | None = None
    source_raw_text: str
    source_rating: str | None = None
    warnings: list[str] = Field(default_factory=list)
    protocol_warnings: list[str] = Field(default_factory=list)
    execution_notes: list[str] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be empty")
        return value


class ParsedDecisionResult(BaseModel):
    raw_text: str
    intents: list[OrderIntent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rejected: bool = False


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    normalized_intent: OrderIntent | None = None
    checks: dict[str, Any] = Field(default_factory=dict)


class BrokerAccountSnapshot(BaseModel):
    account_id: str | None = None
    status: str | None = None
    currency: str = "USD"
    cash: float = 0.0
    equity: float = 0.0
    buying_power: float = 0.0
    portfolio_value: float | None = None
    daytrade_count: int | None = None
    paper: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class BrokerPosition(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float | None = None
    market_value: float | None = None
    cost_basis: float | None = None
    unrealized_pl: float | None = None
    side: str = "long"
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class BrokerOrder(BaseModel):
    id: str | None = None
    client_order_id: str | None = None
    symbol: str
    side: TradeAction
    order_type: OrderType = OrderType.MARKET
    status: str
    qty: float | None = Field(default=None, gt=0.0)
    notional_usd: float | None = Field(default=None, gt=0.0)
    limit_price: float | None = Field(default=None, gt=0.0)
    filled_qty: float | None = Field(default=None, ge=0.0)
    filled_avg_price: float | None = Field(default=None, gt=0.0)
    submitted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class ExecutionConfig(BaseModel):
    project_dir: str = "."
    results_dir: str = "./results"
    db_path: str = "./runtime/tradingagents.db"
    log_dir: str = "./runtime/logs"
    audit_dir: str = "./runtime/audit"
    log_level: str = "INFO"
    paper_trading_enabled: bool = False
    execute: bool = False
    default_order_notional_usd: float = 1000.0
    time_in_force: str = "day"
    order_type: OrderType = OrderType.MARKET
    simulated_starting_equity: float = 100000.0
    selected_analysts: list[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    llm_config_overrides: dict[str, Any] = Field(default_factory=dict)
    underweight_sell_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    sell_entire_position_on_sell: bool = True
    one_order_per_symbol_per_cycle: bool = True
    slippage_bps: float = Field(default=5.0, ge=0.0)
    commission_per_order: float = Field(default=0.0, ge=0.0)
    replay_fill_assumption: str = "next_open"
    market_timezone: str = "America/New_York"
    market_open_time: str = "09:30"
    market_close_time: str = "16:00"
    market_holidays: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(
        default_factory=lambda: ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
    )
    max_symbols_per_cycle: int = Field(default=5, ge=1)
    scheduler_interval_minutes: int = Field(default=15, ge=1)
    daemon_poll_seconds: int = Field(default=15, ge=1)
    market_open_sleep_seconds: int = Field(default=15, ge=1)
    market_closed_sleep_seconds: int = Field(default=300, ge=1)
    daemon_pid_path: str = "./runtime/daemon.pid"
    daemon_lock_path: str = "./runtime/daemon.lock"
    daemon_heartbeat_path: str = "./runtime/daemon-heartbeat.json"
    kill_switch_path: str = "./runtime/KILL_SWITCH"
    default_position_size_pct: float = Field(default=0.02, gt=0.0, le=1.0)
    max_order_notional_usd: float = Field(default=1000.0, gt=0.0)
    min_order_notional_usd: float = Field(default=100.0, ge=0.0)
    allow_fractional_shares: bool = False
    daily_summary_dir: str = "./results/daily"
    agent_id: str = "primary"
    arena_enabled: bool = True
    arena_model: str | None = None
    agent_memory_limit: int = Field(default=10, ge=1)
    max_trades_per_cycle: int = Field(default=2, ge=1)
    conservative_spread_bps: float = Field(default=10.0, ge=0.0)


class RiskConfig(BaseModel):
    paper_only: bool = True
    allowed_symbols: list[str] | None = None
    max_position_notional_pct_per_symbol: float = Field(default=0.20, gt=0.0, le=1.0)
    max_total_gross_exposure_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    max_new_positions_per_day: int = Field(default=3, ge=0)
    max_open_positions: int = Field(default=5, ge=0)
    max_daily_loss_pct: float = Field(default=0.03, ge=0.0, le=1.0)
    min_confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    reject_short_selling: bool = True
    reject_fractional_shares: bool = True
    market_hours_only: bool = True
    cooldown_minutes_per_symbol: int = Field(default=60, ge=0)
    max_order_notional_usd: float = Field(default=1000.0, gt=0.0)
    max_daily_trades: int = Field(default=2, ge=0)
    max_daily_trades_per_symbol: int = Field(default=1, ge=0)
    allow_position_scaling: bool = False
    max_trades_per_cycle: int = Field(default=2, ge=1)
    default_to_hold: bool = True
    require_multiple_signals: bool = True
    min_signals_required: int = Field(default=2, ge=1)
    require_expected_edge: bool = True
    require_market_mispricing_reason: bool = True
    require_new_information_check: bool = True
    require_position_sizing_rationale: bool = True
    reject_if_contradicts_recent_failures: bool = True
    trade_frequency_penalty_enabled: bool = True
    recent_trade_lookback_minutes: int = Field(default=120, ge=0)
    extra_confidence_threshold_after_recent_trade: float = Field(default=0.1, ge=0.0, le=1.0)
    block_reentry_while_position_open: bool = True
    allow_scaling_in: bool = False
    allow_reversal: bool = False
    position_reentry_cooldown_hours: int = Field(default=4, ge=0)
    reversal_cooldown_hours: int = Field(default=8, ge=0)
    max_flip_flops_per_symbol_per_day: int = Field(default=1, ge=0)

    @field_validator("allowed_symbols")
    @classmethod
    def normalize_allowed_symbols(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is None:
            return value
        normalized = sorted({item.strip().upper() for item in value if item.strip()})
        return normalized or None


class SymbolExecutionResult(BaseModel):
    symbol: str
    raw_decision_text: str | None = None
    parsed_decision: ParsedDecisionResult | None = None
    arena_decision: OrderIntent | None = None
    risk_decision: RiskDecision | None = None
    submitted_order: BrokerOrder | None = None
    execution_status: str
    latest_price: float | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    reflection: dict[str, Any] | None = None


class ExecutionResult(SymbolExecutionResult):
    """Structured execution result boundary."""


class NewsItem(BaseModel):
    symbol: str | None = None
    title: str
    source: str
    url: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    content_hash: str
    is_global: bool = False
    seen_before: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class DaemonHeartbeat(BaseModel):
    pid: int | None = None
    status: str
    last_heartbeat_at: datetime
    last_cycle_started_at: datetime | None = None
    last_cycle_completed_at: datetime | None = None
    last_cycle_bucket: str | None = None
    symbols_processed: list[str] = Field(default_factory=list)
    last_error: str | None = None
    paused: bool = False
    stop_requested: bool = False


class DaemonStatus(BaseModel):
    running: bool
    pid: int | None = None
    last_heartbeat_at: datetime | None = None
    last_cycle_started_at: datetime | None = None
    last_cycle_completed_at: datetime | None = None
    last_cycle_bucket: str | None = None
    symbols_processed: list[str] = Field(default_factory=list)
    last_error: str | None = None
    paused: bool = False
    stop_requested: bool = False
    account: BrokerAccountSnapshot | None = None
    open_positions: list[BrokerPosition] = Field(default_factory=list)
    learning_summary: str | None = None
    performance_snapshot: dict[str, Any] | None = None
    trades_today: int = 0
    trades_per_symbol_today: dict[str, int] = Field(default_factory=dict)
    daily_trade_cap_reached: bool = False


class ClosedTrade(BaseModel):
    symbol: str
    qty: float = Field(gt=0.0)
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    realized_pnl: float
    entry_time: datetime | None = None
    exit_time: datetime
    holding_period_minutes: float | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


class AgentReflection(BaseModel):
    symbol: str
    what_changed: str
    correct_signals: list[str] = Field(default_factory=list)
    incorrect_signals: list[str] = Field(default_factory=list)
    lesson: str | None = None
    previous_reasoning: str | None = None
    current_reasoning: str | None = None
    action_taken: TradeAction = TradeAction.HOLD
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AgentMemorySnapshot(BaseModel):
    symbol: str
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)
    recent_closed_trades: list[dict[str, Any]] = Field(default_factory=list)
    recent_losing_trades: list[dict[str, Any]] = Field(default_factory=list)
    recent_winning_trades: list[dict[str, Any]] = Field(default_factory=list)
    recurring_mistakes: list[str] = Field(default_factory=list)
    recurring_success_patterns: list[str] = Field(default_factory=list)
    learning_summary: str | None = None
    previous_reasoning: str | None = None


class PerformanceSnapshot(BaseModel):
    trade_date: str
    cycle_bucket: str | None = None
    account_value: float
    cash: float
    gross_exposure: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    win_rate: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    max_drawdown: float | None = None
    trade_frequency: float | None = None
    open_positions: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class TradingCycleResult(BaseModel):
    run_id: str
    mode: RunMode
    trade_date: str
    symbols: list[str]
    symbol_results: list[SymbolExecutionResult] = Field(default_factory=list)
    db_path: str
    log_path: str
    audit_path: str
    result_path: str
    started_at: datetime
    finished_at: datetime
    learning_summary: str | None = None
    performance_snapshot: PerformanceSnapshot | None = None

    @property
    def approved_count(self) -> int:
        return sum(
            1
            for result in self.symbol_results
            if result.risk_decision and result.risk_decision.approved
        )

    @property
    def executed_count(self) -> int:
        return sum(1 for result in self.symbol_results if result.submitted_order is not None)

    @property
    def error_count(self) -> int:
        return sum(1 for result in self.symbol_results if result.error)
