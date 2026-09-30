"""Shared, loopback-only daemon origin for both managed bridge profiles."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

DEFAULT_DAEMON_ORIGIN = "http://127.0.0.1:8737"


def validate_daemon_origin(origin: str) -> str:
    """Reject credentials, paths and remote destinations before sending a bearer."""
    try:
        parsed = urlsplit(origin)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Frame Bridge daemon origin must use a literal loopback address") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not address.is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Frame Bridge daemon origin must use a literal loopback address and port")
    return origin.rstrip("/")


def configured_daemon_origin(raw_config: dict) -> str:
    """Use the same typed listener settings as bh-host-daemon."""
    from .config_schema import BeadhiveConfig

    settings = BeadhiveConfig.model_validate(raw_config).host.daemon
    address = f"[{settings.bind}]" if ":" in settings.bind else settings.bind
    scheme = "https" if settings.tls.enabled else "http"
    return validate_daemon_origin(f"{scheme}://{address}:{settings.port}")
