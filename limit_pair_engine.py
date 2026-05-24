#!/usr/bin/env python3
"""
Schedule GTC limit buys on upcoming BTC 5m UP/DOWN windows.

Every ``search_interval`` seconds:
  1. Compute the next ``window_count`` five-minute epochs starting
     ``lead_minutes`` after now (aligned to UTC 5m boundaries).
  2. Resolve each slug on Gamma and cache contracts.
  3. Queue windows that are not yet marked sent.

Drain the queue at ``order_spacing`` seconds per window (UP + DOWN limits).

Drop cached contracts once both resting orders are confirmed on the CLOB.
Persist sent/confirmed slugs to disk so restarts do not double-post.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import LOGGER, ActiveContract, BotConfig
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_WINDOW_MINUTES = 5
_WINDOW_SEC = _WINDOW_MINUTES * 60


def _out(msg: str) -> None:
    print(msg, flush=True)


def _ceil_to_window(ts: int, window_sec: int) -> int:
    """Smallest epoch >= ts aligned to window_sec."""
    if ts <= 0:
        return 0
    return ((ts + window_sec - 1) // window_sec) * window_sec


def plan_window_starts(
    now_ts: int,
    *,
    lead_minutes: int,
    window_count: int,
    window_sec: int = _WINDOW_SEC,
) -> list[int]:
    """UTC 5m epochs for the next hour block after lead time (default 12 windows)."""
    anchor = int(now_ts) + int(lead_minutes) * 60
    first = _ceil_to_window(anchor, window_sec)
    n = max(0, int(window_count))
    return [first + i * window_sec for i in range(n)]


@dataclass(slots=True)
class _WindowJob:
    start_ts: int
    contract: ActiveContract


class LimitPairEngine:
    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
    ) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader

        self._lead_minutes = max(0, int(os.getenv("BOT_LIMIT_PAIR_LEAD_MINUTES", "15")))
        self._window_count = max(1, int(os.getenv("BOT_LIMIT_PAIR_WINDOW_COUNT", "12")))
        self._search_interval = max(
            30.0, float(os.getenv("BOT_LIMIT_PAIR_SEARCH_INTERVAL_SEC", "300"))
        )
        self._order_spacing = max(
            1.0, float(os.getenv("BOT_LIMIT_PAIR_ORDER_SPACING_SEC", "10"))
        )
        self._up_px = round(float(os.getenv("BOT_LIMIT_PAIR_UP_PX", "0.50")), 2)
        self._down_px = round(float(os.getenv("BOT_LIMIT_PAIR_DOWN_PX", "0.49")), 2)
        self._shares = max(1, int(os.getenv("BOT_LIMIT_PAIR_SHARES", "5")))
        self._price_tol = max(0.001, float(os.getenv("BOT_LIMIT_PAIR_PRICE_TOL", "0.01")))

        state_path = os.getenv("BOT_LIMIT_PAIR_STATE_PATH", "exports/limit_pair_state.json")
        self._state_path = Path(state_path)
        self._sent_slugs: set[str] = set()
        self._confirmed_slugs: set[str] = set()
        self._contract_cache: dict[str, ActiveContract] = {}
        self._pending_jobs: list[_WindowJob] = []
        self._last_search_monotonic = 0.0
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.error("limit_pair state load failed (%s): %s", self._state_path, exc)
            return
        if not isinstance(raw, dict):
            return
        self._sent_slugs = {str(s) for s in raw.get("sent_slugs") or []}
        self._confirmed_slugs = {str(s) for s in raw.get("confirmed_slugs") or []}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sent_slugs": sorted(self._sent_slugs),
            "confirmed_slugs": sorted(self._confirmed_slugs),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _emit_init(self) -> None:
        starts = plan_window_starts(
            int(time.time()),
            lead_minutes=self._lead_minutes,
            window_count=self._window_count,
        )
        span = ""
        if starts:
            t0 = datetime.fromtimestamp(starts[0], tz=timezone.utc).strftime("%H:%M")
            t1 = datetime.fromtimestamp(
                starts[-1] + _WINDOW_SEC, tz=timezone.utc
            ).strftime("%H:%M")
            span = f" next_block={t0}-{t1}Z"
        _out(
            "INIT "
            f"strategy=limit_pair_5m "
            f"up={self._up_px:g} down={self._down_px:g} shares={self._shares} "
            f"lead_min={self._lead_minutes} windows={self._window_count} "
            f"search_sec={self._search_interval:g} spacing_sec={self._order_spacing:g}"
            f"{span} "
            f"sent={len(self._sent_slugs)} confirmed={len(self._confirmed_slugs)} "
            f"dry_run={self.config.dry_run} "
            f"funder={self.config.funder[:6]}…{self.config.funder[-4:]}"
        )

    def _prune_confirmed_cache(self) -> None:
        drop = [s for s in self._contract_cache if s in self._confirmed_slugs]
        for slug in drop:
            del self._contract_cache[slug]

    def _reconcile_confirmed(self) -> None:
        """Promote sent → confirmed when both limits are visible on the CLOB."""
        if self.config.dry_run:
            return
        changed = False
        try:
            open_orders = self.trader.get_open_orders()
        except Exception as exc:
            LOGGER.debug("get_open_orders for reconcile: %s", exc)
            return

        for slug in list(self._sent_slugs):
            if slug in self._confirmed_slugs:
                continue
            contract = self._contract_cache.get(slug)
            if contract is None:
                continue
            if self._both_limits_present(contract, open_orders=open_orders):
                self._confirmed_slugs.add(slug)
                changed = True
                _out(f"CONFIRMED slug={slug} up={self._up_px:g} down={self._down_px:g}")
        if changed:
            self._prune_confirmed_cache()
            self._save_state()

    def _both_limits_present(
        self,
        contract: ActiveContract,
        *,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> bool:
        if self.config.dry_run:
            return True
        if open_orders is None:
            if self.trader.has_open_limit_buy_near(
                contract.up.token_id, self._up_px, tol=self._price_tol
            ) and self.trader.has_open_limit_buy_near(
                contract.down.token_id, self._down_px, tol=self._price_tol
            ):
                return True
            return False
        return self._token_has_buy_near(
            open_orders, contract.up.token_id, self._up_px
        ) and self._token_has_buy_near(
            open_orders, contract.down.token_id, self._down_px
        )

    @staticmethod
    def _token_has_buy_near(
        orders: list[dict[str, Any]], token_id: str, price: float, *, tol: float = 0.01
    ) -> bool:
        for o in orders:
            tid = str(
                o.get("asset_id")
                or o.get("assetId")
                or o.get("token_id")
                or o.get("tokenId")
                or ""
            )
            if tid != token_id:
                continue
            if str(o.get("side") or "").upper() != "BUY":
                continue
            try:
                px = float(o.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if abs(px - float(price)) > tol:
                continue
            try:
                sz = float(o.get("size") or o.get("original_size") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            if sz > 1e-6:
                return True
        return False

    def _search_windows(self) -> None:
        now_ts = int(time.time())
        targets = plan_window_starts(
            now_ts,
            lead_minutes=self._lead_minutes,
            window_count=self._window_count,
        )
        found = 0
        queued = 0
        for start_ts in targets:
            if start_ts + _WINDOW_SEC <= now_ts:
                continue
            contract = self.locator.get_contract_for_window_start(
                _WINDOW_MINUTES, start_ts
            )
            if contract is None:
                continue
            slug = contract.slug
            found += 1
            self._contract_cache[slug] = contract
            if slug in self._sent_slugs or slug in self._confirmed_slugs:
                continue
            if any(j.contract.slug == slug for j in self._pending_jobs):
                continue
            self._pending_jobs.append(_WindowJob(start_ts=start_ts, contract=contract))
            queued += 1
        if found or queued:
            _out(
                f"SEARCH targets={len(targets)} resolved={found} "
                f"queued={queued} pending={len(self._pending_jobs)}"
            )
        self.trader.sync_ws_subscriptions(list(self._contract_cache.values()))

    def _place_window_pair(self, job: _WindowJob) -> None:
        contract = job.contract
        slug = contract.slug
        if slug in self._sent_slugs:
            return
        if self.config.dry_run:
            self._sent_slugs.add(slug)
            self._save_state()
            _out(
                f"ORDER dry_run slug={slug} UP ${self._up_px:g}x{self._shares} "
                f"DOWN ${self._down_px:g}x{self._shares}"
            )
            return

        placed_any = False
        if not self.trader.has_open_limit_buy_near(
            contract.up.token_id, self._up_px, tol=self._price_tol
        ):
            try:
                self.trader.place_limit_buy(
                    contract.up, self._up_px, self._shares
                )
                placed_any = True
            except Exception as exc:
                LOGGER.error(
                    "[LIMIT_PAIR] %s UP $%.2f x %d failed: %s",
                    slug,
                    self._up_px,
                    self._shares,
                    exc,
                )
        if not self.trader.has_open_limit_buy_near(
            contract.down.token_id, self._down_px, tol=self._price_tol
        ):
            try:
                self.trader.place_limit_buy(
                    contract.down, self._down_px, self._shares
                )
                placed_any = True
            except Exception as exc:
                LOGGER.error(
                    "[LIMIT_PAIR] %s DOWN $%.2f x %d failed: %s",
                    slug,
                    self._down_px,
                    self._shares,
                    exc,
                )

        # Mark sent once we attempted (or orders already exist) so we do not spam.
        if (
            placed_any
            or self._both_limits_present(contract)
        ):
            self._sent_slugs.add(slug)
            self._save_state()
            _out(
                f"ORDER slug={slug} UP ${self._up_px:g}x{self._shares} "
                f"DOWN ${self._down_px:g}x{self._shares}"
            )

    def _drain_queue(self) -> None:
        while self._pending_jobs:
            job = self._pending_jobs.pop(0)
            self._place_window_pair(job)
            if self._pending_jobs:
                time.sleep(self._order_spacing)

    def run(self) -> None:
        self._emit_init()
        self._prune_confirmed_cache()

        while True:
            try:
                now_mono = time.monotonic()
                if (
                    self._last_search_monotonic <= 0
                    or (now_mono - self._last_search_monotonic) >= self._search_interval
                ):
                    self._search_windows()
                    self._last_search_monotonic = now_mono

                self._reconcile_confirmed()
                self._drain_queue()

                sleep_for = min(
                    self._search_interval,
                    max(1.0, self.config.poll_interval_seconds),
                )
                if self._pending_jobs:
                    sleep_for = min(sleep_for, self._order_spacing)
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("limit_pair main loop error")
                time.sleep(max(5.0, self.config.poll_interval_seconds))
