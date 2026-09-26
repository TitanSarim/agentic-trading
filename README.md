# Agentic Trading — Phase 4 Scanner

Python trading-app scaffold for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Scanner = rank only. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 4 adds the **market scanner**: eligibility gates + deterministic opportunity ranking for the V1 universe on `M5`/`M15`. **Score never alone opens a trade** — candidates must still pass RiskEngine. **No live trades.**

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
trader scan
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
uv run trader scan --json
uv run trader backtest --symbol EURUSD -t M5 -n 400
uv run pytest
```

## Scanner (offline)

```powershell
# Rank full V1 universe on M5 + M15 (synthetic bars)
trader scan

# One symbol / timeframe
trader scan --symbol EURUSD --timeframe M5 --bars 120

# Machine-readable + top-N override
trader scan --top-n 2 --json

# Fail-closed demo: risk lock → all markets ineligible
trader scan --lock-risk
```

Opportunity score components (rank only):

```text
opportunity_score =
  trend + volatility + momentum + setup_quality + liquidity
  - spread_penalty - abnormal_volatility_penalty - correlation_penalty
```

Top-N rows may optionally emit a `trend_pullback_v1` candidate for inspection. That candidate is **not** sized or sent — RiskEngine remains required.

## Configuration

Resolution order: **built-in defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `OLLAMA_BASE_URL` / `ollama.base_url` / `provider_url` | `http://192.168.8.22:11434` | Env preferred for overrides |
| `ollama.analyst_model` | `qwen3.8:27b` | Locked primary |
| `universe.timeframes` | `M5`, `M15` | Locked |
| `universe.symbols` | EURUSD, GBPUSD, USDJPY, XAUUSD | V1 max 4 |
| `scanner.top_n` | `3` | Only top-N enter strategy eval |
| `scanner.min_bars` | `64` | Eligibility |
| `scanner.max_data_age_seconds` | `300` | Stale → ineligible |
| `scanner.max_spread_pips` | `3.0` | Wide spread → ineligible |
| `scanner.correlation_penalty` | `12.0` | Soft rank penalty in correlated groups |
| `risk.*` | (see YAML) | Hard gates; LLM cannot raise caps |
| `broker.backend` / `TRADER_BROKER_BACKEND` | `mock` | Use `mt5` only on Windows with MetaTrader5 |
| `mt5.account_mode` | `demo` | Live only after plan Definition of Done |

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
| `trader health` | Config, DB migrate, mock broker, risk + scanner boot, optional Ollama ping |
| `trader config-print` | Dump resolved settings |
| `trader migrate` | Apply SQLite migrations |
| `trader download-bars` | Synthetic historical bars → `market_bars` |
| `trader scan` | Eligibility + opportunity ranking (offline mock) |
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
  scanner/            Eligibility + opportunity ranking (P4)
  data/               MarketDataPort, mock, scenarios, historical
  llm/                OllamaClient (fail-closed) + MockLlm
  risk/               RiskEngine (boss) — sizing, locks, kill/halt
  journal/            SQLite JournalDB (+ opportunity_scores)
research/
  costs.py            Spread / commission / slippage / swap
  backtest.py         Offline simulator + risk wiring + reports
config/default.yaml
migrations/
tests/
```

## Tests

```powershell
uv run pytest
# or: pytest
```

All default tests are offline (no LAN/Ollama/MT5 required). Scanner tests cover eligibility rejects, deterministic ranking, correlation penalty, and “score never alone trades.”

## Phase 4 scope

**In**

- Multi-symbol scanner for configured universe × M5/M15
- Eligibility gates (bars, freshness, spread, gap, broker/API/session, risk lock)
- Deterministic opportunity score components (persisted to `opportunity_scores`)
- Top-N selection for optional strategy evaluation
- `trader scan` CLI (offline/mock)
- Fail-closed when risk is locked

**Out (later phases)**

- Full Qwen schema journal workflow (P5)
- Demo/live MT5 order lifecycle (P6+)
- Dashboard / kill-switch API (P7)
- Live market-session calendar (flags only in P4)

## Safety

- Fail closed when Ollama is down or returns bad JSON
- `risk_modifier` may only **reduce** size
- Scanner score never bypasses RiskEngine
- No martingale / averaging down
- No live order CLI
- Backtests/scans are research only — not a live edge claim
- Do not optimize for a fixed daily dollar target

## Remotes

Canonical GitHub (user): `https://github.com/TitanSarim/agentic-trading`
