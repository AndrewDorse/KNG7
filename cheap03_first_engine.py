#!/usr/bin/env python3
"""
Live / dry-run: **first midpoint** on BTC 15m UP/DOWN where a leg is at or under **3c** (0.03),
same side rule as sims (``_cheap_side_at``), **one** $1 USDC FAK buy per window.

Stdout: **only** ``INIT`` line at start and ``WIN`` lines when a placed trade resolves winning.
Everything else is silent (set library loggers to CRITICAL before trader init).

Resting take-profit: every ``BOT_TP_POLL_SECONDS`` (default 15), and immediately after a fill,
places GTC limit sells at **99¢** for whole-share inventory on **each** of UP and DOWN tokens
that have a position (so two legs get two sells).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Literal

import requests

from config import GAMMA_URL, ActiveContract, BotConfig, TokenMarket
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

    def _emit_init(self, contract: ActiveContract | None) -> None:
        slug = contract.slug if contract else "(no contract yet)"
        _out(
            "INIT "
            f"strategy=first_cheap_03 slug={slug} "
            f"thr={self.thr:g} notional_usdc={self.notional:g} "
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
