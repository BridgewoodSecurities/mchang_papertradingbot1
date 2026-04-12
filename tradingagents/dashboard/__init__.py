from __future__ import annotations

from tradingagents.dashboard.vercel import build_unavailable_snapshot, fetch_remote_snapshot

__all__ = [
    "DashboardDataService",
    "DashboardServer",
    "build_dashboard_data_service",
    "build_unavailable_snapshot",
    "fetch_remote_snapshot",
]


def __getattr__(name: str):
    if name == "build_dashboard_data_service":
        from tradingagents.dashboard.runtime import build_dashboard_data_service

        return build_dashboard_data_service

    if name in {"DashboardDataService", "DashboardServer"}:
        from tradingagents.dashboard.server import DashboardDataService, DashboardServer

        exports = {
            "DashboardDataService": DashboardDataService,
            "DashboardServer": DashboardServer,
        }
        return exports[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
