"""Idempotent client order IDs — retries must not double-submit."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def make_client_order_id(
    *,
    symbol: str,
    side: str,
    strategy: str = "",
    candidate_id: int | None = None,
    seed: str | None = None,
    created_at: datetime | None = None,
) -> str:
    """Build a stable, broker-safe client order id (≤31 chars for MT5 comment).

    Same inputs → same id (idempotent retries). Hash absorbs long strategy names.
    """
    ts = created_at or datetime.now(timezone.utc)
    # Minute bucket keeps id stable across brief retries in the same minute.
    bucket = ts.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    parts = [
        symbol.upper(),
        side.upper(),
        strategy or "na",
        str(candidate_id if candidate_id is not None else "x"),
        seed or "",
        bucket,
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]
    sym = _SAFE.sub("", symbol.upper())[:6]
    # Prefix marks agentic-trading demo orders for reconcile/comment scan.
    raw = f"at6_{sym}_{digest}"
    return raw[:31]
