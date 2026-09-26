"""Deterministic opportunity scoring (rank only — never fires a trade).

opportunity_score =
  trend_score + volatility_score + momentum_score + setup_quality + liquidity_score
  - spread_penalty - abnormal_volatility_penalty - correlation_penalty
"""

from __future__ import annotations

from trading.config import ScannerSettings
from trading.features.pipeline import FeatureConfig, FeatureRow, compute_features, features_ready
from trading.risk.engine import DEFAULT_PIP_SIZE
from trading.scanner.models import ScoreComponents
from trading.types import Bar


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _pip_size(symbol: str, overrides: dict[str, float] | None = None) -> float:
    if overrides and symbol in overrides:
        return overrides[symbol]
    return DEFAULT_PIP_SIZE.get(symbol, 0.0001)


def _trend_score(row: FeatureRow) -> float:
    if row.trend == "up" or row.trend == "down":
        base = 22.0
        # Stronger separation of EMAs → slightly higher.
        if row.ema_fast is not None and row.ema_slow is not None and row.atr:
            sep = abs(row.ema_fast - row.ema_slow) / row.atr
            return _clamp(base + min(8.0, sep * 4.0))
        return base
    if row.trend == "flat":
        return 8.0
    return 0.0


def _volatility_score(row: FeatureRow, atr_median: float) -> float:
    """Prefer tradable ATR — neither dead nor extreme vs recent median."""
    if row.atr is None or row.atr <= 0 or atr_median <= 0:
        return 0.0
    ratio = row.atr / atr_median
    if 0.7 <= ratio <= 1.4:
        return 18.0
    if 0.5 <= ratio < 0.7 or 1.4 < ratio <= 1.8:
        return 10.0
    return 4.0


def _momentum_score(row: FeatureRow) -> float:
    if row.rsi is None:
        return 0.0
    rsi = row.rsi
    # Mild trend-aligned RSI is best; extremes score lower.
    if 45 <= rsi <= 65:
        return 18.0
    if 35 <= rsi < 45 or 65 < rsi <= 75:
        return 12.0
    if 25 <= rsi < 35 or 75 < rsi <= 85:
        return 6.0
    return 2.0


def _setup_quality(row: FeatureRow, prev: FeatureRow | None) -> float:
    """Heuristic quality from price vs EMA / swing structure (no strategy fire)."""
    score = 8.0
    if row.ema_fast is None or row.atr is None or row.atr <= 0:
        return score
    dist = abs(row.close - row.ema_fast) / row.atr
    # Near EMA → pullback-friendly; far → breakout-ish; both ok but capped.
    if dist <= 0.5:
        score += 10.0
    elif dist <= 1.2:
        score += 6.0
    else:
        score += 2.0
    if prev is not None and prev.atr is not None:
        # Range contraction then expansion hint.
        if row.high - row.low > 0 and prev.high - prev.low > 0:
            if (row.high - row.low) >= (prev.high - prev.low):
                score += 4.0
    if row.trend in ("up", "down"):
        score += 4.0
    return _clamp(score, 0.0, 30.0)


def _liquidity_score(bars: list[Bar], lookback: int = 20) -> float:
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if not window:
        return 0.0
    vols = [b.volume for b in window]
    avg = sum(vols) / len(vols)
    last = window[-1].volume
    if avg <= 0:
        return 8.0
    ratio = last / avg
    if ratio >= 1.0:
        return 16.0
    if ratio >= 0.7:
        return 12.0
    return 6.0


def _spread_penalty(
    symbol: str,
    bars: list[Bar],
    settings: ScannerSettings,
    pip_overrides: dict[str, float] | None,
) -> float:
    spread = bars[-1].spread
    if spread is None:
        return 0.0
    pip = _pip_size(symbol, pip_overrides)
    if pip <= 0:
        return 0.0
    spread_pips = spread / pip
    max_spread = settings.max_spread_pips_by_symbol.get(
        symbol.upper(), settings.max_spread_pips
    )
    # Soft penalty before hard eligibility cutoff.
    soft = (max_spread * 0.5) if max_spread else 1.5
    if spread_pips <= soft:
        return 0.0
    excess = spread_pips - soft
    return _clamp(excess * 4.0, 0.0, 25.0)


def _abnormal_vol_penalty(row: FeatureRow, atr_median: float) -> float:
    if row.atr is None or atr_median <= 0:
        return 0.0
    ratio = row.atr / atr_median
    if ratio <= 2.0:
        return 0.0
    return _clamp((ratio - 2.0) * 10.0, 0.0, 30.0)


def _atr_median(features: list[FeatureRow], lookback: int = 40) -> float:
    atrs = [f.atr for f in features[-lookback:] if f.atr is not None and f.atr > 0]
    if not atrs:
        return 0.0
    ordered = sorted(atrs)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def score_market(
    symbol: str,
    bars: list[Bar],
    settings: ScannerSettings,
    *,
    feature_config: FeatureConfig | None = None,
    pip_size_overrides: dict[str, float] | None = None,
    correlation_penalty: float = 0.0,
) -> tuple[ScoreComponents, str]:
    """Compute score components + regime label. Deterministic for fixed bars."""
    cfg = feature_config or FeatureConfig()
    features = compute_features(bars, cfg)
    if not features or not features_ready(features[-1]):
        empty = ScoreComponents()
        return empty, "unknown"

    cur = features[-1]
    prev = features[-2] if len(features) >= 2 else None
    atr_med = _atr_median(features)

    components = ScoreComponents(
        trend_score=_trend_score(cur),
        volatility_score=_volatility_score(cur, atr_med),
        momentum_score=_momentum_score(cur),
        setup_quality=_setup_quality(cur, prev),
        liquidity_score=_liquidity_score(bars),
        spread_penalty=_spread_penalty(symbol, bars, settings, pip_size_overrides),
        abnormal_volatility_penalty=_abnormal_vol_penalty(cur, atr_med),
        correlation_penalty=_clamp(correlation_penalty, 0.0, 40.0),
    )
    regime = cur.trend if cur.trend != "unknown" else "unknown"
    return components, regime
