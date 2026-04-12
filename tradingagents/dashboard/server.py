from __future__ import annotations

import json
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from tradingagents.arena.memory import AgentMemoryService


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _tail_file(path: Path, *, lines: int = 80) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return {
        "path": str(path),
        "lines": content[-lines:],
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
    }


class DashboardDataService:
    def __init__(
        self,
        *,
        execution_config,
        store,
        daemon_service,
        refresh_seconds: int = 5,
        status_cache_seconds: int = 15,
    ):
        self.execution_config = execution_config
        self.store = store
        self.daemon_service = daemon_service
        self.refresh_seconds = refresh_seconds
        self.status_cache_seconds = status_cache_seconds
        self.memory_service = AgentMemoryService(
            store=store,
            agent_id=execution_config.agent_id,
            memory_limit=execution_config.agent_memory_limit,
        )
        self._cached_status: Any = None
        self._cached_status_at: datetime | None = None

    def build_snapshot(self) -> dict[str, Any]:
        status = self._get_status()
        heartbeat = self._read_heartbeat()
        recent_decisions = self.store.get_recent_agent_decisions(
            agent_id=self.execution_config.agent_id,
            limit=20,
        )
        recent_reflections = self.store.get_recent_reflections(
            agent_id=self.execution_config.agent_id,
            limit=20,
        )
        recent_orders = self.store.get_recent_broker_orders(limit=20)
        recent_closed_trades = self.store.get_recent_closed_trades(
            agent_id=self.execution_config.agent_id,
            limit=10,
        )
        learning_state = self.store.get_learning_state(agent_id=self.execution_config.agent_id) or {}
        memory_symbols = self._memory_symbols(
            status=status,
            recent_decisions=recent_decisions,
            recent_orders=recent_orders,
        )
        symbol_memory = {
            symbol: self.memory_service.build_snapshot(symbol=symbol).model_dump(mode="json")
            for symbol in memory_symbols
        }
        return {
            "generated_at": datetime.now().astimezone().isoformat(),
            "refresh_seconds": self.refresh_seconds,
            "overview": {
                "daemon_status": status.model_dump(mode="json"),
                "heartbeat": heartbeat,
                "artifacts": {
                    "db_path": self.execution_config.db_path,
                    "log_dir": self.execution_config.log_dir,
                    "audit_dir": self.execution_config.audit_dir,
                    "results_dir": self.execution_config.results_dir,
                    "heartbeat_path": self.execution_config.daemon_heartbeat_path,
                    "daily_summary_dir": self.execution_config.daily_summary_dir,
                },
            },
            "learning_state": learning_state,
            "symbol_memory": symbol_memory,
            "positions": _json_safe(status.open_positions),
            "account": _json_safe(status.account),
            "performance_snapshot": _json_safe(status.performance_snapshot),
            "recent_pnl": self.store.get_recent_pnl(limit=10),
            "recent_orders": recent_orders,
            "recent_closed_trades": recent_closed_trades,
            "recent_decisions": recent_decisions,
            "recent_reflections": recent_reflections,
            "recent_cycles": self.store.get_recent_cycles(limit=20),
            "recent_runs": self.store.get_recent_runs(limit=20),
            "recent_errors": self.store.get_recent_daemon_errors(limit=20),
            "recent_news": self.store.get_recent_news_items(limit=20),
            "logs": self._log_tails(),
        }

    def render_index(self) -> str:
        return INDEX_HTML

    def _get_status(self):
        now = datetime.now()
        if (
            self._cached_status is not None
            and self._cached_status_at is not None
            and (now - self._cached_status_at).total_seconds() < self.status_cache_seconds
        ):
            return self._cached_status
        self._cached_status = self.daemon_service.get_status()
        self._cached_status_at = now
        return self._cached_status

    def _memory_symbols(
        self,
        *,
        status,
        recent_decisions: list[dict[str, Any]],
        recent_orders: list[dict[str, Any]],
    ) -> list[str]:
        symbols: list[str] = []
        candidates: list[str] = []
        candidates.extend(self.execution_config.watchlist)
        candidates.extend(position.symbol for position in status.open_positions)
        candidates.extend(item.get("symbol") for item in recent_decisions if item.get("symbol"))
        candidates.extend(item.get("symbol") for item in recent_orders if item.get("symbol"))
        for symbol in candidates:
            if not symbol:
                continue
            normalized = symbol.strip().upper()
            if normalized and normalized not in symbols:
                symbols.append(normalized)
        return symbols[: min(6, max(1, self.execution_config.max_symbols_per_cycle))]

    def _read_heartbeat(self) -> dict[str, Any] | None:
        path = Path(self.execution_config.daemon_heartbeat_path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _log_tails(self) -> dict[str, Any]:
        log_dir = Path(self.execution_config.log_dir)
        daemon_stdout = _tail_file(log_dir / "daemon.stdout.log", lines=80)
        latest_runtime_log = None
        candidates = [
            path
            for path in log_dir.glob("*.log")
            if path.is_file() and path.name != "daemon.stdout.log"
        ]
        if candidates:
            latest_runtime_log = _tail_file(
                max(candidates, key=lambda item: item.stat().st_mtime),
                lines=80,
            )
        return {
            "daemon_stdout": daemon_stdout,
            "latest_runtime_log": latest_runtime_log,
        }


class DashboardServer:
    def __init__(
        self,
        *,
        data_service: DashboardDataService,
        host: str = "127.0.0.1",
        port: int = 8000,
    ):
        self.data_service = data_service
        self.host = host
        self.port = port
        self.httpd = ThreadingHTTPServer((host, port), self._handler())

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        data_service = self.data_service

        class RequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(data_service.render_index())
                    return
                if parsed.path == "/api/overview":
                    self._send_json(data_service.build_snapshot())
                    return
                if parsed.path == "/healthz":
                    self._send_json({"ok": True, "timestamp": datetime.now().isoformat()})
                    return
                self.send_error(404, "Not found")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def _send_html(self, body: str) -> None:
                encoded = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def _send_json(self, payload: dict[str, Any]) -> None:
                encoded = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

        return RequestHandler


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TradingAgents Monitor</title>
  <style>
    :root {
      --bg: #f4efe6;
      --bg-accent: linear-gradient(135deg, rgba(255,255,255,0.8), rgba(224,239,230,0.95));
      --card: rgba(255,255,255,0.82);
      --ink: #16211d;
      --muted: #5c6a64;
      --line: rgba(22,33,29,0.12);
      --accent: #126149;
      --accent-soft: #d7eee5;
      --warn: #a85d20;
      --danger: #9a2a2a;
      --shadow: 0 18px 40px rgba(22, 33, 29, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(213,230,221,0.8), transparent 30%),
        radial-gradient(circle at top right, rgba(255,220,188,0.55), transparent 25%),
        var(--bg);
      font-family: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
    }
    header {
      padding: 28px 24px 18px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: clamp(1.8rem, 3vw, 2.8rem);
      letter-spacing: -0.04em;
    }
    .subhead {
      color: var(--muted);
      max-width: 68rem;
      line-height: 1.5;
    }
    main {
      padding: 0 20px 24px;
      display: grid;
      gap: 18px;
    }
    .hero, .panel, .raw-panel {
      background: var(--bg-accent);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }
    .hero {
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .hero-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .badge-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.74);
      border: 1px solid var(--line);
      font-size: 0.92rem;
    }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
    }
    .status-dot.warn { background: var(--warn); }
    .status-dot.danger { background: var(--danger); }
    .metrics {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    }
    .metric {
      padding: 14px;
      border-radius: 18px;
      background: var(--card);
      border: 1px solid var(--line);
    }
    .metric-label {
      font-size: 0.82rem;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .metric-value {
      font-size: 1.25rem;
      font-weight: 700;
    }
    .grid {
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }
    .panel {
      padding: 16px;
      overflow: hidden;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 1.02rem;
      letter-spacing: -0.02em;
    }
    .panel-copy {
      color: var(--muted);
      line-height: 1.45;
      white-space: pre-wrap;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.92rem;
    }
    th, td {
      text-align: left;
      padding: 9px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    td code {
      font-size: 0.85rem;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 4px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.82rem;
      font-weight: 600;
    }
    .list {
      display: grid;
      gap: 10px;
    }
    .list-item {
      padding: 12px;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
    }
    .list-item strong {
      display: block;
      margin-bottom: 4px;
    }
    .raw-panel {
      padding: 16px;
    }
    details {
      background: rgba(255,255,255,0.68);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px 14px;
    }
    summary {
      cursor: pointer;
      font-weight: 600;
    }
    pre {
      margin: 12px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.84rem;
      line-height: 1.4;
      color: #21312b;
    }
    a { color: var(--accent); }
    .muted { color: var(--muted); }
    @media (max-width: 720px) {
      header { padding: 22px 16px 12px; }
      main { padding: 0 14px 20px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>TradingAgents Monitor</h1>
    <div class="subhead">
      Live dashboard for the paper-trading daemon. It auto-refreshes so you can watch heartbeat changes, memory updates,
      positions, orders, reflections, cycles, news, and recent errors without tailing files by hand.
    </div>
  </header>
  <main>
    <section class="hero">
      <div class="hero-top">
        <div class="badge-row" id="badges"></div>
        <div class="muted" id="refresh-label">Loading…</div>
      </div>
      <div class="metrics" id="metrics"></div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Learning Summary</h2>
        <div class="panel-copy" id="learning-summary">Loading…</div>
      </div>
      <div class="panel">
        <h2>Heartbeat</h2>
        <div class="panel-copy" id="heartbeat-panel">Loading…</div>
      </div>
      <div class="panel">
        <h2>Account</h2>
        <div id="account-table"></div>
      </div>
      <div class="panel">
        <h2>Open Positions</h2>
        <div id="positions-table"></div>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Recent Orders</h2>
        <div id="orders-table"></div>
      </div>
      <div class="panel">
        <h2>Recent Decisions</h2>
        <div id="decisions-table"></div>
      </div>
      <div class="panel">
        <h2>Recent Reflections</h2>
        <div id="reflections-list"></div>
      </div>
      <div class="panel">
        <h2>Recent News</h2>
        <div id="news-list"></div>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Cycles</h2>
        <div id="cycles-table"></div>
      </div>
      <div class="panel">
        <h2>Runs</h2>
        <div id="runs-table"></div>
      </div>
      <div class="panel">
        <h2>Errors</h2>
        <div id="errors-list"></div>
      </div>
      <div class="panel">
        <h2>Daily PnL</h2>
        <div id="pnl-table"></div>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Symbol Memory</h2>
        <div id="memory-grid" class="list"></div>
      </div>
      <div class="panel">
        <h2>Artifacts</h2>
        <div id="artifacts-list" class="list"></div>
      </div>
    </section>

    <section class="raw-panel">
      <details open>
        <summary>Runtime Log Tails</summary>
        <div id="logs-panel"></div>
      </details>
      <details>
        <summary>Raw Snapshot JSON</summary>
        <pre id="raw-json"></pre>
      </details>
    </section>
  </main>

  <script>
    const state = { refreshSeconds: 5, timer: null };

    function esc(value) {
      return String(value ?? "-")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function fmtTime(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return esc(value);
      return date.toLocaleString();
    }

    function fmtMoney(value) {
      if (value === null || value === undefined || value === "") return "-";
      const number = Number(value);
      if (Number.isNaN(number)) return esc(value);
      return number.toLocaleString(undefined, { style: "currency", currency: "USD" });
    }

    function fmtPercent(value) {
      if (value === null || value === undefined || value === "") return "-";
      const number = Number(value);
      if (Number.isNaN(number)) return esc(value);
      return `${(number * 100).toFixed(1)}%`;
    }

    function renderTable(columns, rows) {
      if (!rows || rows.length === 0) {
        return `<div class="muted">No rows yet.</div>`;
      }
      const header = columns.map((column) => `<th>${esc(column.label)}</th>`).join("");
      const body = rows.map((row) => {
        const cells = columns.map((column) => `<td>${column.render ? column.render(row) : esc(row[column.key])}</td>`).join("");
        return `<tr>${cells}</tr>`;
      }).join("");
      return `<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function renderList(items, renderItem) {
      if (!items || items.length === 0) {
        return `<div class="muted">Nothing to show yet.</div>`;
      }
      return items.map((item) => `<div class="list-item">${renderItem(item)}</div>`).join("");
    }

    function setHTML(id, html) {
      const node = document.getElementById(id);
      if (node) node.innerHTML = html;
    }

    function updateDashboard(snapshot) {
      state.refreshSeconds = snapshot.refresh_seconds || 5;
      document.getElementById("refresh-label").textContent =
        `Updated ${fmtTime(snapshot.generated_at)} • polling every ${state.refreshSeconds}s`;

      const status = snapshot.overview?.daemon_status || {};
      const heartbeat = snapshot.overview?.heartbeat || {};
      const performance = snapshot.performance_snapshot || {};

      const badges = [
        { label: `Daemon ${status.running ? "running" : "stopped"}`, tone: status.running ? "" : "warn" },
        { label: `Status ${status.last_error ? "error" : (heartbeat.status || "unknown")}`, tone: status.last_error ? "danger" : "" },
        { label: `Paused ${status.paused ? "yes" : "no"}`, tone: status.paused ? "warn" : "" },
        { label: `Trades today ${status.trades_today ?? 0}`, tone: status.daily_trade_cap_reached ? "warn" : "" },
      ];
      setHTML("badges", badges.map((badge) =>
        `<div class="badge"><span class="status-dot ${badge.tone || ""}"></span>${esc(badge.label)}</div>`
      ).join(""));

      const metrics = [
        { label: "Heartbeat", value: fmtTime(status.last_heartbeat_at) },
        { label: "Last Bucket", value: esc(status.last_cycle_bucket || "-") },
        { label: "Equity", value: fmtMoney(snapshot.account?.equity ?? performance.account_value) },
        { label: "Cash", value: fmtMoney(snapshot.account?.cash) },
        { label: "Open Positions", value: esc((snapshot.positions || []).length) },
        { label: "Win Rate", value: fmtPercent(performance.win_rate) },
        { label: "Realized PnL", value: fmtMoney(performance.realized_pnl) },
        { label: "Unrealized PnL", value: fmtMoney(performance.unrealized_pnl) },
      ];
      setHTML("metrics", metrics.map((metric) =>
        `<div class="metric"><div class="metric-label">${esc(metric.label)}</div><div class="metric-value">${metric.value}</div></div>`
      ).join(""));

      setHTML(
        "learning-summary",
        esc(snapshot.learning_state?.learning_summary || status.learning_summary || "No durable lessons recorded yet.")
      );

      setHTML(
        "heartbeat-panel",
        [
          `PID: ${esc(status.pid || "-")}`,
          `Last heartbeat: ${fmtTime(status.last_heartbeat_at)}`,
          `Last cycle start: ${fmtTime(status.last_cycle_started_at)}`,
          `Last cycle end: ${fmtTime(status.last_cycle_completed_at)}`,
          `Processed symbols: ${esc((status.symbols_processed || []).join(", ") || "-")}`,
          `Last error: ${esc(status.last_error || "-")}`
        ].join("\\n")
      );

      setHTML("account-table", renderTable(
        [
          { label: "Field", key: "field" },
          { label: "Value", key: "value" }
        ],
        [
          { field: "Account ID", value: snapshot.account?.account_id || "-" },
          { field: "Status", value: snapshot.account?.status || "-" },
          { field: "Equity", value: fmtMoney(snapshot.account?.equity) },
          { field: "Cash", value: fmtMoney(snapshot.account?.cash) },
          { field: "Buying Power", value: fmtMoney(snapshot.account?.buying_power) },
          { field: "Paper", value: snapshot.account?.paper ? "Yes" : "No" },
        ]
      ));

      setHTML("positions-table", renderTable(
        [
          { label: "Symbol", key: "symbol" },
          { label: "Qty", render: (row) => esc(row.qty) },
          { label: "Entry", render: (row) => fmtMoney(row.avg_entry_price) },
          { label: "Value", render: (row) => fmtMoney(row.market_value) },
          { label: "UPnL", render: (row) => fmtMoney(row.unrealized_pl) },
        ],
        snapshot.positions || []
      ));

      setHTML("orders-table", renderTable(
        [
          { label: "Time", render: (row) => fmtTime(row.submitted_at) },
          { label: "Symbol", key: "symbol" },
          { label: "Side", render: (row) => `<span class="pill">${esc(row.side)}</span>` },
          { label: "Status", key: "status" },
          { label: "Size", render: (row) => row.qty ? esc(row.qty) : fmtMoney(row.notional_usd) },
        ],
        snapshot.recent_orders || []
      ));

      setHTML("decisions-table", renderTable(
        [
          { label: "Time", render: (row) => fmtTime(row.created_at) },
          { label: "Symbol", key: "symbol" },
          { label: "Action", render: (row) => `<span class="pill">${esc(row.action)}</span>` },
          { label: "Confidence", render: (row) => row.confidence !== null && row.confidence !== undefined ? esc(Number(row.confidence).toFixed(2)) : "-" },
          { label: "Signals", render: (row) => esc((row.supporting_signals || []).join(", ") || "-") },
        ],
        snapshot.recent_decisions || []
      ));

      setHTML("reflections-list", renderList(snapshot.recent_reflections || [], (item) => `
        <strong>${esc(item.symbol)} • ${fmtTime(item.created_at)}</strong>
        <div>${esc(item.lesson || item.what_changed || "No reflection text.")}</div>
      `));

      setHTML("news-list", renderList(snapshot.recent_news || [], (item) => `
        <strong>${esc(item.symbol || "GLOBAL")} • ${fmtTime(item.published_at || item.last_seen_at)}</strong>
        <div>${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.title)}</a>` : esc(item.title)}</div>
        <div class="muted">${esc(item.source || "-")}</div>
      `));

      setHTML("cycles-table", renderTable(
        [
          { label: "Bucket", render: (row) => `<code>${esc(row.bucket_start)}</code>` },
          { label: "Status", key: "status" },
          { label: "Symbols", render: (row) => esc((row.symbols || []).join(", ") || "-") },
          { label: "Finished", render: (row) => fmtTime(row.finished_at) },
        ],
        snapshot.recent_cycles || []
      ));

      setHTML("runs-table", renderTable(
        [
          { label: "Run ID", render: (row) => `<code>${esc(row.run_id)}</code>` },
          { label: "Mode", key: "mode" },
          { label: "Trade Date", key: "trade_date" },
          { label: "Status", key: "status" },
        ],
        snapshot.recent_runs || []
      ));

      setHTML("errors-list", renderList(snapshot.recent_errors || [], (item) => `
        <strong>${fmtTime(item.created_at)}</strong>
        <div>${esc(item.error)}</div>
      `));

      setHTML("pnl-table", renderTable(
        [
          { label: "Date", key: "trade_date" },
          { label: "Equity", render: (row) => fmtMoney(row.equity) },
          { label: "Realized", render: (row) => fmtMoney(row.realized_pnl) },
          { label: "Unrealized", render: (row) => fmtMoney(row.unrealized_pnl) },
          { label: "Exposure", render: (row) => fmtMoney(row.gross_exposure) },
        ],
        snapshot.recent_pnl || []
      ));

      const memoryEntries = Object.entries(snapshot.symbol_memory || {});
      setHTML("memory-grid", renderList(memoryEntries, ([symbol, memory]) => `
        <strong>${esc(symbol)}</strong>
        <div class="muted">Latest reasoning: ${esc(memory.previous_reasoning || "-")}</div>
        <div>Lessons: ${esc((memory.recurring_mistakes || []).slice(0, 2).join(" | ") || "-")}</div>
        <div>Success patterns: ${esc((memory.recurring_success_patterns || []).slice(0, 2).join(" | ") || "-")}</div>
        <div>Recent decisions: ${esc((memory.recent_decisions || []).slice(0, 3).map((item) => `${item.action}:${item.symbol}`).join(", ") || "-")}</div>
      `));

      const artifacts = snapshot.overview?.artifacts || {};
      setHTML("artifacts-list", renderList(Object.entries(artifacts), ([key, value]) => `
        <strong>${esc(key)}</strong>
        <div><code>${esc(value)}</code></div>
      `));

      const logBlocks = Object.entries(snapshot.logs || {})
        .filter(([, value]) => value && value.lines)
        .map(([key, value]) => `
          <details open>
            <summary>${esc(key)} • <code>${esc(value.path)}</code></summary>
            <pre>${esc((value.lines || []).join("\\n"))}</pre>
          </details>
        `)
        .join("");
      setHTML("logs-panel", logBlocks || `<div class="muted">No log files found yet.</div>`);

      document.getElementById("raw-json").textContent = JSON.stringify(snapshot, null, 2);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/overview", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const snapshot = await response.json();
        updateDashboard(snapshot);
      } catch (error) {
        document.getElementById("refresh-label").textContent = `Dashboard fetch failed: ${error}`;
      } finally {
        clearTimeout(state.timer);
        state.timer = setTimeout(refresh, state.refreshSeconds * 1000);
      }
    }

    refresh();
  </script>
</body>
</html>
"""
