"""Execution engine — submits only risk-approved plans; demo-gated."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from brokers.base import BrokerPort
from trading.config import ExecutionSettings
from trading.execution.ids import make_client_order_id
from trading.types import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    RiskDecision,
    Side,
    TradeCandidate,
)


class LiveTradingRefused(RuntimeError):
    """Raised when a live (non-demo) submit is attempted without allow_live."""


class DemoConfirmRequired(RuntimeError):
    """Raised when --submit is used without explicit demo confirmation."""


@dataclass
class ExecutionPlan:
    """Risk-approved order ready for broker submit (or dry-run)."""

    candidate: TradeCandidate
    risk: RiskDecision
    client_order_id: str
    side: Side
    volume: float
    stop_loss: float
    take_profit: float
    comment: str = ""
    candidate_id: int | None = None
    account_mode: Literal["demo", "live"] = "demo"

    def to_order_request(self) -> OrderRequest:
        return OrderRequest(
            symbol=self.candidate.symbol,
            side=self.side,
            volume=self.volume,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            client_order_id=self.client_order_id,
            comment=self.comment or self.client_order_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.candidate.symbol,
            "side": self.side.value,
            "volume": self.volume,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "account_mode": self.account_mode,
            "candidate_id": self.candidate_id,
            "risk_reason": self.risk.reason_code,
            "candidate": self.candidate.model_dump(mode="json"),
        }


@dataclass
class ExecutionResult:
    plan: ExecutionPlan
    dry_run: bool
    submitted: bool
    order: OrderResult | None = None
    message: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "submitted": self.submitted,
            "message": self.message,
            "plan": self.plan.to_dict(),
            "order": self.order.model_dump(mode="json") if self.order else None,
            "extras": self.extras,
        }


class ExecutionEngine:
    """Broker-facing executor. Risk must already have approved the plan.

    Demo is the only default path. Live requires ``allow_live`` (Phase 9).
    """

    def __init__(
        self,
        broker: BrokerPort,
        settings: ExecutionSettings | None = None,
        *,
        account_mode: Literal["demo", "live"] = "demo",
    ) -> None:
        self.broker = broker
        self.settings = settings or ExecutionSettings()
        self.account_mode = account_mode

    def build_plan(
        self,
        candidate: TradeCandidate,
        risk: RiskDecision,
        *,
        candidate_id: int | None = None,
        seed: str | None = None,
    ) -> ExecutionPlan:
        if not risk.approved:
            raise ValueError(f"Risk did not approve: {risk.reason_code}")
        if risk.volume <= 0:
            raise ValueError("Risk-approved volume must be > 0")
        if self.settings.require_attached_stop and candidate.stop is None:
            raise ValueError("stop_loss required")
        if self.settings.require_attached_stop and candidate.target is None:
            raise ValueError("take_profit required")

        side = Side.BUY if candidate.direction == "LONG" else Side.SELL
        cid = make_client_order_id(
            symbol=candidate.symbol,
            side=side.value,
            strategy=candidate.strategy,
            candidate_id=candidate_id,
            seed=seed,
        )
        return ExecutionPlan(
            candidate=candidate,
            risk=risk,
            client_order_id=cid,
            side=side,
            volume=float(risk.volume),
            stop_loss=float(candidate.stop),
            take_profit=float(candidate.target),
            comment=cid,
            candidate_id=candidate_id,
            account_mode=self.account_mode,
        )

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        submit: bool = False,
        confirm_demo: bool = False,
    ) -> ExecutionResult:
        """Dry-run by default. ``submit=True`` requires demo + confirm_demo."""
        self._assert_mode_allows_submit(submit=submit, confirm_demo=confirm_demo)

        if not submit:
            return ExecutionResult(
                plan=plan,
                dry_run=True,
                submitted=False,
                order=OrderResult(
                    client_order_id=plan.client_order_id,
                    status=OrderStatus.PENDING,
                    message="dry-run — not submitted",
                ),
                message="dry-run: order not sent (pass --submit --confirm-demo)",
            )

        if not self.broker.is_connected():
            self.broker.connect()

        request = plan.to_order_request()
        if self.settings.require_attached_stop:
            if request.stop_loss is None:
                return ExecutionResult(
                    plan=plan,
                    dry_run=False,
                    submitted=False,
                    order=OrderResult(
                        client_order_id=plan.client_order_id,
                        status=OrderStatus.REJECTED,
                        message="stop_loss required",
                    ),
                    message="rejected: stop_loss required",
                )
            if request.take_profit is None:
                return ExecutionResult(
                    plan=plan,
                    dry_run=False,
                    submitted=False,
                    order=OrderResult(
                        client_order_id=plan.client_order_id,
                        status=OrderStatus.REJECTED,
                        message="take_profit required",
                    ),
                    message="rejected: take_profit required",
                )

        result = self.broker.submit_order(request)
        return ExecutionResult(
            plan=plan,
            dry_run=False,
            submitted=result.status
            in (OrderStatus.FILLED, OrderStatus.PENDING),
            order=result,
            message=result.message or result.status.value,
        )

    def _assert_mode_allows_submit(
        self, *, submit: bool, confirm_demo: bool
    ) -> None:
        if not submit:
            return
        if self.account_mode == "live" and not self.settings.allow_live:
            raise LiveTradingRefused(
                "Live trading is disabled (execution.allow_live=false). "
                "P6 is demo-only; enable live only after Definition of Done (P9)."
            )
        if self.account_mode != "demo":
            raise LiveTradingRefused(
                f"account_mode={self.account_mode!r} — P6 refuses non-demo submits"
            )
        if not confirm_demo:
            raise DemoConfirmRequired(
                "Demo submit requires confirm_demo=True "
                "(CLI: --submit --confirm-demo)"
            )
