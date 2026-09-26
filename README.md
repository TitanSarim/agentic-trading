# Agentic Trading — Phase 6 demo execution (IC Markets)

Python trading app for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 6 wires **demo order lifecycle** on IC Markets via MetaTrader5 Python: idempotent client order IDs, mandatory SL/TP, reconcile vs journal, and `trader execute-demo` (dry-run by default). **No live/real-money path** (`execution.allow_live=false`).

## Quick start (Windows)

```powershell
# Python 3.11+ recommended
cd path\to\agentic-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# On the MT5 host only, also:
pip install MetaTrader5
# or: pip install -e ".[mt5]"

copy .env.example .env

trader health
trader migrate
trader execute-demo --backend mock --dry-run --json
trader analyze --mock --json
trader scan --symbol EURUSD -t M5 --json
```

## Offline tests (CI / cloud)

```powershell
pip install -e ".[dev]"
pytest
trader execute-demo --backend mock --dry-run --json
trader execute-demo --backend mock --submit --confirm-demo --mock-llm --json
```

Cloud has no MT5 — tests use `MockBroker`. Never import MetaTrader5 unless on Windows.

## IC Markets demo on Windows

1. Start the terminal and log into a **demo** account:

```text
C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe
```

2. Install MetaTrader5 Python next to the app:

```powershell
pip install MetaTrader5
```

3. Optional credentials in `.env` (never commit secrets):

```powershell
$env:TRADER_BROKER_BACKEND = "mt5"
$env:TRADER_BROKER_MODE = "demo"
# Optional if not already logged in via terminal UI:
# $env:MT5_LOGIN = "..."
# $env:MT5_PASSWORD = "..."
# $env:MT5_SERVER = "ICMarketsSC-Demo"
```

4. Dry-run (no order sent):

```powershell
trader execute-demo --backend mt5 --dry-run --json
```

5. Submit on **demo only** (explicit confirm required):

```powershell
trader execute-demo --backend mt5 --submit --confirm-demo --json
```

`--submit` without `--confirm-demo` is rejected. Live mode is refused unless `execution.allow_live=true` (Phase 9 — not enabled by default).

## Optional live Ollama (Windows LAN only)

```powershell
$env:OLLAMA_BASE_URL = "http://192.168.8.22:11434"
trader ollama-tags
trader analyze --live --json
trader execute-demo --backend mock --dry-run --live-llm --json
```

Cloud agents **cannot** reach `192.168.8.22`.

## Configuration

Resolution: **defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `broker.backend` / `TRADER_BROKER_BACKEND` | `mock` | `mt5` only on Windows |
| `broker.mode` / `TRADER_BROKER_MODE` | `demo` | Synced with `mt5.account_mode` |
| `execution.allow_live` / `TRADER_ALLOW_LIVE` | `false` | P6 refuses live |
| `execution.require_attached_stop` | `true` | SL + TP required |
| `mt5.terminal_path` | IC Markets Global `terminal64.exe` | Locked path |
| `OLLAMA_BASE_URL` | `http://192.168.8.22:11434` | Env preferred |
| `ollama.analyst_model` | `qwen3.8:27b` | Locked primary |

## CLI

| Command | Purpose |
|---------|---------|
| `trader health` | Config, DB, mock broker, optional Ollama |
| `trader execute-demo` | Demo path: dry-run default; `--submit --confirm-demo` |
| `trader analyze` | Dry analyst (`--mock` / `--live`) |
| `trader scan` | Eligibility + ranking (+ optional `--analyze`) |
| `trader backtest` | Offline strategy backtest |
| `trader demo-roundtrip` | Legacy mock fill smoke |
| `trader ollama-tags` | List models if reachable (exit 2 if not) |

## Layout

```text
apps/trader/          CLI
brokers/              BrokerPort, MockBroker, Mt5Broker (Windows)
trading/
  execution/          IDs, ExecutionEngine, reconcile, demo pipeline
  risk/               RiskEngine (boss)
  llm/                Ollama + MockLlm
  scanner/ strategies/ features/ data/ journal/
config/default.yaml
migrations/001…004_orders_fills.sql
tests/
```

## Phase 6 scope

**In**

- Real `Mt5Broker` via MetaTrader5 when available; graceful fail off Windows / terminal down
- Idempotent client order IDs; SL/TP required
- Reconcile broker positions vs journal (unexpected → lock new trades)
- `broker.mode=demo`; refuse live unless explicitly allowed later (P9)
- `trader execute-demo` dry-run by default; `--submit` only with `--confirm-demo`
- Wire: scan/strategy → risk → optional analyze → demo execute
- Journal orders / fills / positions; offline mock tests green

**Out**

- Live/real-money trading (P9)
- Dashboard / kill-switch API (P7)
- Unattended paper soak (P8)

## Safety

- Fail closed on broker disconnect, unexpected positions, risk locks, LLM errors
- `risk_modifier` may only **reduce** size
- No martingale / averaging down
- No live order path by default
- Do not optimize for a fixed daily dollar target
