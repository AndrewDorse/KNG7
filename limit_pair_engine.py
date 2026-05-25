#!/usr/bin/env python3
"""
Schedule GTC limit buys on upcoming multi-asset 5m UP/DOWN windows.

Work list (closest window start first):
  - Every ``search_interval``: refresh target epochs, append new slots, re-sort.
  - Drop a slot once **both** UP and DOWN limits are on the CLOB.
  - Each ``order_spacing`` cycle: try the **top** slot only; on balance/placement
    error, keep it at the front and retry after ``order_spacing`` (funds may free up).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from config import LOGGER, ActiveContract, BotConfig
from market_locator import GammaMarketLocator
from trader import PolymarketTrader, is_deposit_wallet_flow_error, wallet_config_hint_for_error

_WINDOW_MINUTES = 5
_WINDOW_SEC = _WINDOW_MINUTES * 60
_WINDOWS_PER_HOUR = 60 // _WINDOW_MINUTES  # 12


class _PlaceStatus(Enum):
    COMPLETE = "complete"
    RETRY = "retry"
    NOOP = "noop"


def _out(msg: str) -> None:
    print(msg, flush=True)


def _ceil_to_window(ts: int, window_sec: int) -> int:
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
    """Legacy fixed-count planner (lead + N windows)."""
    anchor = int(now_ts) + int(lead_minutes) * 60
    first = _ceil_to_window(anchor, window_sec)
    n = max(0, int(window_count))
    return [first + i * window_sec for i in range(n)]


def plan_future_window_starts(
    now_ts: int,
    *,
    horizon_minutes: int,
    window_sec: int = _WINDOW_SEC,
) -> list[int]:
    """5m epoch starts strictly in the future within ``now + horizon_minutes``."""
    horizon_sec = max(1, int(horizon_minutes)) * 60
    end_ts = int(now_ts) + horizon_sec
    first = _ceil_to_window(int(now_ts) + 1, window_sec)
    out: list[int] = []
    t = first
    while t <= end_ts:
        if t > int(now_ts):
            out.append(t)
        t += window_sec
    return out


def _path_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _resolve_state_path(configured: str) -> Path:
    """Pick first writable path (Docker default: /app/data)."""
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(Path(configured.strip()))
    candidates.extend(
        [
            Path("/app/data/limit_pair_state.json"),
            Path("exports/limit_pair_state.json"),
        ]
    )
    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.is_absolute() else str(p)
        if key in seen:
            continue
        seen.add(key)
        if _path_writable(p):
            return p
    return candidates[0]


def _is_balance_or_funds_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "insufficient",
        "not enough",
        "insufficient balance",
        "insufficient funds",
        "exceeds balance",
        "exceeds available",
        "balance too low",
        "not enough balance",
        "allowance",
    )
    return any(n in msg for n in needles)


@dataclass(slots=True)
class _WindowJob:
    symbol: str
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

        lp = config
        self._symbols = lp.limit_pair_symbols
        self._sym_rank = {s: i for i, s in enumerate(self._symbols)}
        self._horizon_minutes = lp.limit_pair_next_windows_search_minutes
        self._skip_first_windows = lp.limit_pair_skip_first_windows
        self._search_interval = lp.limit_pair_search_interval_seconds
        self._order_spacing = lp.limit_pair_order_spacing_seconds
        self._up_px = lp.limit_pair_up_px
        self._down_px = lp.limit_pair_down_px
        self._shares = lp.limit_pair_shares
        self._price_tol = lp.limit_pair_price_tol

        self._state_path = _resolve_state_path(lp.limit_pair_state_path)
        self._done_slugs: set[str] = set()
        # Legs we already POSTed (survives restarts) — prevents duplicate limits on retry.
        self._submitted_legs: dict[str, set[str]] = {}
        self._contract_cache: dict[str, ActiveContract] = {}
        self._work_list: list[_WindowJob] = []
        self._last_search_monotonic = 0.0
        self._wallet_blocked = False
        self._idle_log_interval = 60.0
        self._last_idle_log_monotonic = 0.0
        # Skip first N windows only on the first SEARCH after process start; later cycles use full horizon.
        self._startup_skip_pending = self._skip_first_windows > 0
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
        done = raw.get("done_slugs") or raw.get("confirmed_slugs") or raw.get("sent_slugs")
        self._done_slugs = {str(s) for s in (done or [])}
        submitted = raw.get("submitted_legs") or {}
        if isinstance(submitted, dict):
            for slug, legs in submitted.items():
                if isinstance(legs, list):
                    self._submitted_legs[str(slug)] = {
                        str(x).strip().upper() for x in legs if str(x).strip()
                    }

    def _save_state(self) -> None:
        payload = {
            "symbols": list(self._symbols),
            "done_slugs": sorted(self._done_slugs),
            "submitted_legs": {
                slug: sorted(legs) for slug, legs in sorted(self._submitted_legs.items())
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload, indent=2)
        for attempt in range(3):
            path = self._state_path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(body, encoding="utf-8")
                tmp.replace(path)
                return
            except PermissionError as exc:
                fallback = Path("/app/data/limit_pair_state.json")
                if path == fallback and attempt >= 2:
                    LOGGER.error("state write failed at %s: %s", path, exc)
                    raise
                LOGGER.error(
                    "state write permission denied at %s; switching to %s",
                    path,
                    fallback,
                )
                _out(f"STATE_FALLBACK path={fallback} (was {path})")
                self._state_path = fallback
            except OSError as exc:
                if attempt >= 2:
                    raise
                LOGGER.error("state write failed at %s: %s", self._state_path, exc)
                self._state_path = _resolve_state_path()

    def _job_sort_key(self, job: _WindowJob) -> tuple[int, int]:
        return (job.start_ts, self._sym_rank.get(job.symbol, 99))

    def _sort_work_list(self) -> None:
        self._work_list.sort(key=self._job_sort_key)

    def _emit_init(self) -> None:
        now_ts = int(time.time())
        starts = plan_future_window_starts(now_ts, horizon_minutes=self._horizon_minutes)
        span = ""
        if starts:
            t0 = datetime.fromtimestamp(starts[0], tz=timezone.utc).strftime("%H:%M")
            t1 = datetime.fromtimestamp(
                starts[-1] + _WINDOW_SEC, tz=timezone.utc
            ).strftime("%H:%M")
            span = f" block={t0}-{t1}Z"
        sym_s = ",".join(self._symbols)
        per_sym_first = max(0, len(starts) - self._skip_first_windows)
        slots = per_sym_first * len(self._symbols)
        _out(
            "INIT "
            f"strategy=limit_pair_5m symbols={sym_s} "
            f"up={self._up_px:g} down={self._down_px:g} shares={self._shares} "
            f"horizon_min={self._horizon_minutes} skip_first_at_startup={self._skip_first_windows} "
            f"slots_first_search≈{slots}{span} "
            f"search_sec={self._search_interval:g} spacing_sec={self._order_spacing:g} "
            f"state={self._state_path} done={len(self._done_slugs)} queue={len(self._work_list)} "
            f"dry_run={self.config.dry_run} "
            f"funder={self.config.funder[:6]}…{self.config.funder[-4:]}"
        )

    def _prune_work_list(self) -> None:
        now_ts = int(time.time())
        kept: list[_WindowJob] = []
        for job in self._work_list:
            slug = job.contract.slug
            if slug in self._done_slugs:
                continue
            if job.start_ts + _WINDOW_SEC <= now_ts:
                continue
            kept.append(job)
        self._work_list = kept
        self._sort_work_list()

    def _prune_contract_cache(self) -> None:
        for slug in list(self._contract_cache):
            if slug in self._done_slugs:
                del self._contract_cache[slug]

    def _fetch_open_orders(self, *, attempts: int = 3) -> list[dict[str, Any]]:
        last: list[dict[str, Any]] = []
        for i in range(max(1, attempts)):
            last = self.trader.get_open_orders()
            if last or i >= attempts - 1:
                return last
            time.sleep(0.4)
        return last

    def _note_submitted(self, slug: str, leg: str) -> None:
        self._submitted_legs.setdefault(slug, set()).add(leg.strip().upper())
        self._save_state()

    def _leg_resting_shares(
        self, token_id: str, price: float, open_orders: list[dict[str, Any]]
    ) -> float:
        return self.trader.resting_buy_shares_near(
            token_id, price, tol=self._price_tol, open_orders=open_orders
        )

    def _leg_on_book(
        self, token_id: str, price: float, open_orders: list[dict[str, Any]]
    ) -> bool:
        return self._leg_resting_shares(token_id, price, open_orders) >= float(self._shares) - 1e-6

    def _leg_should_skip_post(
        self,
        slug: str,
        leg: str,
        token_id: str,
        price: float,
        open_orders: list[dict[str, Any]],
    ) -> bool:
        """True when we should NOT post another limit for this leg."""
        leg_u = leg.strip().upper()
        resting = self._leg_resting_shares(token_id, price, open_orders)
        if resting >= float(self._shares) - 1e-6:
            return True
        submitted = self._submitted_legs.get(slug, set())
        if leg_u in submitted:
            # POST accepted; book may lag — do not stack duplicate orders.
            return True
        return False

    def _up_on_book(
        self, slug: str, contract: ActiveContract, open_orders: list[dict[str, Any]]
    ) -> bool:
        return self._leg_on_book(contract.up.token_id, self._up_px, open_orders)

    def _down_on_book(
        self, slug: str, contract: ActiveContract, open_orders: list[dict[str, Any]]
    ) -> bool:
        return self._leg_on_book(contract.down.token_id, self._down_px, open_orders)

    def _pair_complete(
        self,
        slug: str,
        contract: ActiveContract,
        open_orders: list[dict[str, Any]],
    ) -> bool:
        if self.config.dry_run:
            return slug in self._done_slugs
        return self._up_on_book(slug, contract, open_orders) and self._down_on_book(
            slug, contract, open_orders
        )

    def _trim_excess_for_contract(
        self, slug: str, contract: ActiveContract, open_orders: list[dict[str, Any]]
    ) -> None:
        if self.config.dry_run:
            return
        for leg, token, px in (
            ("UP", contract.up, self._up_px),
            ("DOWN", contract.down, self._down_px),
        ):
            resting = self.trader.resting_buy_shares_near(
                token.token_id, px, tol=self._price_tol, open_orders=open_orders
            )
            if resting <= float(self._shares) + 1e-6:
                continue
            n = self.trader.cancel_excess_limit_buys(
                token.token_id,
                px,
                float(self._shares),
                tol=self._price_tol,
                open_orders=open_orders,
            )
            if n:
                _out(
                    f"TRIM slug={slug} {leg} resting={resting:g} -> cap {self._shares} "
                    f"cancelled={n}"
                )
                open_orders[:] = self._fetch_open_orders()

    def _mark_done(self, slug: str) -> None:
        self._done_slugs.add(slug)
        self._submitted_legs.pop(slug, None)
        self._save_state()
        self._prune_contract_cache()
        self._work_list = [j for j in self._work_list if j.contract.slug != slug]

    def _reconcile_done_from_clob(self) -> None:
        """Promote work-list slots that already have both limits resting (e.g. after restart)."""
        if self.config.dry_run or not self._work_list:
            return
        try:
            open_orders = self.trader.get_open_orders()
        except Exception as exc:
            LOGGER.debug("get_open_orders reconcile: %s", exc)
            return
        for job in list(self._work_list):
            slug = job.contract.slug
            if slug in self._done_slugs:
                continue
            if self._pair_complete(slug, job.contract, open_orders):
                self._mark_done(slug)
                _out(f"DONE slug={slug} up={self._up_px:g} down={self._down_px:g}")

    def _search_windows(self) -> None:
        """Rebuild queue: future 5m windows in horizon, minus done."""
        now_ts = int(time.time())
        future_starts = plan_future_window_starts(
            now_ts, horizon_minutes=self._horizon_minutes
        )
        skip_n = self._skip_first_windows if self._startup_skip_pending else 0
        active_starts = future_starts[skip_n:]
        if self._startup_skip_pending:
            self._startup_skip_pending = False

        new_queue: list[_WindowJob] = []
        resolved = 0
        gamma_miss = 0

        for symbol in self._symbols:
            for start_ts in active_starts:
                contract = self.locator.get_contract_for_window_start(
                    _WINDOW_MINUTES,
                    start_ts,
                    market_symbol=symbol,
                )
                if contract is None:
                    gamma_miss += 1
                    continue
                slug = contract.slug
                resolved += 1
                self._contract_cache[slug] = contract
                if slug in self._done_slugs:
                    continue
                new_queue.append(
                    _WindowJob(symbol=symbol, start_ts=start_ts, contract=contract)
                )

        self._work_list = new_queue
        self._sort_work_list()
        self._prune_work_list()

        # Re-queued slots get a fresh post attempt each SEARCH (easier restart / recovery).
        queued_slugs = {j.contract.slug for j in self._work_list}
        cleared = 0
        for slug in list(self._submitted_legs):
            if slug in queued_slugs and slug not in self._done_slugs:
                del self._submitted_legs[slug]
                cleared += 1
        if cleared:
            self._save_state()

        top = ""
        if self._work_list:
            j = self._work_list[0]
            top = (
                f" next={j.contract.slug} "
                f"@{datetime.fromtimestamp(j.start_ts, tz=timezone.utc).strftime('%H:%M')}Z"
            )
        cleared_s = ""
        if cleared:
            cleared_s = f" cleared_submitted={cleared}"
        _out(
            f"SEARCH horizon_min={self._horizon_minutes} skip={skip_n} "
            f"epochs={len(future_starts)} active={len(active_starts)} "
            f"resolved={resolved} gamma_miss={gamma_miss} "
            f"queue={len(self._work_list)} done={len(self._done_slugs)}{cleared_s}{top}"
        )
        self.trader.sync_ws_subscriptions(list(self._contract_cache.values()))

    def _maybe_log_idle(self) -> None:
        now_mono = time.monotonic()
        if (now_mono - self._last_idle_log_monotonic) < self._idle_log_interval:
            return
        self._last_idle_log_monotonic = now_mono
        if self._wallet_blocked:
            _out("IDLE wallet_blocked=true — fix .env and restart")
            return
        if not self._work_list:
            _out(
                f"IDLE queue=0 done={len(self._done_slugs)} "
                f"horizon_min={self._horizon_minutes} (waiting for SEARCH)"
            )

    def _place_window_pair(self, job: _WindowJob) -> _PlaceStatus:
        contract = job.contract
        slug = contract.slug

        if self._wallet_blocked:
            return _PlaceStatus.NOOP

        if slug in self._done_slugs:
            return _PlaceStatus.NOOP

        if self.config.dry_run:
            self._mark_done(slug)
            _out(
                f"ORDER dry_run slug={slug} UP ${self._up_px:g}x{self._shares} "
                f"DOWN ${self._down_px:g}x{self._shares}"
            )
            return _PlaceStatus.COMPLETE

        open_orders = self._fetch_open_orders()
        self._trim_excess_for_contract(slug, contract, open_orders)

        if self._pair_complete(slug, contract, open_orders):
            self._mark_done(slug)
            _out(f"DONE slug={slug} (both limits already on book)")
            return _PlaceStatus.COMPLETE

        had_error = False
        balance_blocked = False

        if not self._leg_should_skip_post(
            slug, "UP", contract.up.token_id, self._up_px, open_orders
        ):
            try:
                self.trader.place_limit_buy(contract.up, self._up_px, self._shares)
                self._note_submitted(slug, "UP")
            except Exception as exc:
                had_error = True
                if is_deposit_wallet_flow_error(exc):
                    self._wallet_blocked = True
                    _out(f"WALLET_CONFIG slug={slug} — orders blocked until .env fixed")
                    LOGGER.error("%s", wallet_config_hint_for_error(exc))
                    return _PlaceStatus.NOOP
                if _is_balance_or_funds_error(exc):
                    balance_blocked = True
                LOGGER.error(
                    "[LIMIT_PAIR] %s UP $%.2f x %d failed: %s",
                    slug,
                    self._up_px,
                    self._shares,
                    exc,
                )
        if not self._leg_should_skip_post(
            slug, "DOWN", contract.down.token_id, self._down_px, open_orders
        ):
            try:
                self.trader.place_limit_buy(contract.down, self._down_px, self._shares)
                self._note_submitted(slug, "DOWN")
            except Exception as exc:
                had_error = True
                if is_deposit_wallet_flow_error(exc):
                    self._wallet_blocked = True
                    _out(f"WALLET_CONFIG slug={slug} — orders blocked until .env fixed")
                    LOGGER.error("%s", wallet_config_hint_for_error(exc))
                    return _PlaceStatus.NOOP
                if _is_balance_or_funds_error(exc):
                    balance_blocked = True
                LOGGER.error(
                    "[LIMIT_PAIR] %s DOWN $%.2f x %d failed: %s",
                    slug,
                    self._down_px,
                    self._shares,
                    exc,
                )

        open_orders = self._fetch_open_orders()
        self._trim_excess_for_contract(slug, contract, open_orders)

        if self._pair_complete(slug, contract, open_orders):
            self._mark_done(slug)
            _out(
                f"ORDER slug={slug} UP ${self._up_px:g}x{self._shares} "
                f"DOWN ${self._down_px:g}x{self._shares}"
            )
            return _PlaceStatus.COMPLETE

        if had_error:
            up_rest = self.trader.resting_buy_shares_near(
                contract.up.token_id, self._up_px, tol=self._price_tol, open_orders=open_orders
            )
            down_rest = self.trader.resting_buy_shares_near(
                contract.down.token_id,
                self._down_px,
                tol=self._price_tol,
                open_orders=open_orders,
            )
            why = "balance" if balance_blocked else "error"
            _out(
                f"RETRY_{why.upper()} slug={slug} "
                f"up={up_rest:g}/{self._shares} down={down_rest:g}/{self._shares} "
                f"in={self._order_spacing:g}s"
            )
            return _PlaceStatus.RETRY

        # Book still catching up after successful POST — wait, do not stack orders.
        _out(
            f"WAIT_BOOK slug={slug} in={self._order_spacing:g}s "
            f"(submitted, confirming on CLOB)"
        )
        return _PlaceStatus.RETRY

    def _process_top_job(self) -> _PlaceStatus:
        """Try the closest window at the front of the work list (one slot per cycle)."""
        if not self._work_list:
            return _PlaceStatus.NOOP
        job = self._work_list[0]
        status = self._place_window_pair(job)
        if status == _PlaceStatus.COMPLETE:
            # _mark_done already removed slug from work_list
            pass
        # RETRY: keep job at index 0 — do not pop
        return status

    def run(self) -> None:
        self._emit_init()
        self._prune_contract_cache()
        self._search_windows()
        self._last_search_monotonic = time.monotonic()

        while True:
            try:
                now_mono = time.monotonic()
                if (now_mono - self._last_search_monotonic) >= self._search_interval:
                    self._search_windows()
                    self._last_search_monotonic = now_mono

                self._reconcile_done_from_clob()
                self._maybe_log_idle()

                if self._work_list:
                    status = self._process_top_job()
                    if status in (_PlaceStatus.COMPLETE, _PlaceStatus.RETRY):
                        time.sleep(self._order_spacing)
                        continue

                sleep_for = min(
                    self._search_interval,
                    max(1.0, self.config.poll_interval_seconds),
                )
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("limit_pair main loop error")
                time.sleep(max(5.0, self.config.poll_interval_seconds))
