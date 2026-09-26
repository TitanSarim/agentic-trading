# Agentic Trading — Phase 3 Risk Engine

Python trading-app scaffold for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 3 hardens the **risk engine**: sizing, exposure, drawdown locks, data-freshness, kill/halt hooks — wired into strategy candidates and offline backtests. **No live trades.**

## Quick start (Windows)

```powershell
# Python 3.11+ recommended
cd path\to\agentic-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# or: uv sync  (if uv is installed)

copy .env.example .env

trader health
trader config-print
trader migrate
trader download-bars --count 100
trader backtest --symbol EURUSD --timeframe M5 --bars 400
trader demo-roundtrip

# Optional — only when this Windows host can reach .22:
trader ollama-tags
curl -s http://192.168.8.22:11434/api/tags
```

With `uv`:

```powershell
uv sync
uv run trader health
uv run trader backtest --symbol EURUSD -t M5 -n 400
uv run pytest
```

## Offline backtest (risk-sized)

```powershell
# Uses RiskEngine by default (config backtest.use_risk_engine: true)
trader backtest --symbol EURUSD --timeframe M5 --bars 400

# All V1 universe symbols on M15
trader backtest --timeframe M15 --bars 400

# Machine-readable
trader backtest -s EURUSD -t M5 -n 400 --json
```

Reports include: trade count, win rate, expectancy, profit factor, max drawdown, avg win/loss, net P&L, `risk_rejects`, optional `ending_equity` — **after** spread, commission, and slippage. Volume comes from equity × risk fraction ÷ (stop × point value) when risk is on.

## Configuration

Resolution order: **built-in defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `OLLAMA_BASE_URL` / `ollama.base_url` / `provider_url` | `http://192.168.8.22:11434` | Env preferred for overrides |
| `ollama.analyst_model` | `qwen3.8:27b` | Locked primary |
| `ollama.screen_model` | `qwen3.5:9b` | Optional screener |
| `universe.timeframes` | `M5`, `M15` | Locked |
| `universe.symbols` | EURUSD, GBPUSD, USDJPY, XAUUSD | V1 max 4 |
| `strategy.name` | `trend_pullback_v1` | V1 research choice |
| `risk.risk_fraction_per_trade` | `0.0035` | Hard cap; LLM can only reduce |
| `risk.max_positions` | `1` | Exposure gate |
| `risk.daily_loss_lock_pct` | `0.015` | Locks new entries |
| `risk.weekly_drawdown_lock_pct` | `0.045` | Locks new entries |
| `risk.consecutive_loss_lock` | `3` | Locks new entries |
| `risk.max_data_age_seconds` | `120` | Stale data → reject |
| `risk.max_spread_pips` | `3.0` | Wide spread → reject |
| `backtest.use_risk_engine` | `true` | Size/reject in offline sim |
| `broker.backend` / `TRADER_BROKER_BACKEND` | `mock` | Use `mt5` only on Windows with MetaTrader5 |
| `mt5.account_mode` | `demo` | Live only after plan Definition of Done |
| `database.path` / `TRADER_DB_PATH` | `data/trader.db` | SQLite |

## Topology

```text
Windows: trading app + MT5 terminal64.exe
    --HTTP-->  http://192.168.8.22:11434  (Ollama + Qwen)

Cloud Cursor agents cannot reach .22 or MT5.
Use mocks in CI; run ollama-tags / MT5 checks on the Windows LAN host.
```

MT5 path (locked):

`C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe`

## CLI

| Command | Purpose |
|---------|---------|
| `trader health` | Config, DB migrate, mock broker, risk boot, optional Ollama ping |
| `trader config-print` | Dump resolved settings |
| `trader migrate` | Apply SQLite migrations |
| `trader download-bars` | Synthetic historical bars → `market_bars` |
| `trader backtest` | Offline strategy backtest with costs + risk sizing |
| `trader demo-roundtrip` | Strategy candidate → mock LLM → risk → mock fill |
| `trader ollama-tags` | List models if URL reachable (exit 2 if not) |

## Layout

```text
apps/trader/          CLI entrypoint
brokers/              BrokerPort, MockBroker, Mt5Broker stub
trading/
  config.py           YAML + env settings
  features/           Indicator pipeline (no look-ahead)
  strategies/         trend_pullback_v1
  data/               MarketDataPort, mock, scenarios, historical
  llm/                OllamaClient (fail-closed) + MockLlm
  risk/               RiskEngine (boss) — sizing, locks, kill/halt
  journal/            SQLite JournalDB
research/
  costs.py            Spread / commission / slippage / swap
  backtest.py         Offline simulator + risk wiring + reports
config/default.yaml
migrations/001_initial.sql
tests/
```

## Tests

```powershell
uv run pytest
# or: pytest
```

All default tests are offline (no LAN/Ollama/MT5 required). Risk tests cover every lock / reject path.

## Phase 3 scope

**In**

- Hard risk gates: sizing, max positions, correlation, averaging/martingale bans
- Daily / weekly / consecutive-loss locks
- Data-freshness + spread gates (fail closed)
- Kill switch + halt hooks
- Wire risk into strategy candidate path and offline backtests (reject or reduce size)
- Unit tests for each reject path; LLM cannot raise caps

**Out (later phases)**

- Scanner (P4)
- Full Qwen schema journal workflow (P5)
- Demo/live MT5 order lifecycle (P6+)
- Dashboard / kill-switch API (P7)

## Safety

- Fail closed when Ollama is down or returns bad JSON
- `risk_modifier` may only **reduce** size
- No martingale / averaging down
- No live order CLI
- Backtests are research only — not a live edge claim
- Do not optimize for a fixed daily dollar target

## Remotes

Canonical GitHub (user): `https://github.com/TitanSarim/agentic-trading`

See `internal/github-remote.md` in the Project store for remotes/SHAs after migrate.
