#!/usr/bin/env python3
"""Polymarket CLOB market WebSocket: best bid / ask / mid per outcome token (UP/DOWN)."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

LOGGER = logging.getLogger("polymarket_btc_ladder")

try:
    import websocket
except ImportError as exc:  # pragma: no cover
    websocket = None  # type: ignore[assignment]
    _IMPORT_ERR = exc
else:
    _IMPORT_ERR = None

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketWsFeed:
    """
    Background thread maintains latest bid/ask/mid per asset_id from the market channel.
    Docs: https://docs.polymarket.com/developers/CLOB/websocket/market-channel
    """

    def __init__(self, url: str = DEFAULT_WS_URL) -> None:
        if websocket is None:
            raise RuntimeError(
                "websocket-client is required for MarketWsFeed "
                f"(pip install websocket-client): {_IMPORT_ERR}"
            )
        self._url = url
        self._lock = threading.Lock()
        self._quotes: dict[str, dict[str, float]] = {}
        self._subscribed: tuple[str, ...] = ()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_app: Any = None
        self._ping_stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._ping_stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="poly-ws-market", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._ping_stop.set()
        self._close_ws()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def set_assets(self, asset_ids: list[str]) -> None:
        """Subscribe to these token IDs; reconnects if the set changed."""
        t = tuple(asset_ids)
        with self._lock:
            if t == self._subscribed:
                return
            self._subscribed = t
        LOGGER.debug("WS market: asset set changed (%d ids); reconnect", len(t))
        self._close_ws()

    def mid_for(self, asset_id: str, max_age_sec: float = 4.0) -> float | None:
        with self._lock:
            q = self._quotes.get(asset_id)
            if not q:
                return None
            if time.time() - q["ts"] > max_age_sec:
                return None
            return float(q["mid"])

    def best_bid_ask_for(self, asset_id: str, max_age_sec: float = 4.0) -> tuple[float, float] | None:
        with self._lock:
            q = self._quotes.get(asset_id)
            if not q:
                return None
            if time.time() - q["ts"] > max_age_sec:
                return None
            return float(q["bid"]), float(q["ask"])

    def _close_ws(self) -> None:
        app = self._ws_app
        self._ws_app = None
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                LOGGER.debug("WS close: %s", exc)

    def _set_quote(self, asset_id: str, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0:
            return
        with self._lock:
            self._quotes[asset_id] = {
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
                "ts": time.time(),
            }

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_event(item)
        elif isinstance(data, dict):
            self._handle_event(data)

    def _handle_event(self, msg: dict[str, Any]) -> None:
        et = msg.get("event_type")
        if et == "best_bid_ask":
            aid = str(msg.get("asset_id") or "")
            bb = _to_float(msg.get("best_bid"))
            ba = _to_float(msg.get("best_ask"))
            if aid and bb > 0 and ba > 0:
                self._set_quote(aid, bb, ba)
        elif et == "book":
            aid = str(msg.get("asset_id") or "")
            bids = msg.get("bids") or []
            asks = msg.get("asks") or []
            bb = _book_best_bid(bids)
            ba = _book_best_ask(asks)
            if aid and bb > 0 and ba > 0:
                self._set_quote(aid, bb, ba)
        elif et == "price_change":
            for ch in msg.get("price_changes") or []:
                if not isinstance(ch, dict):
                    continue
                aid = str(ch.get("asset_id") or "")
                bb = _to_float(ch.get("best_bid"))
                ba = _to_float(ch.get("best_ask"))
                if aid and bb > 0 and ba > 0:
                    self._set_quote(aid, bb, ba)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            assets = list(self._subscribed)
            if len(assets) < 1:
                time.sleep(0.3)
                continue
            try:
                self._connect_session(assets)
            except Exception as exc:
                LOGGER.debug("WS market session error: %s", exc)
                time.sleep(1.5)

    def _connect_session(self, assets: list[str]) -> None:
        ready = threading.Event()

        def on_open(ws: Any) -> None:
            sub = {
                "assets_ids": assets,
                "type": "market",
                "custom_feature_enabled": True,
            }
            ws.send(json.dumps(sub))
            ready.set()
            LOGGER.debug("WS market: connected + subscribed (%d assets)", len(assets))

        def on_pong(_ws: Any, _data: str) -> None:
            pass

        self._ws_app = websocket.WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=self._on_message,
            on_error=lambda _ws, e: LOGGER.debug("WS error: %s", e),
            on_close=lambda *_a: LOGGER.debug("WS market closed"),
        )

        def ping_worker() -> None:
            while not self._ping_stop.is_set() and not self._stop.is_set():
                time.sleep(10.0)
                w = self._ws_app
                if w is None:
                    break
                try:
                    w.send("PING")
                except Exception:
                    break

        ping_thread = threading.Thread(target=ping_worker, name="poly-ws-ping", daemon=True)
        ping_thread.start()
        try:
            self._ws_app.run_forever(ping_interval=None)
        finally:
            self._ping_stop.set()


def _to_float(x: Any) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _price_from_level(lv: Any) -> float:
    if isinstance(lv, dict):
        return _to_float(lv.get("price"))
    return _to_float(getattr(lv, "price", 0))


def _book_best_bid(levels: list[Any]) -> float:
    best = 0.0
    for lv in levels or []:
        p = _price_from_level(lv)
        if p > 0 and p > best:
            best = p
    return best


def _book_best_ask(levels: list[Any]) -> float:
    best = 0.0
    for lv in levels or []:
        p = _price_from_level(lv)
        if p > 0 and (best == 0.0 or p < best):
            best = p
    return best
