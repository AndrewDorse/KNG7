#!/usr/bin/env python3
"""KNG7 Docker: **limit_pair_5m** — schedule UP/DOWN GTC limits on upcoming multi-asset 5m windows."""

from __future__ import annotations

import logging
import os
import sys


def _configure_logging() -> None:
    """Keep HTTP/SDK quiet. App logger defaults to ERROR (only errors); INIT/DEAL/WIN go to stdout."""
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

    from limit_pair_engine import LimitPairEngine  # noqa: PLC0415

    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if config.strategy_mode != "limit_pair_5m":
        print(
            "KNG7 image expects BOT_STRATEGY_MODE=limit_pair_5m "
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
    trader = PolymarketTrader(config)
    if not config.dry_run:
        ok, detail = trader.verify_clob_ready()
        if ok:
            print(f"WALLET_OK {detail}", flush=True)
        else:
            print(f"WALLET_WARN {detail}", flush=True)
            print(wallet_config_hint_for_error(Exception(detail)), flush=True)
    LimitPairEngine(config, locator, trader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
