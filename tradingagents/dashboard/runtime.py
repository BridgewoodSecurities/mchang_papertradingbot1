from __future__ import annotations

from pathlib import Path

from tradingagents.brokers.alpaca import AlpacaPaperBroker, BrokerConfigurationError
from tradingagents.daemon.service import DaemonService
from tradingagents.dashboard.server import DashboardDataService
from tradingagents.execution.config import load_execution_config, load_risk_config
from tradingagents.persistence.sqlite_store import SQLitePersistence


class StatusOnlyRunner:
    def __init__(self, risk_config):
        self.risk_config = risk_config


def build_dashboard_data_service(
    *,
    refresh_seconds: int,
    project_dir: str | None = None,
    include_broker: bool = True,
) -> DashboardDataService:
    project_dir = project_dir or str(Path.cwd())
    execution_config = load_execution_config(project_dir=project_dir, execute=True)
    risk_config = load_risk_config()
    store = SQLitePersistence(execution_config.db_path)
    broker = _build_broker(required=False) if include_broker else None
    daemon_service = DaemonService(
        execution_config=execution_config,
        store=store,
        runner=StatusOnlyRunner(risk_config),
        broker=broker,
    )
    return DashboardDataService(
        execution_config=execution_config,
        store=store,
        daemon_service=daemon_service,
        refresh_seconds=refresh_seconds,
    )


def _build_broker(*, required: bool):
    try:
        return AlpacaPaperBroker.from_env()
    except BrokerConfigurationError:
        if required:
            raise
        return None
