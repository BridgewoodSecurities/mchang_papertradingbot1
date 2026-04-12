# Monitoring Dashboard

## What It Shows

The built-in dashboard gives you a live view of the paper-trading system without changing the daemon itself.

It displays:

- daemon running state, pause state, pid, heartbeat, and last cycle bucket
- learning summary and per-symbol memory snapshots
- account equity, cash, open positions, and recent PnL
- recent paper orders, agent decisions, and reflections
- recent daemon cycles, analysis runs, news items, and daemon errors
- runtime artifact paths and live log tails

The page auto-refreshes and works even when the daemon is sleeping outside market hours.

## Run It

Local only:

```bash
tradingagents dashboard run
```

Expose it on your LAN or a remote host:

```bash
tradingagents dashboard run --host 0.0.0.0 --port 8000
```

Then open:

- `http://127.0.0.1:8000` on the same machine
- `http://<server-ip>:8000` from another machine when using `--host 0.0.0.0`

You can change how often the page polls:

```bash
tradingagents dashboard run --refresh-seconds 5
```

## What The Dashboard Reads

The dashboard is read-only. It pulls from the existing runtime state:

- sqlite DB: `./runtime/tradingagents.db`
- heartbeat file: `./runtime/daemon-heartbeat.json`
- logs: `./runtime/logs/`
- results: `./results/`

It does not submit trades, change daemon flags, or bypass any paper-trading safeguards.

## Recommended Usage

Run the daemon in one terminal:

```bash
tradingagents daemon run
```

Run the dashboard in another:

```bash
tradingagents dashboard run
```

Then check status quickly from the CLI whenever needed:

```bash
tradingagents daemon status
tradingagents daemon memory
tradingagents daemon heartbeat
```

## Keeping It Online

For a long-running paper setup, keep the dashboard and daemon in separate sessions.

Example with `tmux`:

```bash
tmux new -s tradingagents-daemon
tradingagents daemon run
```

In another session:

```bash
tmux new -s tradingagents-dashboard
tradingagents dashboard run --host 0.0.0.0 --port 8000
```

## Notes

- The dashboard uses a short cache for broker-backed status calls so it stays responsive without polling Alpaca on every page refresh.
- If the daemon is stopped, the page still shows the most recent persisted state.
- This dashboard is for paper trading only.
