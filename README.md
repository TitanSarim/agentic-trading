# Agentic Trading — Phase 8 soak / walk-forward / stress

Python trading app for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 8 adds **offline validation** before tiny live (P9): chronological walk-forward, cost/risk stress scenarios, and an accelerated soak loop that exercises halt/kill/heartbeats. Artifacts are written under `reports/`. No live trades; demo path remains gated.

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
trader validate --json
```

## Validation before tiny live (P8 → P9)

Run the offline suite and review the DoD report **before** enabling any live path:

```powershell
# Full offline package: walk-forward + stress + soak → reports/p8-validation.{json,md}
trader validate --symbol EURUSD --timeframe M5 --bars 400 --ticks 40

# Or individually:
trader walk-forward --symbol EURUSD --bars 400 --json
trader stress --symbol EURUSD --bars 400 --json
trader soak --ticks 40 --db data/soak.db --json
```

Reports land in `reports/` (see `validation.report_dir` in `config/default.yaml`).

**Still required on the Windows LAN host before P9 (not covered by this offline loop):**

- Unattended IC Markets **demo** soak with MT5 connected
- Broker disconnect/reconnect + restart recovery
- Protective stops verified at the broker
- Compare expected vs actual slippage/fills
- Keep `execution.allow_live=false` until plan §9 is green

## Operator kill switch (P7)

```powershell
trader halt --reason OPS_HALT
trader status --json
trader resume
trader kill --reason EMERGENCY
trader resume --clear-kill
```

## Offline tests (CI / cloud)

```powershell
pip install -e ".[dev]"
pytest
trader walk-forward --bars 400 --json
trader soak --ticks 20 --json
trader validate --bars 400 --ticks 20
```

## IC Markets demo on Windows

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
| `validation.*` | see YAML | Walk-forward windows, soak ticks, report dir |
| `monitoring.api_host` / `TRADER_CONTROL_HOST` | `127.0.0.1` | Control API bind |
| `broker.mode` / `TRADER_BROKER_MODE` | `demo` | Demo gate |
| `execution.allow_live` / `TRADER_ALLOW_LIVE` | `false` | Live only after §9 (P9) |
| `OLLAMA_BASE_URL` | `http://192.168.8.22:11434` | Env preferred |

## CLI

| Command | Purpose |
|---------|---------|
| `trader validate` | Walk-forward + stress + soak → DoD JSON/MD |
| `trader walk-forward` | Chronological OOS folds |
| `trader stress` | Spread / slippage / gap / consecutive-loss shocks |
| `trader soak` | Accelerated offline soak (halt/kill/heartbeats) |
| `trader health` / `status` / `halt` / `resume` / `kill` | Ops + monitoring |
| `trader execute-demo` | Demo path (dry-run default) |
| `trader backtest` / `scan` / `analyze` | Research / analyst |

## Layout

```text
apps/trader/          CLI
research/
  backtest.py         Offline backtester
  walk_forward.py     Chronological OOS harness (P8)
  stress.py           Stress scenarios (P8)
  soak.py             Accelerated soak loop (P8)
  dod_report.py       Pass/fail DoD artifacts (P8)
trading/monitoring/   ControlStore, heartbeats, alerts, control API
brokers/              BrokerPort, MockBroker, Mt5Broker
config/default.yaml
reports/              Validation outputs (gitignored JSON/MD)
tests/
```

## Phase 8 scope

**In**

- Walk-forward / out-of-sample chronological backtest harness
- Stress: spread shock, slippage, gap, consecutive-loss RiskEngine lock
- Accelerated offline soak with heartbeats + halt/kill/resume
- CLI: `walk-forward`, `stress`, `soak`, `validate`
- DoD-style JSON + Markdown under `reports/`
- Offline tests; demo path still gated; no live trades

**Out**

- Live/real-money trading (P9)
- Multi-day unattended **demo** soak on Windows/MT5 (LAN)
- Claiming full plan §9 Definition of Done from offline artifacts alone

## Safety

- Fail closed on kill, halt, broker disconnect, unexpected positions, risk locks, LLM errors
- Kill switch stops **new entries**; open positions are not force-closed by halt
- `risk_modifier` may only **reduce** size
- No live order path by default
- Do not optimize for a fixed daily dollar target
