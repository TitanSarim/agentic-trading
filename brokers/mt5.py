"""MetaTrader5 Python adapter stub (Windows only; real calls behind interface).

P1: connect/submit raise NotImplementedError until Phase 6 demo wiring.
Cloud/CI must use brokers.mock.MockBroker — this module must never be required
to import MetaTrader5 at package import time.
"""

from __future__ import annotations

from trading.types import AccountState, OrderRequest, OrderResult, Position


class Mt5Broker:
    """Stub for co-located MetaTrader5 Python on Windows.

    Install on the MT5 host only::

        pip install MetaTrader5

    Terminal path (locked)::

        C:\\Program Files\\MetaTrader 5 IC Markets Global\\terminal64.exe
    """

    name = "mt5"

    def __init__(
        self,
        *,
        terminal_path: str,
        account_mode: str = "demo",
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
    ) -> None:
        self.terminal_path = terminal_path
        self.account_mode = account_mode
        self.login = login
        self.password = password
        self.server = server
        self._connected = False
        self._mt5 = None

    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "MetaTrader5 package not installed. "
                "Install on Windows next to terminal64.exe, or use broker.backend=mock."
            ) from exc

        if self.account_mode == "live":
            # Soft gate: require explicit awareness; still no CLI live path in P1.
            pass

        initialized = mt5.initialize(path=self.terminal_path)
        if not initialized:
            err = mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {err}")

        if self.login is not None:
            ok = mt5.login(self.login, password=self.password, server=self.server)
            if not ok:
                err = mt5.last_error()
                mt5.shutdown()
                raise RuntimeError(f"MT5 login failed: {err}")

        self._mt5 = mt5
        self._connected = True

    def disconnect(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
        self._mt5 = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def account_state(self) -> AccountState:
        self._require_connected()
        raise NotImplementedError("MT5 account_state — Phase 6 demo wiring")

    def positions(self) -> list[Position]:
        self._require_connected()
        raise NotImplementedError("MT5 positions — Phase 6 demo wiring")

    def submit_order(self, request: OrderRequest) -> OrderResult:
        self._require_connected()
        # Hard stop for P1: never place live/demo orders from this stub path yet.
        raise NotImplementedError(
            "MT5 submit_order not implemented in Phase 1 — use MockBroker; "
            "demo execution is Phase 6"
        )

    def cancel_order(self, client_order_id: str) -> OrderResult:
        self._require_connected()
        raise NotImplementedError("MT5 cancel_order — Phase 6 demo wiring")

    def _require_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise RuntimeError("Mt5Broker is not connected")
