# Agentic Trading — Phase 7 monitoring / kill switches

Python trading app for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 7 adds **operator visibility and kill switches**: persistent halt/kill/resume (SQLite), heartbeats, status CLI, optional local control HTTP API, and log/webhook alert stubs. Kill/halt **stop new entries** (fail-closed); they do not close open positions. Demo execution defaults are unchanged.

## Quick start (Windows)

```powershell
cd path\to\agentic-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# On the MT5 host only:
pip install MetaTrader5

copy .env.example .env

trader health
trader status --json
trader execute-demo --backend mock --dry-run --json
```

## Operator kill switch

Halt and kill state is stored in the journal DB and survives process restarts. `RiskEngine` and `trader execute-demo` both respect it (fail-closed).

```powershell
# Block new entries (positions not auto-closed)
trader halt --reason OPS_HALT
trader status --json

# execute-demo / risk will refuse new entries while halted
trader execute-demo --backend mock --dry-run --json

# Clear halt
trader resume

# Emergency kill (also sets halt). Clear with:
trader kill --reason EMERGENCY
trader resume --clear-kill
# or: trader clear-kill && trader resume
```

### Optional control HTTP (localhost)

```powershell
trader serve-control
# GET  http://127.0.0.1:8787/health
# GET  http://127.0.0.1:8787/status
# POST http://127.0.0.1:8787/halt        {"reason":"OPS_HALT"}
# POST http://127.0.0.1:8787/resume      {"clear_kill":false}
# POST http://127.0.0.1:8787/kill        {"reason":"EMERGENCY"}
# POST http://127.0.0.1:8787/clear-kill
```

No auth — bind to `127.0.0.1` only (default). Override with `TRADER_CONTROL_HOST` / `TRADER_CONTROL_PORT`.

### Alerts

Structured logs always. Optional webhook:

```powershell
$env:TRADER_ALERT_WEBHOOK = "http://127.0.0.1:9999/hook"
```

Empty / unset = log-only (no required SaaS).

## Offline tests (CI / cloud)

```powershell
pip install -e ".[dev]"
pytest
trader status --json
trader halt --reason TEST; trader resume
trader execute-demo --backend mock --dry-run --json
```

## IC Markets demo on Windows

Same as Phase 6 — dry-run default; submit only with `--confirm-demo` on demo:

```powershell
$env:TRADER_BROKER_BACKEND = "mt5"
$env:TRADER_BROKER_MODE = "demo"
trader execute-demo --backend mt5 --dry-run --json
trader execute-demo --backend mt5 --submit --confirm-demo --json
```

Live path remains disabled (`execution.allow_live=false`).

## Configuration

Resolution: **defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `monitoring.api_host` / `TRADER_CONTROL_HOST` | `127.0.0.1` | Control API bind |
| `monitoring.api_port` / `TRADER_CONTROL_PORT` | `8787` | Control API port |
| `monitoring.heartbeat_stale_seconds` / `TRADER_HEARTBEAT_STALE_SECONDS` | `120` | Stale process hint |
| `monitoring.auto_halt_on_risk_lock` | `true` | Auto halt on daily/weekly/consecutive lock |
| `monitoring.webhook_url` / `TRADER_ALERT_WEBHOOK` | empty | Optional POST JSON |
| `broker.mode` / `TRADER_BROKER_MODE` | `demo` | P6 demo gate |
| `execution.allow_live` / `TRADER_ALLOW_LIVE` | `false` | Live only after §9 (P9) |
| `OLLAMA_BASE_URL` | `http://192.168.8.22:11434` | Env preferred |

## CLI

| Command | Purpose |
|---------|---------|
| `trader health` | Config, DB, broker, control flags, Ollama ping |
| `trader status` | Control, heartbeats, equity, recent events |
| `trader halt` / `resume` / `kill` / `clear-kill` | Operator kill switch |
| `trader serve-control` | Minimal local HTTP control API |
| `trader execute-demo` | Demo path (respects halt/kill; dry-run default) |
| `trader analyze` / `scan` / `backtest` | Research / analyst paths |

## Layout

```text
apps/trader/          CLI
brokers/              BrokerPort, MockBroker, Mt5Broker
trading/
  monitoring/         ControlStore, heartbeats, alerts, status, control API
  execution/          IDs, ExecutionEngine, reconcile, demo pipeline
  risk/               RiskEngine (boss; kill/halt hooks)
  llm/ scanner/ strategies/ features/ data/ journal/
config/default.yaml
migrations/001…005_monitoring.sql
tests/
```

## Phase 7 scope

**In**

- Persistent kill / halt / resume (SQLite `control_state`)
- `RiskEngine` + `execute-demo` fail-closed when blocked
- Heartbeats + stale-process detection hooks
- Status CLI + minimal localhost control HTTP
- Alert stubs (log + optional webhook)
- Journal `system_events` for halt / kill / resume
- Auto-halt on account-level risk locks (configurable)
- Offline tests; demo-only defaults unchanged

**Out**

- Live/real-money trading (P9)
- Unattended paper soak / walk-forward (P8)
- Full Grafana dashboard / required SaaS alerts

## Safety

- Fail closed on kill, halt, broker disconnect, unexpected positions, risk locks, LLM errors
- Kill switch stops **new entries**; open positions are managed separately (not force-closed by halt)
- `risk_modifier` may only **reduce** size
- No live order path by default
- Do not optimize for a fixed daily dollar target
