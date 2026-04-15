from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tradingagents.execution.models import BrokerOrder, DaemonHeartbeat, NewsItem


class SQLitePersistence:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS analysis_runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            symbols_json TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            summary_json TEXT,
            result_path TEXT,
            audit_path TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parsed_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            order_id TEXT,
            client_order_id TEXT,
            qty REAL,
            notional_usd REAL,
            submitted_at TEXT NOT NULL,
            is_new_position INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            order_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            equity REAL NOT NULL,
            cash REAL NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_pnl_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            equity REAL NOT NULL,
            cash REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            gross_exposure REAL NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_state (
            name TEXT PRIMARY KEY,
            pid INTEGER,
            hostname TEXT,
            paused INTEGER NOT NULL DEFAULT 0,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            last_heartbeat_at TEXT,
            last_cycle_started_at TEXT,
            last_cycle_completed_at TEXT,
            last_cycle_bucket TEXT,
            symbols_processed_json TEXT NOT NULL DEFAULT '[]',
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid INTEGER NOT NULL,
            hostname TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid INTEGER,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_cycles (
            cycle_id TEXT PRIMARY KEY,
            bucket_start TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            symbols_json TEXT NOT NULL,
            summary_json TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_symbol_buckets (
            bucket_start TEXT NOT NULL,
            symbol TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            run_id TEXT,
            status TEXT NOT NULL,
            error TEXT,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (bucket_start, symbol)
        );
        CREATE TABLE IF NOT EXISTS news_items (
            content_hash TEXT PRIMARY KEY,
            symbol TEXT,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            published_at TEXT,
            summary TEXT,
            is_global INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1,
            last_cycle_bucket TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cycle_news (
            bucket_start TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            symbol TEXT,
            is_new INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (bucket_start, content_hash)
        );
        CREATE TABLE IF NOT EXISTS cycle_context (
            cycle_id TEXT PRIMARY KEY,
            bucket_start TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daemon_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_reports (
            trade_date TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            cycle_bucket TEXT,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            cycle_bucket TEXT,
            symbol TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learning_state (
            agent_id TEXT PRIMARY KEY,
            summary_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS closed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            realized_pnl REAL NOT NULL,
            qty REAL NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            payload_json TEXT NOT NULL,
            closed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            cycle_bucket TEXT,
            trade_date TEXT NOT NULL,
            account_value REAL NOT NULL,
            cash REAL NOT NULL,
            gross_exposure REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            total_pnl REAL NOT NULL,
            win_rate REAL,
            average_win REAL,
            average_loss REAL,
            max_drawdown REAL,
            trade_frequency REAL,
            open_positions INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS counterfactuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            original_action TEXT NOT NULL,
            original_confidence REAL,
            final_action TEXT NOT NULL,
            price_at_decision REAL,
            price_after_1d REAL,
            price_after_5d REAL,
            would_have_pnl_1d REAL,
            would_have_pnl_5d REAL,
            override_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        with self._connect() as connection:
            connection.executescript(schema)

    def record_run(
        self,
        *,
        run_id: str,
        mode: str,
        trade_date: str,
        symbols: list[str],
        status: str,
        started_at: datetime,
        finished_at: datetime | None = None,
        summary: dict[str, Any] | None = None,
        result_path: str | None = None,
        audit_path: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO analysis_runs
                (run_id, mode, trade_date, symbols_json, status, started_at, finished_at, summary_json, result_path, audit_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    trade_date,
                    json.dumps(symbols),
                    status,
                    started_at.isoformat(),
                    finished_at.isoformat() if finished_at else None,
                    json.dumps(summary or {}, default=str),
                    result_path,
                    audit_path,
                ),
            )

    def record_raw_decision(self, *, run_id: str, symbol: str, raw_text: str) -> None:
        self._insert_simple("raw_decisions", run_id=run_id, symbol=symbol, raw_text=raw_text)

    def record_parsed_decision(self, *, run_id: str, symbol: str, payload: dict[str, Any]) -> None:
        self._insert_json("parsed_decisions", run_id=run_id, symbol=symbol, payload=payload)

    def record_risk_decision(self, *, run_id: str, symbol: str, payload: dict[str, Any]) -> None:
        self._insert_json("risk_decisions", run_id=run_id, symbol=symbol, payload=payload)

    def record_broker_order(
        self,
        *,
        run_id: str,
        symbol: str,
        order: BrokerOrder,
        is_new_position: bool,
    ) -> None:
        submitted_at = order.submitted_at or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_orders
                (run_id, symbol, side, status, order_id, client_order_id, qty, notional_usd, submitted_at, is_new_position, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    symbol,
                    order.side.value,
                    order.status,
                    order.id,
                    order.client_order_id,
                    order.qty,
                    order.notional_usd,
                    submitted_at.isoformat(),
                    1 if is_new_position else 0,
                    order.model_dump_json(),
                ),
            )

    def update_broker_order(self, *, order: BrokerOrder) -> int:
        identifiers: list[str] = []
        params: list[Any] = [
            order.status,
            order.id,
            order.client_order_id,
            order.qty,
            order.notional_usd,
            (order.submitted_at or datetime.now(timezone.utc)).isoformat(),
            order.model_dump_json(),
        ]
        if order.id:
            identifiers.append("order_id = ?")
            params.append(order.id)
        if order.client_order_id:
            identifiers.append("client_order_id = ?")
            params.append(order.client_order_id)
        if not identifiers:
            return 0

        query = f"""
            UPDATE broker_orders
            SET
                status = ?,
                order_id = ?,
                client_order_id = ?,
                qty = ?,
                notional_usd = ?,
                submitted_at = ?,
                payload_json = ?
            WHERE {" OR ".join(identifiers)}
        """
        with self._connect() as connection:
            cursor = connection.execute(query, tuple(params))
            return int(cursor.rowcount or 0)

    def record_broker_event(
        self,
        *,
        run_id: str,
        symbol: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO broker_events (run_id, symbol, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    symbol,
                    event_type,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_fill(self, *, run_id: str, symbol: str, order_id: str | None, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fills (run_id, symbol, order_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    symbol,
                    order_id,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def has_fill(self, *, order_id: str | None) -> bool:
        if not order_id:
            return False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM fills
                WHERE order_id = ?
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        return row is not None

    def snapshot_positions(self, *, run_id: str, payload: list[dict[str, Any]]) -> None:
        self._insert_json("position_snapshots", run_id=run_id, payload=payload, symbolless=True)

    def snapshot_equity(self, *, run_id: str, equity: float, cash: float, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO equity_snapshots (run_id, equity, cash, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    equity,
                    cash,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_daily_pnl_summary(
        self,
        *,
        run_id: str,
        trade_date: str,
        equity: float,
        cash: float,
        realized_pnl: float,
        unrealized_pnl: float,
        gross_exposure: float,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_pnl_summaries
                (run_id, trade_date, equity, cash, realized_pnl, unrealized_pnl, gross_exposure, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    trade_date,
                    equity,
                    cash,
                    realized_pnl,
                    unrealized_pnl,
                    gross_exposure,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_last_order_time(self, *, symbol: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT submitted_at FROM broker_orders
                WHERE symbol = ?
                ORDER BY submitted_at DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["submitted_at"])

    def get_last_broker_order(
        self,
        *,
        symbol: str,
        side: str | None = None,
    ) -> dict[str, Any] | None:
        query = """
            SELECT symbol, side, status, order_id, client_order_id, qty, notional_usd, submitted_at, payload_json
            FROM broker_orders
            WHERE symbol = ?
        """
        params: list[Any] = [symbol]
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY submitted_at DESC, id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload.setdefault("symbol", row["symbol"])
        payload.setdefault("side", row["side"])
        payload.setdefault("status", row["status"])
        payload.setdefault("submitted_at", row["submitted_at"])
        payload.setdefault("qty", row["qty"])
        payload.setdefault("notional_usd", row["notional_usd"])
        return payload

    def count_new_positions_for_date(self, *, trade_date: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM broker_orders
                WHERE substr(submitted_at, 1, 10) = ? AND is_new_position = 1
                """,
                (trade_date,),
            ).fetchone()
        return int(row["count"])

    def count_trades_for_date(self, *, trade_date: str, symbol: str | None = None) -> int:
        query = """
            SELECT COUNT(*) AS count
            FROM broker_orders
            WHERE substr(submitted_at, 1, 10) = ?
        """
        params: list[Any] = [trade_date]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        with self._connect() as connection:
            row = connection.execute(
                query,
                tuple(params),
            ).fetchone()
        return int(row["count"])

    def get_trades_per_symbol_for_date(self, *, trade_date: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, COUNT(*) AS count
                FROM broker_orders
                WHERE substr(submitted_at, 1, 10) = ?
                GROUP BY symbol
                ORDER BY symbol
                """,
                (trade_date,),
            ).fetchall()
        return {row["symbol"]: int(row["count"]) for row in rows}

    def count_recent_trades(
        self,
        *,
        since: datetime,
        symbol: str | None = None,
    ) -> int:
        query = """
            SELECT COUNT(*) AS count
            FROM broker_orders
            WHERE submitted_at >= ?
        """
        params: list[Any] = [since.isoformat()]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        with self._connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        return int(row["count"])

    def get_recent_broker_orders(
        self,
        *,
        since: datetime | None = None,
        symbol: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT symbol, side, status, order_id, client_order_id, qty, notional_usd, submitted_at, payload_json
            FROM broker_orders
            WHERE 1 = 1
        """
        params: list[Any] = []
        if since is not None:
            query += " AND submitted_at >= ?"
            params.append(since.isoformat())
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY submitted_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("symbol", row["symbol"])
            payload.setdefault("side", row["side"])
            payload.setdefault("status", row["status"])
            payload.setdefault("submitted_at", row["submitted_at"])
            payload.setdefault("qty", row["qty"])
            payload.setdefault("notional_usd", row["notional_usd"])
            items.append(payload)
        return items

    def get_recent_orders(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, side, status, order_id, client_order_id, qty, notional_usd, submitted_at
                FROM broker_orders
                ORDER BY submitted_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_last_exit_time(self, *, agent_id: str, symbol: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT closed_at
                FROM closed_trades
                WHERE agent_id = ? AND symbol = ?
                ORDER BY closed_at DESC, id DESC
                LIMIT 1
                """,
                (agent_id, symbol),
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["closed_at"])

    def get_recent_pnl(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, equity, cash, realized_pnl, unrealized_pnl, gross_exposure
                FROM daily_pnl_summaries
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_position_snapshot(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM position_snapshots
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return []
        return json.loads(row["payload_json"])

    def get_run_position_snapshots(self, *, run_id: str) -> list[list[dict[str, Any]]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM position_snapshots
                WHERE run_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def record_agent_decision(
        self,
        *,
        agent_id: str,
        run_id: str,
        cycle_bucket: str | None,
        symbol: str,
        action: str,
        confidence: float | None,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_decisions
                (agent_id, run_id, cycle_bucket, symbol, action, confidence, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    run_id,
                    cycle_bucket,
                    symbol,
                    action,
                    confidence,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_recent_agent_decisions(
        self,
        *,
        agent_id: str,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, symbol, action, confidence, cycle_bucket, payload_json, created_at
            FROM agent_decisions
            WHERE agent_id = ?
        """
        params: list[Any] = [agent_id]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("id", row["id"])
            payload.setdefault("symbol", row["symbol"])
            payload.setdefault("action", row["action"])
            payload.setdefault("confidence", row["confidence"])
            payload.setdefault("cycle_bucket", row["cycle_bucket"])
            payload.setdefault("created_at", row["created_at"])
            items.append(payload)
        return items

    def get_last_agent_decision(self, *, agent_id: str, symbol: str) -> dict[str, Any] | None:
        rows = self.get_recent_agent_decisions(agent_id=agent_id, symbol=symbol, limit=1)
        return rows[0] if rows else None

    def record_agent_reflection(
        self,
        *,
        agent_id: str,
        run_id: str,
        cycle_bucket: str | None,
        symbol: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_reflections
                (agent_id, run_id, cycle_bucket, symbol, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    run_id,
                    cycle_bucket,
                    symbol,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_recent_reflections(
        self,
        *,
        agent_id: str,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT symbol, cycle_bucket, payload_json, created_at
            FROM agent_reflections
            WHERE agent_id = ?
        """
        params: list[Any] = [agent_id]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("symbol", row["symbol"])
            payload.setdefault("cycle_bucket", row["cycle_bucket"])
            payload.setdefault("created_at", row["created_at"])
            items.append(payload)
        return items

    def upsert_learning_state(self, *, agent_id: str, summary: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_state (agent_id, summary_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id,
                    json.dumps(summary, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_learning_state(self, *, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json FROM learning_state WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["summary_json"])

    def record_closed_trade(
        self,
        *,
        agent_id: str,
        run_id: str,
        symbol: str,
        realized_pnl: float,
        qty: float,
        entry_price: float,
        exit_price: float,
        payload: dict[str, Any],
        closed_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO closed_trades
                (agent_id, run_id, symbol, realized_pnl, qty, entry_price, exit_price, payload_json, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    run_id,
                    symbol,
                    realized_pnl,
                    qty,
                    entry_price,
                    exit_price,
                    json.dumps(payload, default=str),
                    closed_at,
                ),
            )

    def get_recent_closed_trades(
        self,
        *,
        agent_id: str,
        symbol: str | None = None,
        limit: int = 10,
        winning: bool | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT symbol, realized_pnl, qty, entry_price, exit_price, payload_json, closed_at
            FROM closed_trades
            WHERE agent_id = ?
        """
        params: list[Any] = [agent_id]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if winning is True:
            query += " AND realized_pnl > 0"
        elif winning is False:
            query += " AND realized_pnl <= 0"
        query += " ORDER BY closed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload.setdefault("symbol", row["symbol"])
            payload.setdefault("realized_pnl", row["realized_pnl"])
            payload.setdefault("qty", row["qty"])
            payload.setdefault("entry_price", row["entry_price"])
            payload.setdefault("exit_price", row["exit_price"])
            payload.setdefault("closed_at", row["closed_at"])
            items.append(payload)
        return items

    def record_performance_snapshot(
        self,
        *,
        agent_id: str,
        run_id: str,
        cycle_bucket: str | None,
        trade_date: str,
        account_value: float,
        cash: float,
        gross_exposure: float,
        realized_pnl: float,
        unrealized_pnl: float,
        total_pnl: float,
        win_rate: float | None,
        average_win: float | None,
        average_loss: float | None,
        max_drawdown: float | None,
        trade_frequency: float | None,
        open_positions: int,
        payload: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO performance_snapshots
                (agent_id, run_id, cycle_bucket, trade_date, account_value, cash, gross_exposure, realized_pnl, unrealized_pnl, total_pnl, win_rate, average_win, average_loss, max_drawdown, trade_frequency, open_positions, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    run_id,
                    cycle_bucket,
                    trade_date,
                    account_value,
                    cash,
                    gross_exposure,
                    realized_pnl,
                    unrealized_pnl,
                    total_pnl,
                    win_rate,
                    average_win,
                    average_loss,
                    max_drawdown,
                    trade_frequency,
                    open_positions,
                    json.dumps(payload, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_latest_performance_snapshot(self, *, agent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM performance_snapshots
                WHERE agent_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def get_recent_performance_snapshots(
        self,
        *,
        agent_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM performance_snapshots
                WHERE agent_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def record_counterfactual(
        self,
        run_id: str,
        symbol: str,
        trade_date: str,
        original_action: str,
        original_confidence: float | None,
        final_action: str,
        price_at_decision: float | None,
        override_reason: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO counterfactuals
                (run_id, symbol, trade_date, original_action, original_confidence, final_action, price_at_decision, override_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    symbol,
                    trade_date,
                    original_action,
                    original_confidence,
                    final_action,
                    price_at_decision,
                    override_reason,
                ),
            )

    def update_counterfactual_prices(
        self,
        symbol: str,
        trade_date: str,
        price_after_1d: float | None = None,
        price_after_5d: float | None = None,
    ) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, original_action, price_at_decision, price_after_1d, price_after_5d
                FROM counterfactuals
                WHERE symbol = ? AND trade_date = ?
                """,
                (symbol, trade_date),
            ).fetchall()

            for row in rows:
                price_at_decision = row["price_at_decision"]
                action = str(row["original_action"]).upper()
                direction = 1.0 if action == "BUY" else -1.0
                next_price_1d = row["price_after_1d"] if price_after_1d is None else price_after_1d
                next_price_5d = row["price_after_5d"] if price_after_5d is None else price_after_5d
                pnl_1d = (
                    (next_price_1d - price_at_decision) * direction
                    if next_price_1d is not None and price_at_decision is not None
                    else None
                )
                pnl_5d = (
                    (next_price_5d - price_at_decision) * direction
                    if next_price_5d is not None and price_at_decision is not None
                    else None
                )
                connection.execute(
                    """
                    UPDATE counterfactuals
                    SET price_after_1d = ?,
                        price_after_5d = ?,
                        would_have_pnl_1d = ?,
                        would_have_pnl_5d = ?
                    WHERE id = ?
                    """,
                    (
                        next_price_1d,
                        next_price_5d,
                        pnl_1d,
                        pnl_5d,
                        row["id"],
                    ),
                )

    def get_recent_counterfactuals(
        self,
        symbol: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, run_id, symbol, trade_date, original_action, original_confidence, final_action,
                   price_at_decision, price_after_1d, price_after_5d, would_have_pnl_1d, would_have_pnl_5d,
                   override_reason, created_at
            FROM counterfactuals
            WHERE 1 = 1
        """
        params: list[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY trade_date DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_counterfactual_summary(self) -> dict[str, Any]:
        recent = self.get_recent_counterfactuals(limit=20)
        profitable = sum(1 for item in recent if (item.get("would_have_pnl_1d") or 0.0) > 0)
        unprofitable = sum(1 for item in recent if (item.get("would_have_pnl_1d") or 0.0) < 0)
        pnl_1d = [item["would_have_pnl_1d"] for item in recent if item.get("would_have_pnl_1d") is not None]
        pnl_5d = [item["would_have_pnl_5d"] for item in recent if item.get("would_have_pnl_5d") is not None]
        return {
            "total_overrides": len(recent),
            "profitable_overrides": profitable,
            "unprofitable_overrides": unprofitable,
            "avg_missed_pnl_1d": (sum(pnl_1d) / len(pnl_1d)) if pnl_1d else 0.0,
            "avg_missed_pnl_5d": (sum(pnl_5d) / len(pnl_5d)) if pnl_5d else 0.0,
        }

    def get_pending_counterfactuals(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, symbol, trade_date, original_action, original_confidence, final_action,
                       price_at_decision, price_after_1d, price_after_5d, would_have_pnl_1d, would_have_pnl_5d,
                       override_reason, created_at
                FROM counterfactuals
                WHERE price_after_1d IS NULL OR price_after_5d IS NULL
                ORDER BY trade_date ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, mode, trade_date, symbols_json, status, started_at, finished_at, summary_json, result_path, audit_path
                FROM analysis_runs
                ORDER BY started_at DESC, run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "run_id": row["run_id"],
                    "mode": row["mode"],
                    "trade_date": row["trade_date"],
                    "symbols": json.loads(row["symbols_json"]),
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "summary": json.loads(row["summary_json"] or "{}"),
                    "result_path": row["result_path"],
                    "audit_path": row["audit_path"],
                }
            )
        return items

    def get_recent_cycles(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT cycle_id, bucket_start, started_at, finished_at, status, symbols_json, summary_json, error
                FROM daemon_cycles
                ORDER BY bucket_start DESC, cycle_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "cycle_id": row["cycle_id"],
                    "bucket_start": row["bucket_start"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "symbols": json.loads(row["symbols_json"]),
                    "summary": json.loads(row["summary_json"] or "{}"),
                    "error": row["error"],
                }
            )
        return items

    def get_recent_daemon_errors(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT error, created_at
                FROM daemon_errors
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_news_items(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, title, source, url, published_at, summary, is_global, first_seen_at, last_seen_at, times_seen
                FROM news_items
                ORDER BY last_seen_at DESC, content_hash DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_agent_history(self, *, agent_id: str, limit: int) -> None:
        with self._connect() as connection:
            table_ordering = {
                "agent_decisions": "created_at",
                "agent_reflections": "created_at",
                "closed_trades": "closed_at",
                "performance_snapshots": "created_at",
            }
            for table, ordering_column in table_ordering.items():
                connection.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE agent_id = ?
                      AND id NOT IN (
                        SELECT id FROM {table}
                        WHERE agent_id = ?
                        ORDER BY {ordering_column} DESC, id DESC
                        LIMIT ?
                      )
                    """,
                    (agent_id, agent_id, limit),
                )

    def record_daemon_start(self, *, pid: int, hostname: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daemon_runs (pid, hostname, started_at, status)
                VALUES (?, ?, ?, ?)
                """,
                (pid, hostname, now, "running"),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO daemon_state
                (name, pid, hostname, paused, stop_requested, status, last_heartbeat_at, symbols_processed_json, updated_at)
                VALUES ('primary', ?, ?, 0, 0, ?, ?, '[]', ?)
                """,
                (pid, hostname, "starting", now, now),
            )

    def record_daemon_stop(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE daemon_runs
                SET finished_at = ?, status = ?
                WHERE id = (SELECT id FROM daemon_runs ORDER BY id DESC LIMIT 1)
                """,
                (now, "stopped"),
            )
            connection.execute(
                """
                UPDATE daemon_state
                SET status = ?, stop_requested = 0, updated_at = ?
                WHERE name = 'primary'
                """,
                ("stopped", now),
            )

    def update_daemon_state(self, heartbeat: DaemonHeartbeat) -> None:
        payload_json = heartbeat.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daemon_heartbeats (pid, status, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (heartbeat.pid, heartbeat.status, payload_json, heartbeat.last_heartbeat_at.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO daemon_state
                (name, pid, paused, stop_requested, status, last_heartbeat_at, last_cycle_started_at, last_cycle_completed_at, last_cycle_bucket, symbols_processed_json, last_error, updated_at)
                VALUES ('primary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pid = excluded.pid,
                    paused = excluded.paused,
                    stop_requested = excluded.stop_requested,
                    status = excluded.status,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_cycle_started_at = COALESCE(excluded.last_cycle_started_at, daemon_state.last_cycle_started_at),
                    last_cycle_completed_at = COALESCE(excluded.last_cycle_completed_at, daemon_state.last_cycle_completed_at),
                    last_cycle_bucket = COALESCE(excluded.last_cycle_bucket, daemon_state.last_cycle_bucket),
                    symbols_processed_json = excluded.symbols_processed_json,
                    last_error = COALESCE(excluded.last_error, daemon_state.last_error),
                    updated_at = excluded.updated_at
                """,
                (
                    heartbeat.pid,
                    1 if heartbeat.paused else 0,
                    1 if heartbeat.stop_requested else 0,
                    heartbeat.status,
                    heartbeat.last_heartbeat_at.isoformat(),
                    heartbeat.last_cycle_started_at.isoformat() if heartbeat.last_cycle_started_at else None,
                    heartbeat.last_cycle_completed_at.isoformat() if heartbeat.last_cycle_completed_at else None,
                    heartbeat.last_cycle_bucket,
                    json.dumps(heartbeat.symbols_processed),
                    heartbeat.last_error,
                    now,
                ),
            )

    def set_paused(self, paused: bool) -> None:
        self._update_daemon_flag("paused", 1 if paused else 0)

    def set_stop_requested(self, requested: bool) -> None:
        self._update_daemon_flag("stop_requested", 1 if requested else 0)

    def _update_daemon_flag(self, column: str, value: int) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO daemon_state (name, {column}, updated_at, symbols_processed_json)
                VALUES ('primary', ?, ?, '[]')
                ON CONFLICT(name) DO UPDATE SET
                    {column} = excluded.{column},
                    updated_at = excluded.updated_at
                """,
                (value, datetime.now(timezone.utc).isoformat()),
            )

    def get_daemon_state(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM daemon_state WHERE name = 'primary'"
            ).fetchone()
        return dict(row) if row else None

    def get_daemon_state_value(self, key: str) -> Any:
        state = self.get_daemon_state()
        if not state:
            return None
        return state.get(key)

    def record_daemon_error(self, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO daemon_errors (error, created_at) VALUES (?, ?)",
                (error, now),
            )
            connection.execute(
                """
                INSERT INTO daemon_state (name, last_error, updated_at, symbols_processed_json)
                VALUES ('primary', ?, ?, '[]')
                ON CONFLICT(name) DO UPDATE SET last_error = excluded.last_error, updated_at = excluded.updated_at
                """,
                (error, now),
            )

    def record_cycle_start(
        self,
        *,
        bucket_start: str,
        started_at: datetime,
        symbols: list[str],
    ) -> str:
        cycle_id = f"cycle-{bucket_start}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO daemon_cycles
                (cycle_id, bucket_start, started_at, status, symbols_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    bucket_start,
                    started_at.isoformat(),
                    "running",
                    json.dumps(symbols),
                ),
            )
        return cycle_id

    def record_cycle_end(
        self,
        *,
        cycle_id: str,
        finished_at: datetime,
        status: str,
        summary: dict[str, Any],
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE daemon_cycles
                SET finished_at = ?, status = ?, summary_json = ?, error = ?
                WHERE cycle_id = ?
                """,
                (
                    finished_at.isoformat(),
                    status,
                    json.dumps(summary, default=str),
                    error,
                    cycle_id,
                ),
            )

    def record_symbol_bucket(
        self,
        *,
        bucket_key: str,
        symbol: str,
        cycle_id: str,
        run_id: str,
        status: str,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO processed_symbol_buckets
                (bucket_start, symbol, cycle_id, run_id, status, error, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bucket_key,
                    symbol,
                    cycle_id,
                    run_id,
                    status,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def is_symbol_bucket_processed(self, *, bucket_key: str, symbol: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM processed_symbol_buckets
                WHERE bucket_start = ? AND symbol = ?
                LIMIT 1
                """,
                (bucket_key, symbol),
            ).fetchone()
        return row is not None

    def get_processed_symbols_for_bucket(self, *, bucket_key: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol FROM processed_symbol_buckets
                WHERE bucket_start = ?
                ORDER BY symbol
                """,
                (bucket_key,),
            ).fetchall()
        return [row["symbol"] for row in rows]

    def get_processed_symbols_since(self, *, since: datetime) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT symbol
                FROM processed_symbol_buckets
                WHERE processed_at >= ?
                """,
                (since.isoformat(),),
            ).fetchall()
        return {str(row["symbol"]).upper() for row in rows if row["symbol"]}

    def upsert_news_items(self, items: list[NewsItem]) -> list[NewsItem]:
        now = datetime.now(timezone.utc).isoformat()
        persisted: list[NewsItem] = []
        with self._connect() as connection:
            for item in items:
                row = connection.execute(
                    "SELECT content_hash FROM news_items WHERE content_hash = ?",
                    (item.content_hash,),
                ).fetchone()
                seen_before = row is not None
                connection.execute(
                    """
                    INSERT INTO news_items
                    (content_hash, symbol, title, source, url, published_at, summary, is_global, first_seen_at, last_seen_at, times_seen, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(content_hash) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        times_seen = news_items.times_seen + 1,
                        raw_json = excluded.raw_json
                    """,
                    (
                        item.content_hash,
                        item.symbol,
                        item.title,
                        item.source,
                        item.url,
                        item.published_at.isoformat() if item.published_at else None,
                        item.summary,
                        1 if item.is_global else 0,
                        now,
                        now,
                        1,
                        json.dumps(item.raw, default=str),
                    ),
                )
                persisted.append(item.model_copy(update={"seen_before": seen_before}))
        return persisted

    def record_cycle_context(
        self,
        *,
        cycle_id: str,
        bucket_start: str,
        context: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO cycle_context
                (cycle_id, bucket_start, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cycle_id, bucket_start, json.dumps(context, default=str), now),
            )
            for symbol, items in context.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO cycle_news
                        (bucket_start, content_hash, symbol, is_new, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            bucket_start,
                            item["content_hash"],
                            symbol if symbol != "_global" else None,
                            0 if item.get("seen_before") else 1,
                            now,
                        ),
                    )

    def daily_summary_exists(self, trade_date: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM daily_reports WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()
        return row is not None

    def record_daily_summary(self, *, trade_date: str, path: str, summary: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO daily_reports (trade_date, path, summary_json, generated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    trade_date,
                    path,
                    json.dumps(summary, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def build_daily_summary(self, trade_date: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            cycles = connection.execute(
                """
                SELECT summary_json FROM daemon_cycles
                WHERE substr(bucket_start, 1, 10) = ? AND status = 'completed'
                ORDER BY bucket_start
                """,
                (trade_date,),
            ).fetchall()
            pnl_rows = connection.execute(
                """
                SELECT equity, cash, realized_pnl, unrealized_pnl
                FROM daily_pnl_summaries
                WHERE trade_date = ?
                ORDER BY created_at
                """,
                (trade_date,),
            ).fetchall()
            orders = connection.execute(
                """
                SELECT symbol, side, status
                FROM broker_orders
                WHERE substr(submitted_at, 1, 10) = ?
                """,
                (trade_date,),
            ).fetchall()
            fills = connection.execute(
                """
                SELECT symbol FROM fills
                WHERE substr(created_at, 1, 10) = ?
                """,
                (trade_date,),
            ).fetchall()
            risks = connection.execute(
                """
                SELECT payload_json FROM risk_decisions
                WHERE substr(created_at, 1, 10) = ?
                """,
                (trade_date,),
            ).fetchall()
            news = connection.execute(
                """
                SELECT title, symbol, source FROM news_items
                WHERE substr(last_seen_at, 1, 10) = ?
                ORDER BY last_seen_at DESC
                LIMIT 20
                """,
                (trade_date,),
            ).fetchall()
            perf_rows = connection.execute(
                """
                SELECT payload_json FROM performance_snapshots
                WHERE trade_date = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (trade_date,),
            ).fetchall()
            learning_row = connection.execute(
                """
                SELECT summary_json FROM learning_state
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()

        if not cycles and not orders and not pnl_rows:
            return None

        symbols = set()
        issues: list[str] = []
        buys = sells = holds = 0
        for row in cycles:
            summary = json.loads(row["summary_json"])
            for item in summary.get("symbol_results", []):
                symbols.add(item.get("symbol"))
                status = item.get("execution_status", "")
                if status in {"submitted", "dry-run-approved"}:
                    action = (
                        item.get("submitted_order", {}) or {}
                    ).get("side")
                    if action == "BUY":
                        buys += 1
                    elif action == "SELL":
                        sells += 1
                    else:
                        holds += 1
                else:
                    holds += 1
                risk = item.get("risk_decision") or {}
                issues.extend(risk.get("reasons", []))
                if item.get("error"):
                    issues.append(item["error"])

        top_issues = list(dict.fromkeys(issues))[:5]
        starting_equity = pnl_rows[0]["equity"] if pnl_rows else 0.0
        ending_equity = pnl_rows[-1]["equity"] if pnl_rows else starting_equity
        realized = pnl_rows[-1]["realized_pnl"] if pnl_rows else 0.0
        unrealized = pnl_rows[-1]["unrealized_pnl"] if pnl_rows else 0.0

        news_lines = [
            f"- {row['symbol'] or 'GLOBAL'}: {row['title']} ({row['source']})"
            for row in news
        ]
        latest_performance = json.loads(perf_rows[0]["payload_json"]) if perf_rows else {}
        learning_state = json.loads(learning_row["summary_json"]) if learning_row else {}

        return {
            "cycles_run": len(cycles),
            "symbols_analyzed": sorted(symbol for symbol in symbols if symbol),
            "buys": buys,
            "sells": sells,
            "holds_or_rejections": holds,
            "orders_submitted": len(orders),
            "fills": len(fills),
            "starting_equity": starting_equity,
            "ending_equity": ending_equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "largest_winner": "Unavailable",
            "largest_loser": "Unavailable",
            "top_issues": top_issues,
            "news_lines": news_lines,
            "learning_summary": learning_state.get("learning_summary"),
            "win_rate": latest_performance.get("win_rate"),
            "average_win": latest_performance.get("average_win"),
            "average_loss": latest_performance.get("average_loss"),
            "max_drawdown": latest_performance.get("max_drawdown"),
            "trade_frequency": latest_performance.get("trade_frequency"),
        }

    def _insert_simple(self, table: str, *, run_id: str, symbol: str, raw_text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {table} (run_id, symbol, raw_text, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, symbol, raw_text, datetime.now(timezone.utc).isoformat()),
            )

    def _insert_json(
        self,
        table: str,
        *,
        run_id: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        symbol: str | None = None,
        symbolless: bool = False,
    ) -> None:
        columns = "(run_id, payload_json, created_at)" if symbolless else "(run_id, symbol, payload_json, created_at)"
        values = (run_id, json.dumps(payload, default=str), datetime.now(timezone.utc).isoformat()) if symbolless else (
            run_id,
            symbol,
            json.dumps(payload, default=str),
            datetime.now(timezone.utc).isoformat(),
        )
        placeholders = "(?, ?, ?)" if symbolless else "(?, ?, ?, ?)"
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {table} {columns} VALUES {placeholders}",
                values,
            )
