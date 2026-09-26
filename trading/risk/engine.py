"""Phase 3 risk engine — final authority over sizing and new entries.

Qwen / strategy never bypass these gates. LLM ``risk_modifier`` may only
*reduce* size below the deterministic equity × risk-fraction cap.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trading.config import RiskSettings
from trading.risk.context import RiskMarketContext, RiskPortfolioContext
from trading.types import AccountState, AnalystDecision, RiskDecision, TradeCandidate

# Research point-value stubs (USD per 1.0 price unit per 1.0 lot).
# Same defaults as research.costs — Phase 6+ replaces with broker contracts.
DEFAULT_POINT_VALUE_PER_LOT: dict[str, float] = {
    "EURUSD": 100_000.0,
    "GBPUSD": 100_000.0,
    "USDJPY": 100_000.0 / 150.0,
    "XAUUSD": 100.0,
}

DEFAULT_PIP_SIZE: dict[str, float] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "XAUUSD": 0.01,
}


class RiskEngine:
    """Boss. Hard gates; analyst may only shrink size."""

    name = "risk_v1"

    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()
        if self.settings.allow_llm_increase_risk:
            raise ValueError("allow_llm_increase_risk must remain false in V1")
        self._new_trades_locked = False
        self._lock_reason = ""
        self._kill_switch = False
        self._kill_reason = ""
        self._halted = False
        self._halt_reason = ""
        # Tracked consecutive losses when callers use record_closed_trade.
        self._consecutive_losses = 0

    # --- Kill / halt hooks -------------------------------------------------

    def engage_kill_switch(self, reason: str = "KILL_SWITCH") -> None:
        """Emergency stop: lock all new entries until cleared."""
        self._kill_switch = True
        self._kill_reason = reason or "KILL_SWITCH"
        self.lock_new_trades(self._kill_reason)

    def clear_kill_switch(self) -> None:
        self._kill_switch = False
        self._kill_reason = ""
        if not self._halted:
            self._new_trades_locked = False
            self._lock_reason = ""

    def halt(self, reason: str = "HALTED") -> None:
        """Operator / auto halt (stale data storm, broker issues, etc.)."""
        self._halted = True
        self._halt_reason = reason or "HALTED"
        self.lock_new_trades(self._halt_reason)

    def clear_halt(self) -> None:
        self._halted = False
        self._halt_reason = ""
        if not self._kill_switch:
            self._new_trades_locked = False
            self._lock_reason = ""

    def lock_new_trades(self, reason: str) -> None:
        self._new_trades_locked = True
        self._lock_reason = reason

    def unlock_new_trades(self) -> None:
        if self._kill_switch or self._halted:
            return
        self._new_trades_locked = False
        self._lock_reason = ""

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def new_trades_locked(self) -> bool:
        return self._new_trades_locked or self._kill_switch or self._halted

    @property
    def lock_reason(self) -> str:
        if self._kill_switch:
            return self._kill_reason or "KILL_SWITCH"
        if self._halted:
            return self._halt_reason or "HALTED"
        return self._lock_reason

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def record_closed_trade(self, pnl: float) -> None:
        """Update consecutive-loss streak after a closed trade (research / journal)."""
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def reset_consecutive_losses(self) -> None:
        self._consecutive_losses = 0

    # --- State checks ------------------------------------------------------

    def account_risk_state(
        self,
        account: AccountState,
        portfolio: RiskPortfolioContext | None = None,
    ) -> RiskDecision:
        """Return whether new trades are locked (drawdown / consecutive / kill)."""
        if self._kill_switch:
            return RiskDecision(
                approved=False,
                reason_code=self._kill_reason or "KILL_SWITCH",
                new_trades_locked=True,
            )
        if self._halted:
            return RiskDecision(
                approved=False,
                reason_code=self._halt_reason or "HALTED",
                new_trades_locked=True,
            )
        if self._new_trades_locked:
            return RiskDecision(
                approved=False,
                reason_code=self._lock_reason or "NEW_TRADES_LOCKED",
                new_trades_locked=True,
            )

        if portfolio is not None:
            daily = self._daily_loss_hit(account, portfolio)
            if daily is not None:
                return daily
            weekly = self._weekly_drawdown_hit(account, portfolio)
            if weekly is not None:
                return weekly
            consec = self._consecutive_loss_hit(portfolio)
            if consec is not None:
                return consec

        return RiskDecision(approved=True, reason_code="OK", new_trades_locked=False)

    def validate_and_size(
        self,
        candidate: TradeCandidate,
        analyst: AnalystDecision,
        account: AccountState,
        *,
        market: RiskMarketContext | None = None,
        portfolio: RiskPortfolioContext | None = None,
    ) -> RiskDecision:
        """Approve or reject a candidate and compute volume.

        Order of gates (fail closed):
        kill/halt → account locks → max positions → averaging/martingale →
        correlation → data freshness → spread → analyst → stop → size.
        """
        state = self.account_risk_state(account, portfolio)
        if state.new_trades_locked or not state.approved:
            return state

        if portfolio is not None:
            exposure = self._exposure_gates(candidate, portfolio)
            if exposure is not None:
                return exposure

        if market is not None:
            freshness = self._data_freshness_gate(market)
            if freshness is not None:
                return freshness
            spread = self._spread_gate(candidate.symbol, market)
            if spread is not None:
                return spread

        if analyst.decision != "APPROVE":
            return RiskDecision(
                approved=False,
                reason_code="ANALYST_REJECT",
                new_trades_locked=False,
            )

        stop_distance = abs(candidate.entry - candidate.stop)
        if stop_distance <= 0:
            return RiskDecision(
                approved=False,
                reason_code="INVALID_STOP_DISTANCE",
            )

        # Mandatory stop: candidate.stop must differ from entry (already checked).
        # Confidence may only shrink further via risk_modifier path.
        modifier = self._clamp_modifier(analyst.risk_modifier)
        # Optional confidence haircut — never increases size.
        conf = max(0.0, min(1.0, analyst.confidence))
        if self.settings.apply_confidence_haircut:
            modifier = min(modifier, conf if conf > 0 else modifier)

        risk_fraction = self.settings.risk_fraction_per_trade
        risk_dollars = account.equity * risk_fraction * modifier
        if risk_dollars <= 0:
            return RiskDecision(
                approved=False,
                reason_code="SIZE_TOO_SMALL",
                risk_dollars=0.0,
            )

        point_value = self._point_value(candidate.symbol)
        raw_volume = risk_dollars / (stop_distance * point_value)
        volume = self._quantize_volume(raw_volume)
        if volume < self.settings.min_volume:
            return RiskDecision(
                approved=False,
                reason_code="SIZE_TOO_SMALL",
                risk_dollars=risk_dollars,
            )
        if volume > self.settings.max_volume:
            volume = self.settings.max_volume
            # Recalculate dollars actually risked at capped volume.
            risk_dollars = volume * stop_distance * point_value

        return RiskDecision(
            approved=True,
            volume=volume,
            reason_code="APPROVED",
            risk_dollars=risk_dollars,
            new_trades_locked=False,
        )

    # --- Internal gates ----------------------------------------------------

    def _clamp_modifier(self, modifier: float) -> float:
        # Hard clamp — LLM cannot increase above 1.0 even if schema slips.
        return min(1.0, max(0.0, float(modifier)))

    def _point_value(self, symbol: str) -> float:
        overrides = self.settings.point_value_per_lot
        if overrides and symbol in overrides:
            return float(overrides[symbol])
        return DEFAULT_POINT_VALUE_PER_LOT.get(symbol, 100_000.0)

    def _pip_size(self, symbol: str, market: RiskMarketContext) -> float:
        if market.pip_size is not None and market.pip_size > 0:
            return market.pip_size
        overrides = self.settings.pip_size
        if overrides and symbol in overrides:
            return float(overrides[symbol])
        return DEFAULT_PIP_SIZE.get(symbol, 0.0001)

    def _quantize_volume(self, volume: float) -> float:
        step = self.settings.volume_step
        if step <= 0:
            return round(volume, 2)
        # Round down to step so we never exceed risk dollars.
        # Epsilon avoids float edge cases (e.g. 0.01/0.01 → 0.999999…).
        units = int((volume + 1e-12) / step)
        return round(units * step, 8)

    def _daily_loss_hit(
        self,
        account: AccountState,
        portfolio: RiskPortfolioContext,
    ) -> RiskDecision | None:
        start = portfolio.day_start_equity
        if start <= 0:
            return RiskDecision(
                approved=False,
                reason_code="INVALID_DAY_MARK",
                new_trades_locked=True,
            )
        loss_pct = (start - account.equity) / start
        if loss_pct >= self.settings.daily_loss_lock_pct:
            self.lock_new_trades("DAILY_LOSS_LOCK")
            return RiskDecision(
                approved=False,
                reason_code="DAILY_LOSS_LOCK",
                new_trades_locked=True,
            )
        return None

    def _weekly_drawdown_hit(
        self,
        account: AccountState,
        portfolio: RiskPortfolioContext,
    ) -> RiskDecision | None:
        start = portfolio.week_start_equity
        if start <= 0:
            return RiskDecision(
                approved=False,
                reason_code="INVALID_WEEK_MARK",
                new_trades_locked=True,
            )
        dd_pct = (start - account.equity) / start
        if dd_pct >= self.settings.weekly_drawdown_lock_pct:
            self.lock_new_trades("WEEKLY_DRAWDOWN_LOCK")
            return RiskDecision(
                approved=False,
                reason_code="WEEKLY_DRAWDOWN_LOCK",
                new_trades_locked=True,
            )
        return None

    def _consecutive_loss_hit(
        self,
        portfolio: RiskPortfolioContext,
    ) -> RiskDecision | None:
        # Prefer explicit portfolio count; fall back to engine streak.
        consec = max(portfolio.consecutive_losses, self._consecutive_losses)
        limit = self.settings.consecutive_loss_lock
        if limit > 0 and consec >= limit:
            self.lock_new_trades("CONSECUTIVE_LOSS_LOCK")
            return RiskDecision(
                approved=False,
                reason_code="CONSECUTIVE_LOSS_LOCK",
                new_trades_locked=True,
            )
        return None

    def _exposure_gates(
        self,
        candidate: TradeCandidate,
        portfolio: RiskPortfolioContext,
    ) -> RiskDecision | None:
        if portfolio.open_count >= self.settings.max_positions:
            return RiskDecision(
                approved=False,
                reason_code="MAX_POSITIONS",
                new_trades_locked=False,
            )

        if self.settings.forbid_averaging_down and candidate.symbol in portfolio.open_symbols:
            return RiskDecision(
                approved=False,
                reason_code="AVERAGING_DOWN_FORBIDDEN",
                new_trades_locked=False,
            )

        # Martingale = doubling into a loser — blocked when same-symbol add
        # is forbidden (above) and max_positions is 1. Extra explicit code
        # when forbid_martingale and any same-symbol open exists.
        if self.settings.forbid_martingale and candidate.symbol in portfolio.open_symbols:
            return RiskDecision(
                approved=False,
                reason_code="MARTINGALE_FORBIDDEN",
                new_trades_locked=False,
            )

        # Correlation: reject if an open symbol shares a correlation group.
        for group in self.settings.correlation_groups:
            group_set = set(group)
            if candidate.symbol not in group_set:
                continue
            overlap = portfolio.open_symbols & group_set
            if overlap:
                return RiskDecision(
                    approved=False,
                    reason_code="CORRELATION_EXPOSURE",
                    new_trades_locked=False,
                )
        return None

    def _data_freshness_gate(
        self,
        market: RiskMarketContext,
    ) -> RiskDecision | None:
        max_age = self.settings.max_data_age_seconds
        if max_age <= 0:
            return None
        if market.bar_time is None:
            # Fail closed: no timestamp → cannot prove freshness.
            return RiskDecision(
                approved=False,
                reason_code="STALE_MARKET_DATA",
                new_trades_locked=False,
            )
        age = (market.now - market.bar_time).total_seconds()
        if age < 0:
            # Clock skew / future bar — treat as invalid.
            return RiskDecision(
                approved=False,
                reason_code="STALE_MARKET_DATA",
                new_trades_locked=False,
            )
        if age > max_age:
            return RiskDecision(
                approved=False,
                reason_code="STALE_MARKET_DATA",
                new_trades_locked=False,
            )
        return None

    def _spread_gate(
        self,
        symbol: str,
        market: RiskMarketContext,
    ) -> RiskDecision | None:
        max_pips = self.settings.max_spread_pips
        if max_pips is None or max_pips <= 0:
            return None
        if market.spread is None:
            return RiskDecision(
                approved=False,
                reason_code="SPREAD_UNKNOWN",
                new_trades_locked=False,
            )
        pip = self._pip_size(symbol, market)
        spread_pips = market.spread / pip if pip > 0 else float("inf")
        if spread_pips > max_pips:
            return RiskDecision(
                approved=False,
                reason_code="SPREAD_TOO_WIDE",
                new_trades_locked=False,
            )
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
