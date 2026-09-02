"""Typed, secure-default configuration boundary for the unified host daemon.

The daemon lifecycle must call :func:`validate_for_listener_startup` before it acquires or
binds a socket.  Keeping this validation independent of Uvicorn makes the fail-closed boundary
usable by the service, Doctor, installers, and tests without constructing an ASGI application.
"""

from __future__ import annotations

import ipaddress
import math
import os
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _DaemonSection(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DaemonAuthConfig(_DaemonSection):
    """Bearer-token verification inputs; raw credentials never belong in config.yaml."""

    required: Literal[True] = Field(
        True, description="Authentication is mandatory on every route except /health."
    )
    audience: str = Field("beadhive-host", min_length=1, max_length=128)
    credential_file: Path | None = Field(
        None,
        description=(
            "Absolute owner-readable file containing verifier records; required when enabled."
        ),
    )
    session_revalidation_seconds: float = Field(
        30.0,
        gt=0,
        le=300,
        description=(
            "Maximum seconds a long-lived MCP, SSE, or terminal session may remain open after "
            "its last successful credential check. The daemon session registry closes a session "
            "at its next deadline when expiry, rotation, or revocation is observed."
        ),
    )
    token_rate_limit_per_minute: int = Field(600, ge=1, le=1_000_000)


class DaemonTlsConfig(_DaemonSection):
    enabled: bool = False
    certificate_file: Path | None = None
    private_key_file: Path | None = None
    minimum_version: Literal["TLSv1.2", "TLSv1.3"] = "TLSv1.3"

    @model_validator(mode="after")
    def _complete_pair(self) -> DaemonTlsConfig:
        supplied = self.certificate_file is not None or self.private_key_file is not None
        if self.enabled and (self.certificate_file is None or self.private_key_file is None):
            raise ValueError("direct TLS requires both certificate_file and private_key_file")
        if not self.enabled and supplied:
            raise ValueError("TLS files may not be configured while direct TLS is disabled")
        return self


class DaemonProxyConfig(_DaemonSection):
    tls_terminating: bool = False
    trusted_addresses: tuple[str, ...] = ()

    @field_validator("trusted_addresses")
    @classmethod
    def _addresses_are_exact_ips(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("trusted proxy addresses must be unique")
        for value in values:
            try:
                ipaddress.ip_address(value)
            except ValueError as exc:
                raise ValueError(f"trusted proxy address must be an exact IP: {value!r}") from exc
        return values

    @model_validator(mode="after")
    def _trusted_termination(self) -> DaemonProxyConfig:
        if self.tls_terminating and not self.trusted_addresses:
            raise ValueError("TLS-terminating proxy mode requires trusted_addresses")
        if not self.tls_terminating and self.trusted_addresses:
            raise ValueError("trusted_addresses require tls_terminating proxy mode")
        return self


class DaemonCorsConfig(_DaemonSection):
    allowed_origins: tuple[str, ...] = ()

    @field_validator("allowed_origins")
    @classmethod
    def _exact_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in values:
            raise ValueError("wildcard CORS origins are forbidden")
        if len(set(values)) != len(values):
            raise ValueError("CORS origins must be unique")
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"CORS origin must be an exact HTTP origin: {value!r}")
        return values


class DaemonHttpConfig(_DaemonSection):
    max_connections: int = Field(256, ge=1, le=100_000)
    max_request_body_bytes: int = Field(1_048_576, ge=1_024, le=64 * 1_048_576)
    request_timeout_seconds: float = Field(30.0, gt=0, le=600)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")

    @field_validator("allowed_hosts")
    @classmethod
    def _safe_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or "*" in values:
            raise ValueError("HTTP Host validation requires an exact non-wildcard allowlist")
        if len(set(values)) != len(values):
            raise ValueError("allowed_hosts must be unique")
        if any(not value or "/" in value or "\\" in value for value in values):
            raise ValueError("allowed_hosts entries must be host names or IP literals")
        return values


class DaemonMcpConfig(_DaemonSection):
    mode: Literal["sessionful", "stateless"] = "sessionful"
    max_sessions: int = Field(64, ge=1, le=10_000)
    session_idle_seconds: float = Field(900.0, gt=0, le=86_400)
    session_absolute_seconds: float = Field(14_400.0, gt=0, le=604_800)

    @model_validator(mode="after")
    def _session_bounds(self) -> DaemonMcpConfig:
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("MCP idle timeout may not exceed the absolute timeout")
        return self


class DaemonSseConfig(_DaemonSection):
    replay_events_per_hive: int = Field(2_048, ge=1, le=1_000_000)
    replay_total_bytes: int = Field(64 * 1_048_576, ge=65_536, le=4 * 1_073_741_824)
    client_queue_events: int = Field(256, ge=1, le=100_000)
    max_clients: int = Field(128, ge=1, le=100_000)
    heartbeat_seconds: float = Field(15.0, gt=0, le=300)


class DaemonActivityConfig(_DaemonSection):
    max_body_bytes: int = Field(262_144, ge=1_024, le=16 * 1_048_576)
    max_records_per_read: int = Field(1_000, ge=1, le=100_000)
    idempotency_retention_seconds: float = Field(604_800.0, gt=0, le=31_536_000)


class DaemonTerminalConfig(_DaemonSection):
    """Reserved limits; availability remains false until the PTY verdict is consumed."""

    available: Literal[False] = False
    attach_token_ttl_seconds: float = Field(30.0, gt=0, le=120)
    max_sessions: int = Field(8, ge=1, le=1_000)
    client_queue_bytes: int = Field(262_144, ge=4_096, le=16 * 1_048_576)
    idle_seconds: float = Field(900.0, gt=0, le=86_400)
    absolute_seconds: float = Field(14_400.0, gt=0, le=86_400)

    @model_validator(mode="after")
    def _timeout_bounds(self) -> DaemonTerminalConfig:
        if self.idle_seconds > self.absolute_seconds:
            raise ValueError("terminal idle timeout may not exceed the absolute timeout")
        return self


class DaemonShutdownConfig(_DaemonSection):
    graceful_seconds: float = Field(30.0, gt=0, le=300)
    request_drain_seconds: float = Field(10.0, ge=0, le=300)
    telemetry_flush_seconds: float = Field(3.0, ge=0, le=30)

    @model_validator(mode="after")
    def _fits_total(self) -> DaemonShutdownConfig:
        if self.request_drain_seconds + self.telemetry_flush_seconds > self.graceful_seconds:
            raise ValueError("drain and telemetry budgets must fit graceful_seconds")
        return self


class DaemonStatusConfig(_DaemonSection):
    dependency_probe_timeout_seconds: float = Field(2.0, gt=0, le=30)
    dependency_probe_interval_seconds: float = Field(10.0, gt=0, le=300)


class HostDaemonConfig(_DaemonSection):
    """All listener/security/resource limits validated as one host-local boundary."""

    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = Field(8737, ge=1, le=65_535)
    auth: DaemonAuthConfig = Field(default_factory=DaemonAuthConfig)
    tls: DaemonTlsConfig = Field(default_factory=DaemonTlsConfig)
    proxy: DaemonProxyConfig = Field(default_factory=DaemonProxyConfig)
    cors: DaemonCorsConfig = Field(default_factory=DaemonCorsConfig)
    http: DaemonHttpConfig = Field(default_factory=DaemonHttpConfig)
    mcp: DaemonMcpConfig = Field(default_factory=DaemonMcpConfig)
    sse: DaemonSseConfig = Field(default_factory=DaemonSseConfig)
    activity: DaemonActivityConfig = Field(default_factory=DaemonActivityConfig)
    terminal: DaemonTerminalConfig = Field(default_factory=DaemonTerminalConfig)
    shutdown: DaemonShutdownConfig = Field(default_factory=DaemonShutdownConfig)
    status: DaemonStatusConfig = Field(default_factory=DaemonStatusConfig)

    @field_validator("bind")
    @classmethod
    def _bind_is_an_ip(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("daemon bind must be an exact IP address") from exc

    @model_validator(mode="after")
    def _secure_exposure(self) -> HostDaemonConfig:
        bind = ipaddress.ip_address(self.bind)
        if not bind.is_loopback and not (self.tls.enabled or self.proxy.tls_terminating):
            raise ValueError("non-loopback daemon bind requires direct TLS or trusted TLS proxy")
        if self.enabled and self.auth.credential_file is None:
            raise ValueError("enabled daemon requires auth.credential_file")
        values = (
            self.http.request_timeout_seconds,
            self.mcp.session_idle_seconds,
            self.mcp.session_absolute_seconds,
            self.sse.heartbeat_seconds,
            self.shutdown.graceful_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("daemon durations must be finite")
        return self


class DaemonConfigurationError(ValueError):
    """Configuration or credential material is unsafe for listener startup."""


def _required_private_file(path: Path | None, *, label: str, private: bool) -> None:
    if path is None or not path.is_absolute():
        raise DaemonConfigurationError(f"{label} must be an absolute path")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise DaemonConfigurationError(f"{label} is not readable") from exc
    if not stat.S_ISREG(mode):
        raise DaemonConfigurationError(f"{label} must be a regular file")
    permissions = stat.S_IMODE(mode)
    if not permissions & stat.S_IRUSR:
        raise DaemonConfigurationError(f"{label} must be readable by its owner")
    access_kwargs = (
        {"effective_ids": True} if os.access in getattr(os, "supports_effective_ids", ()) else {}
    )
    try:
        effectively_readable = os.access(path, os.R_OK, **access_kwargs)
    except (NotImplementedError, TypeError):  # pragma: no cover - platform fallback
        effectively_readable = os.access(path, os.R_OK)
    if not effectively_readable:
        raise DaemonConfigurationError(f"{label} is not readable by the daemon process")
    if private and mode & 0o077:
        raise DaemonConfigurationError(f"{label} must not be accessible by group or others")


def validate_for_listener_startup(config: HostDaemonConfig) -> HostDaemonConfig:
    """Refuse disabled, incomplete, or insecure material before a socket is bound."""

    if not config.enabled:
        raise DaemonConfigurationError("host daemon is disabled")
    _required_private_file(config.auth.credential_file, label="auth credential_file", private=True)
    if config.tls.enabled:
        _required_private_file(
            config.tls.certificate_file, label="TLS certificate_file", private=False
        )
        _required_private_file(
            config.tls.private_key_file, label="TLS private_key_file", private=True
        )
    return config
