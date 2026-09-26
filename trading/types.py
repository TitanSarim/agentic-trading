"""Shared domain types for Phase 1 interfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Timeframe(str, Enum):
    M5 = "M5"
    M15 = "M15"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class Bar(BaseModel):
    symbol: str
    timeframe: Timeframe
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float | None = None


class AccountState(BaseModel):
    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    currency: str = "USD"
    account_mode: Literal["demo", "live"] = "demo"


class Position(BaseModel):
    ticket: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    profit: float = 0.0
    client_order_id: str | None = None


class OrderRequest(BaseModel):
    """Broker-facing order plan. P1: interfaces only — no live sends from CLI."""

    symbol: str
    side: Side
    volume: float
    stop_loss: float
    take_profit: float | None = None
    client_order_id: str
    comment: str = ""


class OrderResult(BaseModel):
    client_order_id: str
    status: OrderStatus
    broker_ticket: str | None = None
    fill_price: float | None = None
    message: str = ""


class TradeCandidate(BaseModel):
    symbol: str
    strategy: str
    direction: Literal["LONG", "SHORT"]
    entry: float
    stop: float
    target: float
    risk_reward: float
    regime: str = "unknown"
    setup_score: float = 0.0


class AnalystDecision(BaseModel):
    """Qwen analyst output — advisory only; risk_modifier may only reduce size."""

    decision: Literal["APPROVE", "REJECT"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk_modifier: float = Field(ge=0.0, le=1.0, default=1.0)
    reason_code: str = ""
    raw_response: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_hash: str | None = None
    validated_response: dict | None = None


class RiskDecision(BaseModel):
    approved: bool
    volume: float = 0.0
    reason_code: str = ""
    risk_dollars: float = 0.0
    new_trades_locked: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
