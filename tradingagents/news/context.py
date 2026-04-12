from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from tradingagents.execution.models import NewsItem


class ContextCacheService:
    def __init__(self, store):
        self.store = store

    def fetch_symbol_news(self, symbol: str, *, limit: int = 10) -> list[NewsItem]:
        ticker = yf.Ticker(symbol)
        raw_items = ticker.get_news(count=limit) or []
        items = [self._map_item(article, symbol=symbol, is_global=False) for article in raw_items]
        return self.store.upsert_news_items(items)

    def fetch_global_news(self, *, limit: int = 10) -> list[NewsItem]:
        queries = [
            "stock market economy",
            "Federal Reserve interest rates",
            "inflation economic outlook",
            "global markets trading",
        ]
        mapped: list[NewsItem] = []
        seen_hashes: set[str] = set()
        for query in queries:
            search = yf.Search(
                query=query,
                news_count=limit,
                enable_fuzzy_query=True,
            )
            for article in search.news or []:
                item = self._map_item(article, symbol=None, is_global=True)
                if item.content_hash in seen_hashes:
                    continue
                seen_hashes.add(item.content_hash)
                mapped.append(item)
                if len(mapped) >= limit:
                    return self.store.upsert_news_items(mapped)
        return self.store.upsert_news_items(mapped)

    def fetch_cycle_context(
        self,
        *,
        symbols: list[str],
        global_limit: int = 10,
        symbol_limit: int = 10,
    ) -> dict[str, list[NewsItem]]:
        context: dict[str, list[NewsItem]] = {
            "_global": self.fetch_global_news(limit=global_limit)
        }
        for symbol in symbols:
            try:
                context[symbol] = self.fetch_symbol_news(symbol, limit=symbol_limit)
            except Exception:
                context[symbol] = []
        return context

    def _map_item(
        self,
        article: dict[str, Any],
        *,
        symbol: str | None,
        is_global: bool,
    ) -> NewsItem:
        content = article.get("content", article)
        provider = content.get("provider", {}) or {}
        title = content.get("title") or article.get("title") or "Untitled"
        summary = content.get("summary") or article.get("summary") or ""
        source = provider.get("displayName") or article.get("publisher") or "Unknown"
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        url = url_obj.get("url") or article.get("link") or None
        pub_date = content.get("pubDate")
        published_at = None
        if pub_date:
            try:
                published_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            except ValueError:
                published_at = None
        content_hash = self._hash_item(
            symbol=symbol,
            title=title,
            source=source,
            url=url,
            published_at=published_at,
        )
        return NewsItem(
            symbol=symbol,
            title=title,
            source=source,
            url=url,
            published_at=published_at,
            summary=summary,
            content_hash=content_hash,
            is_global=is_global,
            raw=article,
        )

    def _hash_item(
        self,
        *,
        symbol: str | None,
        title: str,
        source: str,
        url: str | None,
        published_at: datetime | None,
    ) -> str:
        payload = "|".join(
            [
                symbol or "",
                title.strip(),
                source.strip(),
                url or "",
                published_at.astimezone(timezone.utc).isoformat() if published_at else "",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
