"""Alert stubs — structured log + optional webhook (no required SaaS)."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from trading.logging_setup import get_logger

AlertLevel = Literal["info", "warning", "error", "critical"]


class AlertSink:
    """Emit operator alerts to logs and optionally POST JSON to a webhook URL.

    Webhook failures are logged and swallowed — alerting must not crash the trader.
    """

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        enabled: bool = True,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.webhook_url = (webhook_url or "").strip() or None
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._log = get_logger("trading.alerts")
        self.emitted: list[dict[str, Any]] = []

    def emit(
        self,
        level: AlertLevel,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "level": level,
            "code": code,
            "message": message,
            "payload": payload or {},
        }
        if not self.enabled:
            return event

        self.emitted.append(event)
        log_fn = {
            "info": self._log.info,
            "warning": self._log.warning,
            "error": self._log.error,
            "critical": self._log.error,
        }.get(level, self._log.warning)
        log_fn("alert", code=code, message=message, level=level, **(payload or {}))

        if self.webhook_url:
            self._post_webhook(event)
        return event

    def _post_webhook(self, event: dict[str, Any]) -> None:
        assert self.webhook_url is not None
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                resp = client.post(self.webhook_url, json=event)
                if resp.status_code >= 400:
                    self._log.warning(
                        "alert_webhook_failed",
                        status=resp.status_code,
                        url=self.webhook_url,
                    )
        except Exception as exc:  # noqa: BLE001 — never raise from alerts
            self._log.warning(
                "alert_webhook_error",
                error=str(exc),
                url=self.webhook_url,
            )
