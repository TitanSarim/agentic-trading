"""Market scanner — eligibility + ranking. Score never alone opens a trade."""

from __future__ import annotations

from dataclasses import dataclass, field

from trading.config import ScannerSettings, StrategySettings
from trading.features.pipeline import FeatureConfig
from trading.risk.engine import RiskEngine
from trading.scanner.eligibility import EligibilityContext, check_eligibility
from trading.scanner.models import ScanReport, ScannedMarket, ScoreComponents
from trading.scanner.scoring import score_market
from trading.strategies.trend_pullback import TrendPullbackConfig, TrendPullbackStrategy
from trading.types import Bar, Timeframe


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Bars for one symbol/timeframe pair."""

    symbol: str
    timeframe: Timeframe
    bars: list[Bar]


@dataclass
class ScannerConfig:
    """Runtime scanner knobs (usually from Settings.scanner)."""

    settings: ScannerSettings = field(default_factory=ScannerSettings)
    strategy: StrategySettings | None = None
    evaluate_strategy: bool = True
    correlation_groups: list[list[str]] = field(
        default_factory=lambda: [["EURUSD", "GBPUSD"]]
    )
    pip_size: dict[str, float] = field(default_factory=dict)


class MarketScanner:
    """Rank eligible markets by opportunity score.

    Authority: ranking only. Strategy may emit a candidate for top-N; RiskEngine
    remains the boss before any order path (not invoked here).
    """

    name = "scanner_v1"

    def __init__(
        self,
        config: ScannerConfig | None = None,
        *,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self.config = config or ScannerConfig()
        self.risk_engine = risk_engine
        strat_settings = self.config.strategy
        if strat_settings is not None:
            self._strategy = TrendPullbackStrategy(
                TrendPullbackConfig(
                    ema_fast=strat_settings.ema_fast,
                    ema_slow=strat_settings.ema_slow,
                    atr_period=strat_settings.atr_period,
                    rsi_period=strat_settings.rsi_period,
                    swing_lookback=strat_settings.swing_lookback,
                    pullback_atr_frac=strat_settings.pullback_atr_frac,
                    stop_atr_mult=strat_settings.stop_atr_mult,
                    risk_reward=strat_settings.risk_reward,
                    min_setup_score=strat_settings.min_setup_score,
                    ema_touch_atr_frac=strat_settings.ema_touch_atr_frac,
                )
            )
            self._feature_config = FeatureConfig(
                ema_fast=strat_settings.ema_fast,
                ema_slow=strat_settings.ema_slow,
                atr_period=strat_settings.atr_period,
                rsi_period=strat_settings.rsi_period,
                swing_lookback=strat_settings.swing_lookback,
            )
        else:
            self._strategy = TrendPullbackStrategy()
            self._feature_config = FeatureConfig()

    def rank(
        self,
        snapshots: list[MarketSnapshot],
        *,
        context: EligibilityContext | None = None,
    ) -> ScanReport:
        """Score and rank markets. Deterministic for fixed snapshots + config."""
        ctx = context or EligibilityContext(risk_engine=self.risk_engine)
        if ctx.risk_engine is None and self.risk_engine is not None:
            ctx = EligibilityContext(
                now=ctx.now,
                broker_available=ctx.broker_available,
                api_healthy=ctx.api_healthy,
                market_open=ctx.market_open,
                risk_engine=self.risk_engine,
            )

        risk_locked = False
        risk_lock_reason = ""
        if ctx.risk_engine is not None and ctx.risk_engine.new_trades_locked:
            risk_locked = True
            risk_lock_reason = ctx.risk_engine.lock_reason

        # First pass: eligibility + raw scores (no correlation penalty yet).
        raw: list[ScannedMarket] = []
        for snap in snapshots:
            elig = check_eligibility(
                snap.symbol,
                snap.timeframe,
                snap.bars,
                self.config.settings,
                context=ctx,
                feature_config=self._feature_config,
                pip_size_overrides=self.config.pip_size or None,
            )
            if not elig.eligible:
                raw.append(
                    ScannedMarket(
                        symbol=snap.symbol,
                        timeframe=snap.timeframe,
                        eligible=False,
                        reason_code=elig.reason_code,
                        opportunity_score=0.0,
                        components=ScoreComponents(),
                    )
                )
                continue

            components, regime = score_market(
                snap.symbol,
                snap.bars,
                self.config.settings,
                feature_config=self._feature_config,
                pip_size_overrides=self.config.pip_size or None,
                correlation_penalty=0.0,
            )
            raw.append(
                ScannedMarket(
                    symbol=snap.symbol,
                    timeframe=snap.timeframe,
                    eligible=True,
                    reason_code="OK",
                    opportunity_score=round(components.total, 6),
                    components=components,
                    regime=regime,
                )
            )

        # Correlation penalty among eligible peers in the same group (same TF).
        adjusted = self._apply_correlation_penalties(raw)

        # Sort: eligible first by score desc, then symbol/tf for stability.
        eligible = [m for m in adjusted if m.eligible]
        ineligible = [m for m in adjusted if not m.eligible]
        eligible.sort(
            key=lambda m: (-m.opportunity_score, m.symbol, m.timeframe.value)
        )
        for i, market in enumerate(eligible, start=1):
            market.rank = i

        top_n = max(0, self.config.settings.top_n)
        selected: list[ScannedMarket] = []
        for market in eligible[:top_n]:
            market.selected_for_strategy = True
            if self.config.evaluate_strategy:
                snap = next(
                    s
                    for s in snapshots
                    if s.symbol == market.symbol and s.timeframe == market.timeframe
                )
                market.candidate = self._strategy.evaluate(market.symbol, snap.bars)
            selected.append(market)

        markets = eligible + ineligible
        return ScanReport(
            markets=markets,
            top_n=top_n,
            selected=selected,
            risk_locked=risk_locked,
            risk_lock_reason=risk_lock_reason,
        )

    def _apply_correlation_penalties(
        self, markets: list[ScannedMarket]
    ) -> list[ScannedMarket]:
        """Penalize lower-ranked symbols that share a correlation group.

        Within each (timeframe, group), the highest raw score keeps full weight;
        others take ``correlation_penalty`` from config. Does not block eligibility.
        """
        penalty = self.config.settings.correlation_penalty
        if penalty <= 0 or not self.config.correlation_groups:
            return markets

        # Index eligible by timeframe.
        by_tf: dict[Timeframe, list[ScannedMarket]] = {}
        for m in markets:
            if m.eligible:
                by_tf.setdefault(m.timeframe, []).append(m)

        # Map symbol -> group id
        group_of: dict[str, int] = {}
        for gi, group in enumerate(self.config.correlation_groups):
            for sym in group:
                group_of[sym.upper()] = gi

        out: list[ScannedMarket] = []
        for m in markets:
            if not m.eligible or m.symbol.upper() not in group_of:
                out.append(m)
                continue
            peers = [
                p
                for p in by_tf.get(m.timeframe, [])
                if group_of.get(p.symbol.upper()) == group_of[m.symbol.upper()]
            ]
            peers_sorted = sorted(
                peers,
                key=lambda p: (-p.opportunity_score, p.symbol),
            )
            if peers_sorted and peers_sorted[0].symbol == m.symbol:
                out.append(m)
                continue
            # Lower peer in correlated group → apply penalty.
            comps = m.components.model_copy(
                update={"correlation_penalty": penalty}
            )
            out.append(
                m.model_copy(
                    update={
                        "components": comps,
                        "opportunity_score": round(comps.total, 6),
                    }
                )
            )
        return out
