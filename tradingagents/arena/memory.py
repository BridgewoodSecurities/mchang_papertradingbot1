from __future__ import annotations

from collections import Counter
from typing import Any

from tradingagents.execution.models import AgentMemorySnapshot, AgentReflection, OrderIntent
from tradingagents.persistence.sqlite_store import SQLitePersistence


class AgentMemoryService:
    def __init__(
        self,
        *,
        store: SQLitePersistence,
        agent_id: str = "primary",
        memory_limit: int = 10,
    ):
        self.store = store
        self.agent_id = agent_id
        self.memory_limit = memory_limit

    def build_snapshot(self, *, symbol: str) -> AgentMemorySnapshot:
        decisions = self.store.get_recent_agent_decisions(
            agent_id=self.agent_id,
            symbol=symbol,
            limit=self.memory_limit,
        )
        closed = self.store.get_recent_closed_trades(
            agent_id=self.agent_id,
            symbol=symbol,
            limit=self.memory_limit,
        )
        losing = self.store.get_recent_closed_trades(
            agent_id=self.agent_id,
            symbol=symbol,
            limit=self.memory_limit,
            winning=False,
        )
        winning = self.store.get_recent_closed_trades(
            agent_id=self.agent_id,
            symbol=symbol,
            limit=self.memory_limit,
            winning=True,
        )
        learning_state = self.store.get_learning_state(agent_id=self.agent_id) or {}

        recurring_mistakes = list(learning_state.get("recurring_mistakes") or [])
        recurring_successes = list(learning_state.get("recurring_success_patterns") or [])

        if not recurring_mistakes:
            recurring_mistakes = self._derive_mistakes(losing)
        if not recurring_successes:
            recurring_successes = self._derive_success_patterns(winning)

        previous_reasoning = decisions[0].get("reasoning") if decisions else None
        return AgentMemorySnapshot(
            symbol=symbol,
            recent_decisions=decisions,
            recent_closed_trades=closed,
            recent_losing_trades=losing,
            recent_winning_trades=winning,
            recurring_mistakes=recurring_mistakes[: self.memory_limit],
            recurring_success_patterns=recurring_successes[: self.memory_limit],
            learning_summary=learning_state.get("learning_summary"),
            previous_reasoning=previous_reasoning,
        )

    def record_decision(
        self,
        *,
        run_id: str,
        cycle_bucket: str | None,
        intent: OrderIntent,
    ) -> None:
        self.store.record_agent_decision(
            agent_id=self.agent_id,
            run_id=run_id,
            cycle_bucket=cycle_bucket,
            symbol=intent.symbol,
            action=intent.action.value,
            confidence=intent.confidence,
            payload=intent.model_dump(mode="json"),
        )
        self.store.prune_agent_history(agent_id=self.agent_id, limit=max(self.memory_limit * 10, 50))

    def record_reflection(
        self,
        *,
        run_id: str,
        cycle_bucket: str | None,
        reflection: AgentReflection,
    ) -> dict[str, Any]:
        self.store.record_agent_reflection(
            agent_id=self.agent_id,
            run_id=run_id,
            cycle_bucket=cycle_bucket,
            symbol=reflection.symbol,
            payload=reflection.model_dump(mode="json"),
        )
        summary = self._update_learning_summary(reflection=reflection)
        self.store.prune_agent_history(agent_id=self.agent_id, limit=max(self.memory_limit * 10, 50))
        return summary

    def get_learning_state(self) -> dict[str, Any] | None:
        return self.store.get_learning_state(agent_id=self.agent_id)

    def _update_learning_summary(self, *, reflection: AgentReflection) -> dict[str, Any]:
        current = self.store.get_learning_state(agent_id=self.agent_id) or {}
        recurring_mistakes = list(current.get("recurring_mistakes") or [])
        recurring_success_patterns = list(current.get("recurring_success_patterns") or [])
        reflection_history = list(current.get("recent_lessons") or [])

        if reflection.lesson:
            reflection_history.insert(0, reflection.lesson)
            reflection_history = reflection_history[: self.memory_limit]
            if reflection.incorrect_signals:
                recurring_mistakes.insert(0, reflection.lesson)
            elif reflection.correct_signals:
                recurring_success_patterns.insert(0, reflection.lesson)

        recurring_mistakes = self._dedupe(recurring_mistakes)[: self.memory_limit]
        recurring_success_patterns = self._dedupe(recurring_success_patterns)[
            : self.memory_limit
        ]

        learning_summary = self._compose_learning_summary(
            recurring_mistakes=recurring_mistakes,
            recurring_success_patterns=recurring_success_patterns,
            recent_lessons=reflection_history,
        )
        payload = {
            "learning_summary": learning_summary,
            "recent_lessons": reflection_history,
            "recurring_mistakes": recurring_mistakes,
            "recurring_success_patterns": recurring_success_patterns,
        }
        self.store.upsert_learning_state(agent_id=self.agent_id, summary=payload)
        return payload

    def _derive_mistakes(self, losing_trades: list[dict[str, Any]]) -> list[str]:
        if not losing_trades:
            return []
        symbol_counts = Counter(item["symbol"] for item in losing_trades if item.get("symbol"))
        messages = [
            f"Recent losses have repeated in {symbol}; require a stronger edge before re-entering."
            for symbol, count in symbol_counts.items()
            if count >= 2
        ]
        if not messages:
            messages.append("Recent losing trades suggest defaulting to HOLD when the edge is unclear.")
        return messages

    def _derive_success_patterns(self, winning_trades: list[dict[str, Any]]) -> list[str]:
        if not winning_trades:
            return []
        symbol_counts = Counter(item["symbol"] for item in winning_trades if item.get("symbol"))
        messages = [
            f"Recent profitable trades in {symbol} came from patient entries with a clear thesis."
            for symbol, count in symbol_counts.items()
            if count >= 1
        ]
        return messages or ["The best recent trades were patient and thesis-driven rather than reactive."]

    def _compose_learning_summary(
        self,
        *,
        recurring_mistakes: list[str],
        recurring_success_patterns: list[str],
        recent_lessons: list[str],
    ) -> str:
        parts: list[str] = []
        if recurring_mistakes:
            parts.append(f"Avoid: {recurring_mistakes[0]}")
        if recurring_success_patterns:
            parts.append(f"Repeat: {recurring_success_patterns[0]}")
        if recent_lessons:
            parts.append(f"Latest lesson: {recent_lessons[0]}")
        return " ".join(parts) if parts else "No durable lessons yet; continue trading selectively."

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped
