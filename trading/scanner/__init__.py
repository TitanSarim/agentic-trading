"""Market scanner — eligibility + opportunity ranking (Phase 4).

Score ranks setups only. Candidates must still pass RiskEngine before any trade.
"""

from trading.scanner.eligibility import EligibilityContext, check_eligibility
from trading.scanner.engine import MarketScanner, MarketSnapshot, ScannerConfig
from trading.scanner.models import (
    EligibilityResult,
    ScanReport,
    ScannedMarket,
    ScoreComponents,
)
from trading.scanner.scoring import score_market

__all__ = [
    "EligibilityContext",
    "EligibilityResult",
    "MarketScanner",
    "MarketSnapshot",
    "ScanReport",
    "ScannedMarket",
    "ScannerConfig",
    "ScoreComponents",
    "check_eligibility",
    "score_market",
]
