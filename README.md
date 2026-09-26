# Agentic Trading — Phase 5 Qwen / Ollama

Python trading-app scaffold for the Autonomous Qwen + Ollama + MT5 (IC Markets) system.

**Authority:** Risk engine = boss. Qwen = analyst only. Strategy = setup generator. Broker = executor.

**Host:** Windows machine co-located with MT5. Models live on Linux `192.168.8.22` Ollama.

Phase 5 wires the **Qwen analyst** against Ollama with strict JSON schema, timeouts, fail-closed rejects, and journaled decisions. `risk_modifier` may **only reduce** size. **No live trades.**

## Quick start (Windows)

```powershell
# Python 3.11+ recommended
cd path\to\agentic-trading
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# or: uv sync

copy .env.example .env

trader health
trader config-print
trader migrate
trader download-bars --count 100
trader backtest --symbol EURUSD --timeframe M5 --bars 400
trader scan --symbol EURUSD --timeframe M5
trader analyze --mock --json
trader demo-roundtrip

# Optional — only when this Windows host can reach .22:
trader ollama-tags
trader analyze --live --json
curl -s http://192.168.8.22:11434/api/tags
```

With `uv`:

```powershell
uv sync
uv run trader health
uv run trader analyze --mock --json
uv run pytest
```

## Offline tests (CI / cloud)

```powershell
pip install -e ".[dev]"
pytest
# or: uv run pytest

trader analyze --mock --json
trader scan --symbol EURUSD -t M5 --analyze --mock-llm --json
```

All default tests mock the LLM — no LAN/Ollama/MT5 required.

## Optional live Ollama (Windows LAN only)

```powershell
$env:OLLAMA_BASE_URL = "http://192.168.8.22:11434"
trader ollama-tags
trader analyze --live --symbol EURUSD --timeframe M5 --json
```

Cloud Cursor agents **cannot** reach `192.168.8.22`. Exit code `2` from `ollama-tags` / `analyze --live` means unreachable — skippable without LAN.

## Configuration

Resolution order: **built-in defaults < `config/default.yaml` < environment** (env wins).

| Key / env | Default | Notes |
|-----------|---------|--------|
| `OLLAMA_BASE_URL` / `ollama.base_url` / `provider_url` | `http://192.168.8.22:11434` | Env preferred for overrides |
| `ollama.analyst_model` | `qwen3.8:27b` | Locked primary |
| `ollama.screen_model` | `qwen3.5:9b` | Optional screener |
| `ollama.use_screen_model` | `false` | Optional 9b pre-screen |
| `ollama.backend` / `OLLAMA_BACKEND` | `ollama` | Use `mock` in CI |
| `ollama.timeout_seconds` | `30` | Fail-closed on timeout |
| `ollama.fail_closed_on_error` | `true` | Errors → REJECT / no boost |
| `scanner.analyze_candidates` | `false` | Optional analyze after scan |
| `universe.timeframes` | `M5`, `M15` | Locked |
| `risk.allow_llm_increase_risk` | `false` | Hard-locked |
| `broker.backend` / `TRADER_BROKER_BACKEND` | `mock` | `mt5` only on Windows |
| `mt5.account_mode` | `demo` | Live only after Definition of Done |

## Topology

```text
Windows: trading app + MT5 terminal64.exe
    --HTTP-->  http://192.168.8.22:11434  (Ollama + Qwen)

Cloud Cursor agents cannot reach .22 or MT5.
Use mocks in CI; run ollama-tags / analyze --live on the Windows LAN host.
```

## CLI

| Command | Purpose |
|---------|---------|
| `trader health` | Config, DB, mock broker, optional Ollama ping |
| `trader config-print` | Dump resolved settings |
| `trader migrate` | Apply SQLite migrations |
| `trader download-bars` | Synthetic historical bars → `market_bars` |
| `trader backtest` | Offline strategy backtest with costs |
| `trader scan` | Eligibility + opportunity ranking (+ optional `--analyze`) |
| `trader analyze` | Dry analyst call (`--mock` default; `--live` on LAN) |
| `trader demo-roundtrip` | Strategy → mock LLM → risk → mock fill |
| `trader ollama-tags` | List models if URL reachable (exit 2 if not) |

## Layout

```text
apps/trader/          CLI entrypoint
brokers/              BrokerPort, MockBroker, Mt5Broker stub
trading/
  config.py           YAML + env settings
  features/           Indicator pipeline (no look-ahead)
  strategies/         trend_pullback_v1
  scanner/            Eligibility + opportunity ranking
  data/               MarketDataPort, mock, scenarios, historical
  llm/                OllamaClient (fail-closed) + MockLlm + schema
  risk/               RiskEngine (boss)
  journal/            SQLite JournalDB
research/
  costs.py            Spread / commission / slippage / swap
  backtest.py         Offline simulator + reports
config/default.yaml
migrations/001_initial.sql … 003_llm_decisions.sql
tests/
```

## Phase 5 scope

**In**

- Ollama client → `OLLAMA_BASE_URL` / config default `http://192.168.8.22:11434`
- Primary model `qwen3.8:27b`; optional `qwen3.5:9b` screen
- Strict JSON schema; unknown fields / bad enums rejected
- Timeouts and HTTP errors → `REJECT` (fail closed; no trade boost)
- Journal: prompt version, model, input hash, raw + validated response
- `risk_modifier` only reduces size; RiskEngine remains boss
- Optional `scan --analyze` and `trader analyze` dry-run CLI
- Offline mocks for CI

**Out (later phases)**

- Demo/live MT5 order lifecycle (P6+)
- Dashboard / kill-switch API (P7)
- Paper soak / live pilot (P8–P9)

## Safety

- Fail closed when Ollama is down, times out, or returns bad JSON
- `risk_modifier` may only **reduce** size
- No martingale / averaging down
- No live order CLI
- Do not optimize for a fixed daily dollar target
