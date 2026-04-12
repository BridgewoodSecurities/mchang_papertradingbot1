from __future__ import annotations

import re

from tradingagents.execution.models import OrderIntent, OrderType, ParsedDecisionResult, TradeAction


TICKER_PATTERN = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z]{1,4})?\b")


class DecisionParser:
    """Parse portfolio-manager output into structured order intents."""

    RATING_PATTERN = re.compile(
        r"\bRating\b\s*[:\-]\s*(Buy|Overweight|Hold|Underweight|Sell)\b",
        re.IGNORECASE,
    )
    CONFIDENCE_PATTERN = re.compile(
        r"\bconfidence\b\s*[:\-]?\s*(?:(\d{1,3})\s*%|(0?\.\d+))",
        re.IGNORECASE,
    )
    SHARES_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:shares?|units?)", re.IGNORECASE)
    NOTIONAL_BEFORE_PATTERN = re.compile(
        r"(?:position size|allocate|allocation|notional|deploy|buy|sell)\D{0,20}\$([0-9][0-9,]*(?:\.\d+)?)",
        re.IGNORECASE,
    )
    NOTIONAL_AFTER_PATTERN = re.compile(
        r"\$([0-9][0-9,]*(?:\.\d+)?)\D{0,20}(?:position size|allocation|notional|position|stake)",
        re.IGNORECASE,
    )
    LIMIT_PATTERN = re.compile(
        r"\blimit(?: price)?\s*(?:at|around|near|of)?\s*\$([0-9][0-9,]*(?:\.\d+)?)",
        re.IGNORECASE,
    )
    STOP_PATTERN = re.compile(
        r"\b(?:stop(?:-|\s)?loss|stop)\s*(?:at|around|near|of)?\s*\$([0-9][0-9,]*(?:\.\d+)?)",
        re.IGNORECASE,
    )
    TAKE_PROFIT_PATTERN = re.compile(
        r"\b(?:take(?:-|\s)?profit|target)\s*(?:at|around|near|of)?\s*\$([0-9][0-9,]*(?:\.\d+)?)",
        re.IGNORECASE,
    )
    HORIZON_PATTERN = re.compile(
        r"\b(\d+\s*(?:day|week|month|year)s?|intraday|swing|long(?:er)? term)\b",
        re.IGNORECASE,
    )

    def parse(self, raw_text: str, *, default_symbol: str | None = None) -> ParsedDecisionResult:
        text = (raw_text or "").strip()
        if not text:
            return ParsedDecisionResult(
                raw_text=raw_text,
                warnings=["Empty decision text received from TradingAgents."],
                rejected=True,
            )

        warnings: list[str] = []
        blocks = self._extract_blocks(text, default_symbol=default_symbol)
        intents: list[OrderIntent] = []

        for symbol, block in blocks:
            intent = self._parse_block(block, symbol=symbol)
            if intent is None:
                warnings.append(
                    f"Decision for {symbol or 'unknown symbol'} was ambiguous and interpreted as HOLD."
                )
                if symbol:
                    intents.append(
                        OrderIntent(
                            symbol=symbol,
                            action=TradeAction.HOLD,
                            confidence=0.5,
                            rationale=self._extract_rationale(block),
                            source_raw_text=block,
                            source_rating="HOLD",
                            warnings=["Ambiguous decision interpreted as HOLD."],
                        )
                    )
                continue
            intents.append(intent)

        rejected = len(intents) == 0
        if not intents:
            warnings.append("No actionable decision could be parsed safely.")

        return ParsedDecisionResult(
            raw_text=raw_text,
            intents=intents,
            warnings=warnings,
            rejected=rejected,
        )

    def _extract_blocks(
        self, text: str, *, default_symbol: str | None
    ) -> list[tuple[str | None, str]]:
        normalized_default = default_symbol.strip().upper() if default_symbol else None
        explicit_blocks = list(
            re.finditer(
                r"(?ms)^\s*([A-Z]{1,5}(?:\.[A-Z]{1,4})?)\s*[:\-]\s*(.+?)(?=^\s*[A-Z]{1,5}(?:\.[A-Z]{1,4})?\s*[:\-]|\Z)",
                text,
            )
        )
        if explicit_blocks:
            return [(match.group(1).upper(), match.group(2).strip()) for match in explicit_blocks]

        symbol = normalized_default or self._extract_symbol(text)
        return [(symbol, text)]

    def _parse_block(self, text: str, *, symbol: str | None) -> OrderIntent | None:
        normalized_text = self._normalize_markdown(text)
        warnings: list[str] = []
        rating = self._extract_rating(normalized_text)
        action = self._rating_to_action(rating)

        if action is None:
            action = self._extract_phrase_action(normalized_text)
            if action is None:
                return None
            rating = action.value
            warnings.append("Action inferred from free-form language instead of explicit rating.")

        if symbol is None:
            return None

        confidence, confidence_warning = self._extract_confidence(
            normalized_text, action=action, rating=rating
        )
        if confidence_warning:
            warnings.append(confidence_warning)

        quantity = self._extract_float(self.SHARES_PATTERN, normalized_text)
        notional = self._extract_notional(normalized_text)
        limit_price = self._extract_float(self.LIMIT_PATTERN, normalized_text)
        stop_loss = self._extract_float(self.STOP_PATTERN, normalized_text)
        take_profit = self._extract_float(self.TAKE_PROFIT_PATTERN, normalized_text)
        time_horizon = self._extract_horizon(normalized_text)
        rationale = self._extract_rationale(normalized_text)

        order_type = OrderType.LIMIT if limit_price else OrderType.MARKET

        if action != TradeAction.HOLD and quantity is None and notional is None:
            warnings.append("Position sizing was not explicit in the decision text.")

        return OrderIntent(
            symbol=symbol,
            action=action,
            confidence=confidence,
            rationale=rationale,
            quantity=quantity,
            notional_usd=notional,
            order_type=order_type,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_horizon=time_horizon,
            source_raw_text=text,
            source_rating=rating,
            warnings=warnings,
        )

    def _normalize_markdown(self, text: str) -> str:
        normalized = text
        normalized = re.sub(r"[*_`#>\[\]]", "", normalized)
        normalized = re.sub(r"^\s*\d+\.\s*", "", normalized, flags=re.MULTILINE)
        normalized = re.sub(r"\n\s*-\s*", "\n", normalized)
        normalized = re.sub(r"\s+\n", "\n", normalized)
        return normalized.strip()

    def _extract_rating(self, text: str) -> str | None:
        match = self.RATING_PATTERN.search(text)
        if match:
            return match.group(1).upper()
        return None

    def _rating_to_action(self, rating: str | None) -> TradeAction | None:
        if rating is None:
            return None
        mapping = {
            "BUY": TradeAction.BUY,
            "OVERWEIGHT": TradeAction.BUY,
            "HOLD": TradeAction.HOLD,
            "UNDERWEIGHT": TradeAction.SELL,
            "SELL": TradeAction.SELL,
        }
        return mapping.get(rating.upper())

    def _extract_phrase_action(self, text: str) -> TradeAction | None:
        lowered = text.lower()
        buy_patterns = [
            r"\bbuy\b",
            r"\bgo long\b",
            r"\bsmall long\b",
            r"\baccumulate\b",
            r"\binitiate(?: a)? long\b",
            r"\badd to (?:the )?position\b",
        ]
        sell_patterns = [
            r"\bsell\b",
            r"\breduce exposure\b",
            r"\btrim\b",
            r"\bexit(?: the)? position\b",
            r"\bclose(?: the)? position\b",
            r"\bunderweight\b",
            r"\btake profits\b",
        ]
        hold_patterns = [
            r"\bhold\b",
            r"\bno action\b",
            r"\bstay on the sidelines\b",
            r"\bmaintain current position\b",
        ]

        matches = {
            TradeAction.BUY: any(re.search(pattern, lowered) for pattern in buy_patterns),
            TradeAction.SELL: any(re.search(pattern, lowered) for pattern in sell_patterns),
            TradeAction.HOLD: any(re.search(pattern, lowered) for pattern in hold_patterns),
        }

        detected = [action for action, matched in matches.items() if matched]
        if len(detected) != 1:
            return None
        return detected[0]

    def _extract_confidence(
        self, text: str, *, action: TradeAction, rating: str
    ) -> tuple[float | None, str | None]:
        match = self.CONFIDENCE_PATTERN.search(text)
        if match:
            percent, decimal = match.groups()
            if percent is not None:
                return float(percent) / 100.0, None
            if decimal is not None:
                return float(decimal), None

        inferred = {
            "BUY": 0.75,
            "OVERWEIGHT": 0.65,
            "HOLD": 0.50,
            "UNDERWEIGHT": 0.60,
            "SELL": 0.75,
        }
        if rating.upper() in inferred:
            return inferred[rating.upper()], "Confidence inferred from the decision rating."
        return (
            {
                TradeAction.BUY: 0.60,
                TradeAction.SELL: 0.60,
                TradeAction.HOLD: 0.50,
            }[action],
            "Confidence inferred from free-form language.",
        )

    def _extract_symbol(self, text: str) -> str | None:
        for match in TICKER_PATTERN.finditer(text):
            symbol = match.group(0).upper()
            if symbol not in {"BUY", "SELL", "HOLD"}:
                return symbol
        return None

    def _extract_float(self, pattern: re.Pattern[str], text: str) -> float | None:
        match = pattern.search(text)
        if not match:
            return None
        return float(match.group(1).replace(",", ""))

    def _extract_notional(self, text: str) -> float | None:
        for pattern in (self.NOTIONAL_BEFORE_PATTERN, self.NOTIONAL_AFTER_PATTERN):
            value = self._extract_float(pattern, text)
            if value is not None:
                return value
        return None

    def _extract_horizon(self, text: str) -> str | None:
        match = self.HORIZON_PATTERN.search(text)
        if not match:
            return None
        return match.group(1)

    def _extract_rationale(self, text: str) -> str | None:
        for heading in ("Executive Summary", "Investment Thesis"):
            pattern = re.compile(
                rf"{heading}\s*[:\-]?\s*(.+?)(?=\n[A-Z][A-Za-z ]+\s*[:\-]|\Z)",
                re.IGNORECASE | re.DOTALL,
            )
            match = pattern.search(text)
            if match:
                rationale = " ".join(match.group(1).strip().split())
                return rationale[:600]

        paragraphs = [segment.strip() for segment in text.split("\n\n") if segment.strip()]
        if not paragraphs:
            return None
        cleaned = " ".join(paragraphs[0].split())
        return cleaned[:600]
