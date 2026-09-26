"""LLM mock + Ollama client fail-closed parsing (no live network)."""

from __future__ import annotations

from trading.llm.mock import MockLlm
from trading.llm.ollama import OllamaClient
from trading.types import TradeCandidate


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="XAUUSD",
        strategy="stub",
        direction="LONG",
        entry=2400.0,
        stop=2390.0,
        target=2420.0,
        risk_reward=2.0,
    )


def test_mock_llm_approve() -> None:
    llm = MockLlm(decision="APPROVE", risk_modifier=0.8)
    decision = llm.validate_candidate(_candidate())
    assert decision.decision == "APPROVE"
    assert decision.risk_modifier == 0.8
    assert llm.health() is True
    assert "qwen3.8:27b" in llm.list_models()


def test_mock_llm_unhealthy_fail_closed() -> None:
    llm = MockLlm(healthy=False)
    decision = llm.validate_candidate(_candidate())
    assert decision.decision == "REJECT"
    assert decision.reason_code == "LLM_UNAVAILABLE"


def test_ollama_parse_invalid_json_fail_closed() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:9", fail_closed_on_error=True)
    parsed = client._parse_decision("not-json")
    assert parsed.decision == "REJECT"
    assert parsed.reason_code == "INVALID_JSON"


def test_ollama_parse_clamps_risk_modifier() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:9")
    # Values > 1 must be clamped — LLM cannot increase risk.
    parsed = client._parse_decision(
        '{"decision":"APPROVE","confidence":1.5,"risk_modifier":2.0,"reason_code":"X"}'
    )
    assert parsed.decision == "APPROVE"
    assert parsed.risk_modifier == 1.0
    assert parsed.confidence == 1.0


def test_ollama_health_unreachable() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:9", timeout_seconds=0.2)
    assert client.health() is False
    assert client.list_models() == []
