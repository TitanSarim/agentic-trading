# Agentic Trading — Phase 1 Foundation

Python trading-app scaffold for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 1 ships config, structured logging, SQLite journal, broker/market/risk/LLM interfaces with **mocks**, a thin Ollama HTTP client (fail-closed), and a CLI. **No live trades.**

## Quick start (Windows)

```powershell
# Python 3.11+ recommended
cd path\to\agentic-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# or: uv sync  (if uv is installed)

copy .env.example .env
# Edit .env if needed — default OLLAMA_BASE_URL is already the LAN host

trader health
trader config-print
trader migrate
trader download-bars --count 100
trader demo-roundtrip

# Optional — only when this Windows host can reach .22:
trader ollama-tags
curl -s http://192.168.8.22:11434/api/tags
```

With `uv`:

```powershell
uv sync
uv run trader health
uv run pytest
```

## Configuration

Resolution order: **built-in defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `OLLAMA_BASE_URL` / `ollama.base_url` / `provider_url` | `http://192.168.8.22:11434` | Env preferred for overrides |
| `ollama.analyst_model` | `qwen3.8:27b` | Locked primary |
| `ollama.screen_model` | `qwen3.5:9b` | Optional screener |
| `universe.timeframes` | `M5`, `M15` | Locked |
| `universe.symbols` | EURUSD, GBPUSD, USDJPY, XAUUSD | V1 max 4 |
| `broker.backend` / `TRADER_BROKER_BACKEND` | `mock` | Use `mt5` only on Windows with MetaTrader5 installed |
| `mt5.account_mode` | `demo` | Live only after plan Definition of Done |
| `database.path` / `TRADER_DB_PATH` | `data/trader.db` | SQLite for P1 |

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
| `trader demo-roundtrip` | Mock LLM → risk → mock fill (no live orders) |
| `trader ollama-tags` | List models if URL reachable (exit 2 if not) |

## Layout

```text
apps/trader/          CLI entrypoint
brokers/              BrokerPort, MockBroker, Mt5Broker stub
trading/
  config.py           YAML + env settings
  logging_setup.py    structlog
  types.py            shared contracts
  data/               MarketDataPort, mock, historical downloader
  llm/                OllamaClient (fail-closed) + MockLlm
  risk/               RiskEngine stub (boss)
  journal/            SQLite JournalDB
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

## Phase 1 scope

**In**

- Project scaffold + Windows-oriented README
- Config (universe, M5/M15, risk fractions, Ollama URL/models)
- Structured logging + `system_events`
- SQLite migrations for journal tables
- Broker interface + mock; MT5 adapter stubbed (Windows path documented)
- Market data mock + historical download path
- LLM interface + mock + real Ollama HTTP client (fail-closed)
- Risk engine stub enforcing “LLM cannot raise caps”
- CLI: health / config-print / migrate / download-bars / demo-roundtrip / ollama-tags

**Out (later phases)**

- Features, strategies, backtests (P2)
- Full risk locks (P3)
- Scanner (P4)
- Full Qwen schema journal workflow (P5)
- Demo/live MT5 order lifecycle (P6+)
- Dashboard / kill-switch API (P7)

## Safety

- Fail closed when Ollama is down or returns bad JSON
- `risk_modifier` may only **reduce** size
- No martingale / averaging down
- No live order CLI in Phase 1
- Do not optimize for a fixed daily dollar target

See project docs in the Agent Store / Project context for the locked end-to-end plan.
