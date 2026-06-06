#!/usr/bin/env python3
"""BTC 5m late-high dominant 99c GTC limit-buy engine."""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import ActiveContract, BotConfig, LOGGER
from market_locator import GammaMarketLocator
from trader import PolymarketTrader

_WINDOW_MINUTES = 5
_WINDOW_SEC = _WINDOW_MINUTES * 60
_BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def _out(msg: str) -> None:
    print(msg, flush=True)


def _slug_start_ts(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


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
    candidates = [Path(configured.strip())] if configured.strip() else []
    candidates.extend([Path("/app/data/late_high_state.json"), Path("exports/late_high_state.json")])
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.is_absolute() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if _path_writable(path):
            return path
    return candidates[0]


def _fetch_btc_spot(symbol: str, timeout: float) -> float | None:
    try:
        r = requests.get(_BINANCE_PRICE_URL, params={"symbol": symbol.upper()}, timeout=timeout)
        r.raise_for_status()
        px = float(r.json()["price"])
        return px if px > 0 else None
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        LOGGER.debug("late_high BTC spot fetch failed: %s", exc)
        return None


def _fetch_window_open_btc(symbol: str, start_ts: int, timeout: float) -> float | None:
    try:
        r = requests.get(
            _BINANCE_KLINES_URL,
            params={
                "symbol": symbol.upper(),
                "interval": "5m",
                "startTime": int(start_ts) * 1000,
                "limit": 1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        px = float(rows[0][1])
        return px if px > 0 else None
    except (requests.RequestException, IndexError, KeyError, ValueError, TypeError) as exc:
        LOGGER.debug("late_high BTC window open fetch failed: %s", exc)
        return None


def _fetch_recent_window_volume_btc(symbol: str, start_ts: int, timeout: float) -> float | None:
    try:
        r = requests.get(
            _BINANCE_KLINES_URL,
            params={
                "symbol": symbol.upper(),
                "interval": "1m",
                "startTime": int(start_ts) * 1000,
                "limit": 6,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        return sum(max(0.0, float(row[5])) for row in rows)
    except (requests.RequestException, IndexError, KeyError, ValueError, TypeError) as exc:
        LOGGER.debug("late_high BTC volume fetch failed: %s", exc)
        return None


@dataclass(slots=True)
class _WindowState:
    slug: str | None = None
    start_btc: float | None = None
    btc_ticks: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=120))
    recent_volume_btc: float = 0.0
    next_volume_refresh_mono: float = 0.0


class LateHighEngine:
    def __init__(
        self,
        config: BotConfig,
        locator: GammaMarketLocator,
        trader: PolymarketTrader,
    ) -> None:
        self.config = config
        self.locator = locator
        self.trader = trader
        self._state_path = _resolve_state_path(config.late_high_state_path)
        self._submitted_slugs: set[str] = set()
        self._w = _WindowState()
        self._last_idle_log_mono = 0.0
        self._cached_balance_usdc: float | None = None
        self._next_balance_refresh_mono = 0.0
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.error("late_high state load failed (%s): %s", self._state_path, exc)
            return
        if isinstance(raw, dict):
            self._submitted_slugs = {str(s) for s in raw.get("submitted_slugs", [])}

    def _save_state(self) -> None:
        cutoff = int(time.time()) - 48 * 3600
        self._submitted_slugs = {
            slug for slug in self._submitted_slugs if (_slug_start_ts(slug) or 0) >= cutoff
        }
        payload = {
            "submitted_slugs": sorted(self._submitted_slugs),
            "updated_at": int(time.time()),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _reset_window(self, contract: ActiveContract) -> None:
        start_ts = _slug_start_ts(contract.slug)
        start_btc = None
        if start_ts is not None:
            start_btc = _fetch_window_open_btc(
                self.config.btc_feed_symbol,
                start_ts,
                self.config.request_timeout_seconds,
            )
        self._w = _WindowState(slug=contract.slug, start_btc=start_btc)
        self.trader.sync_ws_subscriptions([contract])

    def _update_btc_metrics(self, contract: ActiveContract, btc: float) -> tuple[float, float]:
        now_ts = int(time.time())
        self._w.btc_ticks.append((now_ts, btc))
        start_ts = _slug_start_ts(contract.slug)
        mono = time.monotonic()
        if start_ts is not None and mono >= self._w.next_volume_refresh_mono:
            vol = _fetch_recent_window_volume_btc(
                self.config.btc_feed_symbol,
                start_ts,
                self.config.request_timeout_seconds,
            )
            if vol is not None:
                self._w.recent_volume_btc = float(vol)
            self._w.next_volume_refresh_mono = mono + self.config.late_high_stats_refresh_seconds

        cutoff = now_ts - self.config.late_high_range_lookback_sec
        recent = [px for ts, px in self._w.btc_ticks if ts >= cutoff]
        if not recent and self._w.btc_ticks:
            recent = [self._w.btc_ticks[-1][1]]
        recent_range = max(recent) - min(recent) if recent else 0.0

        cutoff10 = now_ts - 10
        older = [px for ts, px in self._w.btc_ticks if ts <= cutoff10]
        ref = older[-1] if older else (self._w.btc_ticks[0][1] if self._w.btc_ticks else btc)
        move10 = btc - ref
        return recent_range, move10

    def _required_gap(self, elapsed: int, recent_range: float) -> float:
        base = (
            self.config.late_high_late_base_gap_usd
            if elapsed >= self.config.late_high_fallback_sec
            else self.config.late_high_early_base_gap_usd
        )
        return (
            base
            + self.config.late_high_range_gap_mult * max(0.0, recent_range)
            + self.config.late_high_volume_sqrt_gap_mult
            * math.sqrt(max(0.0, self._w.recent_volume_btc))
        )

    def _pick_signal(
        self,
        *,
        contract: ActiveContract,
        elapsed: int,
        btc: float,
        up_mid: float,
        down_mid: float,
        recent_range: float,
        move10: float,
    ) -> tuple[str, float] | None:
        if elapsed < self.config.late_high_entry_lo_sec or elapsed > self.config.late_high_entry_hi_sec:
            return None
        if (
            self.config.late_high_max_recent_range_usd > 0
            and recent_range > self.config.late_high_max_recent_range_usd
        ):
            return None
        if (
            self.config.late_high_max_move_10s_usd > 0
            and abs(move10) > self.config.late_high_max_move_10s_usd
        ):
            return None
        start_btc = self._w.start_btc
        if start_btc is None or start_btc <= 0:
            return None
        req_gap = self._required_gap(elapsed, recent_range)
        up_gap = btc - start_btc
        down_gap = start_btc - btc
        candidates: list[tuple[str, float]] = []
        if up_mid >= self.config.late_high_min_leg_px and up_gap >= req_gap:
            candidates.append(("UP", up_mid))
        if down_mid >= self.config.late_high_min_leg_px and down_gap >= req_gap:
            candidates.append(("DOWN", down_mid))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates[0][0], req_gap

    def _shares_for_balance(self, balance: float) -> tuple[float, float] | None:
        px = self.config.late_high_limit_px
        shares = max((balance * 0.50) / px, self.config.late_high_min_shares)
        shares = math.floor(shares * 10_000) / 10_000
        cost = shares * px
        if balance + 1e-9 < cost:
            return None
        return shares, cost

    def _balance_usdc(self) -> float:
        if self.config.dry_run:
            return max(
                self.config.late_high_limit_px * self.config.late_high_min_shares,
                10.0,
            )
        mono = time.monotonic()
        if self._cached_balance_usdc is None or mono >= self._next_balance_refresh_mono:
            self._cached_balance_usdc = self.trader.wallet_balance_usdc()
            self._next_balance_refresh_mono = mono + self.config.late_high_balance_refresh_seconds
        return self._cached_balance_usdc

    def _tick(self) -> None:
        contract = self.locator.get_active_contract_for_window_minutes(_WINDOW_MINUTES)
        if contract is None:
            return
        if self._w.slug != contract.slug:
            self._reset_window(contract)
        start_ts = _slug_start_ts(contract.slug)
        if start_ts is None:
            return
        if contract.slug in self._submitted_slugs:
            return

        now_ts = int(time.time())
        elapsed = now_ts - start_ts
        btc = _fetch_btc_spot(self.config.btc_feed_symbol, self.config.request_timeout_seconds)
        if btc is None:
            return
        if self._w.start_btc is None:
            self._w.start_btc = btc
        recent_range, move10 = self._update_btc_metrics(contract, btc)

        if self.trader.ws_quotes_active:
            up_mid = self.trader.get_ws_midpoint(contract.up.token_id)
            down_mid = self.trader.get_ws_midpoint(contract.down.token_id)
        else:
            up_mid = self.trader.get_midpoint(contract.up.token_id)
            down_mid = self.trader.get_midpoint(contract.down.token_id)
        if up_mid is None or down_mid is None:
            return

        sig = self._pick_signal(
            contract=contract,
            elapsed=elapsed,
            btc=btc,
            up_mid=up_mid,
            down_mid=down_mid,
            recent_range=recent_range,
            move10=move10,
        )
        if sig is None:
            if time.monotonic() - self._last_idle_log_mono >= 30:
                _out(
                    f"IDLE slug={contract.slug} elapsed={elapsed}s up={up_mid:.3f} down={down_mid:.3f} "
                    f"btc_gap={btc - (self._w.start_btc or btc):.2f} range={recent_range:.2f}"
                )
                self._last_idle_log_mono = time.monotonic()
            return

        side, req_gap = sig
        token = contract.up if side == "UP" else contract.down
        limit_px = self.config.late_high_limit_px
        balance = self._balance_usdc()
        if not self.config.dry_run and balance <= 0:
            _out(f"SKIP slug={contract.slug} reason=no_usdc_balance elapsed={elapsed}s")
            return
        sized = self._shares_for_balance(balance)
        if sized is None:
            need = self.config.late_high_limit_px * self.config.late_high_min_shares
            _out(
                f"SKIP slug={contract.slug} reason=insufficient_for_min_shares "
                f"balance={balance:.4f} need={need:.4f} elapsed={elapsed}s"
            )
            return
        shares, cost = sized

        if self.config.dry_run:
            _out(
                f"ORDER dry_run slug={contract.slug} side={side} limit={limit_px:.2f} "
                f"shares={shares:.4f} cost={cost:.4f} elapsed={elapsed}s req_gap={req_gap:.2f} "
                f"range={recent_range:.2f} vol={self._w.recent_volume_btc:.4f}"
            )
        else:
            self.trader.place_limit_buy(token, limit_px, shares)
            _out(
                f"ORDER slug={contract.slug} side={side} limit={limit_px:.2f} "
                f"shares={shares:.4f} cost={cost:.4f} elapsed={elapsed}s req_gap={req_gap:.2f} "
                f"range={recent_range:.2f} vol={self._w.recent_volume_btc:.4f}"
            )
        self._submitted_slugs.add(contract.slug)
        self._save_state()

    def run(self) -> None:
        _out(
            "INIT "
            f"strategy=late_high_5m symbol=BTC limit={self.config.late_high_limit_px:.2f} "
            f"min_shares={self.config.late_high_min_shares:g} entry={self.config.late_high_entry_lo_sec}-"
            f"{self.config.late_high_entry_hi_sec}s fallback={self.config.late_high_fallback_sec}s "
            f"min_leg={self.config.late_high_min_leg_px:.2f} dry_run={self.config.dry_run} "
            f"balance_refresh={self.config.late_high_balance_refresh_seconds:g}s "
            f"state={self._state_path}"
        )
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("late_high_5m tick error: %s", exc)
            time.sleep(self.config.poll_interval_seconds)
