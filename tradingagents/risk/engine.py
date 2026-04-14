from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradingagents.execution.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerPosition,
    OrderIntent,
    RiskConfig,
    RiskDecision,
    TradeAction,
)
from tradingagents.scheduler.market import MarketSession, is_market_open


class RiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.session = MarketSession()

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        account: BrokerAccountSnapshot,
        positions: list[BrokerPosition],
        open_orders: list[BrokerOrder],
        latest_price: float | None,
        existing_daily_pnl: float = 0.0,
        new_positions_today: int = 0,
        daily_trade_count: int = 0,
        symbol_daily_trade_count: int = 0,
        cycle_trade_count: int = 0,
        recent_trade_count: int = 0,
        recent_symbol_trade_count: int = 0,
        last_order_at: datetime | None = None,
        last_trade: dict | None = None,
        last_exit_at: datetime | None = None,
        recent_symbol_actions: list[str] | None = None,
        now: datetime | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        checks: dict[str, object] = {}
        now = now or datetime.now(timezone.utc)

        if intent.action == TradeAction.HOLD:
            reasons.append("HOLD decision: no broker action will be taken.")
            checks["hold"] = True
            return RiskDecision(
                approved=False,
                reasons=reasons,
                normalized_intent=intent,
                checks=checks,
            )

        if self.config.paper_only and not account.paper:
            reasons.append("Broker account is not marked as paper-only.")
            checks["paper_only"] = False
        else:
            checks["paper_only"] = True

        if self.config.allowed_symbols and intent.symbol not in self.config.allowed_symbols:
            reasons.append(f"Symbol {intent.symbol} is not in the allowed whitelist.")
            checks["allowed_symbols"] = False
        else:
            checks["allowed_symbols"] = True

        effective_confidence_threshold = self.config.min_confidence_threshold
        symbol_position = next((position for position in positions if position.symbol == intent.symbol), None)
        order_notional = self._estimate_order_notional(intent, latest_price=latest_price)
        is_first_entry = (
            intent.action == TradeAction.BUY
            and (symbol_position is None or symbol_position.qty <= 0)
            and order_notional is not None
            and order_notional <= self.config.max_order_notional_usd
        )
        effective_min_signals = self.config.min_signals_required
        if is_first_entry:
            effective_confidence_threshold = max(0.0, effective_confidence_threshold - 0.05)
            effective_min_signals = max(1, effective_min_signals - 1)
        checks["first_entry_discount"] = is_first_entry
        if self.config.trade_frequency_penalty_enabled and recent_trade_count > 0:
            effective_confidence_threshold = min(
                1.0,
                effective_confidence_threshold + self.config.extra_confidence_threshold_after_recent_trade,
            )
        if self.config.trade_frequency_penalty_enabled and recent_symbol_trade_count > 0:
            effective_confidence_threshold = min(
                1.0,
                effective_confidence_threshold + self.config.extra_confidence_threshold_after_recent_trade,
            )
        checks["effective_confidence_threshold"] = effective_confidence_threshold

        if intent.confidence is None or intent.confidence < effective_confidence_threshold:
            reasons.append(
                f"Confidence {intent.confidence!r} is below the threshold of {effective_confidence_threshold:.2f}."
            )
            checks["confidence"] = False
        else:
            checks["confidence"] = True

        signal_count = len(intent.supporting_signals)
        checks["supporting_signal_count"] = signal_count
        checks["effective_min_signals_required"] = effective_min_signals
        if self.config.require_multiple_signals and signal_count < effective_min_signals:
            reasons.append(
                f"Only {signal_count} supporting signal(s) were present; {effective_min_signals} are required."
            )
            checks["multiple_signals"] = False
        else:
            checks["multiple_signals"] = True

        if self.config.require_expected_edge and not (intent.expected_edge or "").strip():
            reasons.append("Expected edge was not stated clearly enough to trade.")
            checks["expected_edge"] = False
        else:
            checks["expected_edge"] = True

        if self.config.require_market_mispricing_reason and not (intent.why_market_wrong or "").strip():
            reasons.append("Trade thesis does not explain why the market is wrong.")
            checks["market_mispricing_reason"] = False
        else:
            checks["market_mispricing_reason"] = True

        if self._is_generic_reasoning(intent.rationale):
            reasons.append("Decision reasoning was too generic to justify a trade.")
            checks["reasoning_quality"] = False
        else:
            checks["reasoning_quality"] = True

        if self._is_insufficient_detail(intent.expected_edge):
            reasons.append("Expected edge explanation was too generic.")
            checks["edge_quality"] = False
        else:
            checks["edge_quality"] = True

        if self._is_insufficient_detail(intent.why_market_wrong):
            reasons.append("Why-the-market-is-wrong explanation was too generic.")
            checks["market_wrong_quality"] = False
        else:
            checks["market_wrong_quality"] = True

        if not intent.risks:
            reasons.append("Risks section is required before trading.")
            checks["risks_present"] = False
        else:
            checks["risks_present"] = True

        if self.config.require_new_information_check and intent.is_new_information is not True:
            reasons.append("Decision did not establish that the edge comes from fresh information.")
            checks["new_information"] = False
        else:
            checks["new_information"] = True

        if self.config.require_position_sizing_rationale and not (
            intent.position_sizing_rationale or ""
        ).strip():
            reasons.append("Position sizing rationale is required before trading.")
            checks["position_sizing_rationale"] = False
        else:
            checks["position_sizing_rationale"] = True

        if intent.fits_success_patterns is False:
            reasons.append("Decision does not fit recent successful trading patterns.")
            checks["fits_success_patterns"] = False
        else:
            checks["fits_success_patterns"] = True

        if (
            self.config.reject_if_contradicts_recent_failures
            and intent.contradicts_recent_failures is True
        ):
            reasons.append("Decision contradicts recent failed trades for this symbol.")
            checks["recent_failures"] = False
        else:
            checks["recent_failures"] = True

        current_symbol_value = abs(symbol_position.market_value or 0.0) if symbol_position else 0.0
        current_total_value = sum(abs(position.market_value or 0.0) for position in positions)
        open_positions_count = sum(1 for position in positions if position.qty > 0)
        open_order_symbols = {order.symbol for order in open_orders}
        checks["open_order_conflict"] = intent.symbol not in open_order_symbols
        if intent.symbol in open_order_symbols:
            reasons.append(f"Open order already exists for {intent.symbol}.")

        checks["order_notional"] = order_notional

        if order_notional is None or order_notional <= 0:
            reasons.append("Could not determine order notional safely.")
        elif order_notional > self.config.max_order_notional_usd:
            reasons.append(
                f"Order notional ${order_notional:.2f} exceeds limit ${self.config.max_order_notional_usd:.2f}."
            )

        if account.equity <= 0:
            reasons.append("Account equity must be positive.")
        else:
            max_symbol_value = account.equity * self.config.max_position_notional_pct_per_symbol
            projected_symbol_value = current_symbol_value + (
                order_notional if intent.action == TradeAction.BUY else -min(order_notional or 0.0, current_symbol_value)
            )
            checks["projected_symbol_value"] = projected_symbol_value
            if intent.action == TradeAction.BUY and projected_symbol_value > max_symbol_value:
                reasons.append(
                    f"Projected {intent.symbol} exposure ${projected_symbol_value:.2f} exceeds per-symbol cap ${max_symbol_value:.2f}."
                )

            projected_gross_exposure = current_total_value + (
                order_notional if intent.action == TradeAction.BUY else -min(order_notional or 0.0, current_symbol_value)
            )
            max_total_exposure = account.equity * self.config.max_total_gross_exposure_pct
            checks["projected_gross_exposure"] = projected_gross_exposure
            if projected_gross_exposure > max_total_exposure:
                reasons.append(
                    f"Projected gross exposure ${projected_gross_exposure:.2f} exceeds cap ${max_total_exposure:.2f}."
                )

        is_new_position = symbol_position is None or symbol_position.qty <= 0
        if (
            intent.action == TradeAction.BUY
            and symbol_position
            and symbol_position.qty > 0
            and self.config.block_reentry_while_position_open
            and not self.config.allow_scaling_in
        ):
            reasons.append(f"Existing open position in {intent.symbol} blocks re-entry while the position is open.")
        elif (
            intent.action == TradeAction.BUY
            and symbol_position
            and symbol_position.qty > 0
            and not self.config.allow_position_scaling
        ):
            reasons.append(f"Existing long position in {intent.symbol} blocks scaling by policy.")

        if intent.action == TradeAction.BUY and is_new_position:
            if open_positions_count >= self.config.max_open_positions:
                reasons.append(
                    f"Open positions limit reached ({open_positions_count}/{self.config.max_open_positions})."
                )
            if new_positions_today >= self.config.max_new_positions_per_day:
                reasons.append(
                    f"New positions today reached ({new_positions_today}/{self.config.max_new_positions_per_day})."
                )

        if existing_daily_pnl <= -(account.equity * self.config.max_daily_loss_pct):
            reasons.append(
                f"Daily PnL {existing_daily_pnl:.2f} breaches max daily loss threshold."
            )
        if self.config.max_daily_trades > 0 and daily_trade_count >= self.config.max_daily_trades:
            reasons.append(
                f"Daily trade count {daily_trade_count} reached max of {self.config.max_daily_trades}."
            )
        if (
            self.config.max_daily_trades_per_symbol > 0
            and symbol_daily_trade_count >= self.config.max_daily_trades_per_symbol
        ):
            reasons.append(
                f"Daily trade count for {intent.symbol} reached max of {self.config.max_daily_trades_per_symbol}."
            )
        if self.config.max_trades_per_cycle > 0 and cycle_trade_count >= self.config.max_trades_per_cycle:
            reasons.append(
                f"Cycle trade count {cycle_trade_count} reached max of {self.config.max_trades_per_cycle}."
            )

        if self.config.reject_short_selling and intent.action == TradeAction.SELL:
            available_qty = symbol_position.qty if symbol_position else 0.0
            if available_qty <= 0:
                reasons.append(f"No long position exists in {intent.symbol}; short selling is disabled.")
            elif intent.quantity and intent.quantity > available_qty + 1e-9:
                reasons.append(
                    f"Sell quantity {intent.quantity:.4f} exceeds current long quantity {available_qty:.4f}."
                )

        if self.config.reject_fractional_shares and intent.quantity is not None:
            if abs(intent.quantity - round(intent.quantity)) > 1e-9:
                reasons.append("Fractional share quantity is disabled by risk policy.")

        if self.config.market_hours_only and not is_market_open(now, self.session):
            reasons.append("Current time is outside regular US market hours.")

        if last_order_at is not None:
            elapsed = now - last_order_at
            if elapsed < timedelta(minutes=self.config.cooldown_minutes_per_symbol):
                reasons.append(
                    f"Cooldown active for {intent.symbol}; last order was {elapsed.total_seconds() / 60:.1f} minutes ago."
                )

        if (
            intent.action == TradeAction.BUY
            and last_exit_at is not None
        ):
            elapsed_since_exit = now - last_exit_at
            reentry_cooldown = timedelta(hours=self.config.position_reentry_cooldown_hours)
            if elapsed_since_exit < reentry_cooldown:
                reasons.append(
                    f"Re-entry cooldown active for {intent.symbol}; last exit was {elapsed_since_exit.total_seconds() / 3600:.1f} hours ago."
                )

        last_trade_side = str((last_trade or {}).get("side", "")).upper()
        last_trade_at = self._parse_datetime((last_trade or {}).get("submitted_at"))
        if (
            not self.config.allow_reversal
            and intent.action == TradeAction.BUY
            and last_trade_side == "SELL"
            and last_trade_at is not None
        ):
            elapsed_since_reversal = now - last_trade_at
            reversal_cooldown = timedelta(hours=self.config.reversal_cooldown_hours)
            if elapsed_since_reversal < reversal_cooldown:
                reasons.append(
                    f"Reversal cooldown active for {intent.symbol}; last SELL was {elapsed_since_reversal.total_seconds() / 3600:.1f} hours ago."
                )

        recent_symbol_actions = recent_symbol_actions or []
        if self.config.max_flip_flops_per_symbol_per_day >= 0:
            non_hold_actions = [action for action in recent_symbol_actions if action in {"BUY", "SELL"}]
            flip_flops = sum(
                1
                for previous, current in zip(non_hold_actions, non_hold_actions[1:])
                if previous != current
            )
            if (
                len(non_hold_actions) >= 2
                and non_hold_actions[0] != intent.action.value
                and non_hold_actions[1] == intent.action.value
            ):
                flip_flops += 1
            checks["flip_flop_count"] = flip_flops
            if flip_flops >= self.config.max_flip_flops_per_symbol_per_day:
                reasons.append(
                    f"Flip-flop guard triggered for {intent.symbol}; recent actions were {', '.join(non_hold_actions[:3])}."
                )
                checks["flip_flop"] = False
            else:
                checks["flip_flop"] = True

        approved = len(reasons) == 0
        return RiskDecision(
            approved=approved,
            reasons=reasons if reasons else ["Approved."],
            normalized_intent=intent,
            checks=checks,
        )

    def _estimate_order_notional(self, intent: OrderIntent, *, latest_price: float | None) -> float | None:
        if intent.notional_usd is not None:
            return intent.notional_usd
        if intent.quantity is not None and latest_price is not None:
            return intent.quantity * latest_price
        if intent.quantity is not None and intent.limit_price is not None:
            return intent.quantity * intent.limit_price
        return None

    def _is_generic_reasoning(self, text: str | None) -> bool:
        if text is None:
            return True
        normalized = " ".join(text.split()).strip().lower()
        if len(normalized) < 50:
            return True
        generic_phrases = (
            "buy now",
            "sell now",
            "strong conviction",
        )
        return any(phrase in normalized for phrase in generic_phrases)

    def _is_insufficient_detail(self, text: str | None) -> bool:
        normalized = " ".join((text or "").split()).strip()
        return len(normalized) <= 20

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value)
