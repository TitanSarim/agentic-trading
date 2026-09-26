"""Thin Ollama HTTP client — fail-closed on errors/timeouts.

Cloud agents cannot reach 192.168.8.22; use MockLlm in CI. Real calls are
optional via CLI when the Windows LAN host can reach Ollama.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from trading.types import AnalystDecision, TradeCandidate

log = structlog.get_logger(__name__)

PROMPT_VERSION = "p1-analyst-v1"

ANALYST_SCHEMA_HINT = (
    'Respond with ONLY JSON: '
    '{"decision":"APPROVE"|"REJECT","confidence":0-1,'
    '"risk_modifier":0-1,"reason_code":"STRING"}'
)


class OllamaClient:
    """HTTP client for Ollama /api/tags and /api/chat."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        analyst_model: str = "qwen3.8:27b",
        screen_model: str = "qwen3.5:9b",
        temperature: float = 0.1,
        timeout_seconds: float = 30.0,
        fail_closed_on_error: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.analyst_model = analyst_model
        self.screen_model = screen_model
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.fail_closed_on_error = fail_closed_on_error

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=min(5.0, self.timeout_seconds)) as client:
                resp = client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.HTTPError, OSError) as exc:
            log.warning("ollama_health_failed", error=str(exc), url=self.base_url)
            return False

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as exc:
            log.warning("ollama_tags_failed", error=str(exc))
            if self.fail_closed_on_error:
                return []
            raise
        models = payload.get("models") or []
        names: list[str] = []
        for item in models:
            name = item.get("name") or item.get("model")
            if name:
                names.append(str(name))
        return names

    def validate_candidate(
        self,
        candidate: TradeCandidate,
        *,
        market_context: dict | None = None,
    ) -> AnalystDecision:
        prompt = {
            "candidate": candidate.model_dump(),
            "market_context": market_context or {},
            "instructions": ANALYST_SCHEMA_HINT,
            "authority": (
                "You are an analyst only. Risk engine sizes and may veto. "
                "risk_modifier may only reduce size (0-1)."
            ),
        }
        raw = self._chat(self.analyst_model, json.dumps(prompt))
        if raw is None:
            return AnalystDecision(
                decision="REJECT",
                confidence=0.0,
                risk_modifier=0.0,
                reason_code="LLM_UNAVAILABLE",
                model=self.analyst_model,
                prompt_version=PROMPT_VERSION,
            )
        parsed = self._parse_decision(raw)
        parsed.raw_response = raw
        parsed.model = self.analyst_model
        parsed.prompt_version = PROMPT_VERSION
        return parsed

    def _chat(self, model: str, user_content: str) -> str | None:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Trading analyst. Output strict JSON only. "
                        "Never suggest sizing above risk engine caps."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {"temperature": self.temperature},
            "format": "json",
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(f"{self.base_url}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
            message = data.get("message") or {}
            content = message.get("content")
            if not content:
                log.warning("ollama_empty_content")
                return None if self.fail_closed_on_error else ""
            return str(content)
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as exc:
            log.warning("ollama_chat_failed", error=str(exc), model=model)
            if self.fail_closed_on_error:
                return None
            raise

    def _parse_decision(self, raw: str) -> AnalystDecision:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("ollama_invalid_json")
            return AnalystDecision(
                decision="REJECT",
                confidence=0.0,
                risk_modifier=0.0,
                reason_code="INVALID_JSON",
                raw_response=raw,
            )
        decision = str(data.get("decision", "")).upper()
        if decision not in {"APPROVE", "REJECT"}:
            return AnalystDecision(
                decision="REJECT",
                confidence=0.0,
                risk_modifier=0.0,
                reason_code="INVALID_DECISION",
                raw_response=raw,
            )
        try:
            confidence = float(data.get("confidence", 0.0))
            risk_modifier = float(data.get("risk_modifier", 1.0))
        except (TypeError, ValueError):
            return AnalystDecision(
                decision="REJECT",
                confidence=0.0,
                risk_modifier=0.0,
                reason_code="INVALID_NUMERIC",
                raw_response=raw,
            )
        # Clamp: risk_modifier may only reduce (never > 1).
        risk_modifier = min(1.0, max(0.0, risk_modifier))
        confidence = min(1.0, max(0.0, confidence))
        return AnalystDecision(
            decision=decision,  # type: ignore[arg-type]
            confidence=confidence,
            risk_modifier=risk_modifier,
            reason_code=str(data.get("reason_code") or "UNSPECIFIED"),
            raw_response=raw,
        )
