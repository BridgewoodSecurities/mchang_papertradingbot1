from __future__ import annotations

import json
import os
from typing import Callable

from tradingagents.dashboard.vercel import (
    INDEX_HTML,
    build_unavailable_snapshot,
    fetch_remote_snapshot,
)


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()

    if method != "GET":
        return _respond_json(start_response, 405, {"error": "Method not allowed"})

    if path in {"/", "/api/index", "/api/index.py"}:
        return _respond_html(start_response, INDEX_HTML)

    if path == "/healthz":
        return _respond_json(start_response, 200, {"ok": True, "target": _deployment_target()})

    if path == "/api/overview":
        snapshot = _load_snapshot()
        return _respond_json(start_response, 200, snapshot)

    return _respond_json(start_response, 404, {"error": "Not found", "path": path})


def _load_snapshot() -> dict:
    refresh_seconds = int(os.getenv("TRADINGAGENTS_VERCEL_REFRESH_SECONDS", "15"))
    proxy_url = os.getenv("TRADINGAGENTS_DASHBOARD_PROXY_URL")

    if proxy_url:
        try:
            return fetch_remote_snapshot(
                proxy_url=proxy_url,
                refresh_seconds=refresh_seconds,
                timeout=float(os.getenv("TRADINGAGENTS_DASHBOARD_PROXY_TIMEOUT", "10")),
            )
        except Exception as exc:
            return build_unavailable_snapshot(
                reason=f"Dashboard proxy fetch failed: {exc}",
                refresh_seconds=refresh_seconds,
                proxy_url=proxy_url,
            )

    try:
        service = _build_local_dashboard_data_service(refresh_seconds=refresh_seconds)
        return service.build_snapshot()
    except Exception as exc:
        return build_unavailable_snapshot(
            reason=f"No live dashboard runtime is available inside this Vercel deployment: {exc}",
            refresh_seconds=refresh_seconds,
            proxy_url=proxy_url,
        )


def _build_local_dashboard_data_service(*, refresh_seconds: int):
    from tradingagents.dashboard.runtime import build_dashboard_data_service

    return build_dashboard_data_service(
        refresh_seconds=refresh_seconds,
        project_dir=os.getcwd(),
        include_broker=os.getenv("TRADINGAGENTS_VERCEL_ENABLE_BROKER", "false").lower() == "true",
    )


def _deployment_target() -> str:
    proxy_url = os.getenv("TRADINGAGENTS_DASHBOARD_PROXY_URL")
    return "proxy" if proxy_url else "local"


def _respond_html(start_response: Callable, body: str):
    encoded = body.encode("utf-8")
    start_response(
        "200 OK",
        [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [encoded]


def _respond_json(start_response: Callable, status_code: int, payload: dict):
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reason = {
        200: "OK",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(status_code, "OK")
    start_response(
        f"{status_code} {reason}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [encoded]
