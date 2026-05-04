#!/usr/bin/env python3
"""
Live / dry-run: BTC 15m UP/DOWN **cheap03** entry.

**Entry** (``BOT_CHEAP03_ENTRY``, default ``btc50_1c`` — pool sweep winner):

- ``btc50_1c`` — **$1** FAK on **first** midpoint touch where a leg is **≤** ``BOT_CHEAP03_PRICE_MAX``
  (default **1¢**), **only if** ``abs(Binance BTCUSDT - anchor) < BOT_BTC_MAX_MOVE_USD`` (default **50**).
  Anchor = first successful spot read after the window slug is active. If the **first** cheap touch
  fails the BTC gate, **no buy** for that window (matches backtest semantics).
- ``dual_limits`` — GTC buys on UP+DOWN at ``BOT_CHEAP03_LIMIT_PX`` × ``BOT_CHEAP03_LIMIT_SHARES``.
- ``market`` — legacy FAK when mid touches ≤ ``BOT_CHEAP03_PRICE_MAX`` (default 3¢), no BTC gate.

**Take-profit:** GTC sells at ``BOT_TP_LIMIT_PX`` (default **70¢** for ``btc50_1c``, **99¢** for
``dual_limits`` / ``market`` unless overridden). Poll ``BOT_TP_POLL_SECONDS``; ``btc50_1c`` forces
TP sync each poll after entry is armed like dual mode.

Stdout: ``INIT`` / ``WIN`` (market). Library logger for entries and TP.
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
        self._seed_up_done: bool = False
        self._seed_down_done: bool = False
        self._btc_anchor_usd: float | None = None

    def _emit_init(self, contract: ActiveContract | None) -> None:
        slug = contract.slug if contract else "(no contract yet)"
        if self._entry_btc50_1c:
            mode = f"btc50_1c|btc_move<{self._btc_max_move_usd:g}|tp={self._tp_limit_px:g}"
        elif self._entry_dual_limits:
            mode = "dual_limits"
        else:
            mode = "market_fak"
        _out(
            "INIT "
            f"strategy=first_cheap_03 slug={slug} "
            f"entry={mode} thr={self.thr:g} notional_usdc={self.notional:g} "
            f"limit_px={self._limit_buy_px:g} limit_shares={self._limit_buy_shares} "
            f"dry_run={self.config.dry_run} funder={self.config.funder[:6]}…{self.config.funder[-4:]}"
        )

    def _emit_win(self, slug: str, side: Side) -> None:
        _out(f"WIN slug={slug} side={side}")

    def _try_resolve_pending(self, current_slug: str) -> None:
        if self._pending is None:
            return
        if self._pending.slug == current_slug:
            return
        # Previous window ended — resolve when Gamma marks closed
        slug = self._pending.slug
        want = self._pending.side
        try:
            w = _gamma_winner(self.session, slug, self.config.request_timeout_seconds)
        except Exception:
            return
        if w is None:
            return
        if w == want:
            self._emit_win(slug, want)
        self._pending = None

    def _maybe_sync_tp_limits(self, contract: ActiveContract, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_tp_sync_monotonic) < self._tp_poll_seconds:
            return
        self.trader.sync_tp_limit_sells(
            contract, tp=self._tp_limit_px, dry_run=self.config.dry_run
        )
        self._last_tp_sync_monotonic = now

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
            LOGGER.warning("[BTC50] Binance %s price fetch failed: %s", sym, exc)
            return None

    def _ensure_btc_anchor(self) -> None:
        if self._btc_anchor_usd is not None:
            return
        px = self._fetch_binance_btc_spot()
        if px is not None:
            self._btc_anchor_usd = px
            LOGGER.info("[BTC50] anchor spot=%.2f %s", px, self.config.btc_feed_symbol)

    def _try_seed_dual_buy_limits(self, contract: ActiveContract) -> None:
        """Resting GTC bids at ``_limit_buy_px`` on UP and DOWN (``cheap03_dual``-style)."""
        lim = round(float(self._limit_buy_px), 2)
        sh = int(self._limit_buy_shares)
        if sh < 1:
            return
        for outcome, token, flag in (
            ("UP", contract.up, "up"),
            ("DOWN", contract.down, "down"),
        ):
            done = self._seed_up_done if flag == "up" else self._seed_down_done
            if done:
                continue
            if self.config.dry_run:
                if flag == "up":
                    self._seed_up_done = True
                else:
                    self._seed_down_done = True
                continue
            if self.trader.has_open_limit_buy_near(token.token_id, lim):
                if flag == "up":
                    self._seed_up_done = True
                else:
                    self._seed_down_done = True
                LOGGER.info(
                    "[CHEAP03_ENTRY] %s | %s | existing BUY ~$%.2f on book",
                    contract.slug,
                    outcome,
                    lim,
                )
                continue
            try:
                self.trader.place_limit_buy(token, lim, sh)
                if flag == "up":
                    self._seed_up_done = True
                else:
                    self._seed_down_done = True
                LOGGER.info(
                    "[CHEAP03_ENTRY] %s | placed BUY limit %s $%.2f x %d",
                    contract.slug,
                    outcome,
                    lim,
                    sh,
                )
            except Exception as exc:
                LOGGER.warning(
                    "[CHEAP03_ENTRY] %s | %s BUY $%.2f x %d failed: %s",
                    contract.slug,
                    outcome,
                    lim,
                    sh,
                    exc,
                )

    def run(self) -> None:
        contract0 = self.locator.get_active_contract()
        self._emit_init(contract0)

        while True:
            try:
                contract = self.locator.get_active_contract()
                if contract is None:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                cur_slug = contract.slug
                if self._last_slug != cur_slug:
                    self._try_resolve_pending(cur_slug)
                    self._last_slug = cur_slug
                    self._fired_this_slug = False
                    # New window: do not inherit the 15s TP gate from the previous market's tokens.
                    self._last_tp_sync_monotonic = 0.0
                    self._seed_up_done = False
                    self._seed_down_done = False
                    self._btc_anchor_usd = None

                # Must run *before* ``_fired_this_slug`` continue — otherwise after a fill we never
                # poll TP again (balance lag / transient place failure would leave no resting sells).
                self._maybe_sync_tp_limits(contract)
                if self._entry_btc50_1c and self._fired_this_slug:
                    self._maybe_sync_tp_limits(contract, force=True)

                if self._entry_dual_limits:
                    self._try_seed_dual_buy_limits(contract)
                    if not (self._seed_up_done and self._seed_down_done):
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    # After both bids rest, re-check TP every poll (fills can land anytime).
                    self._maybe_sync_tp_limits(contract, force=True)
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if self._entry_btc50_1c:
                    self._ensure_btc_anchor()
                    if self._fired_this_slug:
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    mu = self.trader.get_midpoint(contract.up.token_id)
                    md = self.trader.get_midpoint(contract.down.token_id)
                    if mu is None or md is None:
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    side = _cheap_side_at(float(mu), float(md), self.thr)
                    if side is None:
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    btc_now = self._fetch_binance_btc_spot()
                    if self._btc_anchor_usd is None:
                        if btc_now is not None:
                            self._btc_anchor_usd = btc_now
                            LOGGER.info("[BTC50] anchor set at first cheap=%.2f", btc_now)
                        else:
                            time.sleep(self.config.poll_interval_seconds)
                            continue
                    if btc_now is None:
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    move = abs(btc_now - self._btc_anchor_usd)
                    if move >= self._btc_max_move_usd - 1e-9:
                        LOGGER.info(
                            "[BTC50] %s | skip first cheap | |move|=%.2f >= %.0f anchor=%.2f now=%.2f",
                            cur_slug,
                            move,
                            self._btc_max_move_usd,
                            self._btc_anchor_usd,
                            btc_now,
                        )
                        self._fired_this_slug = True
                        time.sleep(self.config.poll_interval_seconds)
                        continue

                    token = contract.up if side == "up" else contract.down
                    entry = float(mu) if side == "up" else float(md)
                    if self.config.dry_run:
                        self._fired_this_slug = True
                        time.sleep(self.config.poll_interval_seconds)
                        continue
                    try:
                        self.trader.place_market_buy_usdc_with_result(
                            token,
                            float(self.notional),
                            confirm_get_order=self.config.polymarket_fak_confirm_get_order,
                        )
                    except Exception:
                        time.sleep(max(2.0, self.config.poll_interval_seconds))
                        continue
                    self._fired_this_slug = True
                    self._pending = _OpenTrade(
                        slug=cur_slug,
                        side=side,
                        token_id=token.token_id,
                        notional_usdc=float(self.notional),
                    )
                    self._maybe_sync_tp_limits(contract, force=True)
                    _ = entry
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if self._fired_this_slug:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                mu = self.trader.get_midpoint(contract.up.token_id)
                md = self.trader.get_midpoint(contract.down.token_id)
                if mu is None or md is None:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                side = _cheap_side_at(float(mu), float(md), self.thr)
                if side is None:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                token: TokenMarket = contract.up if side == "up" else contract.down
                entry = float(mu) if side == "up" else float(md)

                if self.config.dry_run:
                    self._fired_this_slug = True
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                try:
                    self.trader.place_market_buy_usdc_with_result(
                        token,
                        float(self.notional),
                        confirm_get_order=self.config.polymarket_fak_confirm_get_order,
                    )
                except Exception:
                    time.sleep(max(2.0, self.config.poll_interval_seconds))
                    continue

                self._fired_this_slug = True
                self._pending = _OpenTrade(
                    slug=cur_slug,
                    side=side,
                    token_id=token.token_id,
                    notional_usdc=float(self.notional),
                )
                self._maybe_sync_tp_limits(contract, force=True)
                _ = entry
            except KeyboardInterrupt:
                raise
            except Exception:
                # Silent on errors (user asked minimal logs); exit non-zero from main if desired
                time.sleep(max(2.0, self.config.poll_interval_seconds))
            else:
                time.sleep(self.config.poll_interval_seconds)
