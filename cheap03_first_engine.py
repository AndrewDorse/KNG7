#!/usr/bin/env python3
"""
Live / dry-run: BTC **5m and/or 15m** UP/DOWN **cheap03** entry (``BOT_WINDOW_MINUTES``).

Use ``BOT_WINDOW_MINUTES=5,15`` (or ``15,5``) to run **both** lengths in **one** process: separate
state, anchors, and TP polling per lane. Single value (e.g. ``15``) = one lane only.

**Entry** (``BOT_CHEAP03_ENTRY``, default ``btc50_1c`` — pool sweep winner):

- ``btc50_1c`` — **$1** FAK on **first** midpoint touch where a leg is **≤** ``BOT_CHEAP03_PRICE_MAX``
  (default **1¢**), **only if** ``abs(Binance BTCUSDT - anchor) < BOT_BTC_MAX_MOVE_USD`` (default **50**).
  Anchor = first successful spot read after the window slug is active. If the **first** cheap touch
  fails the BTC gate, **no buy** for that window (matches backtest semantics).
- ``dual_limits`` — GTC buys on UP+DOWN at ``BOT_CHEAP03_LIMIT_PX`` × ``BOT_CHEAP03_LIMIT_SHARES``.
- ``market`` — legacy FAK when mid touches ≤ ``BOT_CHEAP03_PRICE_MAX`` (default 3¢), no BTC gate.

**Take-profit:** GTC sells at ``BOT_TP_LIMIT_PX`` (default **70¢** for ``btc50_1c``, **99¢** for
``dual_limits`` / ``market`` unless overridden). Re-sync at ``BOT_TP_POLL_SECONDS``; one immediate
sync after each entry. No per-second TP hammer.

Stdout: ``INIT`` / ``DEAL_START`` / ``WIN``. Stderr: errors only (``BOT_LOG_LEVEL`` default ERROR).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Literal

import requests

from config import GAMMA_URL, LOGGER, ActiveContract, BotConfig, TokenMarket
from http_session import create_polymarket_session
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

Side = Literal["up", "down"]


def _out(msg: str) -> None:
    print(msg, flush=True)


def _cheap_side_at(u: float, d: float, thr: float) -> Side | None:
    bu, bd = u <= thr + 1e-12, d <= thr + 1e-12
    if bu and bd:
        return "up" if u <= d else "down"
    if bu:
        return "up"
    if bd:
        return "down"
    return None


def _parse_jsonish_list(raw: object) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            val = json.loads(s)
            return val if isinstance(val, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _gamma_winner(session: requests.Session, slug: str, timeout: float) -> Side | None:
    """When market is closed, return winning side ``up``/``down`` from Gamma outcome prices."""
    r = session.get(f"{GAMMA_URL}/markets", params={"slug": slug}, timeout=timeout)
    r.raise_for_status()
    arr = r.json()
    if not arr:
        return None
    m = arr[0]
    if not m.get("closed"):
        return None
    names = [str(x).strip().upper() for x in _parse_jsonish_list(m.get("outcomes"))]
    prices_raw = m.get("outcomePrices")
    prices = [float(x) for x in _parse_jsonish_list(prices_raw)]
    if len(names) != len(prices) or len(names) < 2:
        return None
    best_i = max(range(len(prices)), key=lambda i: prices[i])
    w = names[best_i]
    if w == "UP":
        return "up"
    if w == "DOWN":
        return "down"
    return None


@dataclass(slots=True)
class _OpenTrade:
    slug: str
    side: Side
    token_id: str
    notional_usdc: float


@dataclass(slots=True)
class _LaneState:
    """Per-``BOT_WINDOW_MINUTES`` lane (e.g. 5 vs 15) so two markets can trade in parallel."""

    last_slug: str | None = None
    fired_this_slug: bool = False
    pending: _OpenTrade | None = None
    last_tp_sync_monotonic: float = 0.0
    seed_up_done: bool = False
    seed_down_done: bool = False
    btc_anchor_usd: float | None = None
    # dual_limits: run TP sync once when both resting bids are in place (not every poll).
    dual_tp_synced_once: bool = False
    # Diagnostics for "silent no-trade" windows.
    no_mid_streak: int = 0
    no_btc_streak: int = 0
    mid_error_logged: bool = False
    btc_error_logged: bool = False


class Cheap03FirstEngine:
    def __init__(self, config: BotConfig, locator: GammaMarketLocator, trader: PolymarketTrader) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self.session = create_polymarket_session()
        self.notional = float(os.getenv("BOT_CHEAP03_NOTIONAL_USDC", "1.0"))
        self._last_slug: str | None = None
        self._fired_this_slug = False
        self._pending: _OpenTrade | None = None
        self._last_tp_sync_monotonic: float = 0.0
        self._tp_poll_seconds: float = float(os.getenv("BOT_TP_POLL_SECONDS", "15"))
        entry_raw = (os.getenv("BOT_CHEAP03_ENTRY") or "btc50_1c").strip().lower()
        self._entry_btc50_1c: bool = entry_raw in (
            "btc50_1c",
            "btc50",
            "sweep_btc50",
            "btc_gate_1c",
        )
        self._entry_dual_limits: bool = entry_raw in ("dual_limits", "dual", "cheap03_dual")
        self._btc_max_move_usd: float = float(os.getenv("BOT_BTC_MAX_MOVE_USD", "50"))
        if self._entry_btc50_1c:
            self.thr = float(os.getenv("BOT_CHEAP03_PRICE_MAX", "0.01"))
            self._tp_limit_px: float = float(os.getenv("BOT_TP_LIMIT_PX", "0.70"))
        elif self._entry_dual_limits:
            self._tp_limit_px: float = float(os.getenv("BOT_TP_LIMIT_PX", "0.99"))
        else:
            self._tp_limit_px: float = float(os.getenv("BOT_TP_LIMIT_PX", "0.99"))
            self.thr = float(os.getenv("BOT_CHEAP03_PRICE_MAX", "0.03"))
        self._limit_buy_px: float = float(os.getenv("BOT_CHEAP03_LIMIT_PX", "0.03"))
        self._limit_buy_shares: int = int(os.getenv("BOT_CHEAP03_LIMIT_SHARES", "34"))
        self._lanes: dict[int, _LaneState] = {
            int(wm): _LaneState() for wm in self.config.window_minutes_tokens
        }

    def _emit_init(self) -> None:
        lane_slugs: list[str] = []
        for wm in self.config.window_minutes_tokens:
            c = self.locator.get_active_contract_for_window_minutes(int(wm))
            lane_slugs.append(f"{wm}m={c.slug if c else '(none)'}")
        if self._entry_btc50_1c:
            mode = f"btc50_1c|btc_move<{self._btc_max_move_usd:g}|tp={self._tp_limit_px:g}"
        elif self._entry_dual_limits:
            mode = "dual_limits"
        else:
            mode = "market_fak"
        poly_ws = "on" if self.trader.ws_quotes_active else "off"
        _out(
            "INIT "
            f"strategy=first_cheap_03 lanes={'+'.join(lane_slugs)} "
            f"entry={mode} thr={self.thr:g} notional_usdc={self.notional:g} "
            f"limit_px={self._limit_buy_px:g} limit_shares={self._limit_buy_shares} "
            f"poly_ws={poly_ws} dry_run={self.config.dry_run} "
            f"funder={self.config.funder[:6]}…{self.config.funder[-4:]}"
        )

    def _emit_win(self, slug: str, side: Side) -> None:
        _out(f"WIN slug={slug} side={side}")

    def _try_resolve_pending(self, st: _LaneState, current_slug: str) -> None:
        if st.pending is None:
            return
        if st.pending.slug == current_slug:
            return
        # Previous window ended — resolve when Gamma marks closed
        slug = st.pending.slug
        want = st.pending.side
        try:
            w = _gamma_winner(self.session, slug, self.config.request_timeout_seconds)
        except Exception:
            return
        if w is None:
            return
        if w == want:
            self._emit_win(slug, want)
        st.pending = None

    def _maybe_sync_tp_limits(
        self, contract: ActiveContract, st: _LaneState, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not force and (now - st.last_tp_sync_monotonic) < self._tp_poll_seconds:
            return
        self.trader.sync_tp_limit_sells(
            contract, tp=self._tp_limit_px, dry_run=self.config.dry_run
        )
        st.last_tp_sync_monotonic = now

    def _fetch_binance_btc_spot(self) -> float | None:
        if not self.config.btc_feed_enabled:
            return None
        sym = self.config.btc_feed_symbol.upper()
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": sym},
                timeout=self.config.request_timeout_seconds,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as exc:
            LOGGER.error("[BTC50] Binance %s price fetch failed: %s", sym, exc)
            return None

    def _ensure_btc_anchor(self, st: _LaneState, wm: int) -> None:
        if st.btc_anchor_usd is not None:
            return
        px = self._fetch_binance_btc_spot()
        if px is not None:
            st.btc_anchor_usd = px

    def _try_seed_dual_buy_limits(self, contract: ActiveContract, st: _LaneState) -> None:
        """Resting GTC bids at ``_limit_buy_px`` on UP and DOWN (``cheap03_dual``-style)."""
        lim = round(float(self._limit_buy_px), 2)
        sh = int(self._limit_buy_shares)
        if sh < 1:
            return
        for outcome, token, flag in (
            ("UP", contract.up, "up"),
            ("DOWN", contract.down, "down"),
        ):
            done = st.seed_up_done if flag == "up" else st.seed_down_done
            if done:
                continue
            if self.config.dry_run:
                if flag == "up":
                    st.seed_up_done = True
                else:
                    st.seed_down_done = True
                continue
            if self.trader.has_open_limit_buy_near(token.token_id, lim):
                if flag == "up":
                    st.seed_up_done = True
                else:
                    st.seed_down_done = True
                continue
            try:
                self.trader.place_limit_buy(token, lim, sh)
                if flag == "up":
                    st.seed_up_done = True
                else:
                    st.seed_down_done = True
                _out(
                    f"DEAL_START slug={contract.slug} mode=dual_limits "
                    f"side={outcome} limit_px={lim:g} shares={sh}"
                )
            except Exception as exc:
                LOGGER.error(
                    "[CHEAP03_ENTRY] %s | %s BUY $%.2f x %d failed: %s",
                    contract.slug,
                    outcome,
                    lim,
                    sh,
                    exc,
                )

    def _process_lane(self, wm: int, st: _LaneState, contract: ActiveContract) -> None:
        cur_slug = contract.slug
        if st.last_slug != cur_slug:
            self._try_resolve_pending(st, cur_slug)
            st.last_slug = cur_slug
            st.fired_this_slug = False
            st.last_tp_sync_monotonic = 0.0
            st.seed_up_done = False
            st.seed_down_done = False
            st.btc_anchor_usd = None
            st.dual_tp_synced_once = False
            st.no_mid_streak = 0
            st.no_btc_streak = 0
            st.mid_error_logged = False
            st.btc_error_logged = False

        self._maybe_sync_tp_limits(contract, st)

        if self._entry_dual_limits:
            self._try_seed_dual_buy_limits(contract, st)
            if not (st.seed_up_done and st.seed_down_done):
                return
            if not st.dual_tp_synced_once:
                self._maybe_sync_tp_limits(contract, st, force=True)
                st.dual_tp_synced_once = True
            return

        if self._entry_btc50_1c:
            self._ensure_btc_anchor(st, wm)
            if st.fired_this_slug:
                return
            mu = self.trader.get_midpoint(contract.up.token_id)
            md = self.trader.get_midpoint(contract.down.token_id)
            if mu is None or md is None:
                st.no_mid_streak += 1
                if st.no_mid_streak >= 30 and not st.mid_error_logged:
                    st.mid_error_logged = True
                    LOGGER.error(
                        "[DATA] %dm %s no midpoint for 30+ polls (ws=%s). "
                        "Check WSS egress, BOT_POLY_WS_MAX_AGE_SEC, and CLOB /midpoint+book for these tokens.",
                        wm,
                        cur_slug,
                        "on" if self.trader.ws_quotes_active else "off",
                    )
                return
            st.no_mid_streak = 0
            st.mid_error_logged = False
            side = _cheap_side_at(float(mu), float(md), self.thr)
            if side is None:
                return
            btc_now = self._fetch_binance_btc_spot()
            if st.btc_anchor_usd is None:
                if btc_now is not None:
                    st.btc_anchor_usd = btc_now
                else:
                    st.no_btc_streak += 1
                    if st.no_btc_streak >= 10 and not st.btc_error_logged:
                        st.btc_error_logged = True
                        LOGGER.error(
                            "[BTC50] %dm %s cannot set BTC anchor: Binance feed unavailable for 10+ polls.",
                            wm,
                            cur_slug,
                        )
                    return
            if btc_now is None:
                st.no_btc_streak += 1
                if st.no_btc_streak >= 10 and not st.btc_error_logged:
                    st.btc_error_logged = True
                    LOGGER.error(
                        "[BTC50] %dm %s Binance feed unavailable for 10+ polls after cheap trigger.",
                        wm,
                        cur_slug,
                    )
                return
            st.no_btc_streak = 0
            st.btc_error_logged = False
            move = abs(btc_now - st.btc_anchor_usd)
            if move >= self._btc_max_move_usd - 1e-9:
                LOGGER.error(
                    "[BTC50] %dm %s first cheap blocked by BTC gate: move=%.2f >= %.2f (anchor=%.2f now=%.2f)",
                    wm,
                    cur_slug,
                    move,
                    self._btc_max_move_usd,
                    st.btc_anchor_usd,
                    btc_now,
                )
                st.fired_this_slug = True
                return

            token = contract.up if side == "up" else contract.down
            entry = float(mu) if side == "up" else float(md)
            if self.config.dry_run:
                st.fired_this_slug = True
                return
            try:
                self.trader.place_market_buy_usdc_with_result(
                    token,
                    float(self.notional),
                    confirm_get_order=self.config.polymarket_fak_confirm_get_order,
                )
            except Exception as exc:
                LOGGER.error(
                    "[BTC50] %dm %s market buy failed side=%s: %s",
                    wm,
                    cur_slug,
                    side,
                    exc,
                )
                time.sleep(max(2.0, self.config.poll_interval_seconds))
                return
            st.fired_this_slug = True
            st.pending = _OpenTrade(
                slug=cur_slug,
                side=side,
                token_id=token.token_id,
                notional_usdc=float(self.notional),
            )
            self._maybe_sync_tp_limits(contract, st, force=True)
            _out(
                f"DEAL_START slug={cur_slug} mode=btc50_1c side={side.upper()} "
                f"mid~={entry:g} notional_usdc={self.notional:g}"
            )
            return

        if st.fired_this_slug:
            return

        mu = self.trader.get_midpoint(contract.up.token_id)
        md = self.trader.get_midpoint(contract.down.token_id)
        if mu is None or md is None:
            return

        side = _cheap_side_at(float(mu), float(md), self.thr)
        if side is None:
            return

        token: TokenMarket = contract.up if side == "up" else contract.down
        entry = float(mu) if side == "up" else float(md)

        if self.config.dry_run:
            st.fired_this_slug = True
            return

        try:
            self.trader.place_market_buy_usdc_with_result(
                token,
                float(self.notional),
                confirm_get_order=self.config.polymarket_fak_confirm_get_order,
            )
        except Exception as exc:
            LOGGER.error(
                "[CHEAP03] %s market buy failed side=%s: %s",
                cur_slug,
                side,
                exc,
            )
            time.sleep(max(2.0, self.config.poll_interval_seconds))
            return

        st.fired_this_slug = True
        st.pending = _OpenTrade(
            slug=cur_slug,
            side=side,
            token_id=token.token_id,
            notional_usdc=float(self.notional),
        )
        self._maybe_sync_tp_limits(contract, st, force=True)
        _out(
            f"DEAL_START slug={cur_slug} mode=market_fak side={side.upper()} "
            f"mid~={entry:g} notional_usdc={self.notional:g}"
        )
        _ = entry

    def run(self) -> None:
        self._emit_init()

        while True:
            try:
                saw_any = False
                lane_contracts: list[ActiveContract | None] = []
                for wm in self.config.window_minutes_tokens:
                    lane_contracts.append(
                        self.locator.get_active_contract_for_window_minutes(int(wm))
                    )
                self.trader.sync_ws_subscriptions(lane_contracts)
                for wm, contract in zip(self.config.window_minutes_tokens, lane_contracts, strict=True):
                    st = self._lanes[int(wm)]
                    if contract is None:
                        continue
                    saw_any = True
                    self._process_lane(int(wm), st, contract)
                if not saw_any:
                    time.sleep(self.config.poll_interval_seconds)
                else:
                    time.sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("KNG7 main loop error")
                time.sleep(max(2.0, self.config.poll_interval_seconds))
