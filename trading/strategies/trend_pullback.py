"""Deterministic trend-pullback strategy (V1 research choice).

Rules (evaluated on closed bars only):
1. Trend: EMA fast above/below EMA slow.
2. Pullback: prior bar dipped toward EMA fast (long) / rose toward it (short).
3. Confirmation: current close resumes with the trend and clears prior bar high/low.
4. Stop: beyond recent swing ± ATR buffer (or ATR multiple from entry).
5. Target: fixed risk-reward from entry/stop.

Emits ``TradeCandidate`` with ``strategy="trend_pullback_v1"``.
Does not size, call the LLM, or place orders — risk engine remains boss.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.features.pipeline import (
    FeatureConfig,
    FeatureRow,
    compute_features,
    features_ready,
)
from trading.types import Bar, TradeCandidate


STRATEGY_NAME = "trend_pullback_v1"


@dataclass(frozen=True, slots=True)
class TrendPullbackConfig:
    ema_fast: int = 8
    ema_slow: int = 21
    atr_period: int = 14
    rsi_period: int = 14
    swing_lookback: int = 5
    pullback_atr_frac: float = 0.6
    stop_atr_mult: float = 1.5
    risk_reward: float = 2.0
    min_setup_score: float = 50.0
    # Touch EMA: close or low/high within this ATR fraction of EMA fast.
    ema_touch_atr_frac: float = 0.35


class TrendPullbackStrategy:
    name = STRATEGY_NAME

    def __init__(self, config: TrendPullbackConfig | None = None) -> None:
        self.config = config or TrendPullbackConfig()
        self.feature_config = FeatureConfig(
            ema_fast=self.config.ema_fast,
            ema_slow=self.config.ema_slow,
            atr_period=self.config.atr_period,
            rsi_period=self.config.rsi_period,
            swing_lookback=self.config.swing_lookback,
        )

    def evaluate(
        self,
        symbol: str,
        bars: list[Bar],
        *,
        at_index: int | None = None,
    ) -> TradeCandidate | None:
        """Evaluate setup at ``at_index`` (default: last closed bar)."""
        if len(bars) < self.config.ema_slow + 2:
            return None
        idx = len(bars) - 1 if at_index is None else at_index
        if idx < 1 or idx >= len(bars):
            return None

        # Features only from bars through idx — no future bars.
        window = bars[: idx + 1]
        features = compute_features(window, self.feature_config)
        cur = features[idx]
        prev = features[idx - 1]
        if not features_ready(cur) or not features_ready(prev):
            return None
        assert cur.atr is not None and cur.ema_fast is not None
        assert prev.ema_fast is not None

        long_c = self._long_candidate(symbol, cur, prev)
        if long_c is not None:
            return long_c
        return self._short_candidate(symbol, cur, prev)

    def evaluate_series(
        self,
        symbol: str,
        bars: list[Bar],
    ) -> list[tuple[int, TradeCandidate]]:
        """Return (bar_index, candidate) for each bar that fires a setup."""
        out: list[tuple[int, TradeCandidate]] = []
        # Warmup: need slow EMA + 1 prior bar.
        start = self.config.ema_slow + 1
        for i in range(start, len(bars)):
            cand = self.evaluate(symbol, bars, at_index=i)
            if cand is not None:
                out.append((i, cand))
        return out

    def _setup_score(
        self,
        *,
        trend: str,
        rsi: float,
        pullback_depth: float,
        atr: float,
    ) -> float:
        score = 55.0
        if trend in ("up", "down"):
            score += 15.0
        # Prefer RSI not extreme against the move.
        if trend == "up" and 40 <= rsi <= 70:
            score += 10.0
        if trend == "down" and 30 <= rsi <= 60:
            score += 10.0
        if atr > 0:
            depth_frac = pullback_depth / atr
            if 0.2 <= depth_frac <= 1.2:
                score += 10.0
        return min(100.0, score)

    def _long_candidate(
        self,
        symbol: str,
        cur: FeatureRow,
        prev: FeatureRow,
    ) -> TradeCandidate | None:
        cfg = self.config
        assert cur.atr is not None and cur.ema_fast is not None
        assert prev.ema_fast is not None and cur.rsi is not None
        if cur.trend != "up":
            return None

        # Pullback: previous bar traded toward / below EMA fast.
        touch_band = cfg.ema_touch_atr_frac * cur.atr
        pulled = prev.low <= prev.ema_fast + touch_band
        if not pulled:
            return None

        # Confirmation: close resumes above EMA and clears prior high.
        if cur.close <= cur.ema_fast:
            return None
        if cur.close <= prev.high:
            return None

        entry = cur.close
        swing = cur.swing_low if cur.swing_low is not None else entry - cfg.stop_atr_mult * cur.atr
        stop = min(swing, entry - cfg.stop_atr_mult * cur.atr)
        # Ensure stop is below entry by at least a fraction of ATR.
        if entry - stop < cfg.pullback_atr_frac * cur.atr:
            stop = entry - cfg.stop_atr_mult * cur.atr
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + cfg.risk_reward * risk
        pullback_depth = max(0.0, prev.ema_fast - prev.low)
        score = self._setup_score(
            trend=cur.trend,
            rsi=cur.rsi,
            pullback_depth=pullback_depth,
            atr=cur.atr,
        )
        if score < cfg.min_setup_score:
            return None
        return TradeCandidate(
            symbol=symbol,
            strategy=STRATEGY_NAME,
            direction="LONG",
            entry=round(entry, 6),
            stop=round(stop, 6),
            target=round(target, 6),
            risk_reward=cfg.risk_reward,
            regime="trend",
            setup_score=score,
        )

    def _short_candidate(
        self,
        symbol: str,
        cur: FeatureRow,
        prev: FeatureRow,
    ) -> TradeCandidate | None:
        cfg = self.config
        assert cur.atr is not None and cur.ema_fast is not None
        assert prev.ema_fast is not None and cur.rsi is not None
        if cur.trend != "down":
            return None

        touch_band = cfg.ema_touch_atr_frac * cur.atr
        pulled = prev.high >= prev.ema_fast - touch_band
        if not pulled:
            return None

        if cur.close >= cur.ema_fast:
            return None
        if cur.close >= prev.low:
            return None

        entry = cur.close
        swing = cur.swing_high if cur.swing_high is not None else entry + cfg.stop_atr_mult * cur.atr
        stop = max(swing, entry + cfg.stop_atr_mult * cur.atr)
        if stop - entry < cfg.pullback_atr_frac * cur.atr:
            stop = entry + cfg.stop_atr_mult * cur.atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - cfg.risk_reward * risk
        pullback_depth = max(0.0, prev.high - prev.ema_fast)
        score = self._setup_score(
            trend=cur.trend,
            rsi=cur.rsi,
            pullback_depth=pullback_depth,
            atr=cur.atr,
        )
        if score < cfg.min_setup_score:
            return None
        return TradeCandidate(
            symbol=symbol,
            strategy=STRATEGY_NAME,
            direction="SHORT",
            entry=round(entry, 6),
            stop=round(stop, 6),
            target=round(target, 6),
            risk_reward=cfg.risk_reward,
            regime="trend",
            setup_score=score,
        )
