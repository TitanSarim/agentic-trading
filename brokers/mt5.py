"""MetaTrader5 Python adapter (Windows only; real calls behind interface).

Cloud/CI must use brokers.mock.MockBroker — MetaTrader5 is never imported at
package import time. Graceful failure when not on Windows / package missing /
terminal not running.

P6: demo order lifecycle with SL/TP, idempotent client order IDs, reconcile.
Live submits refused unless account_mode=live AND allow_live (P9).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from trading.types import (
    AccountState,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    Side,
)


DEFAULT_TERMINAL_PATH = (
    r"C:\Program Files\MetaTrader 5 IC Markets Global\terminal64.exe"
)


class Mt5Broker:
    """Co-located MetaTrader5 Python on Windows next to terminal64.exe."""

    name = "mt5"

    def __init__(
        self,
        *,
        terminal_path: str = DEFAULT_TERMINAL_PATH,
        account_mode: str = "demo",
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        allow_live: bool = False,
        magic: int = 260601,
        deviation: int = 20,
        mt5_module: Any | None = None,
    ) -> None:
        self.terminal_path = terminal_path
        self.account_mode = account_mode
        self.login = login if login is not None else _env_int("MT5_LOGIN")
        self.password = password if password is not None else os.environ.get("MT5_PASSWORD")
        self.server = server if server is not None else os.environ.get("MT5_SERVER")
        self.allow_live = allow_live
        self.magic = magic
        self.deviation = deviation
        self._connected = False
        self._mt5 = mt5_module  # injectable for tests
        self._order_cache: dict[str, OrderResult] = {}

    def connect(self) -> None:
        if self.account_mode == "live" and not self.allow_live:
            raise RuntimeError(
                "Mt5Broker refuse connect: account_mode=live requires "
                "execution.allow_live=true (Phase 9). P6 is demo-only."
            )

        mt5 = self._mt5
        if mt5 is None:
            if sys.platform != "win32":
                raise RuntimeError(
                    f"Mt5Broker requires Windows (platform={sys.platform}). "
                    "Use broker.backend=mock in CI/cloud."
                )
            try:
                import MetaTrader5 as mt5  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "MetaTrader5 package not installed. "
                    "Install on Windows next to terminal64.exe: pip install MetaTrader5 "
                    "(or pip install -e '.[mt5]'), or use broker.backend=mock."
                ) from exc

        initialized = mt5.initialize(path=self.terminal_path)
        if not initialized:
            err = mt5.last_error()
            raise RuntimeError(
                f"MT5 initialize failed: {err}. "
                "Start terminal64.exe and ensure it is logged into a demo account:\n"
                f"  {self.terminal_path}"
            )

        if self.login is not None:
            ok = mt5.login(self.login, password=self.password, server=self.server)
            if not ok:
                err = mt5.last_error()
                mt5.shutdown()
                raise RuntimeError(f"MT5 login failed: {err}")

        # Soft verify trade mode when API exposes it.
        info = mt5.account_info()
        if info is not None and hasattr(info, "trade_mode"):
            # ACCOUNT_TRADE_MODE_DEMO = 0, CONTEST = 1, REAL = 2
            trade_mode = int(info.trade_mode)
            if trade_mode == 2 and self.account_mode == "demo":
                mt5.shutdown()
                raise RuntimeError(
                    "MT5 terminal is on a REAL account but config account_mode=demo. "
                    "Refuse to connect — switch terminal to demo or set mode explicitly."
                )
            if trade_mode == 2 and not self.allow_live:
                mt5.shutdown()
                raise RuntimeError(
                    "MT5 terminal is on a REAL account; allow_live is false (P6 demo-only)."
                )

        self._mt5 = mt5
        self._connected = True

    def disconnect(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass
        # Keep injectable module for reconnect in tests; clear only real import path.
        if not _is_fake_module(self._mt5):
            self._mt5 = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def account_state(self) -> AccountState:
        self._require_connected()
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info failed: {self._mt5.last_error()}")
        mode = self.account_mode
        if hasattr(info, "trade_mode") and int(info.trade_mode) == 2:
            mode = "live"
        return AccountState(
            balance=float(info.balance),
            equity=float(info.equity),
            margin=float(getattr(info, "margin", 0.0) or 0.0),
            free_margin=float(getattr(info, "margin_free", 0.0) or 0.0),
            currency=str(getattr(info, "currency", "USD") or "USD"),
            account_mode=mode,  # type: ignore[arg-type]
        )

    def positions(self) -> list[Position]:
        self._require_connected()
        raw = self._mt5.positions_get()
        if raw is None:
            return []
        out: list[Position] = []
        for p in raw:
            side = Side.BUY if int(p.type) == 0 else Side.SELL
            comment = str(getattr(p, "comment", "") or "")
            out.append(
                Position(
                    ticket=str(p.ticket),
                    symbol=str(p.symbol),
                    side=side,
                    volume=float(p.volume),
                    entry_price=float(p.price_open),
                    stop_loss=float(p.sl) if float(p.sl) else None,
                    take_profit=float(p.tp) if float(p.tp) else None,
                    profit=float(getattr(p, "profit", 0.0) or 0.0),
                    client_order_id=comment or None,
                )
            )
        return out

    def submit_order(self, request: OrderRequest) -> OrderResult:
        self._require_connected()
        if self.account_mode == "live" and not self.allow_live:
            return OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="live trading disabled (allow_live=false)",
            )

        # Idempotency: return cached result for same client_order_id.
        if request.client_order_id in self._order_cache:
            return self._order_cache[request.client_order_id]

        # Also scan open positions for matching comment.
        for pos in self.positions():
            if pos.client_order_id == request.client_order_id:
                result = OrderResult(
                    client_order_id=request.client_order_id,
                    status=OrderStatus.FILLED,
                    broker_ticket=pos.ticket,
                    fill_price=pos.entry_price,
                    message="idempotent hit: existing position",
                )
                self._order_cache[request.client_order_id] = result
                return result

        if request.stop_loss is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="stop_loss required",
            )
            self._order_cache[request.client_order_id] = result
            return result
        if request.take_profit is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message="take_profit required",
            )
            self._order_cache[request.client_order_id] = result
            return result

        mt5 = self._mt5
        symbol = request.symbol
        if not mt5.symbol_select(symbol, True):
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=f"symbol_select failed: {mt5.last_error()}",
            )
            self._order_cache[request.client_order_id] = result
            return result

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=f"no tick for {symbol}: {mt5.last_error()}",
            )
            self._order_cache[request.client_order_id] = result
            return result

        order_type = mt5.ORDER_TYPE_BUY if request.side == Side.BUY else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if request.side == Side.BUY else tick.bid)
        comment = (request.comment or request.client_order_id)[:31]

        trade_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(request.volume),
            "type": order_type,
            "price": price,
            "sl": float(request.stop_loss),
            "tp": float(request.take_profit),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _resolve_filling(mt5, symbol),
        }

        check = mt5.order_check(trade_request)
        if check is not None and getattr(check, "retcode", 0) not in (0, 10009, 10008):
            # 10009 DONE, 10008 PLACED — order_check may use different codes; treat non-zero carefully
            ret = int(getattr(check, "retcode", -1))
            # ACCOUNT_TRADE check: MT5 TRADE_RETCODE_DONE = 10009; order_check often returns 0 on OK
            if ret not in (0,):
                # Some builds return TRADE_RETCODE_DONE on check; allow 10009
                if ret >= 10000 and ret not in (10008, 10009):
                    result = OrderResult(
                        client_order_id=request.client_order_id,
                        status=OrderStatus.REJECTED,
                        message=f"order_check retcode={ret} comment={getattr(check, 'comment', '')}",
                    )
                    self._order_cache[request.client_order_id] = result
                    return result

        sent = mt5.order_send(trade_request)
        if sent is None:
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=f"order_send returned None: {mt5.last_error()}",
            )
            self._order_cache[request.client_order_id] = result
            return result

        retcode = int(sent.retcode)
        # TRADE_RETCODE_DONE = 10009, DONE_PARTIAL = 10010
        if retcode not in (10009, 10010, 10008):
            result = OrderResult(
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                message=f"order_send retcode={retcode} comment={getattr(sent, 'comment', '')}",
            )
            self._order_cache[request.client_order_id] = result
            return result

        ticket = str(getattr(sent, "order", None) or getattr(sent, "deal", "") or "")
        fill = float(getattr(sent, "price", price) or price)
        result = OrderResult(
            client_order_id=request.client_order_id,
            status=OrderStatus.FILLED if retcode in (10009, 10010) else OrderStatus.PENDING,
            broker_ticket=ticket or None,
            fill_price=fill,
            message=f"mt5 retcode={retcode}",
        )
        self._order_cache[request.client_order_id] = result
        return result

    def cancel_order(self, client_order_id: str) -> OrderResult:
        """Close position tagged with client_order_id comment (demo close path)."""
        self._require_connected()
        mt5 = self._mt5
        targets = [p for p in self.positions() if p.client_order_id == client_order_id]
        if not targets:
            result = OrderResult(
                client_order_id=client_order_id,
                status=OrderStatus.CANCELLED,
                message="no open position for client_order_id",
            )
            self._order_cache[client_order_id] = result
            return result

        last: OrderResult | None = None
        for pos in targets:
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                last = OrderResult(
                    client_order_id=client_order_id,
                    status=OrderStatus.REJECTED,
                    message=f"no tick to close {pos.symbol}",
                )
                continue
            close_type = (
                mt5.ORDER_TYPE_SELL if pos.side == Side.BUY else mt5.ORDER_TYPE_BUY
            )
            price = float(tick.bid if pos.side == Side.BUY else tick.ask)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": float(pos.volume),
                "type": close_type,
                "position": int(pos.ticket),
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": f"x_{client_order_id}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _resolve_filling(mt5, pos.symbol),
            }
            sent = mt5.order_send(req)
            if sent is None or int(sent.retcode) not in (10009, 10010):
                last = OrderResult(
                    client_order_id=client_order_id,
                    status=OrderStatus.REJECTED,
                    message=f"close failed: {None if sent is None else sent.retcode}",
                )
            else:
                last = OrderResult(
                    client_order_id=client_order_id,
                    status=OrderStatus.CANCELLED,
                    broker_ticket=pos.ticket,
                    message="position closed",
                )
        assert last is not None
        self._order_cache[client_order_id] = last
        return last

    def _require_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise RuntimeError("Mt5Broker is not connected")


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _is_fake_module(mod: Any) -> bool:
    return mod is not None and getattr(mod, "__agentic_fake__", False)


def _resolve_filling(mt5: Any, symbol: str) -> int:
    """Pick a filling mode the symbol supports."""
    info = mt5.symbol_info(symbol)
    ioc = getattr(mt5, "ORDER_FILLING_IOC", 1)
    fok = getattr(mt5, "ORDER_FILLING_FOK", 0)
    return_ = getattr(mt5, "ORDER_FILLING_RETURN", 2)
    if info is None:
        return ioc
    mode = int(getattr(info, "filling_mode", 0) or 0)
    # SYMBOL_FILLING_IOC = 2, FOK = 1, RETURN = 4 (bit flags vary by build)
    if mode & 2:
        return ioc
    if mode & 1:
        return fok
    if mode & 4:
        return return_
    return ioc
