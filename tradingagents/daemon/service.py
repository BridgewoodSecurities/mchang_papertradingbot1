from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from tradingagents.execution.logging_utils import setup_logging
from tradingagents.execution.models import DaemonHeartbeat, DaemonStatus, RunMode
from tradingagents.news.context import ContextCacheService
from tradingagents.scheduler.market import MarketSession, get_market_date, is_market_open, is_trading_day
from tradingagents.scheduler.timing import align_to_bucket_start, next_bucket_start

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class DaemonService:
    def __init__(
        self,
        *,
        execution_config,
        store,
        runner,
        broker=None,
    ):
        self.execution_config = execution_config
        self.store = store
        self.runner = runner
        self.broker = broker
        self.session = MarketSession(
            timezone=execution_config.market_timezone,
            market_open_time=execution_config.market_open_time,
            market_close_time=execution_config.market_close_time,
            holidays=tuple(execution_config.market_holidays),
        )
        self.context_service = ContextCacheService(store)
        self.stop_requested = False
        self.current_cycle_bucket: str | None = None
        self._last_market_closed_log_at: datetime | None = None

    def run_forever(self) -> None:
        logger, log_path = setup_logging(self.execution_config, run_id="daemon")
        logger.info("daemon_starting", extra={"pid": os.getpid()})

        with self._exclusive_lock():
            self._write_pid_file()
            self._install_signal_handlers()
            self.store.record_daemon_start(pid=os.getpid(), hostname=socket.gethostname())
            self._write_heartbeat(status="starting")

            backoff_seconds = 0
            try:
                while not self.stop_requested:
                    heartbeat = self.store.get_daemon_state()
                    if heartbeat and heartbeat.get("stop_requested"):
                        break

                    now = datetime.now(timezone.utc)

                    if heartbeat and heartbeat.get("paused"):
                        self._write_heartbeat(status="paused")
                        self._sleep_interruptibly(self.execution_config.market_open_sleep_seconds)
                        continue

                    if not self._is_market_open(now):
                        self._maybe_generate_daily_summary(now=now)
                        self._write_heartbeat(status="market-closed")
                        self._maybe_log_market_closed(logger=logger, now=now)
                        self._sleep_interruptibly(self.execution_config.market_closed_sleep_seconds)
                        continue

                    try:
                        cycle_result = self.run_once(now=now)
                        backoff_seconds = 0
                        if cycle_result:
                            logger.info(
                                "daemon_cycle_complete",
                                extra={
                                    "bucket": cycle_result["bucket_start"],
                                    "symbols": cycle_result["symbols"],
                                    "status": cycle_result["status"],
                                },
                            )
                    except Exception as exc:  # pragma: no cover - top-level hardening
                        backoff_seconds = min(300, max(15, backoff_seconds * 2 or 15))
                        logger.exception("daemon_cycle_failed")
                        self.store.record_daemon_error(str(exc))
                        self._write_heartbeat(status="error", last_error=str(exc))
                        self._sleep_interruptibly(backoff_seconds)
                        continue

                    sleep_seconds = self._seconds_until_next_bucket(datetime.now(timezone.utc))
                    self._sleep_interruptibly(
                        min(sleep_seconds, self.execution_config.market_open_sleep_seconds)
                    )
            finally:
                self.store.record_daemon_stop()
                self._write_heartbeat(status="stopped")
                self._remove_pid_file()
                logger.info("daemon_stopped", extra={"log_path": str(log_path)})

    def run_once(self, *, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        bucket_start = align_to_bucket_start(
            now,
            interval_minutes=self.execution_config.scheduler_interval_minutes,
            session=self.session,
        )
        bucket_key = bucket_start.astimezone(timezone.utc).isoformat()
        market_date = get_market_date(bucket_start, self.session)

        self._write_heartbeat(
            status="idle",
            last_cycle_bucket=bucket_key,
        )

        if not is_trading_day(bucket_start, self.session):
            self._maybe_generate_daily_summary(now=now)
            return {"bucket_start": bucket_key, "symbols": [], "status": "market-closed"}

        if self.execution_config.paper_trading_enabled and self._kill_switch_active():
            execute = False
        else:
            execute = self.execution_config.paper_trading_enabled

        if self.execution_config.scheduler_interval_minutes <= 0:
            raise ValueError("scheduler interval must be positive")

        if not is_market_open(bucket_start, self.session):
            self._maybe_generate_daily_summary(now=now)
            return {"bucket_start": bucket_key, "symbols": [], "status": "outside-market-hours"}

        watchlist = self.execution_config.watchlist[: self.execution_config.max_symbols_per_cycle]
        remaining_symbols = [
            symbol
            for symbol in watchlist
            if not self.store.is_symbol_bucket_processed(bucket_key=bucket_key, symbol=symbol)
        ]
        if not remaining_symbols:
            return {"bucket_start": bucket_key, "symbols": [], "status": "already-processed"}

        cycle_id = self.store.record_cycle_start(
            bucket_start=bucket_key,
            started_at=now,
            symbols=remaining_symbols,
        )
        self._write_heartbeat(
            status="running",
            last_cycle_started_at=now,
            last_cycle_bucket=bucket_key,
        )

        cycle_context = self.context_service.fetch_cycle_context(symbols=remaining_symbols)
        self.store.record_cycle_context(
            cycle_id=cycle_id,
            bucket_start=bucket_key,
            context={
                key: [item.model_dump(mode="json") for item in value]
                for key, value in cycle_context.items()
            },
        )

        result = self.runner.run_cycle(
            symbols=remaining_symbols,
            analysis_date=market_date,
            mode=RunMode.DAEMON,
            execute=execute,
            cycle_bucket=bucket_key,
            cycle_context={
                key: [item.model_dump(mode="json") for item in value]
                for key, value in cycle_context.items()
            },
            cycle_timestamp=now,
        )

        processed_symbols: list[str] = []
        for item in result.symbol_results:
            self.store.record_symbol_bucket(
                bucket_key=bucket_key,
                symbol=item.symbol,
                cycle_id=cycle_id,
                run_id=result.run_id,
                status=item.execution_status,
                error=item.error,
            )
            processed_symbols.append(item.symbol)

        self._record_post_cycle_state(run_id=result.run_id)
        self.store.record_cycle_end(
            cycle_id=cycle_id,
            finished_at=datetime.now(timezone.utc),
            status="completed",
            summary=result.model_dump(mode="json"),
        )
        self._write_heartbeat(
            status="idle",
            last_cycle_completed_at=datetime.now(timezone.utc),
            last_cycle_bucket=bucket_key,
            symbols_processed=processed_symbols,
        )
        return {"bucket_start": bucket_key, "symbols": processed_symbols, "status": "completed"}

    def get_status(self) -> DaemonStatus:
        state = self.store.get_daemon_state()
        running = False
        pid = None
        trade_date = get_market_date(datetime.now(timezone.utc), self.session)
        trades_today = self.store.count_trades_for_date(trade_date=trade_date)
        trades_per_symbol_today = self.store.get_trades_per_symbol_for_date(trade_date=trade_date)
        daily_trade_cap_reached = (
            trades_today >= self.runner.risk_config.max_daily_trades
        )
        if state:
            pid = state.get("pid")
            running = bool(pid and self._pid_is_running(pid))

        account = None
        positions = []
        if self.broker is not None:
            try:
                account = self.broker.get_account()
                positions = self.broker.list_positions()
            except Exception:
                account = None
                positions = []

        return DaemonStatus(
            running=running,
            pid=pid,
            last_heartbeat_at=self._parse_datetime(state.get("last_heartbeat_at")) if state else None,
            last_cycle_started_at=self._parse_datetime(state.get("last_cycle_started_at")) if state else None,
            last_cycle_completed_at=self._parse_datetime(state.get("last_cycle_completed_at")) if state else None,
            last_cycle_bucket=state.get("last_cycle_bucket") if state else None,
            symbols_processed=json.loads(state.get("symbols_processed_json", "[]")) if state else [],
            last_error=state.get("last_error") if state else None,
            paused=bool(state.get("paused")) if state else False,
            stop_requested=bool(state.get("stop_requested")) if state else False,
            account=account,
            open_positions=positions,
            learning_summary=(
                self.store.get_learning_state(agent_id=self.execution_config.agent_id) or {}
            ).get("learning_summary"),
            performance_snapshot=self.store.get_latest_performance_snapshot(
                agent_id=self.execution_config.agent_id
            ),
            trades_today=trades_today,
            trades_per_symbol_today=trades_per_symbol_today,
            daily_trade_cap_reached=daily_trade_cap_reached,
        )

    def _record_post_cycle_state(self, *, run_id: str) -> None:
        if self.broker is None:
            return
        try:
            account = self.broker.get_account()
            positions = self.broker.list_positions()
        except Exception:
            return
        self.store.snapshot_equity(
            run_id=run_id,
            equity=account.equity,
            cash=account.cash,
            payload=account.model_dump(mode="json"),
        )
        self.store.snapshot_positions(
            run_id=run_id,
            payload=[position.model_dump(mode="json") for position in positions],
        )

    def _kill_switch_active(self) -> bool:
        return Path(self.execution_config.kill_switch_path).exists()

    def _is_market_open(self, now: datetime) -> bool:
        return is_market_open(now, self.session)

    def _seconds_until_next_bucket(self, now: datetime) -> int:
        next_start = next_bucket_start(
            now,
            interval_minutes=self.execution_config.scheduler_interval_minutes,
            session=self.session,
        )
        delta = (next_start - now).total_seconds()
        return max(1, int(delta))

    def _sleep_interruptibly(self, seconds: int) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and not self.stop_requested:
            time.sleep(min(1.0, deadline - time.time()))

    def _maybe_log_market_closed(self, *, logger, now: datetime) -> None:
        throttle_seconds = max(60, self.execution_config.market_closed_sleep_seconds)
        if (
            self._last_market_closed_log_at is not None
            and (now - self._last_market_closed_log_at).total_seconds() < throttle_seconds
        ):
            return
        local_now = now.astimezone(self.session.tzinfo)
        logger.info(
            "market closed — sleeping",
            extra={
                "local_time": local_now.isoformat(),
                "sleep_seconds": self.execution_config.market_closed_sleep_seconds,
            },
        )
        self._last_market_closed_log_at = now

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum, frame):  # pragma: no cover - exercised indirectly
            self.stop_requested = True
            self.store.set_stop_requested(True)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        lock_path = Path(self.execution_config.daemon_lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w", encoding="utf-8")
        if fcntl is None:  # pragma: no cover
            yield
            handle.close()
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("Daemon lock is already held by another process.") from exc

        try:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _write_pid_file(self) -> None:
        path = Path(self.execution_config.daemon_pid_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pid_file(self) -> None:
        path = Path(self.execution_config.daemon_pid_path)
        if path.exists():
            path.unlink()

    def _write_heartbeat(
        self,
        *,
        status: str,
        last_cycle_started_at: datetime | None = None,
        last_cycle_completed_at: datetime | None = None,
        last_cycle_bucket: str | None = None,
        symbols_processed: list[str] | None = None,
        last_error: str | None = None,
    ) -> None:
        heartbeat = DaemonHeartbeat(
            pid=os.getpid(),
            status=status,
            last_heartbeat_at=datetime.now(timezone.utc),
            last_cycle_started_at=last_cycle_started_at,
            last_cycle_completed_at=last_cycle_completed_at,
            last_cycle_bucket=last_cycle_bucket,
            symbols_processed=symbols_processed or [],
            last_error=last_error,
            paused=bool(self.store.get_daemon_state_value("paused")),
            stop_requested=bool(self.store.get_daemon_state_value("stop_requested")),
        )
        self.store.update_daemon_state(heartbeat)
        heartbeat_path = Path(self.execution_config.daemon_heartbeat_path)
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_text(heartbeat.model_dump_json(indent=2), encoding="utf-8")

    def _maybe_generate_daily_summary(self, *, now: datetime) -> None:
        trade_date = get_market_date(now, self.session)
        if self.store.daily_summary_exists(trade_date):
            return
        local_now = now.astimezone(self.session.tzinfo)
        if local_now.time() < self.session.close_time:
            return
        summary_dir = Path(self.execution_config.daily_summary_dir)
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary = self.store.build_daily_summary(trade_date)
        if summary is None:
            return
        path = summary_dir / f"{trade_date}.md"
        lines = [
            f"# Daily Paper Trading Summary - {trade_date}",
            "",
            f"- Cycles run: {summary['cycles_run']}",
            f"- Symbols analyzed: {', '.join(summary['symbols_analyzed']) or 'None'}",
            f"- Buys: {summary['buys']}",
            f"- Sells: {summary['sells']}",
            f"- Holds/Rejections: {summary['holds_or_rejections']}",
            f"- Orders submitted: {summary['orders_submitted']}",
            f"- Fills: {summary['fills']}",
            f"- Starting equity: ${summary['starting_equity']:,.2f}",
            f"- Ending equity: ${summary['ending_equity']:,.2f}",
            f"- Realized PnL: ${summary['realized_pnl']:,.2f}",
            f"- Unrealized PnL: ${summary['unrealized_pnl']:,.2f}",
            f"- Win rate: {summary['win_rate']:.1%}" if isinstance(summary.get("win_rate"), (int, float)) else "- Win rate: -",
            f"- Average win: ${summary['average_win']:,.2f}" if isinstance(summary.get("average_win"), (int, float)) else "- Average win: -",
            f"- Average loss: ${summary['average_loss']:,.2f}" if isinstance(summary.get("average_loss"), (int, float)) else "- Average loss: -",
            f"- Max drawdown: {summary['max_drawdown']:.2%}" if isinstance(summary.get("max_drawdown"), (int, float)) else "- Max drawdown: -",
            f"- Trade frequency: {summary['trade_frequency']:.2f}" if isinstance(summary.get("trade_frequency"), (int, float)) else "- Trade frequency: -",
            f"- Largest winner: {summary['largest_winner']}",
            f"- Largest loser: {summary['largest_loser']}",
            "",
            "## Learning Summary",
            summary.get("learning_summary") or "No durable learning summary yet.",
            "",
            "## Common Issues",
        ]
        issues = summary["top_issues"] or ["None"]
        lines.extend(f"- {issue}" for issue in issues)
        lines.extend(["", "## Recent News"])
        news_lines = summary["news_lines"] or ["- None"]
        lines.extend(news_lines)
        path.write_text("\n".join(lines), encoding="utf-8")
        self.store.record_daily_summary(trade_date=trade_date, path=str(path), summary=summary)

    def _pid_is_running(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value)
