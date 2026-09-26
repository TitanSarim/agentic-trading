"""Ollama HTTP client for Qwen analyst — fail-closed on errors/timeouts.

Cloud agents cannot reach 192.168.8.22; use MockLlm in CI. Real calls are
optional via CLI when the Windows LAN host can reach Ollama.

Primary model: ``qwen3.8:27b``. URL: ``OLLAMA_BASE_URL`` / config default
``http://192.168.8.22:11434``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import structlog

from trading.llm.schema import (
    ANALYST_SCHEMA_HINT,
    PROMPT_VERSION,
    parse_analyst_payload,
    reject_decision,
)
from trading.types import AnalystDecision, TradeCandidate

log = structlog.get_logger(__name__)


def candidate_input_hash(
    candidate: TradeCandidate,
    *,
    market_context: dict | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    """Stable SHA-256 of candidate + context + prompt version for journaling."""
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "market_context": market_context or {},
        "prompt_version": prompt_version,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
        use_screen_model: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.analyst_model = analyst_model
        self.screen_model = screen_model
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.fail_closed_on_error = fail_closed_on_error
        self.use_screen_model = use_screen_model
        self._transport = transport
        self._last_error = ""

    def _client(self, *, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            timeout=timeout if timeout is not None else self.timeout_seconds,
            transport=self._transport,
        )

    def health(self) -> bool:
        try:
            with self._client(timeout=min(5.0, self.timeout_seconds)) as client:
                resp = client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except (httpx.HTTPError, OSError) as exc:
            log.warning("ollama_health_failed", error=str(exc), url=self.base_url)
            return False

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
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
        """Ask Qwen to approve/reject. Errors/timeouts → REJECT (fail closed)."""
        input_hash = candidate_input_hash(
            candidate,
            market_context=market_context,
            prompt_version=PROMPT_VERSION,
        )
        prompt = {
            "candidate": candidate.model_dump(mode="json"),
            "market_context": market_context or {},
            "instructions": ANALYST_SCHEMA_HINT,
            "authority": (
                "You are an analyst only. Risk engine sizes and may veto. "
                "risk_modifier may only reduce size (0-1). Never raise caps."
            ),
        }

        # Optional fast screen (9b) — REJECT short-circuits analyst.
        if self.use_screen_model:
            screen_raw = self._chat(self.screen_model, json.dumps(prompt))
            if screen_raw is None:
                return reject_decision(
                    "LLM_TIMEOUT" if self._last_error == "timeout" else "LLM_UNAVAILABLE",
                    model=self.screen_model,
                    input_hash=input_hash,
                )
            screen = parse_analyst_payload(
                screen_raw,
                model=self.screen_model,
                prompt_version=PROMPT_VERSION,
                input_hash=input_hash,
            )
            if screen.decision != "APPROVE":
                screen.reason_code = screen.reason_code or "SCREEN_REJECT"
                return screen

        raw = self._chat(self.analyst_model, json.dumps(prompt))
        if raw is None:
            reason = "LLM_TIMEOUT" if self._last_error == "timeout" else "LLM_UNAVAILABLE"
            return reject_decision(
                reason,
                model=self.analyst_model,
                input_hash=input_hash,
            )
        return parse_analyst_payload(
            raw,
            model=self.analyst_model,
            prompt_version=PROMPT_VERSION,
            input_hash=input_hash,
        )

    def _chat(self, model: str, user_content: str) -> str | None:
        self._last_error = ""
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Trading analyst. Output strict JSON only. "
                        "Never suggest sizing above risk engine caps. "
                        "risk_modifier is a reduction factor in [0,1]."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "stream": False,
            "options": {"temperature": self.temperature},
            "format": "json",
        }
        try:
            with self._client() as client:
                resp = client.post(f"{self.base_url}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
            message = data.get("message") or {}
            content = message.get("content")
            if not content:
                log.warning("ollama_empty_content", model=model)
                self._last_error = "empty"
                return None if self.fail_closed_on_error else ""
            return str(content)
        except httpx.TimeoutException as exc:
            log.warning("ollama_timeout", error=str(exc), model=model)
            self._last_error = "timeout"
            if self.fail_closed_on_error:
                return None
            raise
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as exc:
            log.warning("ollama_chat_failed", error=str(exc), model=model)
            self._last_error = "http"
            if self.fail_closed_on_error:
                return None
            raise
