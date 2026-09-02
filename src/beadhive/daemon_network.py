"""One fail-closed network admission boundary for every host-daemon transport.

The ASGI application depends on :class:`NetworkAdmissionPolicy`, not on listener or proxy
implementation details.  :class:`SecureNetworkAdmissionPolicy` is the product adapter: it owns
request trust decisions and bounded in-memory admission state, while route handlers continue to
own domain behavior and bearer authentication continues to be owned by :mod:`daemon_auth`.

No attacker-controlled value is rendered in a rejection.  Observability is aggregate and keyed
only by the finite error-code vocabulary below; bearer values and client identities are never
retained in metrics.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import islice
from typing import Protocol

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .daemon_config import HostDaemonConfig

_SECURE_SCHEMES = frozenset({"https", "wss"})
_ALLOWED_PREFLIGHT_METHODS = frozenset({"GET", "POST", "DELETE"})
_ALLOWED_PREFLIGHT_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-type",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
    }
)
_OVERFLOW_RATE_KEY = hashlib.sha256(b"beadhive-network-rate-overflow").digest()


class NetworkErrorCode(StrEnum):
    INVALID_HOST = "invalid_host"
    INVALID_ORIGIN = "invalid_origin"
    INVALID_PEER = "invalid_peer"
    INVALID_FORWARDED_HEADERS = "invalid_forwarded_headers"
    UNTRUSTED_FORWARDED_HEADERS = "untrusted_forwarded_headers"
    INSECURE_TRANSPORT = "insecure_transport"
    INVALID_PREFLIGHT = "invalid_preflight"
    REQUEST_BODY_TOO_LARGE = "request_body_too_large"
    REQUEST_TIMEOUT = "request_timeout"
    CONNECTION_LIMIT_REACHED = "connection_limit_reached"
    SESSION_LIMIT_REACHED = "session_limit_reached"
    TOKEN_RATE_LIMITED = "token_rate_limited"
    INVALID_SESSION = "invalid_session"


_ERROR_DETAILS: Mapping[NetworkErrorCode, tuple[int, str, bool]] = {
    NetworkErrorCode.INVALID_HOST: (400, "The request authority is not allowed.", False),
    NetworkErrorCode.INVALID_ORIGIN: (403, "The request origin is not allowed.", False),
    NetworkErrorCode.INVALID_PEER: (400, "The request peer is invalid.", False),
    NetworkErrorCode.INVALID_FORWARDED_HEADERS: (
        400,
        "The forwarded transport metadata is invalid.",
        False,
    ),
    NetworkErrorCode.UNTRUSTED_FORWARDED_HEADERS: (
        403,
        "Forwarded transport metadata is not accepted from this peer.",
        False,
    ),
    NetworkErrorCode.INSECURE_TRANSPORT: (
        400,
        "Remote daemon traffic requires a verified secure transport.",
        False,
    ),
    NetworkErrorCode.INVALID_PREFLIGHT: (403, "The CORS preflight is not allowed.", False),
    NetworkErrorCode.REQUEST_BODY_TOO_LARGE: (413, "The request body is too large.", False),
    NetworkErrorCode.REQUEST_TIMEOUT: (408, "The request body timed out.", True),
    NetworkErrorCode.CONNECTION_LIMIT_REACHED: (
        503,
        "The daemon connection limit is reached.",
        True,
    ),
    NetworkErrorCode.SESSION_LIMIT_REACHED: (
        503,
        "The daemon session limit is reached.",
        True,
    ),
    NetworkErrorCode.TOKEN_RATE_LIMITED: (429, "The request rate limit is reached.", True),
    NetworkErrorCode.INVALID_SESSION: (400, "The MCP session identifier is invalid.", False),
}


class NetworkRejected(RuntimeError):
    """A finite, redacted admission result safe to cross the ASGI boundary."""

    def __init__(self, code: NetworkErrorCode) -> None:
        self.code = code
        self.status_code, self.message, self.retryable = _ERROR_DETAILS[code]
        super().__init__(code.value)


@dataclass(frozen=True)
class NetworkAdmission:
    """One admission whose framework scope is excluded from every rendered artifact."""

    scope: Scope = field(repr=False)
    cors_origin: str | None
    secure: bool
    session_kind: str | None
    mcp_session_key: bytes | None = field(default=None, repr=False)
    mcp_reservation: int | None = field(default=None, repr=False)


@dataclass
class _McpSession:
    created_at: float
    last_seen_at: float


class NetworkBoundaryMetrics:
    """Bounded aggregate counters whose keys come only from ``NetworkErrorCode``."""

    def __init__(self) -> None:
        self._admitted = 0
        self._released = 0
        self._rejected: Counter[str] = Counter()

    def admitted(self) -> None:
        self._admitted += 1

    def released(self) -> None:
        self._released += 1

    def rejected(self, code: NetworkErrorCode) -> None:
        self._rejected[code.value] += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "admitted": self._admitted,
            "released": self._released,
            "active": self._admitted - self._released,
            "rejected": dict(sorted(self._rejected.items())),
        }


class NetworkAdmissionPolicy(Protocol):
    """Consumer-facing port for transport-independent ASGI admission."""

    max_request_body_bytes: int
    request_timeout_seconds: float
    metrics: NetworkBoundaryMetrics

    async def admit(self, scope: Scope) -> NetworkAdmission: ...

    async def release(self, admission: NetworkAdmission) -> None: ...

    async def observe_response_start(
        self, admission: NetworkAdmission, message: Message
    ) -> None: ...

    def record_rejection(self, code: NetworkErrorCode) -> None: ...


def _header_values(scope: Scope) -> dict[bytes, list[bytes]]:
    values: dict[bytes, list[bytes]] = defaultdict(list)
    for raw_name, raw_value in scope.get("headers", ()):
        values[raw_name.lower()].append(raw_value)
    return values


def _ascii_header(values: Mapping[bytes, list[bytes]], name: bytes) -> str | None:
    candidates = values.get(name, ())
    if not candidates:
        return None
    if len(candidates) != 1:
        raise NetworkRejected(
            NetworkErrorCode.INVALID_HOST
            if name == b"host"
            else NetworkErrorCode.INVALID_FORWARDED_HEADERS
        )
    try:
        return candidates[0].decode("ascii")
    except UnicodeDecodeError:
        raise NetworkRejected(
            NetworkErrorCode.INVALID_HOST
            if name == b"host"
            else NetworkErrorCode.INVALID_FORWARDED_HEADERS
        ) from None


def _host_name(authority: str) -> str | None:
    """Return one exact lower-case host name/literal, excluding an optional numeric port."""

    if not authority or any(char.isspace() for char in authority):
        return None
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            return None
        candidate = authority[: closing + 1]
        remainder = authority[closing + 1 :]
        if remainder and (
            not remainder.startswith(":") or not _valid_port(remainder.removeprefix(":"))
        ):
            return None
        try:
            ipaddress.IPv6Address(candidate[1:-1])
        except ValueError:
            return None
        return candidate.lower()
    if authority.count(":") > 1:
        return None
    candidate, separator, port = authority.partition(":")
    if separator and not _valid_port(port):
        return None
    if not candidate or candidate.endswith(".") or any(char in candidate for char in "/\\@?#"):
        return None
    try:
        return str(ipaddress.IPv4Address(candidate))
    except ValueError:
        pass
    try:
        candidate.encode("ascii")
    except UnicodeEncodeError:
        return None
    if any(not (part and part.replace("-", "a").isalnum()) for part in candidate.split(".")):
        return None
    return candidate.lower()


def _valid_port(value: str) -> bool:
    return (
        1 <= len(value) <= 5 and value.isascii() and value.isdigit() and 1 <= int(value) <= 65_535
    )


def _mcp_session_key(headers: Mapping[bytes, list[bytes]], *, reject_invalid: bool) -> bytes | None:
    identifiers = headers.get(b"mcp-session-id", ())
    valid = (
        len(identifiers) == 1
        and 1 <= len(identifiers[0]) <= 256
        and all(0x21 <= value <= 0x7E for value in identifiers[0])
    )
    if identifiers and not valid:
        if reject_invalid:
            raise NetworkRejected(NetworkErrorCode.INVALID_SESSION)
        return None
    if not identifiers:
        return None
    return hashlib.sha256(identifiers[0]).digest()


def _peer(scope: Scope) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    client = scope.get("client")
    if not isinstance(client, tuple) or not client:
        raise NetworkRejected(NetworkErrorCode.INVALID_PEER)
    try:
        return ipaddress.ip_address(str(client[0]))
    except ValueError:
        raise NetworkRejected(NetworkErrorCode.INVALID_PEER) from None


def _session_kind(scope: Scope) -> str | None:
    path = str(scope.get("path", ""))
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if scope.get("type") == "websocket" and path == "/ws/terminal":
        return "terminal"
    if (
        scope.get("type") == "http"
        and path.startswith("/api/v1/hives/")
        and path.endswith("/events")
    ):
        return "sse"
    return None


class SecureNetworkAdmissionPolicy:
    """Secure-default implementation of the network admission port."""

    def __init__(
        self,
        settings: HostDaemonConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.max_request_body_bytes = settings.http.max_request_body_bytes
        self.request_timeout_seconds = settings.http.request_timeout_seconds
        self.metrics = NetworkBoundaryMetrics()
        self._allowed_hosts = frozenset(value.lower() for value in settings.http.allowed_hosts)
        self._allowed_origins = frozenset(settings.cors.allowed_origins)
        self._trusted_proxies = frozenset(
            ipaddress.ip_address(value) for value in settings.proxy.trusted_addresses
        )
        self._session_limits = {
            "mcp": settings.mcp.max_sessions,
            "sse": settings.sse.max_clients,
            "terminal": settings.terminal.max_sessions,
        }
        self._active_connections = 0
        self._active_sessions: Counter[str] = Counter()
        self._mcp_sessions: OrderedDict[bytes, _McpSession] = OrderedDict()
        self._mcp_reservations: set[int] = set()
        self._next_mcp_reservation = 0
        self._rate_windows: OrderedDict[bytes, deque[float]] = OrderedDict()
        self._max_rate_buckets = min(100_000, max(1_024, settings.http.max_connections * 4))
        self._monotonic = monotonic
        self._lock = asyncio.Lock()

    def record_rejection(self, code: NetworkErrorCode) -> None:
        self.metrics.rejected(code)

    @property
    def rate_bucket_count(self) -> int:
        """Aggregate cardinality only; bucket digests are intentionally not observable."""

        return len(self._rate_windows)

    @property
    def active_mcp_session_count(self) -> int:
        """Aggregate persistent MCP session count; identifiers remain unobservable."""

        return len(self._mcp_sessions)

    def _prune_mcp_sessions(self, now: float) -> None:
        idle = self.settings.mcp.session_idle_seconds
        absolute = self.settings.mcp.session_absolute_seconds
        for key, session in tuple(self._mcp_sessions.items()):
            if now - session.last_seen_at >= idle or now - session.created_at >= absolute:
                del self._mcp_sessions[key]

    def _validated_origin(self, scope: Scope, headers: Mapping[bytes, list[bytes]]) -> str | None:
        origins = headers.get(b"origin", ())
        if len(origins) > 1:
            raise NetworkRejected(NetworkErrorCode.INVALID_ORIGIN)
        if not origins:
            if scope.get("type") == "websocket":
                raise NetworkRejected(NetworkErrorCode.INVALID_ORIGIN)
            return None
        try:
            origin = origins[0].decode("ascii")
        except UnicodeDecodeError:
            raise NetworkRejected(NetworkErrorCode.INVALID_ORIGIN) from None
        if origin not in self._allowed_origins:
            raise NetworkRejected(NetworkErrorCode.INVALID_ORIGIN)
        return origin

    def _validated_scope(
        self, scope: Scope, headers: Mapping[bytes, list[bytes]]
    ) -> tuple[Scope, bool]:
        host = _ascii_header(headers, b"host")
        if host is None or _host_name(host) not in self._allowed_hosts:
            raise NetworkRejected(NetworkErrorCode.INVALID_HOST)

        peer = _peer(scope)
        forwarded_names = {
            name for name in headers if name == b"forwarded" or name.startswith(b"x-forwarded-")
        }
        if b"forwarded" in forwarded_names:
            # Supporting two forwarding syntaxes invites conflicting chains.  The installed
            # boundary has one documented, exact X-Forwarded contract.
            raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS)
        if forwarded_names:
            if not self.settings.proxy.tls_terminating or peer not in self._trusted_proxies:
                raise NetworkRejected(NetworkErrorCode.UNTRUSTED_FORWARDED_HEADERS)
            if not forwarded_names.issubset(
                {b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}
            ):
                raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS)
            proto = _ascii_header(headers, b"x-forwarded-proto")
            forwarded_for = _ascii_header(headers, b"x-forwarded-for")
            forwarded_host = _ascii_header(headers, b"x-forwarded-host")
            if proto is None or proto.casefold() != "https" or forwarded_for is None:
                raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS)
            chain = [item.strip() for item in forwarded_for.split(",")]
            if not chain or len(chain) > 8:
                raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS)
            try:
                clients = [ipaddress.ip_address(item) for item in chain]
            except ValueError:
                raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS) from None
            if forwarded_host is not None and _host_name(forwarded_host) not in self._allowed_hosts:
                raise NetworkRejected(NetworkErrorCode.INVALID_FORWARDED_HEADERS)
            effective = dict(scope)
            effective["scheme"] = "wss" if scope.get("type") == "websocket" else "https"
            client = scope.get("client")
            effective["client"] = (str(clients[0]), client[1] if isinstance(client, tuple) else 0)
            return effective, True  # type: ignore[return-value]

        scheme = str(scope.get("scheme", "")).casefold()
        if peer.is_loopback:
            return scope, scheme in _SECURE_SCHEMES
        if not self.settings.tls.enabled or scheme not in _SECURE_SCHEMES:
            raise NetworkRejected(NetworkErrorCode.INSECURE_TRANSPORT)
        return scope, True

    def _rate_key(self, headers: Mapping[bytes, list[bytes]]) -> bytes | None:
        authorizations = headers.get(b"authorization", ())
        if not authorizations:
            return None
        # Authentication owns token shape and validity.  This pre-auth boundary uses only a
        # one-way, process-local bucket key and never exposes or persists it.
        if len(authorizations) != 1 or len(authorizations[0]) > 512:
            return hashlib.sha256(b"malformed-authorization").digest()
        try:
            value = authorizations[0].decode("ascii")
        except UnicodeDecodeError:
            return hashlib.sha256(b"malformed-authorization").digest()
        scheme, separator, token = value.partition(" ")
        if scheme.casefold() != "bearer" or separator != " " or not token or " " in token:
            return hashlib.sha256(b"malformed-authorization").digest()
        return hashlib.sha256(token.encode("ascii")).digest()

    async def admit(self, scope: Scope) -> NetworkAdmission:
        headers = _header_values(scope)
        validated_scope, secure = self._validated_scope(scope, headers)
        origin = self._validated_origin(validated_scope, headers)
        session_kind = _session_kind(validated_scope)
        sessionful_mcp = session_kind == "mcp" and self.settings.mcp.mode == "sessionful"
        request_mcp_key = _mcp_session_key(headers, reject_invalid=True) if sessionful_mcp else None
        now = self._monotonic()
        mcp_reservation: int | None = None

        async with self._lock:
            if scope.get("path") != "/health" and scope.get("method") != "OPTIONS":
                rate_key = self._rate_key(headers)
                if rate_key is not None:
                    cutoff = now - 60.0
                    # Bound attacker-created bucket cardinality.  New identities beyond the
                    # bound share one overflow bucket, so churn cannot grow memory or bypass the
                    # finite overflow rate indefinitely.
                    for candidate in tuple(islice(self._rate_windows, 64)):
                        candidate_window = self._rate_windows[candidate]
                        while candidate_window and candidate_window[0] <= cutoff:
                            candidate_window.popleft()
                        if not candidate_window:
                            del self._rate_windows[candidate]
                    if (
                        rate_key not in self._rate_windows
                        and len(self._rate_windows) >= self._max_rate_buckets - 1
                    ):
                        rate_key = _OVERFLOW_RATE_KEY
                    window = self._rate_windows.setdefault(rate_key, deque())
                    self._rate_windows.move_to_end(rate_key)
                    while window and window[0] <= cutoff:
                        window.popleft()
                    if len(window) >= self.settings.auth.token_rate_limit_per_minute:
                        raise NetworkRejected(NetworkErrorCode.TOKEN_RATE_LIMITED)
                    window.append(now)
            if sessionful_mcp:
                self._prune_mcp_sessions(now)
            if self._active_connections >= self.settings.http.max_connections:
                raise NetworkRejected(NetworkErrorCode.CONNECTION_LIMIT_REACHED)
            if sessionful_mcp:
                if request_mcp_key in self._mcp_sessions:
                    session = self._mcp_sessions[request_mcp_key]
                    session.last_seen_at = now
                    self._mcp_sessions.move_to_end(request_mcp_key)
                elif request_mcp_key is None and scope.get("method") == "POST":
                    if (
                        len(self._mcp_sessions) + len(self._mcp_reservations)
                        >= self._session_limits["mcp"]
                    ):
                        raise NetworkRejected(NetworkErrorCode.SESSION_LIMIT_REACHED)
                    self._next_mcp_reservation += 1
                    mcp_reservation = self._next_mcp_reservation
                    self._mcp_reservations.add(mcp_reservation)
            elif (
                session_kind is not None
                and self._active_sessions[session_kind] >= self._session_limits[session_kind]
            ):
                raise NetworkRejected(NetworkErrorCode.SESSION_LIMIT_REACHED)
            self._active_connections += 1
            if session_kind is not None and not sessionful_mcp:
                self._active_sessions[session_kind] += 1
            self.metrics.admitted()
        return NetworkAdmission(
            validated_scope,
            origin,
            secure,
            session_kind,
            request_mcp_key,
            mcp_reservation,
        )

    async def observe_response_start(self, admission: NetworkAdmission, message: Message) -> None:
        if message.get("type") != "http.response.start" or admission.session_kind != "mcp":
            return
        status = int(message.get("status", 500))
        successful = 200 <= status < 300
        now = self._monotonic()
        async with self._lock:
            reservation = admission.mcp_reservation
            if reservation is not None and reservation in self._mcp_reservations:
                self._mcp_reservations.remove(reservation)
                if successful:
                    response_headers = _header_values({"headers": message.get("headers", ())})
                    response_key = _mcp_session_key(response_headers, reject_invalid=False)
                    if response_key is not None:
                        existing = self._mcp_sessions.get(response_key)
                        if existing is None:
                            self._mcp_sessions[response_key] = _McpSession(now, now)
                        else:
                            existing.last_seen_at = now
                            self._mcp_sessions.move_to_end(response_key)
            if (
                successful
                and admission.mcp_session_key is not None
                and admission.scope.get("method") == "DELETE"
            ):
                self._mcp_sessions.pop(admission.mcp_session_key, None)

    async def release(self, admission: NetworkAdmission) -> None:
        async with self._lock:
            if self._active_connections <= 0:
                raise RuntimeError("network admission released more than once")
            self._active_connections -= 1
            if admission.mcp_reservation is not None:
                self._mcp_reservations.discard(admission.mcp_reservation)
            if admission.session_kind is not None and admission.session_kind != "mcp":
                if self._active_sessions[admission.session_kind] <= 0:
                    raise RuntimeError("network session admission released more than once")
                self._active_sessions[admission.session_kind] -= 1
            self.metrics.released()


def _error_headers(error: NetworkRejected) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if error.retryable:
        headers["Retry-After"] = "1"
    return headers


def _error_response(error: NetworkRejected) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": error.code.value,
                "message": error.message,
                "retryable": error.retryable,
            }
        },
        status_code=error.status_code,
        headers=_error_headers(error),
    )


async def _bounded_receive(
    receive: Receive, maximum: int
) -> tuple[list[Message], NetworkRejected | None]:
    messages: list[Message] = []
    observed = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.disconnect":
            return messages, None
        if message["type"] != "http.request":
            return messages, NetworkRejected(NetworkErrorCode.REQUEST_BODY_TOO_LARGE)
        observed += len(message.get("body", b""))
        if observed > maximum:
            return messages, NetworkRejected(NetworkErrorCode.REQUEST_BODY_TOO_LARGE)
        if not message.get("more_body", False):
            return messages, None


def _preflight(
    scope: Scope, headers: Mapping[bytes, list[bytes]], origin: str | None
) -> Response | NetworkRejected | None:
    if scope.get("type") != "http" or scope.get("method") != "OPTIONS":
        return None
    requested_method = _ascii_header(headers, b"access-control-request-method")
    raw_requested_headers = _ascii_header(headers, b"access-control-request-headers")
    requested_headers = (
        frozenset(
            value.strip().casefold() for value in raw_requested_headers.split(",") if value.strip()
        )
        if raw_requested_headers is not None
        else frozenset()
    )
    if (
        origin is None
        or requested_method not in _ALLOWED_PREFLIGHT_METHODS
        or not requested_headers.issubset(_ALLOWED_PREFLIGHT_HEADERS)
    ):
        return NetworkRejected(NetworkErrorCode.INVALID_PREFLIGHT)
    response_headers = {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": requested_method,
        "Vary": "Origin",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if requested_headers:
        response_headers["Access-Control-Allow-Headers"] = ", ".join(
            sorted(requested_headers, key=str.casefold)
        )
    return Response(status_code=204, headers=response_headers)


class SecureNetworkBoundaryMiddleware:
    """ASGI adapter enforcing one admission policy across HTTP and WebSocket routes."""

    def __init__(self, app: ASGIApp, *, policy: NetworkAdmissionPolicy) -> None:
        self.app = app
        self.policy = policy

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        error: NetworkRejected,
        *,
        record: bool = True,
    ) -> None:
        if record:
            self.policy.record_rejection(error.code)
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": "Forbidden"})
        else:
            await _error_response(error)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        try:
            admission = await self.policy.admit(scope)
        except NetworkRejected as error:
            await self._reject(scope, receive, send, error)
            return
        try:
            headers = _header_values(admission.scope)
            preflight = _preflight(admission.scope, headers, admission.cors_origin)
            if isinstance(preflight, NetworkRejected):
                await self._reject(admission.scope, receive, send, preflight)
                return
            if preflight is not None:
                await preflight(admission.scope, receive, send)
                return

            downstream_receive = receive
            if admission.scope.get("type") == "http":
                content_lengths = headers.get(b"content-length", ())
                if len(content_lengths) > 1:
                    await self._reject(
                        admission.scope,
                        receive,
                        send,
                        NetworkRejected(NetworkErrorCode.REQUEST_BODY_TOO_LARGE),
                    )
                    return
                if content_lengths:
                    try:
                        declared = int(content_lengths[0].decode("ascii"))
                    except (UnicodeDecodeError, ValueError):
                        declared = self.policy.max_request_body_bytes + 1
                    if declared < 0 or declared > self.policy.max_request_body_bytes:
                        await self._reject(
                            admission.scope,
                            receive,
                            send,
                            NetworkRejected(NetworkErrorCode.REQUEST_BODY_TOO_LARGE),
                        )
                        return
                try:
                    async with asyncio.timeout(self.policy.request_timeout_seconds):
                        messages, body_error = await _bounded_receive(
                            receive, self.policy.max_request_body_bytes
                        )
                except TimeoutError:
                    await self._reject(
                        admission.scope,
                        receive,
                        send,
                        NetworkRejected(NetworkErrorCode.REQUEST_TIMEOUT),
                    )
                    return
                if body_error is not None:
                    await self._reject(admission.scope, receive, send, body_error)
                    return
                queue = deque(messages)

                async def replay_receive() -> Message:
                    if queue:
                        return queue.popleft()
                    # SSE/streaming responses keep listening for a real disconnect after the
                    # request body is consumed.  Preserve that live channel instead of inventing
                    # an immediate disconnect at the replay boundary.
                    return await receive()

                downstream_receive = replay_receive

            async def secure_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    await self.policy.observe_response_start(admission, message)
                    response_headers = list(message.get("headers", ()))
                    response_headers.append((b"x-content-type-options", b"nosniff"))
                    if admission.secure:
                        response_headers.append((b"strict-transport-security", b"max-age=31536000"))
                    if admission.cors_origin is not None:
                        response_headers.extend(
                            [
                                (
                                    b"access-control-allow-origin",
                                    admission.cors_origin.encode("ascii"),
                                ),
                                (b"access-control-allow-credentials", b"true"),
                                (b"vary", b"Origin"),
                            ]
                        )
                    message = {**message, "headers": response_headers}
                await send(message)

            await self.app(admission.scope, downstream_receive, secure_send)
        finally:
            await self.policy.release(admission)
