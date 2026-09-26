"""Indicator / feature pipeline — no look-ahead.

Every feature at bar index ``i`` uses only bars ``[0 .. i]`` (the closed bar
at ``i`` and history). Callers must not use future bars for decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.types import Bar


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Features aligned to a single closed bar."""

    index: int
    close: float
    high: float
    low: float
    open: float
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    rsi: float | None
    trend: str  # "up" | "down" | "flat" | "unknown"
    swing_low: float | None
    swing_high: float | None


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    ema_fast: int = 8
    ema_slow: int = 21
    atr_period: int = 14
    rsi_period: int = 14
    swing_lookback: int = 5


def _ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _true_ranges(bars: list[Bar]) -> list[float]:
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar.high - bar.low)
        else:
            prev_close = bars[i - 1].close
            trs.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
    return trs


def _atr_series(bars: list[Bar], period: int) -> list[float | None]:
    trs = _true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if period <= 0 or len(trs) < period:
        return out
    # Wilder-style smoothing from SMA seed.
    atr = sum(trs[:period]) / period
    out[period - 1] = atr
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def _rsi_series(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _trend(ema_fast: float | None, ema_slow: float | None, *, eps: float) -> str:
    if ema_fast is None or ema_slow is None:
        return "unknown"
    if ema_fast > ema_slow + eps:
        return "up"
    if ema_fast < ema_slow - eps:
        return "down"
    return "flat"


def compute_features(
    bars: list[Bar],
    config: FeatureConfig | None = None,
) -> list[FeatureRow]:
    """Build feature rows for ``bars`` with no look-ahead."""
    cfg = config or FeatureConfig()
    if not bars:
        return []

    closes = [b.close for b in bars]
    ema_fast = _ema_series(closes, cfg.ema_fast)
    ema_slow = _ema_series(closes, cfg.ema_slow)
    atr = _atr_series(bars, cfg.atr_period)
    rsi = _rsi_series(closes, cfg.rsi_period)

    rows: list[FeatureRow] = []
    for i, bar in enumerate(bars):
        atr_i = atr[i]
        eps = (atr_i * 0.05) if atr_i is not None else 0.0
        # Swing extremes use only bars up to i (inclusive).
        lo = max(0, i - cfg.swing_lookback + 1)
        window = bars[lo : i + 1]
        swing_low = min(b.low for b in window) if window else None
        swing_high = max(b.high for b in window) if window else None
        rows.append(
            FeatureRow(
                index=i,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                open=bar.open,
                ema_fast=ema_fast[i],
                ema_slow=ema_slow[i],
                atr=atr_i,
                rsi=rsi[i],
                trend=_trend(ema_fast[i], ema_slow[i], eps=eps),
                swing_low=swing_low,
                swing_high=swing_high,
            )
        )
    return rows


def features_ready(row: FeatureRow) -> bool:
    return (
        row.ema_fast is not None
        and row.ema_slow is not None
        and row.atr is not None
        and row.atr > 0
        and row.rsi is not None
    )
