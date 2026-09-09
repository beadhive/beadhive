"""Scoped bearer authority for the unified host daemon.

The credential file stores only SHA-256 digests of high-entropy bearer secrets.  A bearer is
``bh1.<credential-id>.<secret>`` and is carried only in the ``Authorization`` header.  New
requests reload the file before every decision, so an atomic rotation or revocation takes effect
immediately.  Long-lived transports register with :class:`CredentialSessionRegistry`; its
lifespan revalidates each session no later than ``session_revalidation_seconds`` after the last
successful check.  An invalid session is removed and recorded immediately; transport-owned close
callbacks run independently with a timeout no greater than that revalidation bound, so a stuck
callback cannot delay detection or closure of another session.

Nothing in this module logs, traces, serializes, or places a bearer in a URL.  Objects which must
hold a bearer in memory have deliberately redacted ``str``/``repr`` output.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote_to_bytes

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .daemon_contract import AuthScope, ErrorCode, redacted_error

CREDENTIAL_FILE_SCHEMA_VERSION: Final = 1
TOKEN_PREFIX: Final = "bh1"
REDACTED: Final = "[REDACTED]"
_CREDENTIAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOKEN_SECRET = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_MAX_CREDENTIAL_FILE_BYTES = 1_048_576
_MAX_RAW_URL_BYTES = 16_384
_TOKEN_IN_URL = re.compile(rb"bh1\.[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9_-]{43,128}")
_PERCENT_ESCAPE = re.compile(rb"%[0-9A-Fa-f]{2}")
_URL_CREDENTIAL_NAMES = frozenset(
    {
        "access_token",
        "attach-token",
        "attach_token",
        "attachtoken",
        "authorization",
        "bearer",
        "bearer_token",
        "credential",
        "token",
    }
)
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class CredentialFileError(ValueError):
    """A credential file is absent, insecure, malformed, or internally inconsistent."""


class AuthFailureCode(StrEnum):
    MISSING = "auth_missing"
    MALFORMED = "auth_malformed"
    UNKNOWN = "auth_unknown_credential"
    EXPIRED = "auth_expired"
    REVOKED = "auth_revoked"
    ROTATED = "auth_rotated"
    WRONG_AUDIENCE = "auth_wrong_audience"
    WRONG_PRINCIPAL = "auth_wrong_principal"
    WRONG_SCOPE = "auth_wrong_scope"
    CREDENTIAL_FILE = "auth_credential_file_unavailable"
    CREDENTIAL_IN_URL = "auth_credential_in_url"
    SESSION_CLOSED = "auth_session_closed"


class AuthenticationError(PermissionError):
    """Deterministic, redacted authentication/authorization failure."""

    def __init__(self, code: AuthFailureCode, *, status_code: int = 401) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code.value)


class _CredentialModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: _camel(name),
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CredentialRecord(_CredentialModel):
    """One verifier record.  ``secret_digest`` is safe to persist; bearer secrets are not."""

    credential_id: str
    secret_digest: str
    audience: str
    principal: str
    scopes: tuple[AuthScope, ...]
    expires_at: int = Field(gt=0)
    revoked: bool = False

    @field_validator("credential_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _CREDENTIAL_ID.fullmatch(value):
            raise ValueError("credentialId must be an opaque identifier")
        return value

    @field_validator("secret_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("secretDigest must be a SHA-256 verifier")
        return value

    @field_validator("audience", "principal")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        if not _TEXT.fullmatch(value):
            raise ValueError("audience and principal must be bounded printable values")
        return value

    @field_validator("scopes", mode="before")
    @classmethod
    def _scopes_are_explicit_and_unique(cls, value: Any) -> Any:
        if (
            not isinstance(value, (list, tuple))
            or not value
            or not all(isinstance(scope, (str, AuthScope)) for scope in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("scopes must be a non-empty unique list")
        return value


class CredentialFile(_CredentialModel):
    schema_version: int = CREDENTIAL_FILE_SCHEMA_VERSION
    generation: int = Field(ge=1)
    credentials: tuple[CredentialRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> CredentialFile:
        if self.schema_version != CREDENTIAL_FILE_SCHEMA_VERSION:
            raise ValueError("unsupported credential file schemaVersion")
        identifiers = [record.credential_id for record in self.credentials]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("credentialIds must be unique")
        return self


class SecretBearer:
    """A bearer kept in memory with output representations that never reveal it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal_for_authority(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({REDACTED!r})"

    def __str__(self) -> str:
        return REDACTED


@dataclass(frozen=True)
class ProvisionedCredential:
    """One-time provisioning result; its bearer remains redacted from normal output."""

    credential_id: str
    bearer: SecretBearer = field(repr=False)

    def __repr__(self) -> str:
        return f"ProvisionedCredential(credential_id={self.credential_id!r}, bearer={REDACTED!r})"


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    credential_id: str
    principal: str
    audience: str
    scopes: frozenset[AuthScope]
    expires_at: int
    generation: int

    def permits(self, scope: AuthScope) -> bool:
        return scope in self.scopes


def _secret_digest(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()


def _parse_token(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or len(token) > 256 or token.count(".") != 2:
        raise AuthenticationError(AuthFailureCode.MALFORMED)
    prefix, credential_id, secret = token.split(".", 2)
    if (
        prefix != TOKEN_PREFIX
        or not _CREDENTIAL_ID.fullmatch(credential_id)
        or not _TOKEN_SECRET.fullmatch(secret)
    ):
        raise AuthenticationError(AuthFailureCode.MALFORMED)
    return credential_id, secret


def _secure_file_bytes(path: Path) -> bytes:
    if not path.is_absolute():
        raise CredentialFileError("credential file path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise CredentialFileError("credential file is unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialFileError("credential file must be a regular non-symlink")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise CredentialFileError("credential file must have mode 0600")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read(_MAX_CREDENTIAL_FILE_BYTES + 1)
        if not payload or len(payload) > _MAX_CREDENTIAL_FILE_BYTES:
            raise CredentialFileError("credential file size is invalid")
        return payload
    finally:
        os.close(fd)


def load_credential_file(path: Path) -> CredentialFile:
    try:
        return CredentialFile.model_validate_json(_secure_file_bytes(path))
    except CredentialFileError:
        raise
    except (ValueError, TypeError) as exc:
        raise CredentialFileError("credential file schema is invalid") from exc


def _ensure_credential_parent(path: Path) -> None:
    created_parent = False
    try:
        path.parent.mkdir(mode=0o700, parents=True)
        created_parent = True
    except FileExistsError:
        if not path.parent.is_dir():
            raise CredentialFileError("credential file parent is not a directory") from None
    if created_parent:
        path.parent.chmod(0o700)


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.abspath(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _credential_mutation_lock(path: Path) -> Iterator[None]:
    """Serialize one credential transaction across threads and OS processes.

    The persistent sibling lock contains no credential material.  The in-process lock is also
    required because ``flock``'s same-process semantics vary across supported kernels.
    """

    if not path.is_absolute():
        raise CredentialFileError("credential file path must be absolute")
    _ensure_credential_parent(path)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    thread_lock = _thread_lock_for(path)
    with thread_lock:
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError:
            raise CredentialFileError("credential mutation lock is unavailable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CredentialFileError("credential mutation lock must be a regular file")
            os.fchmod(fd, 0o600)
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_credential_file(
    path: Path, value: CredentialFile, *, replace_existing: bool = True
) -> None:
    """Atomically replace one private verifier file without ever persisting bearer secrets."""

    if not path.is_absolute():
        raise CredentialFileError("credential file path must be absolute")
    _ensure_credential_parent(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        payload = value.model_dump_json(by_alias=True, exclude_none=False).encode() + b"\n"
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise CredentialFileError("credential file already exists") from exc
            temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def _new_token(credential_id: str, token_factory: Callable[[int], str]) -> tuple[str, str]:
    secret = token_factory(32)
    if not _TOKEN_SECRET.fullmatch(secret):
        raise CredentialFileError("token factory did not produce a safe high-entropy value")
    return f"{TOKEN_PREFIX}.{credential_id}.{secret}", _secret_digest(secret)


def provision_credential_file(
    path: Path,
    *,
    credential_id: str,
    audience: str,
    principal: str,
    scopes: Sequence[AuthScope | str],
    expires_at: int,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
) -> ProvisionedCredential:
    """Create a new private verifier file and return the bearer exactly once in memory."""

    token, digest = _new_token(credential_id, token_factory)
    record = CredentialRecord(
        credential_id=credential_id,
        secret_digest=digest,
        audience=audience,
        principal=principal,
        scopes=tuple(scopes),
        expires_at=expires_at,
    )
    with _credential_mutation_lock(path):
        _write_credential_file(
            path,
            CredentialFile(generation=1, credentials=(record,)),
            replace_existing=False,
        )
    return ProvisionedCredential(credential_id, SecretBearer(token))


def rotate_credential(
    path: Path,
    credential_id: str,
    *,
    expires_at: int | None = None,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
) -> ProvisionedCredential:
    """Replace one verifier under the same id, making the previous bearer deterministically old."""

    token, digest = _new_token(credential_id, token_factory)
    with _credential_mutation_lock(path):
        current = load_credential_file(path)
        found = False
        records: list[CredentialRecord] = []
        for record in current.credentials:
            if record.credential_id == credential_id:
                found = True
                records.append(
                    CredentialRecord.model_validate(
                        {
                            **record.model_dump(),
                            "secret_digest": digest,
                            "expires_at": record.expires_at if expires_at is None else expires_at,
                            "revoked": False,
                        }
                    )
                )
            else:
                records.append(record)
        if not found:
            raise CredentialFileError("credential id is unknown")
        _write_credential_file(
            path, CredentialFile(generation=current.generation + 1, credentials=tuple(records))
        )
    return ProvisionedCredential(credential_id, SecretBearer(token))


def revoke_credential(path: Path, credential_id: str) -> None:
    with _credential_mutation_lock(path):
        current = load_credential_file(path)
        found = False
        records: list[CredentialRecord] = []
        for record in current.credentials:
            if record.credential_id == credential_id:
                found = True
                records.append(
                    CredentialRecord.model_validate({**record.model_dump(), "revoked": True})
                )
            else:
                records.append(record)
        if not found:
            raise CredentialFileError("credential id is unknown")
        _write_credential_file(
            path, CredentialFile(generation=current.generation + 1, credentials=tuple(records))
        )


def add_credential(
    path: Path,
    *,
    credential_id: str,
    audience: str,
    principal: str,
    scopes: Sequence[AuthScope | str],
    expires_at: int,
    token_factory: Callable[[int], str] = secrets.token_urlsafe,
) -> ProvisionedCredential:
    """Atomically add another independently-scoped principal to an existing authority."""

    token, digest = _new_token(credential_id, token_factory)
    record = CredentialRecord(
        credential_id=credential_id,
        secret_digest=digest,
        audience=audience,
        principal=principal,
        scopes=tuple(scopes),
        expires_at=expires_at,
    )
    with _credential_mutation_lock(path):
        current = load_credential_file(path)
        if any(row.credential_id == credential_id for row in current.credentials):
            raise CredentialFileError("credential id already exists")
        _write_credential_file(
            path,
            CredentialFile(
                generation=current.generation + 1,
                credentials=(*current.credentials, record),
            ),
        )
    return ProvisionedCredential(credential_id, SecretBearer(token))


class CredentialAuthority:
    """File-backed authority.  Every decision observes the latest complete atomic generation."""

    def __init__(
        self,
        path: Path,
        *,
        audience: str,
        session_revalidation_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _TEXT.fullmatch(audience):
            raise ValueError("audience must be a bounded printable value")
        if not 0 < session_revalidation_seconds <= 300:
            raise ValueError("session revalidation bound must be in (0, 300] seconds")
        self.path = path
        self.audience = audience
        self.session_revalidation_seconds = float(session_revalidation_seconds)
        self.clock = clock

    def authenticate(
        self,
        bearer: str | SecretBearer | None,
        *,
        required_scope: AuthScope | None = None,
        expected_principal: str | None = None,
        now: float | None = None,
    ) -> AuthenticatedPrincipal:
        if bearer is None:
            raise AuthenticationError(AuthFailureCode.MISSING)
        token = bearer.reveal_for_authority() if isinstance(bearer, SecretBearer) else bearer
        credential_id, secret = _parse_token(token)
        try:
            credential_file = load_credential_file(self.path)
        except CredentialFileError:
            # Runtime failures deliberately discard file/path exception chains before a request
            # boundary or structured logger can render them.
            raise AuthenticationError(AuthFailureCode.CREDENTIAL_FILE) from None
        record = next(
            (row for row in credential_file.credentials if row.credential_id == credential_id),
            None,
        )
        if record is None:
            raise AuthenticationError(AuthFailureCode.UNKNOWN)
        candidate = _secret_digest(secret)
        if not hmac.compare_digest(candidate, record.secret_digest):
            raise AuthenticationError(AuthFailureCode.ROTATED)
        resolved_now = self.clock() if now is None else now
        if record.revoked:
            raise AuthenticationError(AuthFailureCode.REVOKED)
        if record.expires_at <= resolved_now:
            raise AuthenticationError(AuthFailureCode.EXPIRED)
        if not hmac.compare_digest(record.audience, self.audience):
            raise AuthenticationError(AuthFailureCode.WRONG_AUDIENCE)
        if expected_principal is not None and not hmac.compare_digest(
            record.principal, expected_principal
        ):
            raise AuthenticationError(AuthFailureCode.WRONG_PRINCIPAL)
        scopes = frozenset(record.scopes)
        if required_scope is not None and required_scope not in scopes:
            raise AuthenticationError(AuthFailureCode.WRONG_SCOPE, status_code=403)
        return AuthenticatedPrincipal(
            credential_id=record.credential_id,
            principal=record.principal,
            audience=record.audience,
            scopes=scopes,
            expires_at=record.expires_at,
            generation=credential_file.generation,
        )


def bearer_from_headers(headers: Sequence[tuple[bytes, bytes]]) -> str:
    authorizations = [value for name, value in headers if name.lower() == b"authorization"]
    if not authorizations:
        raise AuthenticationError(AuthFailureCode.MISSING)
    if len(authorizations) != 1:
        raise AuthenticationError(AuthFailureCode.MALFORMED)
    try:
        value = authorizations[0].decode("ascii")
    except UnicodeDecodeError:
        raise AuthenticationError(AuthFailureCode.MALFORMED) from None
    if len(value) > 512:
        raise AuthenticationError(AuthFailureCode.MALFORMED)
    scheme, separator, token = value.partition(" ")
    if scheme.casefold() != "bearer" or separator != " " or not token or " " in token:
        raise AuthenticationError(AuthFailureCode.MALFORMED)
    return token


def request_scope(scope: Scope) -> AuthScope | None:
    """Map the stable route surface to independent scopes; unknown paths still require auth."""

    path = scope.get("path", "")
    method = scope.get("method", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        return AuthScope.MCP_CONTROL
    if scope["type"] == "websocket" and path == "/ws/terminal":
        return AuthScope.TERMINAL_ATTACH
    if method == "POST" and path == "/api/v1/terminal/attach-token":
        return AuthScope.TERMINAL_ATTACH
    if method == "POST" and path.startswith("/api/v1/runs/") and path.endswith("/activity"):
        return AuthScope.ACTIVITY_PUBLISH
    if method == "GET" and (
        path == "/openapi.json"
        or path.startswith("/api/v1/factory")
        or path.startswith("/api/v1/hives/")
        or (path.startswith("/api/v1/runs/") and path.endswith("/activity"))
    ):
        return AuthScope.OPERATOR_READ
    return None


def _strict_url_decodings(raw: bytes) -> tuple[bytes, ...] | None:
    """Return raw/once/twice-decoded bytes, or None for an unsafe encoding.

    URL parsing libraries intentionally accept malformed percent escapes and normalize duplicate
    encodings.  Authentication must make the opposite choice: ambiguity is a rejection, and a
    third encoding layer is never necessary for this API.
    """

    if len(raw) > _MAX_RAW_URL_BYTES:
        return None
    levels: list[bytes] = []
    current = raw
    for index in range(3):
        cursor = 0
        while cursor < len(current):
            if current[cursor] == ord("%"):
                if _PERCENT_ESCAPE.match(current, cursor) is None:
                    return None
                cursor += 3
            else:
                cursor += 1
        levels.append(current)
        decoded = unquote_to_bytes(current)
        if decoded == current:
            return tuple(levels)
        current = decoded
        if index == 2:
            return None
    return tuple(levels)


def _normalized_query_name(raw: bytes) -> str | None:
    try:
        return raw.decode("ascii").casefold()
    except UnicodeDecodeError:
        return None


def _credential_in_url(scope: Scope) -> bool:
    """Fail closed on credential-shaped material in raw path/query, including encoding tricks."""

    raw_path = scope.get("raw_path")
    if not isinstance(raw_path, bytes):
        raw_path = str(scope.get("path", "")).encode("utf-8", errors="surrogatepass")
    raw_query = scope.get("query_string", b"")
    if not isinstance(raw_query, bytes):
        return True
    path_levels = _strict_url_decodings(raw_path)
    query_levels = _strict_url_decodings(raw_query)
    if path_levels is None or query_levels is None:
        return True
    if any(_TOKEN_IN_URL.search(level) for level in (*path_levels, *query_levels)):
        return True

    for level in query_levels:
        for query_field in level.split(b"&"):
            name, _separator, _value = query_field.partition(b"=")
            normalized = _normalized_query_name(name)
            if normalized in _URL_CREDENTIAL_NAMES:
                return True
    return False


def authentication_error_response(error: AuthenticationError) -> JSONResponse:
    """Render one checked, credential-free authentication response."""

    forbidden = error.status_code == 403
    body = redacted_error(ErrorCode.FORBIDDEN if forbidden else ErrorCode.UNAUTHORIZED).to_wire()
    headers = {} if forbidden else {"WWW-Authenticate": 'Bearer realm="beadhive-host"'}
    return JSONResponse(body, status_code=error.status_code, headers=headers)


class BearerAuthMiddleware:
    """Authenticate every HTTP/WebSocket route except public ``/health`` before dispatch."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authority: CredentialAuthority,
        scope_resolver: Callable[[Scope], AuthScope | None] = request_scope,
        expected_principal: Callable[[Scope], str | None] | None = None,
    ) -> None:
        self.app = app
        self.authority = authority
        self.scope_resolver = scope_resolver
        self.expected_principal = expected_principal or (lambda _scope: None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        try:
            if _credential_in_url(scope):
                raise AuthenticationError(AuthFailureCode.CREDENTIAL_IN_URL)
            bearer = bearer_from_headers(scope.get("headers", ()))
            required_scope = self.scope_resolver(scope)
            principal = self.authority.authenticate(
                bearer,
                required_scope=required_scope,
                expected_principal=self.expected_principal(scope),
            )
        except AuthenticationError as exc:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4403 if exc.status_code == 403 else 4401,
                        "reason": "Forbidden" if exc.status_code == 403 else "Unauthorized",
                    }
                )
            else:
                await authentication_error_response(exc)(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["auth_principal"] = principal
        state["auth_bearer"] = SecretBearer(bearer)
        if required_scope is AuthScope.MCP_CONTROL:
            # FastMCP's stateful HTTP manager owns sessions through the standard ASGI
            # authentication identity, not Starlette request state.  Carry only the stable,
            # non-secret credential identity into that contract; the bearer remains redacted.
            from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
            from mcp.server.auth.provider import AccessToken
            from starlette.authentication import AuthCredentials

            scopes = sorted(item.value for item in principal.scopes)
            scope["auth"] = AuthCredentials(scopes)
            scope["user"] = AuthenticatedUser(
                AccessToken(
                    token=REDACTED,
                    client_id=principal.credential_id,
                    scopes=scopes,
                    expires_at=principal.expires_at,
                    resource=principal.audience,
                    subject=principal.principal,
                    claims={"iss": principal.audience},
                )
            )
        await self.app(scope, receive, send)


CloseCallback = Callable[[AuthFailureCode], Awaitable[None]]


class CloseCallbackStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class SessionClosureRecord:
    """Redacted deterministic outcome for one invalidated live session."""

    credential_id: str
    principal: str
    reason: AuthFailureCode
    detected_at: float
    callback_status: CloseCallbackStatus = CloseCallbackStatus.SCHEDULED


@dataclass(eq=False)
class CredentialSession:
    """One long-lived session whose stored bearer is always redacted from output."""

    authority: CredentialAuthority = field(repr=False)
    bearer: SecretBearer = field(repr=False)
    required_scope: AuthScope
    expected_principal: str | None
    principal: AuthenticatedPrincipal
    next_revalidation_at: float
    close_callback: CloseCallback = field(repr=False)
    closed: bool = False
    close_reason: AuthFailureCode | None = None

    def __repr__(self) -> str:
        return (
            "CredentialSession("
            f"credential_id={self.principal.credential_id!r}, "
            f"principal={self.principal.principal!r}, "
            f"required_scope={self.required_scope.value!r}, closed={self.closed!r})"
        )


class CredentialSessionRegistry:
    """Own live sessions and enforce the configured revalidation/closure deadline.

    Close callbacks must be native async callables and must not block synchronously.  They may
    catch the first cancellation to perform async cleanup; the registry's second cancellation is
    terminal.  A callback that suppresses both is outside the supported contract and is
    force-closed so it cannot survive registry lifespan exit.
    """

    def __init__(
        self,
        authority: CredentialAuthority,
        *,
        close_timeout_seconds: float | None = None,
        closure_history: int = 1_024,
    ) -> None:
        self.authority = authority
        resolved_close_timeout = (
            min(1.0, authority.session_revalidation_seconds)
            if close_timeout_seconds is None
            else close_timeout_seconds
        )
        if not 0 < resolved_close_timeout <= authority.session_revalidation_seconds:
            raise ValueError("session close timeout must fit the credential revalidation bound")
        if closure_history < 1:
            raise ValueError("closure history must retain at least one outcome")
        self.close_timeout_seconds = float(resolved_close_timeout)
        self._sessions: set[CredentialSession] = set()
        self._closures: deque[SessionClosureRecord] = deque(maxlen=closure_history)
        self._callback_supervisors: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    @property
    def closure_records(self) -> tuple[SessionClosureRecord, ...]:
        return tuple(self._closures)

    def open(
        self,
        bearer: str | SecretBearer,
        *,
        required_scope: AuthScope,
        close: CloseCallback,
        expected_principal: str | None = None,
        now: float | None = None,
    ) -> CredentialSession:
        if not inspect.iscoroutinefunction(close):
            raise TypeError("session close callback must be async")
        resolved_now = self.authority.clock() if now is None else now
        secret = bearer if isinstance(bearer, SecretBearer) else SecretBearer(bearer)
        principal = self.authority.authenticate(
            secret,
            required_scope=required_scope,
            expected_principal=expected_principal,
            now=resolved_now,
        )
        session = CredentialSession(
            authority=self.authority,
            bearer=secret,
            required_scope=required_scope,
            expected_principal=expected_principal,
            principal=principal,
            next_revalidation_at=min(
                resolved_now + self.authority.session_revalidation_seconds,
                float(principal.expires_at),
            ),
            close_callback=close,
        )
        self._sessions.add(session)
        self._wake.set()
        return session

    def _detach(
        self, session: CredentialSession, reason: AuthFailureCode, *, detected_at: float
    ) -> tuple[CredentialSession, SessionClosureRecord] | None:
        if session.closed:
            return None
        session.closed = True
        session.close_reason = reason
        self._sessions.discard(session)
        record = SessionClosureRecord(
            credential_id=session.principal.credential_id,
            principal=session.principal.principal,
            reason=reason,
            detected_at=detected_at,
        )
        self._closures.append(record)
        return session, record

    async def _invoke_close_callback(
        self, session: CredentialSession, record: SessionClosureRecord
    ) -> None:
        try:
            await session.close_callback(record.reason)
        except asyncio.CancelledError:
            if record.callback_status is CloseCallbackStatus.SCHEDULED:
                record.callback_status = CloseCallbackStatus.CANCELLED
            raise
        except Exception:
            record.callback_status = CloseCallbackStatus.ERROR
        else:
            record.callback_status = CloseCallbackStatus.COMPLETED

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _cancel_and_reap_callback_tasks(
        self,
        tasks: Mapping[asyncio.Task[None], SessionClosureRecord],
        *,
        status: CloseCallbackStatus,
    ) -> None:
        pending = {task for task in tasks if not task.done()}
        for task in pending:
            record = tasks[task]
            if record.callback_status is CloseCallbackStatus.SCHEDULED:
                record.callback_status = status
            task.cancel()

        # One turn permits ordinary cancellation cleanup.  A second cancellation is the explicit
        # terminal signal in the supported callback contract.
        if pending:
            await asyncio.sleep(0)
        pending = {task for task in pending if not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.sleep(0)

        # Never hand a contract-violating callback to event-loop teardown.  Closing the suspended
        # registry coroutine also closes the callback it is awaiting; cancelling once more wakes
        # the Task so gather can reap its terminal RuntimeError/CancelledError deterministically.
        pending = {task for task in pending if not task.done()}
        for task in pending:
            try:
                task.get_coro().close()
            except (Exception, GeneratorExit):
                pass
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._consume_task(task)

    async def _supervise_close_callbacks(
        self, detached: Sequence[tuple[CredentialSession, SessionClosureRecord]]
    ) -> None:
        tasks = {
            asyncio.create_task(
                self._invoke_close_callback(session, record),
                name=f"daemon-close-session-{record.credential_id}",
            ): record
            for session, record in detached
        }
        if not tasks:
            return
        try:
            done, pending = await asyncio.wait(tasks, timeout=self.close_timeout_seconds)
        except asyncio.CancelledError:
            # asyncio.wait() does not cancel its children when its own waiter is cancelled.
            await self._cancel_and_reap_callback_tasks(
                tasks,
                status=CloseCallbackStatus.CANCELLED,
            )
            raise
        for task in done:
            self._consume_task(task)
        if pending:
            await self._cancel_and_reap_callback_tasks(
                {task: tasks[task] for task in pending},
                status=CloseCallbackStatus.TIMED_OUT,
            )

    def _schedule_close_callbacks(
        self, detached: Sequence[tuple[CredentialSession, SessionClosureRecord]]
    ) -> None:
        if not detached:
            return
        supervisor = asyncio.create_task(
            self._supervise_close_callbacks(detached),
            name="daemon-session-close-supervisor",
        )
        self._callback_supervisors.add(supervisor)

        def finished(task: asyncio.Task[None]) -> None:
            self._retire_supervisor(task)

        supervisor.add_done_callback(finished)

    def _retire_supervisor(self, task: asyncio.Task[None]) -> None:
        """Remove a completed supervisor without depending on callback scheduling order."""

        self._callback_supervisors.discard(task)
        if task.done():
            self._consume_task(task)

    async def drain_close_callbacks(self) -> None:
        """Wait only for bounded supervisors, never for a hung transport callback itself."""

        loop = asyncio.get_running_loop()
        try:
            while self._callback_supervisors:
                local: list[asyncio.Task[None]] = []
                for task in tuple(self._callback_supervisors):
                    if task.done():
                        # Awaiting an already-done gather does not suspend.  Retire it here so a
                        # queued done callback cannot be starved by a tight drain loop.
                        self._retire_supervisor(task)
                    elif task.get_loop() is loop:
                        local.append(task)
                    else:
                        # A registry can be exercised by successive asyncio.run() calls.  Never
                        # attach a task owned by the prior loop to this one; request cancellation
                        # when that loop is still serviceable and fence it out of this drain.
                        self._callback_supervisors.discard(task)
                        owner = task.get_loop()
                        if not owner.is_closed():
                            try:
                                owner.call_soon_threadsafe(task.cancel)
                            except RuntimeError:
                                pass

                if not local:
                    continue

                # The supervisor alone owns the configured callback deadline.  A second drain
                # timer racing the same deadline can misclassify a timeout as cancellation.
                # Supervisors contain no transport await outside their bounded asyncio.wait().
                done, _pending = await asyncio.wait(local)
                for task in done:
                    self._retire_supervisor(task)
        except asyncio.CancelledError:
            # Lifespan cancellation must not orphan supervisors or their callback children.
            cancelled: list[asyncio.Task[None]] = []
            for task in tuple(self._callback_supervisors):
                self._callback_supervisors.discard(task)
                if task.get_loop() is loop and not task.done():
                    task.cancel()
                    task.add_done_callback(self._consume_task)
                    cancelled.append(task)
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            raise

    async def close(self, session: CredentialSession) -> None:
        detached = self._detach(
            session,
            AuthFailureCode.SESSION_CLOSED,
            detected_at=self.authority.clock(),
        )
        self._schedule_close_callbacks((detached,) if detached is not None else ())

    def unregister(self, session: CredentialSession) -> None:
        """Forget an ordinarily completed transport without recording invalidation."""

        if session.closed:
            return
        session.closed = True
        self._sessions.discard(session)
        self._wake.set()

    async def revalidate_due(self, *, now: float | None = None) -> None:
        resolved_now = self.authority.clock() if now is None else now
        detached: list[tuple[CredentialSession, SessionClosureRecord]] = []
        for session in tuple(self._sessions):
            if session.closed or session.next_revalidation_at > resolved_now:
                continue
            try:
                principal = self.authority.authenticate(
                    session.bearer,
                    required_scope=session.required_scope,
                    expected_principal=session.expected_principal,
                    now=resolved_now,
                )
            except AuthenticationError as exc:
                invalid = self._detach(session, exc.code, detected_at=resolved_now)
                if invalid is not None:
                    detached.append(invalid)
                continue
            session.principal = principal
            session.next_revalidation_at = min(
                resolved_now + self.authority.session_revalidation_seconds,
                float(principal.expires_at),
            )
        # Every invalid session is already removed and recorded before callback execution begins.
        # Callback supervisors are independent of the revalidation loop and bounded as a group.
        self._schedule_close_callbacks(detached)

    async def run(self) -> None:
        while not self._stopping:
            await self.revalidate_due()
            if not self._sessions:
                timeout = self.authority.session_revalidation_seconds
            else:
                timeout = max(
                    0.0,
                    min(session.next_revalidation_at for session in self._sessions)
                    - self.authority.clock(),
                )
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(self):
        self._stopping = False
        task = asyncio.create_task(self.run(), name="daemon-credential-session-revalidation")
        try:
            yield self
        finally:
            self._stopping = True
            self._wake.set()
            await task
            detached = [
                invalid
                for session in tuple(self._sessions)
                if (
                    invalid := self._detach(
                        session,
                        AuthFailureCode.SESSION_CLOSED,
                        detected_at=self.authority.clock(),
                    )
                )
                is not None
            ]
            self._schedule_close_callbacks(detached)
            await self.drain_close_callbacks()


def assert_redacted(value: Any, secrets_to_find: Sequence[str]) -> None:
    """Test/support guard: reject a rendered artifact containing a credential value."""

    rendered = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    if any(secret in rendered for secret in secrets_to_find):
        raise ValueError("sensitive credential material reached a rendered artifact")
