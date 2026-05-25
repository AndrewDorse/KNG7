#!/usr/bin/env python3
"""
Schedule GTC limit buys on upcoming multi-asset 5m UP/DOWN windows.

Work list (closest window start first):
  - Every ``search_interval``: refresh target epochs, append new slots, re-sort.
  - Drop a slot once **both** UP and DOWN have resting limits and/or filled position.
  - Post a leg only when that token has **no** resting buy and **no** position.
  - **T+15…T+60:** if the window is not fully hedged with both legs filled, force-exit:
    cancel all window orders + sell all window positions @ 1¢ FAK, poll 1s until clear.
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
# Min conditional balance to treat as "has position" (avoid dust reposts).
_POSITION_EPS = 0.01


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


def _slug_window_start(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


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


@dataclass(slots=True)
class _WindowExposure:
    """Resting orders + positions for one window's UP/DOWN tokens only."""

    up_pos: float
    down_pos: float
    up_rest: float
    down_rest: float


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
        self._window_start_by_slug: dict[str, int] = {}
        self._cleanup_done_slugs: set[str] = set()
        self._cleanup_offset_sec = lp.limit_pair_window_cleanup_offset_sec
        self._cleanup_until_sec = lp.limit_pair_window_cleanup_until_sec
        self._exit_sell_px = lp.limit_pair_exit_sell_price
        self._cleanup_poll_sec = lp.limit_pair_cleanup_poll_sec
        self._cleanup_sell_rounds = lp.limit_pair_cleanup_sell_max_rounds
        self._cleanup_armed_slugs: set[str] = set()
        self._cleanup_flatten_active: set[str] = set()
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
        cleaned = raw.get("cleanup_done_slugs") or []
        self._cleanup_done_slugs = {str(s) for s in cleaned}
        flatten_active = raw.get("cleanup_flatten_active") or []
        self._cleanup_flatten_active = {str(s) for s in flatten_active}

    def _save_state(self) -> None:
        payload = {
            "symbols": list(self._symbols),
            "done_slugs": sorted(self._done_slugs),
            "submitted_legs": {
                slug: sorted(legs) for slug, legs in sorted(self._submitted_legs.items())
            },
            "cleanup_done_slugs": sorted(self._cleanup_done_slugs),
            "cleanup_flatten_active": sorted(self._cleanup_flatten_active),
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
            f"cleanup_t+{self._cleanup_offset_sec}-{self._cleanup_until_sec}s "
            f"poll={self._cleanup_poll_sec:g}s exit_px={self._exit_sell_px:g} "
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
        """Keep contracts long enough for T+15 cleanup; drop old finished windows only."""
        now_ts = int(time.time())
        for slug in list(self._contract_cache):
            start = self._window_start_by_slug.get(slug) or _slug_window_start(slug) or 0
            if start <= 0:
                continue
            if slug in self._cleanup_flatten_active:
                continue
            if now_ts < (start + _WINDOW_SEC):
                continue
            if slug not in self._cleanup_done_slugs and slug not in self._done_slugs:
                continue
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

    def _leg_resting_any(
        self, token_id: str, open_orders: list[dict[str, Any]]
    ) -> float:
        return self.trader.resting_buy_shares_on_token(
            token_id, open_orders=open_orders
        )

    def _leg_resting_near(
        self, token_id: str, price: float, open_orders: list[dict[str, Any]]
    ) -> float:
        return self.trader.resting_buy_shares_near(
            token_id, price, tol=self._price_tol, open_orders=open_orders
        )

    def _leg_position_shares(self, token_id: str) -> float:
        try:
            return max(0.0, float(self.trader.token_balance(token_id)))
        except Exception as exc:
            LOGGER.debug("token_balance %s: %s", token_id[:16], exc)
            return 0.0

    def _leg_covered(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]],
        *,
        position_shares: float | None = None,
    ) -> bool:
        """True when this side already has a resting buy and/or filled position."""
        resting = self._leg_resting_any(token_id, open_orders)
        if resting > 1e-6:
            return True
        pos = (
            position_shares
            if position_shares is not None
            else self._leg_position_shares(token_id)
        )
        return pos >= _POSITION_EPS

    def _leg_should_post(
        self,
        token_id: str,
        open_orders: list[dict[str, Any]],
        *,
        position_shares: float | None = None,
    ) -> bool:
        """Post only when no resting order and no position on this token."""
        return not self._leg_covered(
            token_id, open_orders, position_shares=position_shares
        )

    def _pair_complete(
        self,
        slug: str,
        contract: ActiveContract,
        open_orders: list[dict[str, Any]],
        *,
        up_pos: float | None = None,
        down_pos: float | None = None,
    ) -> bool:
        if self.config.dry_run:
            return slug in self._done_slugs
        up_position = (
            up_pos if up_pos is not None else self._leg_position_shares(contract.up.token_id)
        )
        down_position = (
            down_pos
            if down_pos is not None
            else self._leg_position_shares(contract.down.token_id)
        )
        return self._leg_covered(
            contract.up.token_id, open_orders, position_shares=up_position
        ) and self._leg_covered(
            contract.down.token_id, open_orders, position_shares=down_position
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
        self._work_list = [j for j in self._work_list if j.contract.slug != slug]

    def _window_start_for(self, slug: str, contract: ActiveContract) -> int:
        return (
            self._window_start_by_slug.get(slug)
            or _slug_window_start(slug)
            or 0
        )

    def _contract_for_slug(self, slug: str) -> ActiveContract | None:
        if slug in self._contract_cache:
            return self._contract_cache[slug]
        start = _slug_window_start(slug)
        if start is None:
            return None
        sym = str(slug).split("-")[0].upper()
        if sym not in self._symbols:
            return None
        contract = self.locator.get_contract_for_window_start(
            _WINDOW_MINUTES, start, market_symbol=sym
        )
        if contract is not None:
            self._contract_cache[slug] = contract
            self._window_start_by_slug[slug] = start
        return contract

    def _contracts_due_cleanup(
        self, now_ts: int
    ) -> list[tuple[str, ActiveContract, int]]:
        """Windows in T+offset…T+until (and flatten-active until flat)."""
        slug_keys = (
            set(self._contract_cache)
            | set(self._submitted_legs)
            | self._cleanup_flatten_active
        )
        due: list[tuple[str, ActiveContract, int]] = []
        for slug in slug_keys:
            if slug in self._cleanup_done_slugs:
                continue
            contract = self._contract_for_slug(slug)
            if contract is None:
                continue
            start = self._window_start_for(slug, contract)
            if start <= 0:
                continue
            trigger = start + self._cleanup_offset_sec
            in_window = trigger <= now_ts < start + _WINDOW_SEC
            if in_window or slug in self._cleanup_flatten_active:
                due.append((slug, contract, start))
        due.sort(key=lambda x: x[2])
        return due

    def _in_cleanup_intensive_window(self, elapsed: int) -> bool:
        """T+15s inclusive through T+60s exclusive — 1 Hz cancel/sell/verify."""
        return self._cleanup_offset_sec <= elapsed < self._cleanup_until_sec

    def _cleanup_phase_active(self, now_ts: int | None = None) -> bool:
        """True while flatten is active (pauses new limit placement)."""
        return bool(self._cleanup_flatten_active)

    def _window_exposure(
        self, contract: ActiveContract, open_orders: list[dict[str, Any]]
    ) -> _WindowExposure:
        """Snapshot this window's tokens only (not other windows' token IDs)."""
        return _WindowExposure(
            up_pos=self.trader.token_balance_allowance_refreshed(
                contract.up.token_id
            ),
            down_pos=self.trader.token_balance_allowance_refreshed(
                contract.down.token_id
            ),
            up_rest=self.trader.resting_order_shares_on_token(
                contract.up.token_id, open_orders=open_orders
            ),
            down_rest=self.trader.resting_order_shares_on_token(
                contract.down.token_id, open_orders=open_orders
            ),
        )

    def _exposure_detail(self, exp: _WindowExposure) -> str:
        return (
            f"UP:rest={exp.up_rest:g},pos={exp.up_pos:g} "
            f"DOWN:rest={exp.down_rest:g},pos={exp.down_pos:g}"
        )

    def _window_has_any_exposure(self, exp: _WindowExposure) -> bool:
        return (
            exp.up_pos >= _POSITION_EPS
            or exp.down_pos >= _POSITION_EPS
            or exp.up_rest > 1e-6
            or exp.down_rest > 1e-6
        )

    def _window_has_both_legs_filled(self, exp: _WindowExposure) -> bool:
        return exp.up_pos >= _POSITION_EPS and exp.down_pos >= _POSITION_EPS

    def _should_trigger_window_flatten(self, exp: _WindowExposure) -> bool:
        """
        At T+15 and later, flatten any window that still has exposure but is not fully
        hedged with both UP and DOWN positions filled.
        """
        return self._window_has_any_exposure(exp) and not self._window_has_both_legs_filled(exp)

    def _window_is_flat(self, contract: ActiveContract) -> tuple[bool, str]:
        """No open orders and no position on either UP/DOWN token."""
        open_orders = self._fetch_open_orders()
        exp = self._window_exposure(contract, open_orders)
        if exp.up_rest > 1e-6 or exp.up_pos >= _POSITION_EPS:
            return False, self._exposure_detail(exp)
        if exp.down_rest > 1e-6 or exp.down_pos >= _POSITION_EPS:
            return False, self._exposure_detail(exp)
        return True, self._exposure_detail(exp)

    def _finish_cleanup_monitoring(self, slug: str, *, reason: str) -> None:
        """Stop cleanup polls for this slug without marking placement done."""
        self._cleanup_done_slugs.add(slug)
        self._cleanup_armed_slugs.discard(slug)
        self._cleanup_flatten_active.discard(slug)
        self._save_state()
        _out(f"CLEANUP_MONITOR_END slug={slug} {reason}")

    def _finish_window_cleanup(self, slug: str) -> None:
        self._cleanup_done_slugs.add(slug)
        self._cleanup_flatten_active.discard(slug)
        self._cleanup_armed_slugs.discard(slug)
        self._done_slugs.add(slug)
        self._submitted_legs.pop(slug, None)
        self._work_list = [j for j in self._work_list if j.contract.slug != slug]
        self._save_state()

    def _run_cleanup_tick(
        self, slug: str, contract: ActiveContract, start_ts: int
    ) -> None:
        """T+15…T+60: on one-side risk, cancel ALL orders + sell ALL positions in window."""
        if self._wallet_blocked:
            return

        now_ts = int(time.time())
        elapsed = now_ts - start_ts
        if elapsed < self._cleanup_offset_sec:
            return

        open_orders = self._fetch_open_orders()
        exp = self._window_exposure(contract, open_orders)
        detail = self._exposure_detail(exp)
        intensive = self._in_cleanup_intensive_window(elapsed)

        if self.config.dry_run:
            if self._should_trigger_window_flatten(exp):
                self._cleanup_flatten_active.add(slug)
            if slug in self._cleanup_flatten_active and elapsed >= self._cleanup_offset_sec:
                self._finish_window_cleanup(slug)
            elif elapsed >= self._cleanup_until_sec:
                self._finish_cleanup_monitoring(slug, reason="dry_run")
            return

        if slug not in self._cleanup_flatten_active:
            if not self._window_has_any_exposure(exp):
                if intensive:
                    _out(f"CLEANUP_WATCH slug={slug} t+{elapsed}s flat {detail}")
                if elapsed >= self._cleanup_until_sec:
                    self._finish_cleanup_monitoring(slug, reason="flat_by_t60")
                return
            if self._window_has_both_legs_filled(exp):
                if intensive:
                    _out(f"CLEANUP_WATCH slug={slug} t+{elapsed}s both_legs_filled {detail}")
                if elapsed >= self._cleanup_until_sec:
                    self._finish_cleanup_monitoring(slug, reason="both_legs_filled")
                return
            if not self._should_trigger_window_flatten(exp):
                return
            self._cleanup_flatten_active.add(slug)
            self._save_state()
            _out(
                f"CLEANUP_TRIGGER slug={slug} t+{elapsed}s "
                f"cancel_all+sell_all exit={self._exit_sell_px:g} {detail}"
            )

        try:
            result = self.trader.flatten_window_contract(
                contract,
                self._exit_sell_px,
                max_sell_rounds=self._cleanup_sell_rounds,
                position_eps=_POSITION_EPS,
            )
        except Exception as exc:
            LOGGER.exception("CLEANUP_FLATTEN failed slug=%s t+%ss: %s", slug, elapsed, exc)
            _out(f"CLEANUP_ERROR slug={slug} t+{elapsed}s {exc}")
            return

        _out(
            f"CLEANUP_FLATTEN slug={slug} t+{elapsed}s/{self._cleanup_until_sec}s "
            f"cancel={result.get('cancel_confirmed')}/{result.get('cancel_attempted')} "
            f"up_rest={result.get('up_rest'):g} down_rest={result.get('down_rest'):g} "
            f"up_pos={result.get('up_pos'):g} down_pos={result.get('down_pos'):g} "
            f"sells={result.get('sells')} flat={result.get('flat')}"
        )

        if result.get("flat"):
            _out(
                f"CLEANUP_DONE slug={slug} t+{elapsed}s "
                f"UP:rest={result.get('up_rest'):g},pos={result.get('up_pos'):g} "
                f"DOWN:rest={result.get('down_rest'):g},pos={result.get('down_pos'):g}"
            )
            self._finish_window_cleanup(slug)
            return

        if intensive:
            _out(f"CLEANUP_POLL slug={slug} t+{elapsed}s NOT_FLAT {detail}")
        elif elapsed >= self._cleanup_until_sec:
            _out(
                f"CLEANUP_STUCK slug={slug} t+{elapsed}s past_t+{self._cleanup_until_sec}s "
                f"{detail} (keep polling 1s until flat)"
            )

    def _run_due_window_cleanups(self) -> None:
        now_ts = int(time.time())
        for slug, contract, start_ts in self._contracts_due_cleanup(now_ts):
            try:
                self._run_cleanup_tick(slug, contract, start_ts)
            except Exception:
                LOGGER.exception("window cleanup failed slug=%s", slug)
        self._prune_contract_cache()

    def _main_loop_sleep_sec(self) -> float:
        now_ts = int(time.time())
        if self._contracts_due_cleanup(now_ts):
            return self._cleanup_poll_sec
        return min(
            self._search_interval,
            max(1.0, self.config.poll_interval_seconds),
        )

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
            up_pos = self._leg_position_shares(job.contract.up.token_id)
            down_pos = self._leg_position_shares(job.contract.down.token_id)
            if self._pair_complete(
                slug, job.contract, open_orders, up_pos=up_pos, down_pos=down_pos
            ):
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
                self._window_start_by_slug[slug] = start_ts
                if slug in self._done_slugs:
                    continue
                new_queue.append(
                    _WindowJob(symbol=symbol, start_ts=start_ts, contract=contract)
                )

        self._work_list = new_queue
        self._sort_work_list()
        self._prune_work_list()

        top = ""
        if self._work_list:
            j = self._work_list[0]
            top = (
                f" next={j.contract.slug} "
                f"@{datetime.fromtimestamp(j.start_ts, tz=timezone.utc).strftime('%H:%M')}Z"
            )
        _out(
            f"SEARCH horizon_min={self._horizon_minutes} skip={skip_n} "
            f"epochs={len(future_starts)} active={len(active_starts)} "
            f"resolved={resolved} gamma_miss={gamma_miss} "
            f"queue={len(self._work_list)} done={len(self._done_slugs)}{top}"
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
        if job.start_ts > 0:
            self._window_start_by_slug[slug] = job.start_ts
        self._contract_cache[slug] = contract

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
        up_pos = self._leg_position_shares(contract.up.token_id)
        down_pos = self._leg_position_shares(contract.down.token_id)
        self._trim_excess_for_contract(slug, contract, open_orders)

        if self._pair_complete(
            slug, contract, open_orders, up_pos=up_pos, down_pos=down_pos
        ):
            self._mark_done(slug)
            _out(
                f"DONE slug={slug} up_rest={self._leg_resting_any(contract.up.token_id, open_orders):g} "
                f"up_pos={up_pos:g} down_rest={self._leg_resting_any(contract.down.token_id, open_orders):g} "
                f"down_pos={down_pos:g}"
            )
            return _PlaceStatus.COMPLETE

        had_error = False
        balance_blocked = False
        placed_any = False

        for leg, token, px, pos in (
            ("UP", contract.up, self._up_px, up_pos),
            ("DOWN", contract.down, self._down_px, down_pos),
        ):
            rest_any = self._leg_resting_any(token.token_id, open_orders)
            if not self._leg_should_post(
                token.token_id, open_orders, position_shares=pos
            ):
                _out(
                    f"SKIP_POST slug={slug} {leg} resting={rest_any:g} pos={pos:g}"
                )
                continue
            try:
                self.trader.place_limit_buy(token, px, self._shares)
                self._note_submitted(slug, leg)
                placed_any = True
                _out(f"POST slug={slug} {leg} ${px:g}x{self._shares}")
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
                    "[LIMIT_PAIR] %s %s $%.2f x %d failed: %s",
                    slug,
                    leg,
                    px,
                    self._shares,
                    exc,
                )

        open_orders = self._fetch_open_orders()
        up_pos = self._leg_position_shares(contract.up.token_id)
        down_pos = self._leg_position_shares(contract.down.token_id)
        self._trim_excess_for_contract(slug, contract, open_orders)

        if self._pair_complete(
            slug, contract, open_orders, up_pos=up_pos, down_pos=down_pos
        ):
            self._mark_done(slug)
            _out(f"DONE slug={slug} after post")
            return _PlaceStatus.COMPLETE

        if had_error:
            why = "balance" if balance_blocked else "error"
            _out(
                f"RETRY_{why.upper()} slug={slug} "
                f"up_rest={self._leg_resting_any(contract.up.token_id, open_orders):g} "
                f"up_pos={up_pos:g} "
                f"down_rest={self._leg_resting_any(contract.down.token_id, open_orders):g} "
                f"down_pos={down_pos:g} in={self._order_spacing:g}s"
            )
            return _PlaceStatus.RETRY

        if not placed_any:
            _out(
                f"ADVANCE slug={slug} both sides covered "
                f"up_rest={self._leg_resting_any(contract.up.token_id, open_orders):g} "
                f"up_pos={up_pos:g} "
                f"down_rest={self._leg_resting_any(contract.down.token_id, open_orders):g} "
                f"down_pos={down_pos:g}"
            )
            self._mark_done(slug)
            return _PlaceStatus.COMPLETE

        _out(
            f"PENDING slug={slug} in={self._order_spacing:g}s "
            f"up_rest={self._leg_resting_any(contract.up.token_id, open_orders):g} "
            f"up_pos={up_pos:g} "
            f"down_rest={self._leg_resting_any(contract.down.token_id, open_orders):g} "
            f"down_pos={down_pos:g}"
        )
        return _PlaceStatus.RETRY

    def _process_top_job(self) -> _PlaceStatus:
        """Try the closest window at the front of the work list (one slot per cycle)."""
        if not self._work_list:
            return _PlaceStatus.NOOP
        job = self._work_list[0]
        status = self._place_window_pair(job)
        if status == _PlaceStatus.COMPLETE:
            return status
        if status == _PlaceStatus.RETRY:
            # If another queued window is closer in time, don't block the whole bot on one slug.
            if len(self._work_list) > 1:
                self._work_list.append(self._work_list.pop(0))
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
                self._run_due_window_cleanups()
                self._maybe_log_idle()

                if self._work_list and not self._cleanup_phase_active():
                    status = self._process_top_job()
                    if status in (_PlaceStatus.COMPLETE, _PlaceStatus.RETRY):
                        time.sleep(self._order_spacing)
                        continue

                time.sleep(self._main_loop_sleep_sec())
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("limit_pair main loop error")
                time.sleep(max(5.0, self.config.poll_interval_seconds))
