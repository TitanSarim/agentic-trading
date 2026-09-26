"""Config loading: YAML defaults + env overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trading.config import Settings, load_settings


def test_default_settings_locked_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("TRADER_CONFIG", raising=False)
    cfg = Path("config/default.yaml")
    settings = load_settings(cfg, load_dotenv_file=False)
    assert settings.ollama.base_url == "http://192.168.8.22:11434"
    assert settings.provider_url == settings.ollama.base_url
    assert settings.ollama.analyst_model == "qwen3.8:27b"
    assert settings.ollama.screen_model == "qwen3.5:9b"
    assert settings.universe.timeframes == ["M5", "M15"]
    assert settings.universe.symbols == ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    assert settings.risk.allow_llm_increase_risk is False
    assert settings.mt5.account_mode == "demo"
    assert settings.broker.backend == "mock"


def test_env_overrides_ollama_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/")
    monkeypatch.setenv("OLLAMA_ANALYST_MODEL", "qwen3.6:27b")
    monkeypatch.setenv("TRADER_BROKER_BACKEND", "mock")
    monkeypatch.setenv("TRADER_DB_PATH", "data/ci.db")
    settings = load_settings("config/default.yaml", load_dotenv_file=False)
    assert settings.ollama.base_url == "http://127.0.0.1:11434"
    assert settings.ollama.analyst_model == "qwen3.6:27b"
    assert settings.database.path == "data/ci.db"


def test_provider_url_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "provider_url: http://10.0.0.1:11434\nuniverse:\n  symbols: [EURUSD]\n  timeframes: [M5, M15]\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg, load_dotenv_file=False)
    assert settings.ollama.base_url == "http://10.0.0.1:11434"


def test_rejects_unknown_timeframe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        "universe:\n  symbols: [EURUSD]\n  timeframes: [H1]\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_settings(cfg, load_dotenv_file=False)


def test_settings_model_defaults() -> None:
    s = Settings()
    assert s.ollama.fail_closed_on_error is True
    assert s.execution.require_attached_stop is True
