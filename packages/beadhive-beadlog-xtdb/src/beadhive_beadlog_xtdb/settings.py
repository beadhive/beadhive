"""The ``plugins.beadlog-xtdb`` fragment as the plugin reads it, and the DSN endpoint parser.

Root validates the fragment (``BeadlogXtdbConfig``) before the factory or doctor sees it; this
module only reads the already-valid mapping and never imports psycopg. A DSN is parsed just far
enough to know where it points (host, port, database, user), which is all doctor and the target
label need; a password is never part of it (the config model refuses one).
"""

from __future__ import annotations

import ipaddress
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_DSN = "host=127.0.0.1 port=5432 user=xtdb dbname=xtdb"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8080/healthz/alive"
#: The XTDB 2.2.0 image digest the loader and the bh-cntr0 spike validated.
VALIDATED_IMAGE = (
    "ghcr.io/xtdb/xtdb@sha256:a6a426505b95a36e4e0e1d0de88efcf4220cb6bde8f1f5e594b0a1f92a948a77"
)


@dataclass(frozen=True)
class Endpoint:
    """Where a DSN points; ``host`` is ``127.0.0.1`` when the DSN names none (libpq would use a
    local socket, which XTDB does not serve, so the loopback address is what is reached)."""

    host: str
    port: int
    dbname: str
    user: str

    @property
    def label(self) -> str:
        return f"xtdb://{self.user}@{self.host}:{self.port}/{self.dbname}"


@dataclass(frozen=True)
class XtdbSettings:
    """The validated fragment's keys the plugin uses (``credential`` stays with root)."""

    enabled: bool = False
    dsn: str = DEFAULT_DSN
    image: str = VALIDATED_IMAGE
    data_dir: str = ""
    health_url: str = DEFAULT_HEALTH_URL

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> XtdbSettings:
        if not isinstance(values, Mapping):
            raise TypeError("settings must be a mapping")

        def text(key: str, default: str) -> str:
            value = values.get(key, default)
            if not isinstance(value, str):
                raise TypeError(f"plugins.beadlog-xtdb.{key} must be a string")
            return value

        return cls(
            enabled=values.get("enabled") is True,
            dsn=text("dsn", DEFAULT_DSN) or DEFAULT_DSN,
            image=text("image", VALIDATED_IMAGE),
            data_dir=text("data_dir", ""),
            health_url=text("health_url", DEFAULT_HEALTH_URL) or DEFAULT_HEALTH_URL,
        )

    def endpoint(self) -> Endpoint:
        return parse_dsn(self.dsn)


def parse_dsn(dsn: str) -> Endpoint:
    """The endpoint of a libpq key=value string or ``postgresql://`` URI (defaults: loopback,
    5432, user and database ``xtdb``). Raises ``ValueError`` without repeating the input."""
    params: dict[str, str] = {}
    text = dsn.strip()
    if text.lower().startswith(("postgresql://", "postgres://")):
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            raise ValueError("dsn is not a valid postgresql:// URI") from None
        if parts.hostname:
            params["host"] = unquote(parts.hostname)
        if port is not None:
            params["port"] = str(port)
        if parts.username:
            params["user"] = unquote(parts.username)
        if parts.path.strip("/"):
            params["dbname"] = unquote(parts.path.strip("/"))
        for item in parts.query.split("&"):
            key, sep, value = item.partition("=")
            if sep and key in {"host", "port", "user", "dbname"}:
                params[key] = unquote(value)
    else:
        try:
            tokens = shlex.split(text)
        except ValueError:
            raise ValueError("dsn is not a valid key=value connection string") from None
        for token in tokens:
            key, sep, value = token.partition("=")
            if not sep:
                raise ValueError("dsn is not a valid key=value connection string")
            params[key.strip()] = value
    host = (params.get("host") or params.get("hostaddr") or "127.0.0.1").split(",")[0]
    try:
        port = int((params.get("port") or "5432").split(",")[0])
    except ValueError:
        raise ValueError("dsn port is not a number") from None
    return Endpoint(host, port, params.get("dbname") or "xtdb", params.get("user") or "xtdb")


def is_loopback(host: str) -> bool:
    """``host`` is a loopback name or address (or a local socket path)."""
    name = host.strip().strip("[]").lower()
    if name.startswith("/") or name in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


__all__ = [
    "DEFAULT_DSN",
    "DEFAULT_HEALTH_URL",
    "VALIDATED_IMAGE",
    "Endpoint",
    "XtdbSettings",
    "is_loopback",
    "parse_dsn",
]
