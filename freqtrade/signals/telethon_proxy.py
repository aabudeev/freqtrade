"""
PySocks tuple for Telethon MTProto. HTTP(S)_PROXY / TG_PROXY in environment are not picked up automatically —
need to pass proxy= to TelegramClient.
"""
from __future__ import annotations

import os
from urllib.parse import unquote, urlparse


def telethon_proxy_from_env() -> tuple | None:
    for key in (
        "TELEGRAM_MTPROXY",
        "TG_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    ):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        if "://" not in raw:
            raw = f"socks5://{raw}"
        p = urlparse(raw)
        if not p.hostname:
            continue
        try:
            import socks
        except ImportError:
            return None

        scheme = (p.scheme or "http").lower()
        port = p.port
        user = unquote(p.username) if p.username else None
        pwd = unquote(p.password) if p.password else None
        rdns = os.environ.get("TELEGRAM_PROXY_RDNS", "true").lower() in ("1", "true", "yes")

        if scheme in ("socks5", "socks5h"):
            port = port or 1080
            use_rdns = rdns or scheme == "socks5h"
            if user and pwd:
                return (socks.SOCKS5, p.hostname, port, use_rdns, user, pwd)
            return (socks.SOCKS5, p.hostname, port, use_rdns)
        if scheme == "socks4":
            port = port or 1080
            return (socks.SOCKS4, p.hostname, port)
        if scheme in ("http", "https"):
            port = port or 8080
            if user and pwd:
                return (socks.HTTP, p.hostname, port, rdns, user, pwd)
            return (socks.HTTP, p.hostname, port, rdns)
    return None
