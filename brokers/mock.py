"""In-memory mock broker for tests and cloud CI (no MT5 required)."""

from __future__ import annotations

from trading.types import (
    AccountState,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    utc_now,
)


class MockBroker:
    """Deterministic broker stub. Does not place real trades."""

    name = "mock"

    def __init__(
        self,
        *,
        balance: float = 1000.0,
        account_mode: str = "demo",
        fill_orders: bool = True,
        require_take_profit: bool = True,
        allow_live: bool = False,
    ) -> None:
        self._balance = balance
        self._equity = balance
        self._account_mode = account_mode  # type: ignore[assignment]
        self._connected = False
        self._fill_orders = fill_orders
        self._require_take_profit = require_take_profit
        self._allow_live = allow_live
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderResult] = {}
        self._ticket_seq = 1000

    def connect(self) -> None:
        if self._account_mode == "live" and not self._allow_live:
            raise RuntimeError(
                "MockBroker refuse connect: account_mode=live requires allow_live"
            )
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def account_state(self) -> AccountState:
        self._require_connected()
        return AccountState(
            balance=self._balance,
            equity=self._equity,
            free_margin=self._equity,
            account_mode=self._account_mode,  # type: ignore[arg-type]
        )

    def positions(self) -> list[Position]:
        self._require_connected()
        return list(self._positions.values())

    def inject_position(self, position: Position) -> None:
        """Test helper: simulate an unexpected broker position."""
        self._positions[position.ticket] = position

    def submit_order(self, request: OrderRequest) -> OrderResult:
        self._require_connected()
        if self._account_mode == "live" and not self._allow_live:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="live trading disabled (allow_live=false)",
            )
            self._orders[request.client_order_id] = result
            return result

        if request.client_order_id in self._orders:
            return self._orders[request.client_order_id]

        if request.stop_loss is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="stop_loss required",
            )
            self._orders[request.client_order_id] = result
            return result

        if self._require_take_profit and request.take_profit is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="take_profit required",
            )
            self._orders[request.client_order_id] = result
            return result

        if not self._fill_orders:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="mock fill disabled",
            )
            self._orders[request.client_order_id] = result
            return result

        self._ticket_seq += 1
        ticket = str(self._ticket_seq)
        # Synthetic fill near entry zone — mock has no live quotes.
        fill_price = request.stop_loss + (
            0.001 if request.side.value == "BUY" else -0.001
        )

        position = Position(
            ticket=ticket,
            symbol=request.symbol,
            side=request.side,
            volume=request.volume,
            entry_price=fill_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            client_order_id=request.client_order_id,
        )
        self._positions[ticket] = position
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=OrderStatus.FILLED,
            broker_ticket=ticket,
            fill_price=fill_price,
            message=f"mock fill at {utc_now().isoformat()}",
        )
        self._orders[request.client_order_id] = result
        return result

    def cancel_order(self, client_order_id: str) -> OrderResult:
        self._require_connected()
        existing = self._orders.get(client_order_id)
        if existing and existing.status == OrderStatus.FILLED:
            to_remove = [
                t
                for t, p in self._positions.items()
                if p.client_order_id == client_order_id
            ]
            for ticket in to_remove:
                del self._positions[ticket]
        result = OrderResult(
            client_order_id=client_order_id,
            status=OrderStatus.CANCELLED,
            message="mock cancelled",
        )
        self._orders[client_order_id] = result
        return result

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MockBroker is not connected")
