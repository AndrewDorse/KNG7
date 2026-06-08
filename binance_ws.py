#!/usr/bin/env python3
"""Binance mini-ticker WebSocket with short rolling price histories."""

from __future__ import annotations

from collections import deque
import json
import logging
import threading
import time
from typing import Any

try:
    import websocket
except ImportError as exc:  # pragma: no cover
    websocket = None  # type: ignore[assignment]
    _IMPORT_ERR = exc
else:
    _IMPORT_ERR = None


LOGGER = logging.getLogger("polymarket_btc_ladder")
DEFAULT_WS_URL = "wss://stream.binance.com:9443/stream"


class BinancePriceFeed:
    """Maintain recent Binance spot prices for multiple USDT pairs."""

    def __init__(
        self,
        symbols: tuple[str, ...],
        *,
        url: str = DEFAULT_WS_URL,
        history_seconds: float = 30.0,
    ) -> None:
        if websocket is None:
            raise RuntimeError(
                "websocket-client is required for BinancePriceFeed "
                f"(pip install websocket-client): {_IMPORT_ERR}"
            )
        self._symbols = tuple(sorted({str(s).upper() for s in symbols}))
        self._url = url
        self._history_seconds = max(15.0, float(history_seconds))
        self._lock = threading.Lock()
        self._prices: dict[str, deque[tuple[float, float]]] = {
            symbol: deque() for symbol in self._symbols
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_app: Any = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="binance-ws-prices",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        app = self._ws_app
        self._ws_app = None
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def move_bps(
        self,
        symbol: str,
        *,
        lookback_seconds: float,
        max_age_seconds: float,
    ) -> float | None:
        """Return current move from the first observation at/before the lookback."""
        now = time.time()
        target = now - lookback_seconds
        with self._lock:
            points = list(self._prices.get(symbol.upper(), ()))
        if not points or now - points[-1][0] > max_age_seconds:
            return None
        prior = min(points, key=lambda point: abs(point[0] - target))
        if abs(target - prior[0]) > max_age_seconds:
            return None
        old_px = prior[1]
        new_px = points[-1][1]
        if old_px <= 0:
            return None
        return (new_px - old_px) / old_px * 10_000.0

    def range_bps(
        self,
        symbol: str,
        *,
        lookback_seconds: float,
        max_age_seconds: float,
    ) -> float | None:
        now = time.time()
        cutoff = now - lookback_seconds
        with self._lock:
            points = list(self._prices.get(symbol.upper(), ()))
        if not points or now - points[-1][0] > max_age_seconds:
            return None
        selected = [price for observed_at, price in points if observed_at >= cutoff]
        if not selected or points[0][0] > cutoff + max_age_seconds:
            return None
        latest = selected[-1]
        if latest <= 0:
            return None
        return (max(selected) - min(selected)) / latest * 10_000.0

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            envelope = json.loads(message)
            data = envelope.get("data", envelope)
            symbol = str(data.get("s") or "").upper()
            price = float(data.get("c"))
            event_ms = float(data.get("E") or 0)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return
        if symbol.endswith("USDT"):
            symbol = symbol[:-4]
        if symbol not in self._prices or price <= 0:
            return
        observed_at = event_ms / 1000.0 if event_ms > 0 else time.time()
        cutoff = observed_at - self._history_seconds
        with self._lock:
            points = self._prices[symbol]
            points.append((observed_at, price))
            while points and points[0][0] < cutoff:
                points.popleft()

    def _run_loop(self) -> None:
        streams = "/".join(f"{symbol.lower()}usdt@miniTicker" for symbol in self._symbols)
        url = f"{self._url}?streams={streams}"
        while not self._stop.is_set():
            try:
                app = websocket.WebSocketApp(url, on_message=self._on_message)
                self._ws_app = app
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                LOGGER.warning("Binance WS error: %s", exc)
            finally:
                self._ws_app = None
            if not self._stop.is_set():
                time.sleep(1.0)
