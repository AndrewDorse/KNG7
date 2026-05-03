#!/usr/bin/env python3
"""
Live / dry-run: BTC 15m UP/DOWN **cheap03** entry.

**Entry** (``BOT_CHEAP03_ENTRY``, default ``dual_limits``):
  ``dual_limits`` — on each new window, place **GTC limit buys** at ``BOT_CHEAP03_LIMIT_PX``
  (default 0.03) for ``BOT_CHEAP03_LIMIT_SHARES`` (default 34) on **both** UP and DOWN (resting bids).
  ``market`` — legacy: no entry limits; **one** $1 USDC FAK when midpoint first touches ≤3¢
  (``_cheap_side_at``), same as early KNG7.

Stdout: **only** ``INIT`` line at start and ``WIN`` lines when a placed trade resolves winning
(market mode only today). Other activity uses the library logger.

Resting take-profit: every ``BOT_TP_POLL_SECONDS`` (default 15), and immediately after a fill
(market path), GTC limit sells at **99¢** for whole-share inventory per side.
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
        self.thr = float(os.getenv("BOT_CHEAP03_PRICE_MAX", "0.03"))
        self.notional = float(os.getenv("BOT_CHEAP03_NOTIONAL_USDC", "1.0"))
        self._last_slug: str | None = None
        self._fired_this_slug = False
        self._pending: _OpenTrade | None = None
        self._last_tp_sync_monotonic: float = 0.0
        self._tp_poll_seconds: float = float(os.getenv("BOT_TP_POLL_SECONDS", "15"))
        entry_raw = (os.getenv("BOT_CHEAP03_ENTRY") or "dual_limits").strip().lower()
        self._entry_dual_limits: bool = entry_raw not in (
            "market",
            "fak",
            "first_touch",
            "first_cheap",
        )
        self._limit_buy_px: float = float(os.getenv("BOT_CHEAP03_LIMIT_PX", "0.03"))
        self._limit_buy_shares: int = int(os.getenv("BOT_CHEAP03_LIMIT_SHARES", "34"))
        self._seed_up_done: bool = False
        self._seed_down_done: bool = False

    def _emit_init(self, contract: ActiveContract | None) -> None:
        slug = contract.slug if contract else "(no contract yet)"
        mode = "dual_limits" if self._entry_dual_limits else "market_fak"
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
        self.trader.sync_tp_limit_sells_99c(contract, dry_run=self.config.dry_run)
        self._last_tp_sync_monotonic = now

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

                # Must run *before* ``_fired_this_slug`` continue — otherwise after a fill we never
                # poll TP again (balance lag / transient place failure would leave no 99c sells).
                self._maybe_sync_tp_limits(contract)

                if self._entry_dual_limits:
                    self._try_seed_dual_buy_limits(contract)
                    if not (self._seed_up_done and self._seed_down_done):
                        time.sleep(self.config.poll_interval_seconds)
                        continue
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
