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

## Deploy To Vercel

This repo now includes a Vercel-compatible Python entrypoint at `api/index.py` plus `vercel.json`, so the dashboard can deploy without the "No python entrypoint found" error.

There are two deployment modes:

1. UI-only or placeholder mode
   The Vercel deployment serves the dashboard shell successfully, but it cannot read your local sqlite runtime by itself.

2. Proxy mode
   Set `TRADINGAGENTS_DASHBOARD_PROXY_URL` in Vercel to a public dashboard backend that exposes `/api/overview`.
   In this mode, Vercel hosts the dashboard UI and proxies live snapshot data from your real daemon host.
   If you do not set the env var, the deployment also falls back to the repo-level `dashboard_proxy_url.txt` file when present.

Suggested Vercel environment variables:

```bash
TRADINGAGENTS_DASHBOARD_PROXY_URL=https://your-daemon-host.example.com
TRADINGAGENTS_VERCEL_REFRESH_SECONDS=15
```

Notes:

- if `TRADINGAGENTS_DASHBOARD_PROXY_URL` already ends with `/api/overview`, it will be used directly
- otherwise the deployment appends `/api/overview`
- if `dashboard_proxy_url.txt` exists in the repo root, Vercel can use that as the proxy target without an extra env var
- without a proxy URL, the deployment returns an informative placeholder snapshot instead of failing
- Vercel is a good fit for the dashboard frontend, not for the long-running trading daemon itself

### Vercel Steps

1. Import the repo into Vercel.
2. Leave the framework preset as `Other`.
3. Set `TRADINGAGENTS_DASHBOARD_PROXY_URL` if you want live remote data.
4. Deploy.

Routes after deployment:

- `/` for the dashboard
- `/api/overview` for the JSON snapshot
- `/healthz` for a simple health check

## Public Backend URL

When the local dashboard is being tunneled publicly, the current backend URL is stored in:

- `./runtime/public_backend_url.txt`
- `./runtime/public_backend_api_url.txt`

The first file is the base public dashboard URL. The second file is the exact JSON endpoint that the hosted Vercel dashboard consumes.

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
