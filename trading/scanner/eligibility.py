"""Eligibility gates for the market scanner (fail closed).

Gates (plan §4.4): sufficient data, freshness, spread, abnormal gap,
broker availability, API health, market session, active risk lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading.config import ScannerSettings
from trading.features.pipeline import FeatureConfig, compute_features, features_ready
from trading.risk.engine import DEFAULT_PIP_SIZE, RiskEngine
from trading.scanner.models import EligibilityResult
from trading.types import Bar, Timeframe


@dataclass(frozen=True, slots=True)
class EligibilityContext:
    """Optional runtime flags supplied by the caller / CLI."""

    now: datetime | None = None
    broker_available: bool = True
    api_healthy: bool = True
    market_open: bool = True
    risk_engine: RiskEngine | None = None


def _pip_size(symbol: str, overrides: dict[str, float] | None = None) -> float:
    if overrides and symbol in overrides:
        return overrides[symbol]
    return DEFAULT_PIP_SIZE.get(symbol, 0.0001)


def check_eligibility(
    symbol: str,
    timeframe: Timeframe,
    bars: list[Bar],
    settings: ScannerSettings,
    *,
    context: EligibilityContext | None = None,
    feature_config: FeatureConfig | None = None,
    pip_size_overrides: dict[str, float] | None = None,
) -> EligibilityResult:
    """Return whether ``symbol``/``timeframe`` may enter ranking."""
    ctx = context or EligibilityContext()
    now = ctx.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if ctx.risk_engine is not None and ctx.risk_engine.new_trades_locked:
        return EligibilityResult(
            eligible=False,
            reason_code="RISK_LOCKED",
            details={"lock_reason": ctx.risk_engine.lock_reason},
        )

    if not ctx.broker_available:
        return EligibilityResult(eligible=False, reason_code="BROKER_UNAVAILABLE")

    if not ctx.api_healthy:
        return EligibilityResult(eligible=False, reason_code="API_UNHEALTHY")

    if not ctx.market_open:
        return EligibilityResult(eligible=False, reason_code="MARKET_CLOSED")

    if len(bars) < settings.min_bars:
        return EligibilityResult(
            eligible=False,
            reason_code="INSUFFICIENT_BARS",
            details={"bars": len(bars), "min_bars": settings.min_bars},
        )

    last = bars[-1]
    bar_time = last.time
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    age = (now - bar_time).total_seconds()
    if age > settings.max_data_age_seconds:
        return EligibilityResult(
            eligible=False,
            reason_code="STALE_DATA",
            details={"age_seconds": age, "max_age": settings.max_data_age_seconds},
        )

    pip = _pip_size(symbol, pip_size_overrides)
    spread = last.spread
    max_spread = settings.max_spread_pips_by_symbol.get(
        symbol.upper(), settings.max_spread_pips
    )
    if spread is not None and max_spread is not None:
        spread_pips = spread / pip if pip > 0 else float("inf")
        if spread_pips > max_spread:
            return EligibilityResult(
                eligible=False,
                reason_code="SPREAD_TOO_WIDE",
                details={
                    "spread": spread,
                    "spread_pips": round(spread_pips, 4),
                    "max_spread_pips": max_spread,
                },
            )

    # Abnormal gap: |open - prior close| vs ATR / close.
    if len(bars) >= 2:
        prior_close = bars[-2].close
        gap = abs(last.open - prior_close)
        gap_frac = gap / prior_close if prior_close else 0.0
        if gap_frac > settings.max_gap_frac:
            return EligibilityResult(
                eligible=False,
                reason_code="ABNORMAL_GAP",
                details={"gap_frac": round(gap_frac, 6), "max_gap_frac": settings.max_gap_frac},
            )

    features = compute_features(bars, feature_config or FeatureConfig())
    if not features or not features_ready(features[-1]):
        return EligibilityResult(eligible=False, reason_code="FEATURES_NOT_READY")

    return EligibilityResult(
        eligible=True,
        reason_code="OK",
        details={"age_seconds": age, "bars": len(bars), "timeframe": timeframe.value},
    )
