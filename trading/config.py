"""Settings loader: YAML defaults + env overrides (env wins)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


DEFAULT_CONFIG_PATH = Path("config/default.yaml")
DEFAULT_OLLAMA_URL = "http://192.168.8.22:11434"
DEFAULT_ANALYST_MODEL = "qwen3.8:27b"
DEFAULT_SCREEN_MODEL = "qwen3.5:9b"
DEFAULT_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")
DEFAULT_TIMEFRAMES = ("M5", "M15")


class OllamaSettings(BaseModel):
    base_url: str = DEFAULT_OLLAMA_URL
    screen_model: str = DEFAULT_SCREEN_MODEL
    analyst_model: str = DEFAULT_ANALYST_MODEL
    temperature: float = 0.1
    timeout_seconds: float = 30.0
    fail_closed_on_error: bool = True


class Mt5Settings(BaseModel):
    terminal_path: str = (
        r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe"
    )
    broker: str = "IC Markets Global"
    account_mode: Literal["demo", "live"] = "demo"


class UniverseSettings(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    timeframes: list[str] = Field(default_factory=lambda: list(DEFAULT_TIMEFRAMES))

    @field_validator("timeframes")
    @classmethod
    def only_locked_timeframes(cls, value: list[str]) -> list[str]:
        allowed = set(DEFAULT_TIMEFRAMES)
        unknown = [tf for tf in value if tf not in allowed]
        if unknown:
            raise ValueError(
                f"Unsupported timeframes {unknown}; V1 locked to {sorted(allowed)}"
            )
        return value


class RiskSettings(BaseModel):
    """Research defaults — risk engine is boss; LLM cannot raise these caps."""

    risk_fraction_per_trade: float = 0.0035
    max_positions: int = 1
    daily_loss_lock_pct: float = 0.015
    weekly_drawdown_lock_pct: float = 0.045
    consecutive_loss_lock: int = 3
    allow_llm_increase_risk: bool = False


class ExecutionSettings(BaseModel):
    require_attached_stop: bool = True
    idempotent_client_order_ids: bool = True


class DatabaseSettings(BaseModel):
    path: str = "data/trader.db"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_logs: bool = False


class BrokerSettings(BaseModel):
    backend: Literal["mock", "mt5"] = "mock"


class StrategySettings(BaseModel):
    """V1 research strategy: trend_pullback_v1 (deterministic)."""

    name: str = "trend_pullback_v1"
    ema_fast: int = 8
    ema_slow: int = 21
    atr_period: int = 14
    rsi_period: int = 14
    swing_lookback: int = 5
    pullback_atr_frac: float = 0.6
    stop_atr_mult: float = 1.5
    risk_reward: float = 2.0
    min_setup_score: float = 50.0
    ema_touch_atr_frac: float = 0.35


class CostSettings(BaseModel):
    """Research cost model — spread/commission/slippage/swap."""

    commission_per_lot: float = 7.0
    slippage_pips: float = 0.5
    swap_per_lot_per_day: float = 0.0


class BacktestSettings(BaseModel):
    volume: float = 0.10
    max_hold_bars: int = 48
    one_position: bool = True
    default_bars: int = 400
    scenario: Literal["flat", "trend_pullback"] = "trend_pullback"


class Settings(BaseModel):
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    mt5: Mt5Settings = Field(default_factory=Mt5Settings)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    costs: CostSettings = Field(default_factory=CostSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    config_path: str | None = None

    @property
    def provider_url(self) -> str:
        """Alias for ollama.base_url (plan §10)."""
        return self.ollama.base_url


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


def _apply_provider_url_alias(raw: dict[str, Any]) -> dict[str, Any]:
    """Support top-level provider_url as alias for ollama.base_url."""
    provider = raw.pop("provider_url", None)
    if provider is None:
        return raw
    ollama = dict(raw.get("ollama") or {})
    ollama.setdefault("base_url", provider)
    raw["ollama"] = ollama
    return raw


def _env_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    ollama: dict[str, Any] = {}

    if url := os.environ.get("OLLAMA_BASE_URL"):
        ollama["base_url"] = url.rstrip("/")
    if model := os.environ.get("OLLAMA_ANALYST_MODEL"):
        ollama["analyst_model"] = model
    if screen := os.environ.get("OLLAMA_SCREEN_MODEL"):
        ollama["screen_model"] = screen
    if ollama:
        overrides["ollama"] = ollama

    if db := os.environ.get("TRADER_DB_PATH"):
        overrides["database"] = {"path": db}

    if level := os.environ.get("TRADER_LOG_LEVEL"):
        overrides["logging"] = {"level": level.upper()}

    if backend := os.environ.get("TRADER_BROKER_BACKEND"):
        overrides["broker"] = {"backend": backend.lower()}

    if mode := os.environ.get("TRADER_ACCOUNT_MODE"):
        overrides["mt5"] = {"account_mode": mode.lower()}

    return overrides


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    if env_path := os.environ.get("TRADER_CONFIG"):
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_settings(
    config_path: str | Path | None = None,
    *,
    load_dotenv_file: bool = True,
) -> Settings:
    """Load settings. Resolution: defaults < YAML < env (env wins)."""
    if load_dotenv_file:
        load_dotenv(override=False)

    path = resolve_config_path(config_path)
    raw = _apply_provider_url_alias(_load_yaml(path))
    merged = _deep_merge(raw, _env_overrides())
    settings = Settings.model_validate(merged)
    return settings.model_copy(update={"config_path": str(path)})
