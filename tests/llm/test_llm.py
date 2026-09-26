"""Phase 5 LLM — schema, fail-closed Ollama, mock, pipeline (offline)."""

from __future__ import annotations

import json

import httpx
import pytest

from trading.llm.mock import MockLlm
from trading.llm.ollama import OllamaClient, candidate_input_hash
from trading.llm.pipeline import analyze_candidates
from trading.llm.schema import PROMPT_VERSION, parse_analyst_payload
from trading.risk.engine import RiskEngine
from trading.types import AccountState, TradeCandidate


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="XAUUSD",
        strategy="trend_pullback_v1",
        direction="LONG",
        entry=2400.0,
        stop=2390.0,
        target=2420.0,
        risk_reward=2.0,
        regime="trend",
        setup_score=84,
    )


def test_mock_llm_approve() -> None:
    llm = MockLlm(decision="APPROVE", risk_modifier=0.8)
    decision = llm.validate_candidate(_candidate())
    assert decision.decision == "APPROVE"
    assert decision.risk_modifier == 0.8
    assert decision.input_hash
    assert decision.validated_response is not None
    assert decision.prompt_version == PROMPT_VERSION
    assert llm.health() is True
    assert "qwen3.8:27b" in llm.list_models()


def test_mock_llm_unhealthy_fail_closed() -> None:
    llm = MockLlm(healthy=False)
    decision = llm.validate_candidate(_candidate())
    assert decision.decision == "REJECT"
    assert decision.reason_code == "LLM_UNAVAILABLE"
    assert decision.risk_modifier == 0.0


def test_mock_llm_timeout_fail_closed() -> None:
    llm = MockLlm(simulate_timeout=True)
    decision = llm.validate_candidate(_candidate())
    assert decision.decision == "REJECT"
    assert decision.reason_code == "LLM_TIMEOUT"


def test_parse_invalid_json_fail_closed() -> None:
    parsed = parse_analyst_payload("not-json")
    assert parsed.decision == "REJECT"
    assert parsed.reason_code == "INVALID_JSON"


def test_parse_unknown_fields_fail_closed() -> None:
    raw = json.dumps(
        {
            "decision": "APPROVE",
            "confidence": 0.5,
            "risk_modifier": 0.8,
            "reason_code": "OK",
            "leverage": 100,
        }
    )
    parsed = parse_analyst_payload(raw)
    assert parsed.decision == "REJECT"
    assert parsed.reason_code == "UNKNOWN_FIELDS"


def test_parse_invalid_decision_fail_closed() -> None:
    raw = json.dumps(
        {
            "decision": "MAYBE",
            "confidence": 0.5,
            "risk_modifier": 0.8,
            "reason_code": "X",
        }
    )
    parsed = parse_analyst_payload(raw)
    assert parsed.decision == "REJECT"
    assert parsed.reason_code == "INVALID_DECISION"


def test_parse_clamps_risk_modifier() -> None:
    # Values > 1 must be clamped — LLM cannot increase risk via schema.
    # Pydantic Field(le=1.0) rejects >1 → SCHEMA/INVALID_NUMERIC reject.
    # Clamping path: exactly at boundary via float coerce after valid range.
    raw = json.dumps(
        {
            "decision": "APPROVE",
            "confidence": 1.0,
            "risk_modifier": 1.0,
            "reason_code": "TREND_BREAKOUT_ALIGNED",
        }
    )
    parsed = parse_analyst_payload(raw, model="qwen3.8:27b")
    assert parsed.decision == "APPROVE"
    assert parsed.risk_modifier == 1.0
    assert parsed.validated_response is not None


def test_parse_rejects_risk_modifier_above_one() -> None:
    raw = json.dumps(
        {
            "decision": "APPROVE",
            "confidence": 0.9,
            "risk_modifier": 2.0,
            "reason_code": "BOOST",
        }
    )
    parsed = parse_analyst_payload(raw)
    assert parsed.decision == "REJECT"
    assert parsed.reason_code in {"INVALID_NUMERIC", "SCHEMA_INVALID"}


def test_ollama_health_unreachable() -> None:
    client = OllamaClient(base_url="http://127.0.0.1:9", timeout_seconds=0.2)
    assert client.health() is False
    assert client.list_models() == []


def test_ollama_chat_timeout_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    transport = httpx.MockTransport(handler)
    client = OllamaClient(
        base_url="http://ollama.test",
        timeout_seconds=0.1,
        fail_closed_on_error=True,
        transport=transport,
    )
    decision = client.validate_candidate(_candidate())
    assert decision.decision == "REJECT"
    assert decision.reason_code == "LLM_TIMEOUT"
    assert decision.risk_modifier == 0.0


def test_ollama_chat_http_error_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    client = OllamaClient(
        base_url="http://ollama.test",
        fail_closed_on_error=True,
        transport=transport,
    )
    decision = client.validate_candidate(_candidate())
    assert decision.decision == "REJECT"
    assert decision.reason_code == "LLM_UNAVAILABLE"


def test_ollama_chat_success_parses_schema() -> None:
    body = {
        "decision": "APPROVE",
        "confidence": 0.76,
        "risk_modifier": 0.80,
        "reason_code": "TREND_BREAKOUT_ALIGNED",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps(body)}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = OllamaClient(
        base_url="http://ollama.test",
        analyst_model="qwen3.8:27b",
        transport=transport,
    )
    decision = client.validate_candidate(_candidate(), market_context={"tf": "M5"})
    assert decision.decision == "APPROVE"
    assert decision.confidence == pytest.approx(0.76)
    assert decision.risk_modifier == pytest.approx(0.80)
    assert decision.model == "qwen3.8:27b"
    assert decision.input_hash
    assert decision.validated_response == body


def test_input_hash_stable() -> None:
    c = _candidate()
    h1 = candidate_input_hash(c, market_context={"a": 1})
    h2 = candidate_input_hash(c, market_context={"a": 1})
    h3 = candidate_input_hash(c, market_context={"a": 2})
    assert h1 == h2
    assert h1 != h3


def test_analyze_candidates_pipeline() -> None:
    llm = MockLlm(decision="REJECT", reason_code="WEAK_SETUP")
    results = analyze_candidates(llm, [_candidate(), _candidate()])
    assert len(results) == 2
    assert all(not r.approved_by_analyst for r in results)
    assert llm.calls == 2


def test_risk_modifier_reduces_size_via_engine() -> None:
    """End-to-end: analyst modifier shrinks RiskEngine size; never raises."""
    from datetime import datetime, timezone

    from trading.config import RiskSettings
    from trading.risk.context import RiskMarketContext, RiskPortfolioContext

    risk = RiskEngine(
        RiskSettings(
            risk_fraction_per_trade=0.01,
            min_volume=0.01,
            volume_step=0.01,
            max_spread_pips=None,
        )
    )
    account = AccountState(balance=1000.0, equity=1000.0, account_mode="demo")
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    market = RiskMarketContext(now=now, bar_time=now, spread=0.00012, pip_size=0.0001)
    portfolio = RiskPortfolioContext(
        day_start_equity=1000.0,
        week_start_equity=1000.0,
        consecutive_losses=0,
        open_positions=[],
    )
    cand = TradeCandidate(
        symbol="EURUSD",
        strategy="trend_pullback_v1",
        direction="LONG",
        entry=1.1000,
        stop=1.0990,
        target=1.1020,
        risk_reward=2.0,
    )
    full = MockLlm(decision="APPROVE", risk_modifier=1.0).validate_candidate(cand)
    half = MockLlm(decision="APPROVE", risk_modifier=0.5).validate_candidate(cand)
    d_full = risk.validate_and_size(
        cand, full, account, market=market, portfolio=portfolio
    )
    d_half = risk.validate_and_size(
        cand, half, account, market=market, portfolio=portfolio
    )
    assert d_full.approved and d_half.approved
    assert d_half.volume < d_full.volume
    assert d_half.risk_dollars == pytest.approx(d_full.risk_dollars * 0.5)
