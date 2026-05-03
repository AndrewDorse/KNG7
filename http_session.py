#!/usr/bin/env python3
"""TLS-aware requests.Session for Polymarket HTTP APIs.

Plain requests on Windows + Python 3.12+ sometimes hits
``SSLV3_ALERT_HANDSHAKE_FAILURE`` against gamma/clob. This module builds a
Session with an explicit SSLContext, certifi (or OS trust store), and a normal
browser User-Agent.

Env:
  BOT_SSL_INSECURE=1       Disable cert verify (last resort; logs a warning).
  BOT_SSL_CA_BUNDLE=path   Extra CA bundle path (corporate proxy).
  BOT_USE_TRUSTSTORE=0     On Windows, skip truststore and use only certifi.
  BOT_HTTP_USER_AGENT=...  Override default User-Agent.
"""

from __future__ import annotations

import os
import platform
import ssl
import warnings
from typing import Any

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


def _ssl_context_insecure() -> ssl.SSLContext:
    warnings.warn(
        "BOT_SSL_INSECURE=1: TLS certificate verification is disabled.",
        UserWarning,
        stacklevel=2,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ssl_context_secure() -> ssl.SSLContext:
    skip_truststore = os.getenv("BOT_USE_TRUSTSTORE", "").strip().lower() in (
        "0",
        "false",
        "no",
    )
    if platform.system() == "Windows" and not skip_truststore:
        try:
            import truststore

            return truststore.ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass

    ca_env = os.getenv("BOT_SSL_CA_BUNDLE", "").strip()
    cafile = ca_env if ca_env and os.path.isfile(ca_env) else certifi.where()
    ctx = create_urllib3_context()
    ctx.load_verify_locations(cafile=cafile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class _PolymarketHTTPAdapter(HTTPAdapter):
    """HTTPAdapter that always passes our SSLContext into the connection pool."""

    def __init__(self, ssl_context: ssl.SSLContext, **kwargs: Any):
        self._pm_ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs.setdefault("ssl_context", self._pm_ssl_context)
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs.setdefault("ssl_context", self._pm_ssl_context)
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def create_polymarket_session() -> requests.Session:
    insecure = os.getenv("BOT_SSL_INSECURE", "").strip().lower() in ("1", "true", "yes")
    ctx = _ssl_context_insecure() if insecure else _ssl_context_secure()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.getenv(
                "BOT_HTTP_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            ),
            "Accept": "application/json, text/plain, */*",
        }
    )
    session.verify = False if insecure else True
    session.mount("https://", _PolymarketHTTPAdapter(ssl_context=ctx))
    return session
