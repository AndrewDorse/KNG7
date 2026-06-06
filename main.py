#!/usr/bin/env python3
"""KNG7 Docker entrypoint."""

from __future__ import annotations

import logging
import os
import sys


def _configure_logging() -> None:
    """Quiet noisy dependencies while preserving bot error output."""
    for name in (
        "urllib3",
        "requests",
        "websocket",
        "websockets",
        "py_clob_client",
        "py_clob_client_v2",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    app = logging.getLogger("polymarket_btc_ladder")
    level_name = (os.getenv("BOT_LOG_LEVEL") or "ERROR").strip().upper()
    app.setLevel(getattr(logging, level_name, logging.INFO))
    if not app.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        app.addHandler(h)
    app.propagate = False

    logging.getLogger().setLevel(logging.WARNING)


def main() -> int:
    _configure_logging()

    from config import BotConfig, BotConfigError  # noqa: PLC0415
    from market_locator import GammaMarketLocator  # noqa: PLC0415
    from trader import PolymarketTrader, wallet_config_hint_for_error  # noqa: PLC0415

    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if config.strategy_mode not in ("limit_pair_5m", "late_high_5m"):
        print(
            "KNG7 image expects BOT_STRATEGY_MODE=late_high_5m or limit_pair_5m "
            f"(got {config.strategy_mode!r}).",
            file=sys.stderr,
        )
        return 2
    if config.dry_run:
        logging.getLogger("polymarket_btc_ladder").error(
            "POLY_DRY_RUN=true: bot will NOT place real orders. "
            "Set POLY_DRY_RUN=false for live trading."
        )

    locator = GammaMarketLocator(config)
    try:
        trader = PolymarketTrader(config)
    except Exception as exc:
        print(f"WALLET_FAIL init: {exc}", file=sys.stderr)
        print(wallet_config_hint_for_error(exc), file=sys.stderr)
        return 2

    if not config.dry_run:
        ok, detail = trader.verify_clob_ready()
        print(f"WALLET_CHECK {'OK' if ok else 'FAIL'} {detail}", flush=True)
        if not ok:
            print(wallet_config_hint_for_error(Exception(detail)), file=sys.stderr)
            return 2

    if config.strategy_mode == "late_high_5m":
        from late_high_engine import LateHighEngine  # noqa: PLC0415

        LateHighEngine(config, locator, trader).run()
    else:
        from limit_pair_engine import LimitPairEngine  # noqa: PLC0415

        LimitPairEngine(config, locator, trader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
