#!/usr/bin/env python3
"""Configuration, data types, and shared utilities."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("polymarket_btc_ladder")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
GAMMA_URL = "https://gamma-api.polymarket.com"
BUY = "BUY"
SELL = "SELL"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class BotConfigError(RuntimeError):
    pass


def _strip_env_copy_artifacts(value: str) -> str:
    """Remove stray prefixes from pasted env values (e.g. `.git0x...` from a broken .env line)."""
    s = value.strip().strip('"').strip("'")
    if s.startswith(".git") and s[4:].strip().lower().startswith("0x"):
        LOGGER.warning(
            "Stripped stray '.git' prefix from an address/env value — fix your .env (duplicate key or bad paste)."
        )
        s = s[4:].strip()
    return s


def _normalize_polymarket_funder(raw: str) -> str:
    """POLY_FUNDER must be a checksummable 0x + 40 hex EVM address."""
    s = _strip_env_copy_artifacts(raw)
    if not s.startswith("0x"):
        raise BotConfigError(
            "POLY_FUNDER must be an Ethereum address starting with 0x. "
            f"Check for typos or a stray prefix. Raw length={len(raw.strip())!r}."
        )
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", s):
        raise BotConfigError(
            "POLY_FUNDER must be exactly 0x plus 40 hexadecimal characters "
            f"(42 chars total). After cleanup, got length {len(s)}. First chars: {s[:12]}…"
        )
    return s


def _normalize_strategy_mode(raw: str | None) -> str:
    """Canonicalize strategy_mode so strategy aliases always match engine guards."""
    s = (raw or "iy2").strip().lower()
    for ch in ("\r", "\n", "\t"):
        s = s.replace(ch, "")
    s = s.replace("-", "_")
    s = "_".join(s.split())
    if s in ("btc_perp15", "btc_perp_15", "perp15", "btc_15m_perp", "btc_perpetual_15m", "polymarket_btc_15m_perpetual"):
        return "btc_perp15"
    if s in ("volume_scalp_up", "volume_scalp", "vol_scalp_up"):
        return "volume_scalp_up"
    if s in ("champ4_6s", "champ4", "champ4_live", "wallet_dual", "wallet_dual_live"):
        return "champ4_6s"
    if s in ("paladin", "paladin_live", "paladin_pair"):
        return "paladin"
    if s in ("paladin_v7", "paladin7", "paladin_v7_live", "kng3", "kng3_live"):
        return "paladin_v7"
    if s in ("paladin_v9", "paladin9", "paladin_v9_live", "kng3_v9", "v9_live"):
        return "paladin_v9"
    if s in ("shaman_v1", "shaman1", "shaman"):
        return "shaman_v1"
    if s in ("first_cheap_03", "cheap03", "3c_first", "first_cheap3"):
        return "first_cheap_03"
    if s in ("iy2", "iy_2", "wallet_overlap", "wallet_overlap_live", "iy2_live"):
        return "iy2"
    if s in ("iy3", "iy_3", "wallet_overlap_path", "wallet_overlap_path_live", "iy3_live"):
        return "iy3"
    if "t10" in s:
        return s
    if "scalp" in s and "volume" in s:
        return "volume_scalp_up"
    if s in ("scalp_up", "btc_volume_scalp", "vol_scalp", "volumescalp"):
        return "volume_scalp_up"
    return s


@dataclass(slots=True)
class BotConfig:
    private_key: str
    funder: str
    bot_version: str = "2026-04-15 19:10:00"
    signature_type: int = 0
    dry_run: bool = True
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 10.0
    log_level: str = "INFO"
    relayer_api_key: str = ""
    relayer_secret: str = ""
    relayer_passphrase: str = ""
    force_exit_before_end_seconds: int = 15
    # Ladder config
    ladder_prices: list = field(default_factory=lambda: [0.44, 0.34, 0.24, 0.14])
    shares_per_level: int = 5
    order_cooldown_seconds: float = 3.0
    hedge_offset: float = 0.02
    market_symbol: str = "BTC"
    window_minutes: int = 15
    # Unused by ``GammaMarketLocator`` (kept for env compatibility): old grace flipped to next slug mid-window.
    window_pick_current_grace_seconds: int = 300
    trade_one_window: bool = False
    strategy_budget_cap_usdc: float = 80.0
    strategy_wallet_reserve_usdc: float = 0.0
    strategy_min_budget_usdc: float = 15.0
    strategy_entry_delay_seconds: int = 0
    strategy_new_order_cutoff_seconds: int = 30
    strategy_fill_grace_seconds: float = 5.0
    strategy_stale_order_seconds: float = 8.0
    strategy_max_live_orders: int = 4
    strategy_heartbeat_interval_seconds: int = 15
    strategy_price_record_interval_seconds: float = 1.0
    strategy_price_buffer: float = 0.02
    strategy_primary_flip_threshold: float = 0.05
    strategy_max_reversals: int = 1
    strategy_min_stop_orders_per_side: int = 5
    strategy_primary_unlock_seconds: int = 90
    strategy_primary_lock_seconds: int = 720
    strategy_pair_soft_limit: float = 1.03
    strategy_pair_hard_limit: float = 1.06
    strategy_late_stop_seconds: int = 780
    strategy_late_stop_worst_case_usdc: float = 1.5
    strategy_balance_retry_seconds: int = 10
    strategy_balance_retry_attempts: int = 3
    btc_feed_enabled: bool = True
    btc_feed_poll_seconds: float = 1.0
    btc_feed_symbol: str = "BTCUSDT"
    signal_preset: str = "w1"
    # SHAMAN-only entry (main.py): use shaman_v1. Other mode strings remain for config/imports elsewhere.
    strategy_mode: str = "shaman_v1"
    # volume scalp: fixed-lot directional entries with one shared TP per held side plus stop/time-exit risk control.
    volume_scalp_tp_offset: float = 0.12
    volume_scalp_stop_offset: float = 0.05
    volume_scalp_shares: int = 6
    volume_scalp_max_orders_per_side: int = 3
    volume_scalp_entry_min_elapsed: int = 60
    volume_scalp_entry_max_elapsed: int = 840
    volume_scalp_time_exit_seconds_remaining: int = 60
    volume_scalp_volume_ratio: float = 2.5
    # BTC 15m perp ladder: UP-only, early BTC trend gate, passive entry ladder.
    btc_perp15_monitor_seconds: int = 120
    btc_perp15_btc_trend_threshold: float = 0.0005
    btc_perp15_entry_window_seconds: int = 420
    btc_perp15_ladder_prices: list[float] = field(default_factory=lambda: [0.44, 0.43, 0.40])
    btc_perp15_min_shares: int = 6
    btc_perp15_risk_pct: float = 0.10
    btc_perp15_tp_price: float = 0.99
    btc_perp15_sample_interval_seconds: float = 5.0
    # When T-remaining <= this, flatten any positive window position with a marketable sell (btc_perp15 only).
    btc_perp15_end_dump_seconds_remaining: float = 15.0
    # CLOB market WebSocket (PALADIN / low-latency quotes) + FAK fill confirmation
    polymarket_ws_enabled: bool = True
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_fak_confirm_get_order: bool = True
    # PALADIN live (pair-only): primary goal is a low *held* pair cost (avg_up + avg_down after fills), not chasing
    # instantaneous pm_up+pm_down (often ~1.0 in an efficient book). Stagger second leg: default uses post-fill
    # blended cap (paladin_max_blended_pair_avg_sum) + ROI; set paladin_stagger_second_leg_require_live_mid_pair_sum
    # True for legacy behavior that also requires live mid sum <= paladin_pair_sum_max until hedge-force.
    # Symmetric pair opens / sims still use paladin_pair_sum_max on live mids where applicable.
    # Empty-book ROI hint: (pm_u+pm_d) <= 1/(1+target_min_roi). Hedge-force timer can relax via paladin_pair_sum_max_on_forced_hedge.
    paladin_pair_sum_max: float = 0.97
    # After hedge-force timer: second leg may complete if mid sum <= this (default 1.0 = any valid book).
    paladin_pair_sum_max_on_forced_hedge: float | None = 1.0
    # Calibrated ladder (PALADIN/calibrate_ladder_wallet_windows.py): target_min_roi=0 matches 100-window sim.
    paladin_target_min_roi: float = 0.0
    paladin_heartbeat_seconds: float = 15.0
    # Staggered pair: first FAK on cheaper side if mid <= this; complete pair when sum+ROI allow.
    paladin_stagger_pair: bool = True
    paladin_first_leg_max_px: float = 0.55
    # Sim/live: after this many seconds past hedge-ready, force the 2nd leg (ROI gate skipped; pair-sum cap uses
    # paladin_pair_sum_max_on_forced_hedge, default 1.0). 90s matches PALADIN A/B variant E.
    paladin_stagger_hedge_force_after_seconds: float | None = 90.0
    # Cap inventory per outcome side (None / <=0 = no cap). PALADIN v4 live default: 10/side (override via env).
    paladin_max_shares_per_side: float | None = 10.0
    # Live default 0 (no artificial delay). Use ~2 in replay/batch to mimic one fill per sim second.
    paladin_cooldown_seconds: float = 0.0
    # Per-leg clip cap when min leg >= 20 (pair_clip_candidates_dynamic upper bound).
    paladin_dynamic_clip_cap: float = 12.0
    # 0.0 matches ladder calibration; >0 tightens effective pair_sum after each fill.
    paladin_pair_sum_tighten_per_fill: float = 0.0
    paladin_pair_sum_min_floor: float = 0.90
    paladin_pending_hedge_bypass_imbalance_shares: float | None = 10.0
    paladin_discipline_relax_after_forced_sec: float | None = 60.0
    # PALADIN v4: stricter second-leg vs book; cap post-fill avg_up+avg_down (both legs must exist for check).
    paladin_second_leg_book_improve_eps: float = 0.013
    # If True: stagger 2nd leg (non-forced) also requires pm_up+pm_down <= effective pair_sum_max (legacy).
    # If False (default): 2nd leg is gated on held/post-fill avg via paladin_max_blended_pair_avg_sum + ROI, not live mid sum.
    paladin_stagger_second_leg_require_live_mid_pair_sum: bool = False
    # Target ~97c blended pair cost: block fills that would push avg_up+avg_down above this.
    paladin_max_blended_pair_avg_sum: float | None = 0.97
    # If True, new stagger first leg (when already holding UP or DOWN) only on higher-mid side.
    paladin_stagger_winning_side_first_when_position: bool = False
    # When win-side stagger blocks and inventory is balanced, add symmetric pair if ROI/sum gates pass.
    paladin_stagger_symmetric_fallback_when_balanced: bool = True
    paladin_stagger_symmetric_fallback_roi_discount: float = 0.03
    # False: symmetric fallback first leg also respects max blended avg (disciplined inventory).
    paladin_stagger_symmetric_fallback_skip_first_leg_blend_cap: bool = False
    # Causal ladder: alternate UP/DN first leg when balanced; pace time between completed pairs; optional trailing dip filter.
    # PALADIN v4 ladder pacing (default: gap=100s after 2nd leg, no trailing, slip=0.02; hedge force 90s variant E).
    paladin_stagger_alternate_first_leg_when_balanced: bool = True
    paladin_min_elapsed_between_pair_starts: float | None = 100.0
    paladin_entry_trailing_min_low_seconds: int | None = None
    paladin_entry_trailing_low_slippage: float = 0.02
    # PALADIN v7 live (Binance volume spike + BTC impulse → PM legs; small-budget preset by default)
    paladin_v7_volume_lookback_sec: int = 60
    paladin_v7_volume_spike_ratio: float = 2.5
    paladin_v7_volume_floor: float = 1e-6
    paladin_v7_btc_abs_move_min_usd: float = 2.0
    paladin_v7_first_leg_max_pm: float = 0.62
    # Balanced re-entry spike buys are ignored outside this PM band.
    paladin_v7_balanced_entry_min_pm: float = 0.20
    paladin_v7_balanced_entry_max_pm: float = 0.80
    # Extra price buffer for marketable BTC-spike entries so they cross reliably when the book moves fast.
    paladin_v7_spike_market_price_buffer: float = 0.02
    paladin_v7_cheap_other_margin: float = 0.04
    paladin_v7_cheap_pair_sum_max: float = 0.99
    # Max *our* pair cost: cheap hedge held VWAP + opposite + slip (not raw pm_u+pm_d).
    paladin_v7_cheap_pair_avg_sum_nonforced_max: float = 0.96
    # Hedge cheap-gate uses opposite_mid + this buffer vs cap (FAK VWAP often > mid).
    paladin_v7_cheap_hedge_slip_buffer: float = 0.012
    # Extra PM discount added to slip in cheap-hedge limit math (sim + live resting clamp).
    paladin_v7_hedge_slip_addon_pm: float = 0.10
    # Seconds after first leg before a *cheap* hedge may execute (0 = immediate when gate passes).
    paladin_v7_cheap_hedge_min_delay_sec: float = 0.0
    paladin_v7_hedge_timeout_seconds: float = 90.0
    paladin_v7_forced_hedge_max_book_sum: float = 1.30
    # Legacy layer-entry cooldown kept on config; spike-only mode no longer uses a non-spike layer path.
    paladin_v7_layer2_cooldown_sec: float = 5.0
    # After each completed pair: min wait before the next BTC-spike entry when the book is balanced.
    paladin_v7_pair_cooldown_sec: float = 5.0
    # First leg, layer-2 dip add, and hedge clip (BOT_PALADIN_V7_BASE_ORDER_SHARES; legacy BOT_PALADIN_V7_CLIP_SHARES).
    paladin_v7_base_order_shares: float = 5.0
    paladin_v7_max_shares_per_side: float = 25.0
    # Legacy higher-VWAP dip threshold kept on config; spike-only mode no longer uses it for entries.
    paladin_v7_layer2_dip_below_avg: float = 0.05
    # Hedge-price cap starts at 1 - this deduction, then tightens by layer_level_offset_step per layer.
    paladin_v7_cheap_balance_start_deduction: float = 0.08
    # Legacy layer tightening knob kept on config; spike-only mode no longer uses it for entries.
    paladin_v7_layer_level_offset_step: float = 0.01
    # Legacy lower-VWAP deep-dip threshold kept on config; spike-only mode no longer uses it for entries.
    paladin_v7_layer2_low_vwap_dip_below_avg: float = 0.20
    # No new-risk entries in the last N seconds of the window (flat and balanced).
    paladin_v7_no_new_layers_last_seconds: float = 60.0
    # Balanced PM-lead layer: lead mid must be <= that leg VWAP minus this (PM dollars; sweep best 0.10).
    paladin_v7_balanced_layer_below_avg_pm: float = 0.10
    # |up−down| <= this (shares) counts as balanced for spike re-entry checks (default 1.0).
    paladin_v7_balance_share_tolerance: float = 1.0
    # Imbalance repair: buy lighter side when pm_light + VWAP(heavy) < this (default 0.97).
    paladin_v7_imbalance_repair_max_pair_sum: float = 0.97
    paladin_v7_min_notional: float = 1.0
    paladin_v7_min_shares: float = 5.0
    paladin_v7_limit_order_cancel_seconds: float = 5.0
    # Live: poll CLOB conditional balances vs SimState; debounce to tolerate API delay.
    paladin_v7_reconcile_enabled: bool = True
    paladin_v7_reconcile_interval_seconds: float = 5.0
    paladin_v7_reconcile_share_tolerance: float = 0.35
    paladin_v7_reconcile_confirm_reads: int = 2
    # Extra safety: when the model says "balanced" but API keeps reporting imbalance, trust API only after
    # repeated stable reads so one stale allowance response cannot trigger unnecessary hedge churn.
    paladin_v7_api_reality_confirm_reads: int = 5
    paladin_v7_api_reality_confirm_interval_seconds: float = 2.0
    paladin_v7_reconcile_flatten: bool = True
    paladin_v7_reconcile_flatten_min_imbalance: float = 0.25
    paladin_v7_reconcile_flatten_cooldown_seconds: float = 10.0
    # SHAMAN v1: Binance 5m/15m candle-close pattern rules -> Polymarket UP/DOWN FAK
    shaman_v1_rules_path: str = ""
    shaman_v1_kline_limit: int = 500
    shaman_v1_price_pad: float = 0.03
    # SHAMAN v1: each rule on the winning side (nG or nR at bar close) adds this much USDC to clip notional
    # when **two or more** rules agree (2×$1, 8×$8, …). Single-rule clips use ``shaman_v1_usdc_single_signal``.
    shaman_v1_usdc_per_signal: float = 1.0
    shaman_v1_usdc_single_signal: float = 1.25
    # Hard cap on total clip notional (many rules can fire on one bar).
    shaman_v1_notional_max_usdc: float = 500.0
    shaman_v1_min_shares: int = 1
    shaman_v1_min_notional_usdc: float = 1.0

    @property
    def window_size_seconds(self) -> int:
        return self.window_minutes * 60

    @property
    def market_slug_prefix(self) -> str:
        return f"{self.market_symbol.lower()}-updown-{self.window_minutes}m"

    @property
    def ladder_complements(self) -> list[float]:
        """True complement prices: 1.00 - cheap price."""
        return [round(1.0 - p, 2) for p in self.ladder_prices]

    @property
    def ladder_hedge_prices(self) -> list[float]:
        """Hedge prices: 1.00 - cheap - offset (always profitable)."""
        return [self.hedge_price_for(p) for p in self.ladder_prices]

    def hedge_price_for(self, cheap_price: float) -> float:
        """Calculate hedge price that guarantees profit.
        
        e.g. cheap=$0.44, offset=$0.02 → hedge=$0.54
             pair cost = $0.44 + $0.54 = $0.98 < $1.00 → +$0.02/sh
        """
        return round(1.0 - cheap_price - self.hedge_offset, 2)

    @classmethod
    def from_env(cls) -> "BotConfig":
        private_key = _strip_env_copy_artifacts(os.getenv("POLY_PRIVATE_KEY") or "")
        funder_raw = os.getenv("POLY_FUNDER") or ""
        if not private_key:
            raise BotConfigError("POLY_PRIVATE_KEY is required.")
        if not funder_raw.strip():
            raise BotConfigError("POLY_FUNDER is required.")
        funder = _normalize_polymarket_funder(funder_raw)

        raw_prices = os.getenv("BOT_LADDER_PRICES", "")
        if raw_prices.strip():
            ladder_prices = [float(p.strip()) for p in raw_prices.split(",")]
        else:
            ladder_prices = [0.44, 0.34, 0.24, 0.14]

        volume_scalp_tp_raw = _env_float("BOT_VOLUME_SCALP_TP_OFFSET", 0.12)
        if volume_scalp_tp_raw > 1.0:
            volume_scalp_tp_raw = volume_scalp_tp_raw / 100.0
        raw_perp15_ladder = os.getenv("BOT_PERP15_LADDER_PRICES", "").strip()
        if raw_perp15_ladder:
            perp15_ladder = sorted({float(p.strip()) for p in raw_perp15_ladder.split(",") if p.strip()}, reverse=True)
        else:
            perp15_ladder = [0.44, 0.43, 0.40]

        raw_mode = _normalize_strategy_mode(os.getenv("BOT_STRATEGY_MODE", "shaman_v1"))
        default_strategy_budget = (
            400.0
            if raw_mode == "paladin_v9"
            else (
                10.0
                if raw_mode == "paladin_v7"
                else (
                    5.0
                    if raw_mode == "first_cheap_03"
                    else (30.0 if raw_mode == "shaman_v1" else 80.0)
                )
            )
        )

        cfg = cls(
            private_key=private_key.strip(),
            funder=funder,
            bot_version=os.getenv("BOT_VERSION", "paladin-v9-kng3-2026-04-25").strip(),
            signature_type=_env_int("POLY_SIGNATURE_TYPE", 1),
            relayer_api_key=os.getenv("RELAYER_API_KEY", ""),
            relayer_secret=os.getenv("RELAYER_SECRET", ""),
            relayer_passphrase=os.getenv("RELAYER_PASSPHRASE", ""),
            dry_run=_env_bool("POLY_DRY_RUN", True),
            poll_interval_seconds=_env_float("BOT_POLL_INTERVAL_SECONDS", 1.0),
            request_timeout_seconds=_env_float("BOT_REQUEST_TIMEOUT_SECONDS", 10.0),
            log_level=os.getenv("BOT_LOG_LEVEL", "INFO").upper(),
            force_exit_before_end_seconds=_env_int("BOT_FORCE_EXIT_BEFORE_END_SECONDS", 15),
            shares_per_level=max(1, _env_int("BOT_SHARES_PER_LEVEL", 5)),
            ladder_prices=ladder_prices,
            order_cooldown_seconds=_env_float("BOT_ORDER_COOLDOWN_SECONDS", 3.0),
            hedge_offset=_env_float("BOT_HEDGE_OFFSET", 0.02),
            market_symbol=os.getenv("BOT_MARKET_SYMBOL", "BTC").upper(),
            window_minutes=_env_int("BOT_WINDOW_MINUTES", 15),
            window_pick_current_grace_seconds=_env_int("BOT_WINDOW_PICK_CURRENT_GRACE_SECONDS", 300),
            trade_one_window=_env_bool("BOT_TRADE_ONE_WINDOW", False),
            strategy_budget_cap_usdc=_env_float("BOT_STRATEGY_BUDGET_CAP_USDC", default_strategy_budget),
            strategy_wallet_reserve_usdc=_env_float("BOT_STRATEGY_WALLET_RESERVE_USDC", 0.0),
            strategy_min_budget_usdc=_env_float("BOT_STRATEGY_MIN_BUDGET_USDC", 15.0),
            strategy_entry_delay_seconds=max(0, _env_int("BOT_STRATEGY_ENTRY_DELAY_SECONDS", 0)),
            strategy_new_order_cutoff_seconds=_env_int("BOT_STRATEGY_NEW_ORDER_CUTOFF_SECONDS", 30),
            strategy_fill_grace_seconds=_env_float("BOT_STRATEGY_FILL_GRACE_SECONDS", 5.0),
            strategy_stale_order_seconds=_env_float("BOT_STRATEGY_STALE_ORDER_SECONDS", 8.0),
            strategy_max_live_orders=_env_int("BOT_STRATEGY_MAX_LIVE_ORDERS", 4),
            strategy_heartbeat_interval_seconds=_env_int("BOT_STRATEGY_HEARTBEAT_INTERVAL_SECONDS", 15),
            strategy_price_record_interval_seconds=_env_float("BOT_STRATEGY_PRICE_RECORD_INTERVAL_SECONDS", 1.0),
            strategy_price_buffer=_env_float("BOT_STRATEGY_PRICE_BUFFER", 0.02),
            strategy_primary_flip_threshold=_env_float("BOT_STRATEGY_PRIMARY_FLIP_THRESHOLD", 0.05),
            strategy_max_reversals=_env_int("BOT_STRATEGY_MAX_REVERSALS", 1),
            strategy_min_stop_orders_per_side=_env_int("BOT_STRATEGY_MIN_STOP_ORDERS_PER_SIDE", 5),
            strategy_primary_unlock_seconds=_env_int("BOT_STRATEGY_PRIMARY_UNLOCK_SECONDS", 90),
            strategy_primary_lock_seconds=_env_int("BOT_STRATEGY_PRIMARY_LOCK_SECONDS", 720),
            strategy_pair_soft_limit=_env_float("BOT_STRATEGY_PAIR_SOFT_LIMIT", 1.03),
            strategy_pair_hard_limit=_env_float("BOT_STRATEGY_PAIR_HARD_LIMIT", 1.06),
            strategy_late_stop_seconds=_env_int("BOT_STRATEGY_LATE_STOP_SECONDS", 780),
            strategy_late_stop_worst_case_usdc=_env_float("BOT_STRATEGY_LATE_STOP_WORST_CASE_USDC", 1.5),
            strategy_balance_retry_seconds=_env_int("BOT_STRATEGY_BALANCE_RETRY_SECONDS", 10),
            strategy_balance_retry_attempts=_env_int("BOT_STRATEGY_BALANCE_RETRY_ATTEMPTS", 3),
            btc_feed_enabled=_env_bool("BOT_BTC_FEED_ENABLED", True),
            btc_feed_poll_seconds=_env_float("BOT_BTC_FEED_POLL_SECONDS", 1.0),
            btc_feed_symbol=os.getenv("BOT_BTC_FEED_SYMBOL", "BTCUSDT").upper(),
            signal_preset=os.getenv("BOT_SIGNAL_PRESET", "w1").strip().lower(),
            strategy_mode=raw_mode,
            volume_scalp_tp_offset=volume_scalp_tp_raw,
            volume_scalp_stop_offset=_env_float("BOT_VOLUME_SCALP_STOP_OFFSET", 0.05),
            volume_scalp_shares=max(1, _env_int("BOT_VOLUME_SCALP_SHARES", 6)),
            volume_scalp_max_orders_per_side=max(1, _env_int("BOT_VOLUME_SCALP_MAX_ORDERS_PER_SIDE", 3)),
            volume_scalp_entry_min_elapsed=max(0, _env_int("BOT_VOLUME_SCALP_ENTRY_MIN_ELAPSED", 60)),
            volume_scalp_entry_max_elapsed=max(1, _env_int("BOT_VOLUME_SCALP_ENTRY_MAX_ELAPSED", 840)),
            volume_scalp_time_exit_seconds_remaining=max(1, _env_int("BOT_VOLUME_SCALP_TIME_EXIT_SECONDS_REMAINING", 60)),
            volume_scalp_volume_ratio=_env_float("BOT_VOLUME_SCALP_VOLUME_RATIO", 2.5),
            btc_perp15_monitor_seconds=max(30, _env_int("BOT_PERP15_MONITOR_SECONDS", 120)),
            btc_perp15_btc_trend_threshold=_env_float("BOT_PERP15_BTC_TREND_THRESHOLD", 0.0005),
            btc_perp15_entry_window_seconds=max(60, _env_int("BOT_PERP15_ENTRY_WINDOW_SECONDS", 420)),
            btc_perp15_ladder_prices=perp15_ladder,
            btc_perp15_min_shares=max(1, _env_int("BOT_PERP15_MIN_SHARES", 6)),
            btc_perp15_risk_pct=_env_float("BOT_PERP15_RISK_PCT", 0.10),
            btc_perp15_tp_price=_env_float("BOT_PERP15_TP_PRICE", 0.99),
            btc_perp15_sample_interval_seconds=_env_float("BOT_PERP15_SAMPLE_INTERVAL_SECONDS", 5.0),
            btc_perp15_end_dump_seconds_remaining=max(1.0, _env_float("BOT_PERP15_END_DUMP_SECONDS_REMAINING", 15.0)),
            polymarket_ws_enabled=_env_bool("BOT_POLY_WS_ENABLED", True),
            polymarket_ws_url=os.getenv(
                "BOT_POLY_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ).strip(),
            polymarket_fak_confirm_get_order=_env_bool("BOT_POLY_FAK_CONFIRM_ORDER", True),
            paladin_pair_sum_max=_env_float("BOT_PALADIN_PAIR_SUM_MAX", 0.97),
            paladin_pair_sum_max_on_forced_hedge=(
                None
                if _env_float("BOT_PALADIN_PAIR_SUM_MAX_ON_FORCE", 1.0) <= 0
                else min(1.0, _env_float("BOT_PALADIN_PAIR_SUM_MAX_ON_FORCE", 1.0))
            ),
            paladin_target_min_roi=_env_float("BOT_PALADIN_TARGET_MIN_ROI", 0.0),
            paladin_heartbeat_seconds=max(5.0, _env_float("BOT_PALADIN_HEARTBEAT_SEC", 15.0)),
            paladin_stagger_pair=_env_bool("BOT_PALADIN_STAGGER_PAIR", True),
            paladin_first_leg_max_px=_env_float("BOT_PALADIN_FIRST_LEG_MAX_PX", 0.55),
            paladin_stagger_hedge_force_after_seconds=(
                None
                if _env_float("BOT_PALADIN_STAGGER_HEDGE_FORCE_SEC", 90.0) <= 0
                else _env_float("BOT_PALADIN_STAGGER_HEDGE_FORCE_SEC", 90.0)
            ),
            paladin_max_shares_per_side=(
                None
                if _env_float("BOT_PALADIN_MAX_SHARES_PER_SIDE", 10.0) <= 0
                else _env_float("BOT_PALADIN_MAX_SHARES_PER_SIDE", 10.0)
            ),
            paladin_cooldown_seconds=max(0.0, _env_float("BOT_PALADIN_COOLDOWN_SEC", 0.0)),
            paladin_dynamic_clip_cap=max(5.0, _env_float("BOT_PALADIN_DYNAMIC_CLIP_CAP", 12.0)),
            paladin_pair_sum_tighten_per_fill=max(
                0.0, _env_float("BOT_PALADIN_PAIR_SUM_TIGHTEN_PER_FILL", 0.0)
            ),
            paladin_pair_sum_min_floor=max(
                0.80, min(0.999, _env_float("BOT_PALADIN_PAIR_SUM_MIN_FLOOR", 0.90))
            ),
            paladin_pending_hedge_bypass_imbalance_shares=(
                None
                if _env_float("BOT_PALADIN_PENDING_HEDGE_BYPASS_IMBALANCE_SH", 10.0) <= 0
                else _env_float("BOT_PALADIN_PENDING_HEDGE_BYPASS_IMBALANCE_SH", 10.0)
            ),
            paladin_discipline_relax_after_forced_sec=(
                None
                if _env_float("BOT_PALADIN_DISCIPLINE_RELAX_AFTER_FORCE_SEC", 60.0) <= 0
                else _env_float("BOT_PALADIN_DISCIPLINE_RELAX_AFTER_FORCE_SEC", 60.0)
            ),
            paladin_second_leg_book_improve_eps=max(
                0.0, _env_float("BOT_PALADIN_SECOND_LEG_BOOK_IMPROVE_EPS", 0.013)
            ),
            paladin_stagger_second_leg_require_live_mid_pair_sum=_env_bool(
                "BOT_PALADIN_STAGGER_SECOND_LEG_REQUIRE_LIVE_MID_PAIR_SUM", False
            ),
            paladin_max_blended_pair_avg_sum=(
                None
                if _env_float("BOT_PALADIN_MAX_BLENDED_PAIR_AVG_SUM", 0.97) <= 0
                else _env_float("BOT_PALADIN_MAX_BLENDED_PAIR_AVG_SUM", 0.97)
            ),
            paladin_stagger_winning_side_first_when_position=_env_bool(
                "BOT_PALADIN_STAGGER_WINNING_SIDE_FIRST_WHEN_POSITION", False
            ),
            paladin_stagger_symmetric_fallback_when_balanced=_env_bool(
                "BOT_PALADIN_STAGGER_SYMMETRIC_FALLBACK_WHEN_BALANCED", True
            ),
            paladin_stagger_symmetric_fallback_roi_discount=_env_float(
                "BOT_PALADIN_STAGGER_SYMMETRIC_FALLBACK_ROI_DISCOUNT", 0.03
            ),
            paladin_stagger_symmetric_fallback_skip_first_leg_blend_cap=_env_bool(
                "BOT_PALADIN_STAGGER_SYMMETRIC_FALLBACK_SKIP_FIRST_BLEND", False
            ),
            paladin_stagger_alternate_first_leg_when_balanced=_env_bool(
                "BOT_PALADIN_STAGGER_ALTERNATE_FIRST_WHEN_BALANCED", True
            ),
            paladin_min_elapsed_between_pair_starts=(
                None
                if _env_float("BOT_PALADIN_MIN_ELAPSED_BETWEEN_PAIR_STARTS", 100.0) < 0
                else _env_float("BOT_PALADIN_MIN_ELAPSED_BETWEEN_PAIR_STARTS", 100.0)
            ),
            paladin_entry_trailing_min_low_seconds=(
                None
                if _env_int("BOT_PALADIN_ENTRY_TRAILING_MIN_LOW_SEC", -1) < 0
                else _env_int("BOT_PALADIN_ENTRY_TRAILING_MIN_LOW_SEC", -1)
            ),
            paladin_entry_trailing_low_slippage=_env_float(
                "BOT_PALADIN_ENTRY_TRAILING_LOW_SLIPPAGE", 0.02
            ),
            paladin_v7_volume_lookback_sec=max(5, _env_int("BOT_PALADIN_V7_VOL_LOOKBACK_SEC", 60)),
            paladin_v7_volume_spike_ratio=max(1.01, _env_float("BOT_PALADIN_V7_VOL_SPIKE_RATIO", 2.5)),
            paladin_v7_volume_floor=max(0.0, _env_float("BOT_PALADIN_V7_VOL_FLOOR", 1e-6)),
            paladin_v7_btc_abs_move_min_usd=max(0.0, _env_float("BOT_PALADIN_V7_BTC_MOVE_MIN_USD", 2.0)),
            paladin_v7_first_leg_max_pm=min(0.99, max(0.01, _env_float("BOT_PALADIN_V7_FIRST_LEG_MAX_PM", 0.62))),
            paladin_v7_balanced_entry_min_pm=min(
                0.99, max(0.01, _env_float("BOT_PALADIN_V7_BALANCED_ENTRY_MIN_PM", 0.20))
            ),
            paladin_v7_balanced_entry_max_pm=min(
                0.99, max(0.01, _env_float("BOT_PALADIN_V7_BALANCED_ENTRY_MAX_PM", 0.80))
            ),
            paladin_v7_spike_market_price_buffer=max(
                0.0, min(0.05, _env_float("BOT_PALADIN_V7_SPIKE_MARKET_PRICE_BUFFER", 0.02))
            ),
            paladin_v7_cheap_other_margin=max(0.0, _env_float("BOT_PALADIN_V7_CHEAP_OTHER_MARGIN", 0.04)),
            paladin_v7_cheap_pair_sum_max=min(1.0, _env_float("BOT_PALADIN_V7_CHEAP_PAIR_SUM_MAX", 0.99)),
            paladin_v7_cheap_pair_avg_sum_nonforced_max=min(
                0.99,
                max(0.85, _env_float("BOT_PALADIN_V7_CHEAP_PAIR_AVG_SUM_NONFORCED_MAX", 0.96)),
            ),
            paladin_v7_cheap_hedge_slip_buffer=max(
                0.0, min(0.05, _env_float("BOT_PALADIN_V7_CHEAP_HEDGE_SLIP_BUFFER", 0.012))
            ),
            paladin_v7_hedge_slip_addon_pm=max(
                0.0, min(0.15, _env_float("BOT_PALADIN_V7_HEDGE_SLIP_ADDON_PM", 0.10))
            ),
            paladin_v7_cheap_hedge_min_delay_sec=max(
                0.0, _env_float("BOT_PALADIN_V7_CHEAP_HEDGE_MIN_DELAY_SEC", 0.0)
            ),
            paladin_v7_hedge_timeout_seconds=max(1.0, _env_float("BOT_PALADIN_V7_HEDGE_TIMEOUT_SEC", 90.0)),
            paladin_v7_forced_hedge_max_book_sum=min(
                1.50, max(1.0, _env_float("BOT_PALADIN_V7_FORCED_HEDGE_SUM_MAX", 1.30))
            ),
            paladin_v7_layer2_cooldown_sec=max(
                5.0, min(300.0, _env_float("BOT_PALADIN_V7_LAYER2_COOLDOWN_SEC", 5.0))
            ),
            paladin_v7_pair_cooldown_sec=max(
                5.0, min(300.0, _env_float("BOT_PALADIN_V7_PAIR_COOLDOWN_SEC", 5.0))
            ),
            paladin_v7_base_order_shares=(
                max(1.0, _env_float("BOT_PALADIN_V7_BASE_ORDER_SHARES", 5.0))
                if (os.getenv("BOT_PALADIN_V7_BASE_ORDER_SHARES") or "").strip()
                else max(1.0, _env_float("BOT_PALADIN_V7_CLIP_SHARES", 5.0))
            ),
            paladin_v7_max_shares_per_side=max(1.0, _env_float("BOT_PALADIN_V7_MAX_SHARES_PER_SIDE", 25.0)),
            paladin_v7_layer2_dip_below_avg=max(
                0.0, min(0.5, _env_float("BOT_PALADIN_V7_LAYER2_DIP_BELOW_AVG", 0.05))
            ),
            paladin_v7_cheap_balance_start_deduction=max(
                0.0, min(0.5, _env_float("BOT_PALADIN_V7_CHEAP_BALANCE_START_DEDUCTION", 0.08))
            ),
            paladin_v7_layer_level_offset_step=max(
                0.0, min(0.1, _env_float("BOT_PALADIN_V7_LAYER_LEVEL_OFFSET_STEP", 0.01))
            ),
            paladin_v7_layer2_low_vwap_dip_below_avg=max(
                0.0, min(0.95, _env_float("BOT_PALADIN_V7_LAYER2_LOW_VWAP_DIP_BELOW_AVG", 0.20))
            ),
            paladin_v7_no_new_layers_last_seconds=max(
                0.0, min(300.0, _env_float("BOT_PALADIN_V7_NO_NEW_LAYERS_LAST_SEC", 60.0))
            ),
            paladin_v7_balanced_layer_below_avg_pm=max(
                0.0, min(0.25, _env_float("BOT_PALADIN_V7_BALANCED_LAYER_BELOW_AVG_PM", 0.10))
            ),
            paladin_v7_balance_share_tolerance=max(
                0.0, min(50.0, _env_float("BOT_PALADIN_V7_BALANCE_SHARE_TOLERANCE", 1.0))
            ),
            paladin_v7_imbalance_repair_max_pair_sum=max(
                0.5, min(1.0, _env_float("BOT_PALADIN_V7_IMBALANCE_REPAIR_MAX_PAIR_SUM", 0.97))
            ),
            paladin_v7_min_notional=max(0.01, _env_float("BOT_PALADIN_V7_MIN_NOTIONAL", 1.0)),
            paladin_v7_min_shares=max(1.0, _env_float("BOT_PALADIN_V7_MIN_SHARES", 5.0)),
            paladin_v7_limit_order_cancel_seconds=max(
                1.0, _env_float("BOT_PALADIN_V7_LIMIT_ORDER_CANCEL_SEC", 5.0)
            ),
            paladin_v7_reconcile_enabled=_env_bool("BOT_PALADIN_V7_RECONCILE_ENABLED", True),
            paladin_v7_reconcile_interval_seconds=max(
                2.0, _env_float("BOT_PALADIN_V7_RECONCILE_INTERVAL_SEC", 5.0)
            ),
            paladin_v7_reconcile_share_tolerance=max(
                0.05, _env_float("BOT_PALADIN_V7_RECONCILE_SHARE_TOL", 0.35)
            ),
            paladin_v7_reconcile_confirm_reads=max(1, _env_int("BOT_PALADIN_V7_RECONCILE_CONFIRM_READS", 2)),
            paladin_v7_api_reality_confirm_reads=max(
                1, _env_int("BOT_PALADIN_V7_API_REALITY_CONFIRM_READS", 5)
            ),
            paladin_v7_api_reality_confirm_interval_seconds=max(
                0.5, _env_float("BOT_PALADIN_V7_API_REALITY_CONFIRM_INTERVAL_SEC", 2.0)
            ),
            paladin_v7_reconcile_flatten=_env_bool("BOT_PALADIN_V7_RECONCILE_FLATTEN", True),
            paladin_v7_reconcile_flatten_min_imbalance=max(
                0.05, _env_float("BOT_PALADIN_V7_RECONCILE_FLATTEN_MIN_IMB", 0.25)
            ),
            paladin_v7_reconcile_flatten_cooldown_seconds=max(
                2.0, _env_float("BOT_PALADIN_V7_RECONCILE_FLATTEN_COOLDOWN_SEC", 10.0)
            ),
            shaman_v1_rules_path=os.getenv("BOT_SHAMAN_V1_RULES_PATH", "").strip(),
            # Match research depth: body/range token buckets use percentiles over the full window.
            # 500 bars was far short of 28d backtests (~8k bars) and drove nG=nR=0 "TIE" despite good candles.
            shaman_v1_kline_limit=max(120, min(10_000, _env_int("BOT_SHAMAN_V1_KLINE_LIMIT", 2500))),
            shaman_v1_price_pad=max(0.0, _env_float("BOT_SHAMAN_V1_PRICE_PAD", 0.03)),
            shaman_v1_usdc_per_signal=max(
                0.5, _env_float("BOT_SHAMAN_V1_USDC_PER_SIGNAL", 1.0)
            ),
            shaman_v1_usdc_single_signal=max(
                0.5, _env_float("BOT_SHAMAN_V1_USDC_SINGLE_SIGNAL", 1.25)
            ),
            shaman_v1_notional_max_usdc=max(
                1.0, _env_float("BOT_SHAMAN_V1_NOTIONAL_MAX", 500.0)
            ),
            shaman_v1_min_shares=max(1, _env_int("BOT_SHAMAN_V1_MIN_SHARES", 1)),
            shaman_v1_min_notional_usdc=max(0.5, _env_float("BOT_SHAMAN_V1_MIN_NOTIONAL_USDC", 1.0)),
        )
        if cfg.strategy_mode in ("paladin_v7", "paladin_v9") and (
            cfg.strategy_budget_cap_usdc + 1e-9 < cfg.strategy_min_budget_usdc
        ):
            raise BotConfigError(
                f"BOT_STRATEGY_BUDGET_CAP_USDC ({cfg.strategy_budget_cap_usdc}) must be >= "
                f"BOT_STRATEGY_MIN_BUDGET_USDC ({cfg.strategy_min_budget_usdc}) for strategy_mode="
                f"{cfg.strategy_mode!r}"
            )
        return cfg


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TokenMarket:
    market_id: str
    condition_id: str
    slug: str
    question: str
    token_id: str
    outcome: str
    end_time: datetime
    enable_order_book: bool
    minimum_tick_size: str | None = None
    neg_risk: bool | None = None


@dataclass(slots=True)
class ActiveContract:
    market_id: str
    slug: str
    question: str
    condition_id: str
    end_time: datetime
    up: TokenMarket
    down: TokenMarket
    raw_market: dict[str, Any] = field(repr=False)


# ---------------------------------------------------------------------------
# Ladder level: the core state machine
# ---------------------------------------------------------------------------
@dataclass
class LadderLevel:
    """
    One price level of the ladder.

    Each level has cheap_price (e.g. $0.44) and complement (hedge price, e.g. $0.54).
    
    State machine per level:
    
    IDLE → place cheap UP + cheap DOWN
    
    When DOWN cheap fills:
      → place hedge UP@complement ($0.54)
      → state = DOWN_FILLED_HEDGED
      → waiting for UP cheap ($0.44) OR UP hedge ($0.54)
        - UP cheap fills → PROFIT pair ($0.44+$0.44=$0.88, +$0.12/sh) → cancel hedge → COMPLETE
        - UP hedge fills → HEDGED pair ($0.44+$0.54=$0.98, +$0.02/sh) → cancel cheap → COMPLETE
    
    When UP cheap fills:
      → place hedge DOWN@complement ($0.54)
      → state = UP_FILLED_HEDGED
      → waiting for DOWN cheap ($0.44) OR DOWN hedge ($0.54)
        - DOWN cheap fills → PROFIT pair → cancel hedge → COMPLETE
        - DOWN hedge fills → HEDGED pair → cancel cheap → COMPLETE
    
    When BOTH cheap fill simultaneously:
      → PROFIT pair → COMPLETE (no hedge needed)
    
    COMPLETE → reload (back to IDLE)
    
    ALL outcomes are profitable. No breakeven case exists.
    """
    price: float          # cheap price, e.g. 0.44
    complement: float     # hedge price = 1.0 - price - offset, e.g. 0.54
    shares: int

    # State
    state: str = "IDLE"
    # States: IDLE, PLACING, ACTIVE, 
    #         UP_FILLED, DOWN_FILLED,
    #         UP_FILLED_HEDGED, DOWN_FILLED_HEDGED,
    #         COMPLETE

    # Order tracking — cheap side
    up_cheap_order_id: str | None = None
    up_cheap_filled: bool = False
    up_cheap_fill_price: float = 0.0

    down_cheap_order_id: str | None = None
    down_cheap_filled: bool = False
    down_cheap_fill_price: float = 0.0

    # Order tracking — hedge side
    up_hedge_order_id: str | None = None
    up_hedge_filled: bool = False
    up_hedge_fill_price: float = 0.0

    down_hedge_order_id: str | None = None
    down_hedge_filled: bool = False
    down_hedge_fill_price: float = 0.0

    # Result
    pair_cost: float = 0.0
    pair_profit: float = 0.0
    completions: int = 0  # how many times this level completed in window

    def reset(self) -> None:
        """Reset to IDLE for reload."""
        self.state = "IDLE"
        self.up_cheap_order_id = None
        self.up_cheap_filled = False
        self.up_cheap_fill_price = 0.0
        self.down_cheap_order_id = None
        self.down_cheap_filled = False
        self.down_cheap_fill_price = 0.0
        self.up_hedge_order_id = None
        self.up_hedge_filled = False
        self.up_hedge_fill_price = 0.0
        self.down_hedge_order_id = None
        self.down_hedge_filled = False
        self.down_hedge_fill_price = 0.0
        self.pair_cost = 0.0
        self.pair_profit = 0.0

    def get_all_live_order_ids(self) -> list[str]:
        """Return all non-None order IDs for this level."""
        ids = []
        for oid in (
            self.up_cheap_order_id,
            self.down_cheap_order_id,
            self.up_hedge_order_id,
            self.down_hedge_order_id,
        ):
            if oid:
                ids.append(oid)
        return ids

    def __repr__(self) -> str:
        return (
            f"Level(${self.price}/${self.complement} "
            f"state={self.state} "
            f"up_c={'✓' if self.up_cheap_filled else ('⏳' if self.up_cheap_order_id else '·')} "
            f"dn_c={'✓' if self.down_cheap_filled else ('⏳' if self.down_cheap_order_id else '·')} "
            f"up_h={'✓' if self.up_hedge_filled else ('⏳' if self.up_hedge_order_id else '·')} "
            f"dn_h={'✓' if self.down_hedge_filled else ('⏳' if self.down_hedge_order_id else '·')} "
            f"done={self.completions})"
        )


@dataclass
class WindowStats:
    """Stats for one 5-minute window."""
    slug: str = ""
    pairs_completed: int = 0
    profit_pairs: int = 0
    hedged_pairs: int = 0      # was breakeven_pairs — now always profitable
    total_profit: float = 0.0
    total_orders_placed: int = 0
    total_fills: int = 0
    total_cancels: int = 0


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def parse_jsonish_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [p.strip() for p in text.split(",") if p.strip()]
    return [value]


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_datetime(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def parse_balance_response(response: Any, decimals: int = 6) -> float:
    if isinstance(response, dict):
        raw = response.get("balance")
    else:
        raw = response
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1_000_000:
            return val / (10 ** decimals)
        return val
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return 0.0
        if cleaned.isdigit():
            return int(cleaned) / (10 ** decimals)
        if "." in cleaned:
            try:
                val = float(cleaned)
                return val / (10 ** decimals) if val > 1_000_000 else val
            except ValueError:
                return 0.0
    return 0.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be a float") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BotConfigError(f"{name} must be an int") from exc


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("polymarket_btc_ladder")
    logger.setLevel(getattr(logging, level, logging.INFO))
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s - %(message)s"
    ))
    logger.addHandler(console)
    return logger


def setup_file_logger(window_slug: str) -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = logs_dir / f"{ts}_{window_slug}.log"

    logger = logging.getLogger("polymarket_btc_ladder")
    for h in logger.handlers[:]:
        if isinstance(h, logging.FileHandler):
            h.close()
            logger.removeHandler(h)

    fh = logging.FileHandler(filepath)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logger.addHandler(fh)
    LOGGER.info("Log file: %s", filepath.absolute())


def append_window_balance_snapshot(
    *,
    fetched_at: datetime,
    log_file: str,
    slug: str,
    question: str,
    ends_at: str,
    wallet_usdc: float,
    budget_usdc: float,
    baseline_up: float,
    baseline_down: float,
    dry_run: bool,
    source: str = "live_window_start",
) -> Path:
    outdir = Path("exports")
    outdir.mkdir(exist_ok=True)
    path = outdir / "window_balance_snapshots.csv"
    row = {
        "fetched_at": fetched_at.isoformat(sep=" ", timespec="seconds"),
        "log_file": log_file,
        "slug": slug,
        "question": question,
        "ends_at": ends_at,
        "wallet_usdc": round(wallet_usdc, 4),
        "budget_usdc": round(budget_usdc, 4),
        "baseline_up": round(baseline_up, 4),
        "baseline_down": round(baseline_down, 4),
        "dry_run": str(dry_run).lower(),
        "source": source,
    }
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return path


def prepare_window_price_snapshot_file(*, log_file: str, slug: str) -> Path:
    outdir = Path("exports") / "window_price_snapshots"
    outdir.mkdir(parents=True, exist_ok=True)
    if log_file:
        basename = Path(log_file).stem
    else:
        basename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}"
    path = outdir / f"{basename}_prices.csv"
    if not path.exists() or path.stat().st_size == 0:
        row = {
            "recorded_at": "",
            "slug": "",
            "question": "",
            "elapsed_sec": "",
            "remaining_sec": "",
            "up_price": "",
            "down_price": "",
            "primary_side": "",
            "total_spend_usdc": "",
            "shares_up": "",
            "shares_down": "",
            "avg_up": "",
            "avg_down": "",
            "pair_sum": "",
            "dry_run": "",
        }
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writeheader()
    return path


def append_window_price_snapshot(
    *,
    path: Path,
    recorded_at: datetime,
    slug: str,
    question: str,
    elapsed_sec: int,
    remaining_sec: int,
    up_price: float,
    down_price: float,
    primary_side: str,
    total_spend_usdc: float,
    shares_up: int,
    shares_down: int,
    avg_up: float,
    avg_down: float,
    pair_sum: float,
    dry_run: bool,
) -> Path:
    row = {
        "recorded_at": recorded_at.isoformat(sep=" ", timespec="seconds"),
        "slug": slug,
        "question": question,
        "elapsed_sec": elapsed_sec,
        "remaining_sec": remaining_sec,
        "up_price": round(up_price, 4),
        "down_price": round(down_price, 4),
        "primary_side": primary_side,
        "total_spend_usdc": round(total_spend_usdc, 4),
        "shares_up": shares_up,
        "shares_down": shares_down,
        "avg_up": round(avg_up, 4),
        "avg_down": round(avg_down, 4),
        "pair_sum": round(pair_sum, 4),
        "dry_run": str(dry_run).lower(),
    }
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writerow(row)
    return path


def prepare_public_price_snapshot_file(*, slug: str) -> Path:
    outdir = Path("exports") / "window_price_snapshots_public"
    outdir.mkdir(parents=True, exist_ok=True)
    basename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}"
    path = outdir / f"{basename}_prices.csv"
    if not path.exists() or path.stat().st_size == 0:
        row = {
            "recorded_at": "",
            "slug": "",
            "question": "",
            "elapsed_sec": "",
            "remaining_sec": "",
            "up_price": "",
            "down_price": "",
            "source": "",
        }
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writeheader()
    return path


def append_public_price_snapshot(
    *,
    path: Path,
    recorded_at: datetime,
    slug: str,
    question: str,
    elapsed_sec: int,
    remaining_sec: int,
    up_price: float,
    down_price: float,
    source: str = "public_recorder",
) -> Path:
    row = {
        "recorded_at": recorded_at.isoformat(sep=" ", timespec="seconds"),
        "slug": slug,
        "question": question,
        "elapsed_sec": elapsed_sec,
        "remaining_sec": remaining_sec,
        "up_price": round(up_price, 4),
        "down_price": round(down_price, 4),
        "source": source,
    }
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writerow(row)
    return path
