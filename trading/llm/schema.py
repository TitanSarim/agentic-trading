"""Strict analyst JSON schema — reject unknown fields / invalid enums.

Qwen is advisory only. ``risk_modifier`` is clamped to [0, 1] so it can
never raise size above the RiskEngine cap.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trading.types import AnalystDecision

PROMPT_VERSION = "p5-analyst-v1"

ALLOWED_DECISIONS = frozenset({"APPROVE", "REJECT"})
ALLOWED_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

ANALYST_SCHEMA_HINT = (
    "Respond with ONLY a JSON object using exactly these keys "
    "(no extras, no markdown): "
    '{"decision":"APPROVE"|"REJECT","confidence":0.0-1.0,'
    '"risk_modifier":0.0-1.0,"reason_code":"UPPER_SNAKE"} '
    "risk_modifier may only REDUCE size (never > 1). "
    "You do not size or execute trades — RiskEngine is final authority."
)


class AnalystSchema(BaseModel):
    """Validated analyst payload. Extra fields are forbidden."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "REJECT"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk_modifier: float = Field(ge=0.0, le=1.0, default=1.0)
    reason_code: str = "UNSPECIFIED"

    @field_validator("decision", mode="before")
    @classmethod
    def _normalize_decision(cls, value: Any) -> str:
        return str(value).strip().upper()

    @field_validator("reason_code", mode="before")
    @classmethod
    def _normalize_reason(cls, value: Any) -> str:
        text = str(value or "UNSPECIFIED").strip().upper().replace(" ", "_")
        if not text:
            return "UNSPECIFIED"
        if not ALLOWED_REASON_RE.match(text):
            # Soft-normalize: keep alphanumeric underscores only.
            cleaned = re.sub(r"[^A-Z0-9_]", "_", text)
            cleaned = re.sub(r"_+", "_", cleaned).strip("_")
            return cleaned[:64] or "UNSPECIFIED"
        return text[:64]

    @field_validator("confidence", "risk_modifier", mode="before")
    @classmethod
    def _coerce_float(cls, value: Any) -> float:
        return float(value)


def reject_decision(
    reason_code: str,
    *,
    raw_response: str | None = None,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    input_hash: str | None = None,
) -> AnalystDecision:
    """Fail-closed REJECT used for errors, timeouts, and schema failures."""
    return AnalystDecision(
        decision="REJECT",
        confidence=0.0,
        risk_modifier=0.0,
        reason_code=reason_code,
        raw_response=raw_response,
        model=model,
        prompt_version=prompt_version,
        input_hash=input_hash,
        validated_response=None,
    )


def parse_analyst_payload(
    raw: str,
    *,
    model: str | None = None,
    prompt_version: str = PROMPT_VERSION,
    input_hash: str | None = None,
) -> AnalystDecision:
    """Parse and validate analyst JSON. Always fail closed on bad input."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return reject_decision(
            "INVALID_JSON",
            raw_response=raw,
            model=model,
            prompt_version=prompt_version,
            input_hash=input_hash,
        )

    if not isinstance(data, dict):
        return reject_decision(
            "INVALID_JSON_TYPE",
            raw_response=raw,
            model=model,
            prompt_version=prompt_version,
            input_hash=input_hash,
        )

    # Reject clearly stale / non-schema keys before pydantic (clearer reason).
    unknown = set(data.keys()) - {"decision", "confidence", "risk_modifier", "reason_code"}
    if unknown:
        return reject_decision(
            "UNKNOWN_FIELDS",
            raw_response=raw,
            model=model,
            prompt_version=prompt_version,
            input_hash=input_hash,
        )

    try:
        schema = AnalystSchema.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        # Distinguish missing/invalid decision vs numeric issues when possible.
        decision_raw = str(data.get("decision", "")).strip().upper()
        if decision_raw and decision_raw not in ALLOWED_DECISIONS:
            code = "INVALID_DECISION"
        elif "confidence" in data or "risk_modifier" in data:
            code = "INVALID_NUMERIC"
        else:
            code = "SCHEMA_INVALID"
        return reject_decision(
            code,
            raw_response=raw,
            model=model,
            prompt_version=prompt_version,
            input_hash=input_hash,
        )

    # Hard clamp again: risk_modifier may only reduce (never > 1).
    risk_modifier = min(1.0, max(0.0, schema.risk_modifier))
    confidence = min(1.0, max(0.0, schema.confidence))
    validated = {
        "decision": schema.decision,
        "confidence": confidence,
        "risk_modifier": risk_modifier,
        "reason_code": schema.reason_code,
    }
    return AnalystDecision(
        decision=schema.decision,
        confidence=confidence,
        risk_modifier=risk_modifier,
        reason_code=schema.reason_code,
        raw_response=raw,
        model=model,
        prompt_version=prompt_version,
        input_hash=input_hash,
        validated_response=validated,
    )
