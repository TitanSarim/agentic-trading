"""Phase 4 scanner — eligibility, deterministic ranking, no trade authority."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.config import ScannerSettings
from trading.data.mock import MockMarketData
from trading.risk.engine import RiskEngine
from trading.scanner import (
    EligibilityContext,
    MarketScanner,
    MarketSnapshot,
    ScannerConfig,
    check_eligibility,
    score_market,
)
from trading.types import Bar, Timeframe


def _now() -> datetime:
    return datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _snapshots(
    *,
    symbols: list[str] | None = None,
    timeframes: list[Timeframe] | None = None,
    count: int = 120,
    scenario: str = "trend_pullback",
    seed: int = 7,
) -> list[MarketSnapshot]:
    symbols = symbols or ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    timeframes = timeframes or [Timeframe.M5, Timeframe.M15]
    market = MockMarketData(symbols=symbols, scenario=scenario, seed=seed)  # type: ignore[arg-type]
    out: list[MarketSnapshot] = []
    for sym in symbols:
        for tf in timeframes:
            bars = market.get_bars(sym, tf, count=count)
            # Anchor last bar to frozen "now" so freshness gates are stable.
            anchored = _anchor_bars(bars, _now(), tf)
            out.append(MarketSnapshot(symbol=sym, timeframe=tf, bars=anchored))
    return out


def _anchor_bars(bars: list[Bar], end: datetime, tf: Timeframe) -> list[Bar]:
    step = timedelta(minutes=5 if tf == Timeframe.M5 else 15)
    start = end - step * (len(bars) - 1)
    out: list[Bar] = []
    for i, bar in enumerate(bars):
        out.append(
            bar.model_copy(update={"time": start + step * i})
        )
    return out


def test_score_deterministic() -> None:
    snaps = _snapshots(symbols=["EURUSD"], timeframes=[Timeframe.M5], seed=21)
    bars = snaps[0].bars
    settings = ScannerSettings()
    a, regime_a = score_market("EURUSD", bars, settings)
    b, regime_b = score_market("EURUSD", bars, settings)
    assert a == b
    assert regime_a == regime_b
    assert a.total == b.total


def test_rank_deterministic_and_top_n() -> None:
    snaps = _snapshots(seed=42)
    cfg = ScannerConfig(settings=ScannerSettings(top_n=2, min_bars=64))
    scanner = MarketScanner(cfg)
    ctx = EligibilityContext(now=_now())
    r1 = scanner.rank(snaps, context=ctx)
    r2 = scanner.rank(snaps, context=ctx)
    assert r1.to_dict() == r2.to_dict()
    eligible = [m for m in r1.markets if m.eligible]
    assert eligible
    assert all(m.rank is not None for m in eligible)
    # Rank order matches score desc.
    scores = [m.opportunity_score for m in eligible]
    assert scores == sorted(scores, reverse=True)
    assert len(r1.selected) == min(2, len(eligible))
    assert all(m.selected_for_strategy for m in r1.selected)
    # Non-selected eligible are not flagged.
    selected_keys = {(m.symbol, m.timeframe) for m in r1.selected}
    for m in eligible:
        if (m.symbol, m.timeframe) not in selected_keys:
            assert m.selected_for_strategy is False


def test_eligibility_stale_and_spread() -> None:
    snaps = _snapshots(symbols=["EURUSD"], timeframes=[Timeframe.M5], count=80)
    bars = snaps[0].bars
    settings = ScannerSettings(min_bars=64, max_data_age_seconds=60, max_spread_pips=3.0)

    stale_ctx = EligibilityContext(now=_now() + timedelta(hours=2))
    stale = check_eligibility(
        "EURUSD", Timeframe.M5, bars, settings, context=stale_ctx
    )
    assert not stale.eligible
    assert stale.reason_code == "STALE_DATA"

    wide = [b.model_copy(update={"spread": 0.01}) for b in bars]
    wide_result = check_eligibility(
        "EURUSD",
        Timeframe.M5,
        wide,
        settings,
        context=EligibilityContext(now=_now()),
    )
    assert not wide_result.eligible
    assert wide_result.reason_code == "SPREAD_TOO_WIDE"


def test_eligibility_insufficient_bars_and_risk_lock() -> None:
    settings = ScannerSettings(min_bars=64)
    few = _snapshots(symbols=["GBPUSD"], timeframes=[Timeframe.M15], count=10)[0].bars
    result = check_eligibility(
        "GBPUSD",
        Timeframe.M15,
        few,
        settings,
        context=EligibilityContext(now=_now()),
    )
    assert not result.eligible
    assert result.reason_code == "INSUFFICIENT_BARS"

    risk = RiskEngine()
    risk.lock_new_trades("TEST_LOCK")
    snaps = _snapshots(symbols=["USDJPY"], timeframes=[Timeframe.M5], count=100)
    locked = check_eligibility(
        "USDJPY",
        Timeframe.M5,
        snaps[0].bars,
        settings,
        context=EligibilityContext(now=_now(), risk_engine=risk),
    )
    assert not locked.eligible
    assert locked.reason_code == "RISK_LOCKED"


def test_abnormal_gap_rejects() -> None:
    snaps = _snapshots(symbols=["EURUSD"], timeframes=[Timeframe.M5], count=80)
    bars = list(snaps[0].bars)
    # Force a huge gap on the last bar open vs prior close.
    prior = bars[-2]
    bars[-1] = bars[-1].model_copy(
        update={"open": prior.close * 1.05, "spread": 0.00012}
    )
    settings = ScannerSettings(min_bars=64, max_gap_frac=0.01)
    result = check_eligibility(
        "EURUSD",
        Timeframe.M5,
        bars,
        settings,
        context=EligibilityContext(now=_now()),
    )
    assert not result.eligible
    assert result.reason_code == "ABNORMAL_GAP"


def test_score_never_alone_trades() -> None:
    """Scanner may emit candidates for top-N but never submits orders / sizes."""
    snaps = _snapshots(seed=3)
    scanner = MarketScanner(
        ScannerConfig(
            settings=ScannerSettings(top_n=3, evaluate_strategy=True),
            evaluate_strategy=True,
        )
    )
    report = scanner.rank(snaps, context=EligibilityContext(now=_now()))
    # No RiskDecision / volume / order fields on the report.
    payload = report.to_dict()
    assert "note" in payload
    assert "never opens a trade" in payload["note"].lower() or "never" in payload["note"].lower()
    for row in report.selected:
        # Candidate optional; even if present, scanner does not approve risk.
        assert row.selected_for_strategy is True
    # Ensure MarketScanner has no submit/validate_and_size API confusion.
    assert not hasattr(scanner, "validate_and_size")
    assert not hasattr(scanner, "submit_order")


def test_correlation_penalty_applied() -> None:
    snaps = _snapshots(
        symbols=["EURUSD", "GBPUSD"],
        timeframes=[Timeframe.M5],
        seed=99,
    )
    cfg = ScannerConfig(
        settings=ScannerSettings(top_n=2, correlation_penalty=15.0, min_bars=64),
        correlation_groups=[["EURUSD", "GBPUSD"]],
    )
    report = MarketScanner(cfg).rank(snaps, context=EligibilityContext(now=_now()))
    eligible = [m for m in report.markets if m.eligible]
    assert len(eligible) == 2
    # Exactly one of the pair should carry the correlation penalty (the lower).
    penalties = [m.components.correlation_penalty for m in eligible]
    assert 15.0 in penalties
    assert 0.0 in penalties


def test_broker_and_market_flags() -> None:
    snaps = _snapshots(symbols=["EURUSD"], timeframes=[Timeframe.M5], count=80)
    settings = ScannerSettings(min_bars=64)
    closed = check_eligibility(
        "EURUSD",
        Timeframe.M5,
        snaps[0].bars,
        settings,
        context=EligibilityContext(now=_now(), market_open=False),
    )
    assert closed.reason_code == "MARKET_CLOSED"
    down = check_eligibility(
        "EURUSD",
        Timeframe.M5,
        snaps[0].bars,
        settings,
        context=EligibilityContext(now=_now(), broker_available=False),
    )
    assert down.reason_code == "BROKER_UNAVAILABLE"
