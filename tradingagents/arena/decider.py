from __future__ import annotations

import json
import re
from typing import Any

from tradingagents.execution.config import build_analysis_config
from tradingagents.execution.models import (
    AgentMemorySnapshot,
    AgentReflection,
    OrderIntent,
    ParsedDecisionResult,
    TradeAction,
)
from tradingagents.llm_clients.factory import create_llm_client


class ArenaDecisionEngine:
    GENERIC_REASONING_PATTERNS = (
        r"\bbuy now\b",
        r"\bsell now\b",
        r"\bstrong conviction\b",
        r"\bbullish outlook\b",
        r"\bbearish outlook\b",
        r"\bgood (?:buy|sell)\b",
        r"\bpositive momentum\b",
        r"\bnegative momentum\b",
    )

    def __init__(self, execution_config, risk_config):
        self.execution_config = execution_config
        self.risk_config = risk_config
        self.analysis_config = build_analysis_config(execution_config)
        self._llm = None
        if execution_config.arena_enabled:
            try:
                self._llm = self._build_llm()
            except Exception:
                self._llm = None

    def decide(
        self,
        *,
        symbol: str,
        analysis_date: str,
        raw_decision_text: str,
        parsed_decision: ParsedDecisionResult,
        base_intent: OrderIntent | None,
        cycle_inputs: dict[str, Any],
        memory_snapshot: AgentMemorySnapshot,
    ) -> tuple[OrderIntent, AgentReflection]:
        if self._llm is None:
            intent, reflection = self._fallback_decision(
                symbol=symbol,
                raw_decision_text=raw_decision_text,
                base_intent=base_intent,
                cycle_inputs=cycle_inputs,
                memory_snapshot=memory_snapshot,
            )
            return self._apply_hold_bias(intent=intent, reflection=reflection)

        prompt = self._build_prompt(
            symbol=symbol,
            analysis_date=analysis_date,
            raw_decision_text=raw_decision_text,
            parsed_decision=parsed_decision,
            base_intent=base_intent,
            cycle_inputs=cycle_inputs,
            memory_snapshot=memory_snapshot,
        )
        try:
            response = self._llm.invoke(prompt)
            payload = self._parse_json_response(response.content)
            intent, reflection = self._map_payload(
                payload=payload,
                symbol=symbol,
                raw_decision_text=raw_decision_text,
                base_intent=base_intent,
                cycle_inputs=cycle_inputs,
                memory_snapshot=memory_snapshot,
            )
            return self._apply_hold_bias(intent=intent, reflection=reflection)
        except Exception as exc:
            intent, reflection = self._fallback_decision(
                symbol=symbol,
                raw_decision_text=raw_decision_text,
                base_intent=base_intent,
                cycle_inputs=cycle_inputs,
                memory_snapshot=memory_snapshot,
            )
            intent.protocol_warnings.append(
                f"Arena decision engine fallback used after structured prompt failure: {exc}"
            )
            return self._apply_hold_bias(intent=intent, reflection=reflection)

    def _build_llm(self):
        provider = self.analysis_config.get("llm_provider", "openai")
        model = (
            self.execution_config.arena_model
            or self.analysis_config.get("quick_think_llm")
            or self.analysis_config.get("deep_think_llm")
        )
        kwargs: dict[str, Any] = {}
        if provider == "openai" and self.analysis_config.get("openai_reasoning_effort"):
            kwargs["reasoning_effort"] = self.analysis_config["openai_reasoning_effort"]
        base_url = self.analysis_config.get("backend_url")
        return create_llm_client(provider, model, base_url=base_url, **kwargs).get_llm()

    def _build_prompt(
        self,
        *,
        symbol: str,
        analysis_date: str,
        raw_decision_text: str,
        parsed_decision: ParsedDecisionResult,
        base_intent: OrderIntent | None,
        cycle_inputs: dict[str, Any],
        memory_snapshot: AgentMemorySnapshot,
    ) -> str:
        portfolio_state = json.dumps(cycle_inputs.get("portfolio") or {}, indent=2, default=str)
        pnl_state = json.dumps(cycle_inputs.get("recent_pnl") or [], indent=2, default=str)
        news_state = json.dumps(cycle_inputs.get("news") or [], indent=2, default=str)
        latest_price = cycle_inputs.get("latest_price")
        parsed_json = json.dumps(parsed_decision.model_dump(mode="json"), indent=2)
        memory_json = json.dumps(memory_snapshot.model_dump(mode="json"), indent=2)
        base_json = json.dumps(base_intent.model_dump(mode="json"), indent=2) if base_intent else "null"
        return f"""
You are the final disciplined trading agent in a Prediction Arena-style workflow.
You must follow this protocol exactly for {symbol} on {analysis_date}:

1. RECEIVE: current timestamp, market data, news/context, portfolio, recent PnL.
2. REVIEW: open positions, unrealized PnL, recent closed trades, mistakes, successes.
3. ANALYZE: identify opportunities, risks, and what changed.
4. DECIDE: output a structured BUY, SELL, or HOLD decision.
5. REFLECT: compare this reasoning to the last cycle and extract one concise lesson.

Hard rules:
- Default to HOLD unless the signal is strong.
- HOLD is preferred when uncertain, and no trade is better than a weak trade.
- Do not trade just because a cycle occurred.
- Never trade without a clear expected edge and an explanation of why the market is wrong.
- Every BUY or SELL must cite multiple supporting signals.
- Confidence must reflect uncertainty honestly and should stay low when evidence is mixed.
- Reject stale reasoning: say whether the information is new or already priced in.
- Reject trades that do not fit past successful patterns or that repeat recent failures.
- Long-only US equities, no margin, no shorting, no options, no crypto.
- Return JSON only, no markdown.

Current time: {cycle_inputs.get("timestamp")}
Latest price: {latest_price}
Trades today: {cycle_inputs.get("trades_today")}
Trades in recent lookback: {cycle_inputs.get("recent_trade_count")}
Trades in recent lookback for {symbol}: {cycle_inputs.get("recent_symbol_trade_count")}
Approaching daily cap: {cycle_inputs.get("approaching_daily_trade_cap")}
Open position context:
{json.dumps(cycle_inputs.get("open_position") or {}, indent=2, default=str)}
Last trade context:
{json.dumps(cycle_inputs.get("last_trade") or {}, indent=2, default=str)}
Cooldown context:
{json.dumps(cycle_inputs.get("cooldowns") or {}, indent=2, default=str)}

Portfolio state:
{portfolio_state}

Recent PnL:
{pnl_state}

Recent news/context:
{news_state}

Memory:
{memory_json}

Parsed TradingAgents decision:
{parsed_json}

Base intent extracted from the TradingAgents report:
{base_json}

Raw TradingAgents report:
{raw_decision_text}

Return exactly this JSON shape:
{{
  "action": "BUY|SELL|HOLD",
  "confidence": 0.0,
  "reasoning": "string",
  "expected_edge": "string",
  "why_market_wrong": "string",
  "supporting_signals": ["string"],
  "is_new_information": true,
  "fits_success_patterns": true,
  "contradicts_recent_failures": false,
  "position_sizing_rationale": "string",
  "risks": ["string"],
  "time_horizon": "string or null",
  "previous_reasoning_change": "string",
  "reflection": {{
    "what_changed": "string",
    "correct_signals": ["string"],
    "incorrect_signals": ["string"],
    "lesson": "string"
  }}
}}
""".strip()

    def _parse_json_response(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def _map_payload(
        self,
        *,
        payload: dict[str, Any],
        symbol: str,
        raw_decision_text: str,
        base_intent: OrderIntent | None,
        cycle_inputs: dict[str, Any],
        memory_snapshot: AgentMemorySnapshot,
    ) -> tuple[OrderIntent, AgentReflection]:
        action_value = str(payload.get("action", "HOLD")).upper()
        action = TradeAction(action_value) if action_value in TradeAction._value2member_map_ else TradeAction.HOLD
        risks = payload.get("risks") or []
        if isinstance(risks, str):
            risks = [risks]
        supporting_signals = self._normalize_signals(
            payload.get("supporting_signals"),
            base_intent=base_intent,
            raw_decision_text=raw_decision_text,
            cycle_inputs=cycle_inputs,
            memory_snapshot=memory_snapshot,
        )
        confidence = payload.get("confidence")
        if confidence is None and base_intent is not None:
            confidence = base_intent.confidence
        intent = OrderIntent(
            symbol=symbol,
            action=action,
            confidence=confidence,
            rationale=payload.get("reasoning") or (base_intent.rationale if base_intent else "No rationale."),
            quantity=base_intent.quantity if base_intent else None,
            notional_usd=base_intent.notional_usd if base_intent else None,
            order_type=base_intent.order_type if base_intent else "market",
            limit_price=base_intent.limit_price if base_intent else None,
            stop_loss=base_intent.stop_loss if base_intent else None,
            take_profit=base_intent.take_profit if base_intent else None,
            time_horizon=payload.get("time_horizon") or (base_intent.time_horizon if base_intent else None),
            expected_edge=payload.get("expected_edge"),
            supporting_signals=supporting_signals,
            risks=risks,
            position_sizing_rationale=payload.get("position_sizing_rationale"),
            why_market_wrong=payload.get("why_market_wrong"),
            is_new_information=payload.get("is_new_information"),
            fits_success_patterns=payload.get("fits_success_patterns"),
            contradicts_recent_failures=payload.get("contradicts_recent_failures"),
            previous_reasoning_change=payload.get("previous_reasoning_change"),
            source_raw_text=raw_decision_text,
            source_rating=base_intent.source_rating if base_intent else action.value,
            warnings=list(base_intent.warnings if base_intent else []),
            protocol_warnings=[],
            execution_notes=list(base_intent.execution_notes if base_intent else []),
        )
        reflection_payload = payload.get("reflection") or {}
        reflection = AgentReflection(
            symbol=symbol,
            what_changed=reflection_payload.get("what_changed") or payload.get("previous_reasoning_change") or "No major change identified.",
            correct_signals=list(reflection_payload.get("correct_signals") or []),
            incorrect_signals=list(reflection_payload.get("incorrect_signals") or []),
            lesson=reflection_payload.get("lesson"),
            previous_reasoning=memory_snapshot.previous_reasoning,
            current_reasoning=intent.rationale,
            action_taken=intent.action,
            confidence=intent.confidence,
        )
        return intent, reflection

    def _fallback_decision(
        self,
        *,
        symbol: str,
        raw_decision_text: str,
        base_intent: OrderIntent | None,
        cycle_inputs: dict[str, Any],
        memory_snapshot: AgentMemorySnapshot,
    ) -> tuple[OrderIntent, AgentReflection]:
        contradicts_recent_failures = bool(memory_snapshot.recent_losing_trades)
        if base_intent is None:
            base_intent = OrderIntent(
                symbol=symbol,
                action=TradeAction.HOLD,
                confidence=0.5,
                rationale="No structured intent was available, so the agent defaulted to HOLD.",
                source_raw_text=raw_decision_text,
                source_rating="HOLD",
                warnings=["No base intent was available for arena processing."],
            )
        intent = base_intent.model_copy(
            update={
                "expected_edge": base_intent.expected_edge or (base_intent.rationale or "")[:240],
                "supporting_signals": self._normalize_signals(
                    base_intent.supporting_signals,
                    base_intent=base_intent,
                    raw_decision_text=raw_decision_text,
                    cycle_inputs=cycle_inputs,
                    memory_snapshot=memory_snapshot,
                ),
                "risks": base_intent.risks or ["Structured arena decision unavailable; rely on conservative sizing and risk caps."],
                "position_sizing_rationale": base_intent.position_sizing_rationale or "Use the configured capped notional sizing policy and avoid oversized entries.",
                "why_market_wrong": base_intent.why_market_wrong or "The portfolio thesis assumes the latest research output is not fully reflected in price yet.",
                "is_new_information": True if base_intent.action != TradeAction.HOLD else None,
                "fits_success_patterns": not contradicts_recent_failures if base_intent.action != TradeAction.HOLD else True,
                "contradicts_recent_failures": contradicts_recent_failures if base_intent.action != TradeAction.HOLD else False,
                "previous_reasoning_change": "Fallback path used; no structured comparison was generated.",
            }
        )
        if contradicts_recent_failures and intent.action != TradeAction.HOLD:
            intent.protocol_warnings.append(
                "Recent losing trades for this symbol exist; the fallback decision is likely to be rejected."
            )
        reflection = AgentReflection(
            symbol=symbol,
            what_changed="Fallback decision path used.",
            correct_signals=[],
            incorrect_signals=[],
            lesson="When structured reasoning fails, default to HOLD or require a stronger edge.",
            previous_reasoning=memory_snapshot.previous_reasoning,
            current_reasoning=intent.rationale,
            action_taken=intent.action,
            confidence=intent.confidence,
        )
        return intent, reflection

    def _apply_hold_bias(
        self,
        *,
        intent: OrderIntent,
        reflection: AgentReflection,
    ) -> tuple[OrderIntent, AgentReflection]:
        if intent.action == TradeAction.HOLD:
            return intent, reflection

        signal_count = len(intent.supporting_signals)
        weak_reasoning = self._is_generic_reasoning(intent.rationale)
        weak_edge = self._is_generic_reasoning(intent.expected_edge)
        weak_market_wrong = self._is_generic_reasoning(intent.why_market_wrong)
        warnings = list(intent.protocol_warnings)
        should_hold = False

        if intent.confidence is None or intent.confidence < self.risk_config.min_confidence_threshold:
            warnings.append(
                f"Confidence {intent.confidence!r} did not clear the arena hold-bias threshold of {self.risk_config.min_confidence_threshold:.2f}."
            )
            should_hold = True

        if self.risk_config.require_multiple_signals and signal_count < self.risk_config.min_signals_required:
            warnings.append(
                f"Only {signal_count} supporting signal(s) were found; {self.risk_config.min_signals_required} are required."
            )
            should_hold = True

        if weak_reasoning:
            warnings.append("Reasoning was too generic for a live trade decision.")
            should_hold = True
        if weak_edge or not (intent.expected_edge or "").strip():
            warnings.append("Expected edge was weak or generic.")
            should_hold = True
        if weak_market_wrong or not (intent.why_market_wrong or "").strip():
            warnings.append("Why-the-market-is-wrong explanation was weak or generic.")
            should_hold = True
        if not intent.risks:
            warnings.append("Risks were missing, so the decision defaulted to HOLD.")
            should_hold = True

        if not should_hold:
            return intent.model_copy(update={"protocol_warnings": warnings}), reflection

        held_intent = intent.model_copy(
            update={
                "action": TradeAction.HOLD,
                "source_rating": "HOLD",
                "protocol_warnings": warnings,
            }
        )
        held_reflection = reflection.model_copy(
            update={
                "action_taken": TradeAction.HOLD,
                "lesson": reflection.lesson
                or "No trade is better than a weak trade; wait for stronger multi-signal confirmation.",
                "incorrect_signals": (list(reflection.incorrect_signals) + warnings)[:6],
            }
        )
        return held_intent, held_reflection

    def _normalize_signals(
        self,
        signals: Any,
        *,
        base_intent: OrderIntent | None,
        raw_decision_text: str,
        cycle_inputs: dict[str, Any],
        memory_snapshot: AgentMemorySnapshot,
    ) -> list[str]:
        normalized: list[str] = []
        raw_signals = signals if isinstance(signals, list) else []
        for signal in raw_signals:
            if isinstance(signal, str):
                cleaned = signal.strip()
                if cleaned and cleaned not in normalized:
                    normalized.append(cleaned)

        text = " ".join(
            part
            for part in [
                raw_decision_text or "",
                base_intent.rationale if base_intent and base_intent.rationale else "",
                base_intent.expected_edge if base_intent and base_intent.expected_edge else "",
            ]
            if part
        ).lower()
        news_items = cycle_inputs.get("news") or []
        if any(not item.get("seen_before", False) for item in news_items if isinstance(item, dict)):
            normalized.append("fresh news catalyst")
        if any(keyword in text for keyword in ("breakout", "trend", "support", "resistance", "moving average")):
            normalized.append("price/trend confirmation")
        if any(keyword in text for keyword in ("rsi", "macd", "ema", "sma", "momentum", "technical")):
            normalized.append("technical confirmation")
        portfolio = cycle_inputs.get("portfolio") or {}
        if (
            isinstance(portfolio, dict)
            and float(portfolio.get("gross_exposure", 0.0) or 0.0)
            < float(portfolio.get("equity", 0.0) or 0.0)
        ):
            normalized.append("portfolio/risk alignment")
        if memory_snapshot.recurring_success_patterns and not memory_snapshot.recent_losing_trades:
            normalized.append("reflection/memory support")
        if base_intent and (base_intent.expected_edge or base_intent.why_market_wrong):
            normalized.append("strong agent edge explanation")

        seen: set[str] = set()
        deduped: list[str] = []
        for signal in normalized:
            key = signal.strip().lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(signal.strip())
        return deduped

    def _is_generic_reasoning(self, text: str | None) -> bool:
        if text is None:
            return True
        normalized = " ".join(text.split()).strip().lower()
        if len(normalized) < 30:
            return True
        return any(re.search(pattern, normalized) for pattern in self.GENERIC_REASONING_PATTERNS)
