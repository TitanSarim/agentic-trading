# Agentic Trading — Phase 2 Research

Python trading-app scaffold for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 2 adds an offline **feature pipeline**, **cost model**, one deterministic strategy (`trend_pullback_v1`), and **reproducible backtests**. **No live trades.**

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

## Offline backtest

```powershell
# Single symbol / timeframe (synthetic trend_pullback scenario — cloud-safe)
trader backtest --symbol EURUSD --timeframe M5 --bars 400

# All V1 universe symbols on M15
trader backtest --timeframe M15 --bars 400

# Machine-readable
trader backtest -s EURUSD -t M5 -n 400 --json
```

Reports include: trade count, win rate, expectancy, profit factor, max drawdown, avg win/loss, net P&L — **after** spread, commission, and slippage.

Default bars are **synthetic** (`backtest.scenario: trend_pullback`). This is research scaffolding, not live edge. On Windows you can later swap in downloaded MT5 bars via the historical path; cloud CI stays on mocks.

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
| `costs.commission_per_lot` | `7.0` | Round-turn USD / lot |
| `costs.slippage_pips` | `0.5` | Applied entry + exit |
| `backtest.volume` | `0.10` | Fixed research size |
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
| `trader health` | Config, DB migrate, mock broker, optional Ollama ping |
| `trader config-print` | Dump resolved settings |
| `trader migrate` | Apply SQLite migrations |
| `trader download-bars` | Synthetic historical bars → `market_bars` |
| `trader backtest` | Offline strategy backtest with costs (P2) |
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
  risk/               RiskEngine stub (boss)
  journal/            SQLite JournalDB
research/
  costs.py            Spread / commission / slippage / swap
  backtest.py         Offline simulator + reports
config/default.yaml
migrations/001_initial.sql
tests/
```

## Tests

```powershell
uv run pytest
# or: pytest
```

All default tests are offline (no LAN/Ollama/MT5 required).

## Phase 2 scope

**In**

- Feature pipeline for M5/M15 without look-ahead
- Cost model (spread, commission, slippage, swap hook)
- One deterministic strategy: `trend_pullback_v1` + `TradeCandidate` contract
- Offline backtests with expectancy / PF / max DD / win-loss stats
- CLI `trader backtest`; README + config for research defaults
- Risk / Qwen boundaries preserved (LLM stubbed; risk still boss)

**Out (later phases)**

- Full risk locks (P3)
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

## Migrating to a new Origin repo

This branch may live on a temporary Project remote until you create a standard repo in Cursor (**Create repo**). Then:

```powershell
# After Create repo (example name)
git remote add new-origin https://origin.cursor.com/git/<your-ns>/agentic-trading.git
# or: git remote set-url origin <new-url>

git push -u new-origin main
git push -u new-origin cursor/p1-trading-foundation-74bd
git push -u new-origin cursor/p2-research-strategy-d67d
```

Prefer merging P1 into `main` on the new remote so `main` is usable, then open a draft PR from the P2 branch.

See project docs in the Agent Store / Project context for the locked end-to-end plan.
