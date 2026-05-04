#!/usr/bin/env python3
"""KNG7 Docker: **first_cheap_03** — BTC 5m and/or 15m in one process (BOT_WINDOW_MINUTES); btc50_1c or dual/market."""

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
    from trader import PolymarketTrader  # noqa: PLC0415

    from cheap03_first_engine import Cheap03FirstEngine  # noqa: PLC0415

    try:
        config = BotConfig.from_env()
    except BotConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if config.strategy_mode != "first_cheap_03":
        print(
            "KNG7 image expects BOT_STRATEGY_MODE=first_cheap_03 "
            f"(got {config.strategy_mode!r}).",
            file=sys.stderr,
        )
        return 2

    locator = GammaMarketLocator(config)
    trader = PolymarketTrader(config)
    Cheap03FirstEngine(config, locator, trader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
