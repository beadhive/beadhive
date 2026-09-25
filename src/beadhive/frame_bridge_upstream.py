"""Private ``frame-bridge.upstream.v1`` server for the aggregate Gateway.

This module deliberately has no listener entry point.  Factory service custody owns
the AF_UNIX process boundary; this module owns request authentication, server-owned
scope, and the adapter to the local host daemon.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, quote

import httpx
from joserfc import jws
from joserfc.jwk import OKPKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import daemon_auth, daemon_contract, operator_work_items

UPSTREAM_CONTRACT = "frame-bridge.upstream.v1"
GATEWAY_ISSUER = "gateway/dev/aggregate"
_PREFIX = "/frame-bridge/upstream/v1"
_LOOPBACK_ORIGIN = "http://127.0.0.1:8420"
_MAX_JSON_BYTES = 1 << 20
_MAX_QUERY_BYTES = 16 * 1024
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9_-]{22}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
_KID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_INSTANCE_ID = re.compile(r"[a-z0-9-]+/[a-z0-9-]+\Z")
_HIVE_ID = re.compile(r"[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+\Z")
_WORK_ITEM_ID = re.compile(r"[A-Za-z0-9._~-]{1,256}\Z")
_PRINCIPAL = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_JTI = re.compile(r"[A-Za-z0-9_-]{22,}\Z")
_DEFAULT_REQUEST_ID = "req_" + "0" * 22
_PRIVATE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT")


class VerifierConfigError(ValueError):
    """The root-owned Gateway verifier set is not safe to use."""


@dataclass(frozen=True)
class PrivateRequestError(Exception):
    """One contract-safe private failure, without a serialized cause."""

    code: str
    status_code: int
    retryable: bool


class SourceUnavailable(Exception):
    """The owned host daemon did not provide a safe source response."""


class SourceNotFound(Exception):
    """The configured source is absent."""


class ResnapshotRequired(Exception):
    """A requested source cursor or revision cannot safely continue."""


class WorkItemDetailTooLarge(Exception):
    """The exact source detail cannot cross the bounded disclosure seam."""


class WorkItemPageTooLarge(Exception):
    """The requested source page cannot cross the bounded disclosure seam."""


class IdempotencyConflict(Exception):
    """A refresh idempotency key was reused with different input."""


def _base64url_bytes(value: str) -> bytes:
    if not isinstance(value, str) or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _strict_json_object(value: bytes) -> dict[str, object]:
    if not 1 <= len(value) <= _MAX_JSON_BYTES:
        raise ValueError("JSON body size is incompatible")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("JSON body is incompatible") from exc

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_: str) -> object:
        raise ValueError("non-finite JSON number")

    try:
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("JSON body is incompatible") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON root is incompatible")
    return parsed


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class RegisteredInstance:
    """The one server-owned Gateway instance exposed by this Factory bridge."""

    instance_id: str = "dev/demo"
    factory_id: str = "development"
    primary_hive_id: str = "github/beadhive/beadhive"

    def __post_init__(self) -> None:
        if _INSTANCE_ID.fullmatch(self.instance_id) is None:
            raise ValueError("private instance ID is incompatible")
        if _OPAQUE_ID.fullmatch(self.factory_id) is None:
            raise ValueError("private factory ID is incompatible")
        if _HIVE_ID.fullmatch(self.primary_hive_id) is None:
            raise ValueError("private primary hive ID is incompatible")

    def to_wire(self) -> dict[str, object]:
        return {
            "instanceId": self.instance_id,
            "factoryId": self.factory_id,
            "primaryHiveId": self.primary_hive_id,
            "publicContracts": ["gateway.v1", "gateway.read.v1"],
        }


@dataclass(frozen=True)
class GatewayVerifier:
    issuer: str
    kid: str
    key: OKPKey
    not_before: int
    not_after: int


@dataclass(frozen=True)
class GatewayVerifierSet:
    """Immutable provisioned keys; remote key discovery is intentionally absent."""

    digest: str
    by_kid: Mapping[str, GatewayVerifier]

    @classmethod
    def from_document(cls, value: object) -> GatewayVerifierSet:
        if not isinstance(value, dict) or set(value) != {"verifierConfigVersion", "issuers"}:
            raise VerifierConfigError("Gateway verifier configuration is incompatible")
        if value.get("verifierConfigVersion") != 1:
            raise VerifierConfigError("Gateway verifier configuration version is incompatible")
        issuers = value.get("issuers")
        if not isinstance(issuers, list) or not 1 <= len(issuers) <= 8:
            raise VerifierConfigError("Gateway verifier issuers are incompatible")
        by_kid: dict[str, GatewayVerifier] = {}
        seen_issuers: set[str] = set()
        for issuer_entry in issuers:
            if not isinstance(issuer_entry, dict) or set(issuer_entry) != {
                "issuer",
                "acceptedKeys",
                "revokedKeyIds",
            }:
                raise VerifierConfigError("Gateway verifier issuer is incompatible")
            issuer = issuer_entry.get("issuer")
            accepted = issuer_entry.get("acceptedKeys")
            revoked = issuer_entry.get("revokedKeyIds")
            if (
                not isinstance(issuer, str)
                or issuer != GATEWAY_ISSUER
                or issuer in seen_issuers
                or not isinstance(accepted, list)
                or not 1 <= len(accepted) <= 8
                or not isinstance(revoked, list)
            ):
                raise VerifierConfigError("Gateway verifier issuer is incompatible")
            seen_issuers.add(issuer)
            revoked_ids: set[str] = set()
            for item in revoked:
                if not isinstance(item, str) or _KID.fullmatch(item) is None or item in revoked_ids:
                    raise VerifierConfigError("Gateway revoked key list is incompatible")
                revoked_ids.add(item)
            for key_entry in accepted:
                if not isinstance(key_entry, dict) or set(key_entry) != {
                    "kid",
                    "alg",
                    "jwk",
                    "notBefore",
                    "notAfter",
                }:
                    raise VerifierConfigError("Gateway verifier key is incompatible")
                kid = key_entry.get("kid")
                not_before = key_entry.get("notBefore")
                not_after = key_entry.get("notAfter")
                jwk = key_entry.get("jwk")
                if (
                    not isinstance(kid, str)
                    or _KID.fullmatch(kid) is None
                    or kid in by_kid
                    or kid in revoked_ids
                    or key_entry.get("alg") != "EdDSA"
                    or type(not_before) is not int
                    or type(not_after) is not int
                    or not_before < 0
                    or not_after <= not_before
                    or not isinstance(jwk, dict)
                    or set(jwk) != {"kty", "crv", "x"}
                    or jwk.get("kty") != "OKP"
                    or jwk.get("crv") != "Ed25519"
                    or not isinstance(jwk.get("x"), str)
                ):
                    raise VerifierConfigError("Gateway verifier key is incompatible")
                try:
                    public_bytes = _base64url_bytes(jwk["x"])
                    key = OKPKey.import_key(jwk)
                except Exception as exc:
                    raise VerifierConfigError("Gateway verifier key is incompatible") from exc
                if len(public_bytes) != 32:
                    raise VerifierConfigError("Gateway verifier key is incompatible")
                by_kid[kid] = GatewayVerifier(issuer, kid, key, not_before, not_after)
        return cls(
            digest="sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest(),
            by_kid=by_kid,
        )

    def verifier_for(self, kid: str, *, now: float) -> GatewayVerifier:
        candidate = self.by_kid.get(kid)
        if candidate is None or now < candidate.not_before or now > candidate.not_after:
            raise PrivateRequestError("upstream_authentication_failed", 401, False)
        return candidate


def _secure_authority_bytes(path: Path, *, require_root: bool) -> bytes:
    """Read one bounded regular, non-link authority file without following its leaf."""

    if not path.is_absolute():
        raise VerifierConfigError("Gateway verifier path must be absolute")
    if require_root:
        current = path.parent
        while True:
            try:
                parent = os.lstat(current)
            except OSError as exc:
                raise VerifierConfigError("Gateway verifier directory is unavailable") from exc
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != 0
                or stat.S_IMODE(parent.st_mode) & 0o022
            ):
                raise VerifierConfigError("Gateway verifier directory is incompatible")
            if current == current.parent:
                break
            current = current.parent
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerifierConfigError("Gateway verifier file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o640
            or info.st_nlink != 1
            or (require_root and info.st_uid != 0)
        ):
            raise VerifierConfigError("Gateway verifier file is incompatible")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_JSON_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise VerifierConfigError("Gateway verifier file is incompatible")
        if total == 0:
            raise VerifierConfigError("Gateway verifier file is incompatible")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_gateway_verifier_set(path: Path, *, require_root: bool = True) -> GatewayVerifierSet:
    """Load provisioned verifier keys; this boundary has no JWK discovery URL."""

    try:
        return GatewayVerifierSet.from_document(
            _strict_json_object(_secure_authority_bytes(path, require_root=require_root))
        )
    except VerifierConfigError:
        raise
    except Exception as exc:
        raise VerifierConfigError("Gateway verifier configuration is incompatible") from exc


class GatewayVerifierStore:
    """Atomic verifier pointer; invalid reload retains trust but flips readiness false."""

    def __init__(self, active: GatewayVerifierSet) -> None:
        self._active = active
        self._ready = True

    @property
    def active(self) -> GatewayVerifierSet:
        return self._active

    @property
    def ready(self) -> bool:
        return self._ready

    def reload(self, loader: Callable[[], GatewayVerifierSet]) -> bool:
        try:
            candidate = loader()
        except Exception:
            self._ready = False
            return False
        self._active = candidate
        self._ready = True
        return True


class FrameBridgeUpstreamSource(Protocol):
    """Narrow port for the one host daemon selected by Factory configuration."""

    async def online(self) -> bool: ...

    async def directory(self, *, limit: int, cursor: str | None) -> Mapping[str, object]: ...

    async def snapshot(self) -> Mapping[str, object]: ...

    async def work_items(
        self,
        *,
        view: str,
        limit: int,
        cursor: str | None,
        priorities: tuple[str, ...],
        labels: tuple[str, ...],
        assignee: str | None,
        issue_type: str | None,
        parent: str | None,
    ) -> Mapping[str, object]: ...

    async def work_item_detail(self, *, bead_id: str) -> Mapping[str, object]: ...

    async def events(self, *, subscription: str, after: str | None) -> AsyncIterator[bytes]: ...

    async def refresh(
        self, *, expected_revision: str, correlation_id: str
    ) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class _LoopbackDaemonAuth(httpx.Auth):
    """Reveal the daemon bearer only to the fixed loopback daemon client."""

    def __init__(self, bearer: daemon_auth.SecretBearer) -> None:
        self._bearer = bearer

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._bearer.reveal_for_authority()}"
        yield request


class HostDaemonFrameBridgeSource:
    """Live adapter for the registered host daemon; it never receives Gateway authority."""

    def __init__(
        self,
        *,
        daemon_bearer: daemon_auth.SecretBearer,
        instance: RegisteredInstance,
        display_name: str = "Beadhive",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(daemon_bearer, daemon_auth.SecretBearer):
            raise TypeError("daemon bearer must remain a SecretBearer")
        if not 1 <= len(display_name) <= 256:
            raise ValueError("private display name is incompatible")
        self._instance = instance
        self._display_name = display_name
        self._client = client or httpx.AsyncClient(
            base_url=_LOOPBACK_ORIGIN,
            timeout=httpx.Timeout(5.0, read=None),
            trust_env=False,
        )
        if str(self._client.base_url).rstrip("/") != _LOOPBACK_ORIGIN:
            raise ValueError("private daemon client must use the fixed loopback origin")
        self._auth = _LoopbackDaemonAuth(daemon_bearer)
        self._encoded_hive = quote(instance.primary_hive_id, safe="-._~")

    async def online(self) -> bool:
        try:
            response = await self._client.get("/health")
            value = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return (
            response.status_code == 200
            and isinstance(value, dict)
            and value.get("status") == "live"
            and value.get("ready") is True
        )

    async def snapshot(self) -> Mapping[str, object]:
        try:
            response = await self._client.get(
                f"/api/v1/hives/{self._encoded_hive}/snapshot-with-work-items", auth=self._auth
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailable from exc
        if response.status_code == 404:
            raise SourceNotFound
        if response.status_code != 200:
            raise SourceUnavailable
        try:
            payload = _strict_json_object(response.content)
            daemon_contract.RemoteHiveSnapshotResponse.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise SourceUnavailable from exc
        hive = payload.get("hive")
        if not isinstance(hive, dict) or hive.get("prefix") != self._instance.primary_hive_id:
            raise SourceUnavailable
        return payload

    async def work_items(
        self,
        *,
        view: str,
        limit: int,
        cursor: str | None,
        priorities: tuple[str, ...],
        labels: tuple[str, ...],
        assignee: str | None,
        issue_type: str | None,
        parent: str | None,
    ) -> Mapping[str, object]:
        params: list[tuple[str, str]] = [("queue", view), ("limit", str(limit))]
        params.extend(("priority", value) for value in priorities)
        params.extend(("label", value) for value in labels)
        for name, value in (("assignee", assignee), ("type", issue_type), ("parent", parent)):
            if value is not None:
                params.append((name, value))
        if cursor is not None:
            params.append(("cursor", cursor))
        try:
            response = await self._client.get(
                f"/api/v1/hives/{self._encoded_hive}/work-item-pages",
                params=params,
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailable from exc
        if response.status_code == 404:
            raise SourceNotFound
        if response.status_code == 409:
            raise ResnapshotRequired
        if response.status_code == 413:
            raise WorkItemPageTooLarge
        if response.status_code != 200:
            raise SourceUnavailable
        try:
            payload = _strict_json_object(response.content)
            validated = daemon_contract.RemoteWorkItemQueue.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise SourceUnavailable from exc
        if (
            validated.hive_id != self._instance.primary_hive_id
            or validated.queue != view
            or validated.limit != limit
            or validated.filters.priorities != priorities
            or validated.filters.labels != labels
            or validated.filters.assignee != assignee
            or validated.filters.type != issue_type
            or validated.filters.parent != parent
        ):
            raise SourceUnavailable
        return validated.to_wire()

    async def work_item_detail(self, *, bead_id: str) -> Mapping[str, object]:
        encoded_bead = quote(bead_id, safe="-._~")
        try:
            response = await self._client.get(
                f"/api/v1/hives/{self._encoded_hive}/work-item-details/{encoded_bead}",
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            raise SourceUnavailable from exc
        if response.status_code == 404:
            raise SourceNotFound
        if response.status_code == 413:
            raise WorkItemDetailTooLarge
        if response.status_code != 200:
            raise SourceUnavailable
        try:
            payload = _strict_json_object(response.content)
            validated = daemon_contract.RemoteWorkItemDetail.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise SourceUnavailable from exc
        if validated.hive_id != self._instance.primary_hive_id or validated.item.id != bead_id:
            raise SourceUnavailable
        return validated.to_wire()

    async def directory(self, *, limit: int, cursor: str | None) -> Mapping[str, object]:
        snapshot = await self.snapshot()
        revision = snapshot.get("revision")
        generated_at = snapshot.get("generatedAt")
        if not isinstance(revision, str) or not revision or type(generated_at) is not int:
            raise SourceUnavailable
        return {
            "generatedAt": generated_at,
            "revision": revision,
            "items": []
            if cursor is not None
            else [
                {
                    "hiveId": self._instance.primary_hive_id,
                    "displayName": self._display_name,
                    "availability": "available",
                    "coverage": "complete",
                    "asOf": generated_at,
                }
            ][:limit],
            "nextCursor": None,
        }

    async def events(self, *, subscription: str, after: str | None) -> AsyncIterator[bytes]:
        params: dict[str, str] = {"subscription": subscription}
        if after is not None:
            params["cursor"] = after
        context = self._client.stream(
            "GET",
            f"/api/v1/hives/{self._encoded_hive}/events",
            params=params,
            auth=self._auth,
        )
        try:
            response = await context.__aenter__()
        except httpx.HTTPError as exc:
            raise SourceUnavailable from exc
        if response.status_code == 409:
            await context.__aexit__(None, None, None)
            raise ResnapshotRequired
        if response.status_code == 404:
            await context.__aexit__(None, None, None)
            raise SourceNotFound
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if response.status_code != 200 or media_type != "text/event-stream":
            await context.__aexit__(None, None, None)
            raise SourceUnavailable

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    if chunk:
                        yield chunk
            except httpx.HTTPError:
                return
            finally:
                await context.__aexit__(None, None, None)

        return stream()

    async def refresh(self, *, expected_revision: str, correlation_id: str) -> Mapping[str, object]:
        del correlation_id
        snapshot = await self.snapshot()
        if snapshot.get("revision") != expected_revision:
            raise ResnapshotRequired
        return snapshot

    async def close(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class PrivateFrameBridgeConfig:
    """Fixed Factory identity and capacity; callers cannot choose any of these fields."""

    verifier_store: GatewayVerifierStore
    instance: RegisteredInstance = field(default_factory=RegisteredInstance)
    host_id: str = "factory"
    host_epoch: str = field(default_factory=lambda: str(uuid.uuid4()))
    admission_limit: int = 32

    def __post_init__(self) -> None:
        if _OPAQUE_ID.fullmatch(self.host_id) is None:
            raise ValueError("private host ID is incompatible")
        try:
            epoch = uuid.UUID(self.host_epoch)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("private host epoch is incompatible") from exc
        if str(epoch) != self.host_epoch.lower() or not 1 <= self.admission_limit <= 64:
            raise ValueError("private Frame Bridge configuration is incompatible")

    def registration(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "contractVersion": UPSTREAM_CONTRACT,
            "hostId": self.host_id,
            "hostEpoch": self.host_epoch,
            "instances": [self.instance.to_wire()],
        }

    def registration_digest(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_json(self.registration())).hexdigest()


@dataclass(frozen=True)
class _AttestedRequest:
    request_id: str
    principal_sub: str


class _ReplayCache:
    def __init__(self) -> None:
        self._accepted: dict[tuple[str, str], float] = {}

    def accept(self, issuer: str, jti: str, expires_at: int, *, now: float) -> None:
        self._accepted = {key: expiry for key, expiry in self._accepted.items() if expiry > now}
        key = (issuer, jti)
        if key in self._accepted:
            raise PrivateRequestError("upstream_authentication_failed", 401, False)
        self._accepted[key] = expires_at + 30


class _Admission:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    def acquire(self) -> Callable[[], None] | None:
        if self._active >= self._limit:
            return None
        self._active += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._active -= 1

        return release


@dataclass(frozen=True)
class _RefreshReceipt:
    input_digest: str
    expires_at: float
    snapshot: Mapping[str, object]


class _RefreshReceipts:
    def __init__(self) -> None:
        self._items: dict[str, _RefreshReceipt] = {}

    def get(self, key: str, body: bytes, *, now: float) -> Mapping[str, object] | None:
        self._items = {item: value for item, value in self._items.items() if value.expires_at > now}
        receipt = self._items.get(key)
        if receipt is None:
            return None
        if receipt.input_digest != hashlib.sha256(body).hexdigest():
            raise IdempotencyConflict
        return receipt.snapshot

    def remember(
        self, key: str, body: bytes, snapshot: Mapping[str, object], *, now: float
    ) -> None:
        self._items[key] = _RefreshReceipt(
            input_digest=hashlib.sha256(body).hexdigest(),
            expires_at=now + 600,
            snapshot=snapshot,
        )


def _header_values(request: Request, name: bytes) -> list[str]:
    values: list[str] = []
    for raw_name, raw_value in request.scope["headers"]:
        if raw_name.lower() == name:
            try:
                values.append(raw_value.decode("ascii"))
            except UnicodeDecodeError:
                values.append("")
    return values


def _request_id(request: Request) -> str:
    values = _header_values(request, b"x-beadhive-request-id")
    if len(values) == 1 and _REQUEST_ID.fullmatch(values[0]) is not None:
        return values[0]
    return _DEFAULT_REQUEST_ID


def _private_error(request_id: str, failure: PrivateRequestError) -> JSONResponse:
    messages = {
        "invalid_request": "The private request is invalid.",
        "upstream_authentication_failed": "Gateway attestation failed.",
        "upstream_authorization_failed": "Gateway request is not authorized.",
        "source_not_found": "The configured source was not found.",
        "work_items_page_too_large": "The requested work-items page is too large.",
        "work_item_detail_too_large": "The exact work-item detail is too large.",
        "registration_mismatch": "The private registration does not match.",
        "resnapshot_required": "A fresh snapshot is required.",
        "idempotency_conflict": "The refresh receipt conflicts.",
        "upstream_busy": "The private source is busy.",
        "source_unavailable": "The private source is unavailable.",
        "upstream_timeout": "The private source timed out.",
        "upstream_internal": "The private source failed.",
    }
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if failure.code == "upstream_busy":
        headers["Retry-After"] = "1"
    return JSONResponse(
        {
            "schemaVersion": 1,
            "contractVersion": UPSTREAM_CONTRACT,
            "requestId": request_id,
            "error": {
                "code": failure.code,
                "message": messages[failure.code],
                "retryable": failure.retryable,
            },
        },
        status_code=failure.status_code,
        headers=headers,
    )


def _json(payload: Mapping[str, object], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _canonical_target(request: Request) -> tuple[str, list[tuple[str, str]]]:
    raw_path = request.scope.get("raw_path", b"")
    raw_query = request.scope.get("query_string", b"")
    if not isinstance(raw_path, bytes) or not isinstance(raw_query, bytes):
        raise PrivateRequestError("invalid_request", 400, False)
    try:
        path = raw_path.decode("ascii")
        query = raw_query.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PrivateRequestError("invalid_request", 400, False) from exc
    if (
        len(path) > 2048
        or len(query.encode("utf-8")) > _MAX_QUERY_BYTES
        or not path.startswith(_PREFIX + "/")
    ):
        raise PrivateRequestError("invalid_request", 400, False)
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
    except ValueError as exc:
        raise PrivateRequestError("invalid_request", 400, False) from exc
    ordered = sorted(enumerate(pairs), key=lambda item: (item[1][0].encode("utf-8"), item[0]))
    canonical_query = "&".join(
        f"{quote(key, safe='-._~')}={quote(value, safe='-._~')}" for _, (key, value) in ordered
    )
    if query != canonical_query or "+" in query:
        raise PrivateRequestError("invalid_request", 400, False)
    return path + ("?" + query if query else ""), pairs


class _PrivateUpstreamApplication:
    def __init__(
        self,
        *,
        config: PrivateFrameBridgeConfig,
        source: FrameBridgeUpstreamSource,
        now: Callable[[], float],
    ) -> None:
        self._config = config
        self._source = source
        self._now = now
        self._replays = _ReplayCache()
        self._admission = _Admission(config.admission_limit)
        self._receipts = _RefreshReceipts()

    async def dispatch(self, request: Request) -> Response:
        request_id = _request_id(request)
        try:
            target, query = _canonical_target(request)
            if target == f"{_PREFIX}/livez":
                self._reject_public_headers(request)
                if request.method != "GET" or query:
                    raise PrivateRequestError("invalid_request", 400, False)
                return _json({"status": "live"})
            scope, operation, maximum_deadline_ms = self._operation_for(request, target, query)
            attested = self._authenticate(request, target, scope)
            deadline_ms = self._deadline(request, maximum_deadline_ms)
            release = self._admission.acquire()
            if release is None:
                raise PrivateRequestError("upstream_busy", 429, True)
            if operation == "events":
                return await self._events(
                    request,
                    deadline_ms=deadline_ms,
                    query=query,
                    release=release,
                )
            try:
                async with asyncio.timeout(deadline_ms / 1000):
                    return await self._one_shot(
                        request,
                        request_id=attested.request_id,
                        operation=operation,
                        query=query,
                    )
            except TimeoutError:
                raise PrivateRequestError("upstream_timeout", 504, True) from None
            finally:
                release()
        except PrivateRequestError as failure:
            return _private_error(request_id, failure)
        except SourceNotFound:
            return _private_error(request_id, PrivateRequestError("source_not_found", 404, False))
        except SourceUnavailable:
            return _private_error(request_id, PrivateRequestError("source_unavailable", 503, True))
        except ResnapshotRequired:
            return _private_error(
                request_id, PrivateRequestError("resnapshot_required", 409, False)
            )
        except WorkItemDetailTooLarge:
            return _private_error(
                request_id, PrivateRequestError("work_item_detail_too_large", 413, False)
            )
        except WorkItemPageTooLarge:
            return _private_error(
                request_id, PrivateRequestError("work_items_page_too_large", 413, False)
            )
        except IdempotencyConflict:
            return _private_error(
                request_id, PrivateRequestError("idempotency_conflict", 409, False)
            )
        except Exception:
            return _private_error(request_id, PrivateRequestError("upstream_internal", 500, False))

    def _reject_public_headers(self, request: Request) -> None:
        for raw_name, _ in request.scope["headers"]:
            name = raw_name.lower()
            if (
                name == b"origin"
                or name == b"forwarded"
                or name == b"x-original-url"
                or name == b"x-original-host"
                or name.startswith(b"x-forwarded-")
            ):
                raise PrivateRequestError("invalid_request", 400, False)
            if name.startswith(b"x-beadhive-") and name not in {
                b"x-beadhive-upstream-contract",
                b"x-beadhive-request-id",
                b"x-beadhive-deadline-ms",
            }:
                raise PrivateRequestError("invalid_request", 400, False)

    def _operation_for(
        self, request: Request, target: str, query: list[tuple[str, str]]
    ) -> tuple[str, str, int]:
        path = target.partition("?")[0]
        instance = self._config.instance.instance_id.replace("/", "%2F")
        hive = self._config.instance.primary_hive_id.replace("/", "%2F")
        expected: tuple[str, str, str, int] | None = None
        if path == f"{_PREFIX}/readyz":
            expected = ("upstream:health", "readyz", "GET", 1_000)
        elif path == f"{_PREFIX}/registration":
            expected = ("upstream:registration", "registration", "GET", 1_000)
        elif path == f"{_PREFIX}/instances/{instance}/hives":
            self._directory_query(query)
            expected = ("upstream:read", "directory", "GET", 5_000)
        elif path == f"{_PREFIX}/instances/{instance}/hives/{hive}/snapshot":
            if query:
                raise PrivateRequestError("invalid_request", 400, False)
            expected = ("upstream:read", "snapshot", "GET", 5_000)
        elif path == f"{_PREFIX}/instances/{instance}/hives/{hive}/work-items":
            self._work_items_query(query)
            expected = ("upstream:read", "work-items", "GET", 5_000)
        elif path.startswith(f"{_PREFIX}/instances/{instance}/hives/{hive}/work-items/"):
            bead_id = path.rsplit("/", 1)[-1]
            if query or _WORK_ITEM_ID.fullmatch(bead_id) is None:
                raise PrivateRequestError("invalid_request", 400, False)
            expected = ("upstream:read", "work-item-detail", "GET", 5_000)
        elif path == f"{_PREFIX}/instances/{instance}/hives/{hive}/events":
            self._events_query(request, query)
            expected = ("upstream:events", "events", "GET", 30_000)
        elif path == f"{_PREFIX}/instances/{instance}/hives/{hive}/refresh":
            if query:
                raise PrivateRequestError("invalid_request", 400, False)
            expected = ("upstream:refresh", "refresh", "POST", 10_000)
        if expected is None or request.method != expected[2]:
            raise PrivateRequestError("invalid_request", 400, False)
        return expected[0], expected[1], expected[3]

    @staticmethod
    def _directory_query(query: list[tuple[str, str]]) -> None:
        if not query or len(query) > 2 or {key for key, _ in query} - {"limit", "cursor"}:
            raise PrivateRequestError("invalid_request", 400, False)
        values = dict(query)
        if len(values) != len(query):
            raise PrivateRequestError("invalid_request", 400, False)
        limit = values.get("limit", "50")
        if not limit.isascii() or not limit.isdecimal() or str(int(limit)) != limit:
            raise PrivateRequestError("invalid_request", 400, False)
        if not 1 <= int(limit) <= 200:
            raise PrivateRequestError("invalid_request", 400, False)
        cursor = values.get("cursor")
        if cursor is not None and not 1 <= len(cursor) <= 2048:
            raise PrivateRequestError("invalid_request", 400, False)

    @staticmethod
    def _events_query(request: Request, query: list[tuple[str, str]]) -> None:
        if not query or len(query) > 2 or {key for key, _ in query} - {"subscription", "after"}:
            raise PrivateRequestError("invalid_request", 400, False)
        values = dict(query)
        if len(values) != len(query) or not 1 <= len(values.get("subscription", "")) <= 512:
            raise PrivateRequestError("invalid_request", 400, False)
        after = values.get("after")
        if after is not None and not 3 <= len(after) <= 512:
            raise PrivateRequestError("invalid_request", 400, False)
        last_event = _header_values(request, b"last-event-id")
        if len(last_event) > 1 or (last_event and after is not None and last_event[0] != after):
            raise PrivateRequestError("invalid_request", 400, False)

    @staticmethod
    def _work_items_query(query: list[tuple[str, str]]) -> dict[str, object]:
        allowed = {"view", "limit", "cursor", "priority", "label", "assignee", "type", "parent"}
        if not query or {key for key, _ in query} - allowed:
            raise PrivateRequestError("invalid_request", 400, False)
        grouped: dict[str, list[str]] = {}
        for key, value in query:
            grouped.setdefault(key, []).append(value)
        if any(
            len(grouped.get(name, ())) > 1
            for name in ("view", "limit", "cursor", "assignee", "type", "parent")
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        view = grouped.get("view", [""])[0]
        if view not in operator_work_items.QUEUES:
            raise PrivateRequestError("invalid_request", 400, False)
        raw_limit = grouped.get("limit", [str(operator_work_items.DEFAULT_LIMIT)])[0]
        if not raw_limit.isascii() or not raw_limit.isdecimal() or str(int(raw_limit)) != raw_limit:
            raise PrivateRequestError("invalid_request", 400, False)
        limit = int(raw_limit)
        if not 1 <= limit <= operator_work_items.MAX_LIMIT:
            raise PrivateRequestError("invalid_request", 400, False)
        priorities = grouped.get("priority", [])
        if (
            len(priorities) > operator_work_items.MAX_PRIORITIES
            or any(re.fullmatch(r"P[0-4]", value) is None for value in priorities)
            or priorities != sorted(set(priorities))
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        labels = grouped.get("label", [])
        if (
            len(labels) > operator_work_items.MAX_LABEL_FILTERS
            or labels != sorted(set(labels))
            or any(
                not value or len(value.encode("utf-8")) > operator_work_items.MAX_LABEL_FILTER_BYTES
                for value in labels
            )
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        cursor = grouped.get("cursor", [None])[0]
        if cursor is not None and (
            not cursor or len(cursor.encode("utf-8")) > operator_work_items.MAX_CURSOR_BYTES
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        scalars = {name: grouped.get(name, [None])[0] for name in ("assignee", "type", "parent")}
        if any(
            value is not None
            and (
                not value
                or len(value.encode("utf-8")) > operator_work_items.MAX_SCALAR_FILTER_BYTES
            )
            for value in scalars.values()
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        return {
            "view": view,
            "limit": limit,
            "cursor": cursor,
            "priorities": tuple(priorities),
            "labels": tuple(labels),
            "assignee": scalars["assignee"],
            "issue_type": scalars["type"],
            "parent": scalars["parent"],
        }

    @staticmethod
    def _deadline(request: Request, maximum: int) -> int:
        values = _header_values(request, b"x-beadhive-deadline-ms")
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise PrivateRequestError("invalid_request", 400, False)
        deadline = int(values[0])
        if not 1 <= deadline <= maximum or str(deadline) != values[0]:
            raise PrivateRequestError("invalid_request", 400, False)
        return deadline

    def _authenticate(self, request: Request, target: str, expected_scope: str) -> _AttestedRequest:
        self._reject_public_headers(request)
        if not self._config.verifier_store.ready:
            raise PrivateRequestError("source_unavailable", 503, True)
        contract = _header_values(request, b"x-beadhive-upstream-contract")
        request_ids = _header_values(request, b"x-beadhive-request-id")
        authorization = _header_values(request, b"authorization")
        if (
            contract != [UPSTREAM_CONTRACT]
            or len(request_ids) != 1
            or _REQUEST_ID.fullmatch(request_ids[0]) is None
            or len(authorization) != 1
            or not authorization[0].startswith("Bearer ")
            or authorization[0].strip() != authorization[0]
        ):
            raise PrivateRequestError("upstream_authentication_failed", 401, False)
        token = authorization[0].removeprefix("Bearer ")
        if not token or len(token) > 16_384:
            raise PrivateRequestError("upstream_authentication_failed", 401, False)
        try:
            segments = token.split(".")
            if len(segments) != 3 or any(not item for item in segments):
                raise ValueError
            header = _strict_json_object(_base64url_bytes(segments[0]))
            if (
                set(header) != {"alg", "kid", "typ"}
                or header.get("alg") != "EdDSA"
                or header.get("typ") != "JWT"
            ):
                raise ValueError
            kid = header.get("kid")
            if not isinstance(kid, str) or _KID.fullmatch(kid) is None:
                raise ValueError
            now = self._now()
            verifier = self._config.verifier_store.active.verifier_for(kid, now=now)
            jws.deserialize_compact(token, verifier.key, algorithms=["EdDSA"])
            claims = _strict_json_object(_base64url_bytes(segments[1]))
        except PrivateRequestError:
            raise
        except Exception as exc:
            raise PrivateRequestError("upstream_authentication_failed", 401, False) from exc
        required = {
            "iss",
            "sub",
            "aud",
            "iat",
            "nbf",
            "exp",
            "jti",
            "request_id",
            "method",
            "target_sha256",
            "host_id",
            "instance_id",
            "factory_id",
            "principal_sub",
            "scope",
        }
        if set(claims) != required:
            raise PrivateRequestError("upstream_authentication_failed", 401, False)
        try:
            issuer = claims["iss"]
            issued_at, not_before, expires_at = claims["iat"], claims["nbf"], claims["exp"]
            jti = claims["jti"]
            principal = claims["principal_sub"]
            if (
                issuer != verifier.issuer
                or claims["sub"] != issuer
                or claims["aud"] != UPSTREAM_CONTRACT
                or any(type(item) is not int for item in (issued_at, not_before, expires_at))
                or not_before < issued_at - 2
                or not_before > issued_at
                or not 0 < expires_at - issued_at <= 10
                or now > expires_at + 2
                or now < not_before - 2
                or not isinstance(jti, str)
                or _JTI.fullmatch(jti) is None
                or len(_base64url_bytes(jti)) < 16
                or claims["request_id"] != request_ids[0]
                or claims["method"] != request.method
                or claims["target_sha256"] != hashlib.sha256(target.encode("ascii")).hexdigest()
                or claims["host_id"] != self._config.host_id
                or claims["instance_id"] != self._config.instance.instance_id
                or claims["factory_id"] != self._config.instance.factory_id
                or claims["scope"] != expected_scope
                or not isinstance(principal, str)
                or (
                    principal != "system:gateway-health" and _PRINCIPAL.fullmatch(principal) is None
                )
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PrivateRequestError("upstream_authorization_failed", 403, False) from None
        self._replays.accept(issuer, jti, expires_at, now=now)
        return _AttestedRequest(request_ids[0], principal)

    async def _one_shot(
        self,
        request: Request,
        *,
        request_id: str,
        operation: str,
        query: list[tuple[str, str]],
    ) -> Response:
        del request_id
        if operation == "registration":
            return _json(self._config.registration())
        if operation == "readyz":
            if not await self._source.online():
                raise SourceUnavailable
            return _json(
                {
                    "schemaVersion": 1,
                    "contractVersion": UPSTREAM_CONTRACT,
                    "status": "ready",
                    "hostId": self._config.host_id,
                    "hostEpoch": self._config.host_epoch,
                    "registrationDigest": self._config.registration_digest(),
                    "verifierSetDigest": self._config.verifier_store.active.digest,
                    "sourceStatus": "ready",
                }
            )
        if operation == "directory":
            values = dict(query)
            page = await self._source.directory(
                limit=int(values.get("limit", "50")), cursor=values.get("cursor")
            )
            return _json(
                {
                    "schemaVersion": 1,
                    "hostId": self._config.host_id,
                    "generatedAt": page["generatedAt"],
                    "revision": page["revision"],
                    "items": page["items"],
                    "nextCursor": page["nextCursor"],
                }
            )
        if operation == "snapshot":
            return _json(dict(await self._source.snapshot()))
        if operation == "work-items":
            normalized = self._work_items_query(query)
            page = daemon_contract.RemoteWorkItemQueue.model_validate(
                await self._source.work_items(**normalized)
            ).to_wire()
            view = page.pop("queue")
            return _json({**page, "view": view})
        if operation == "work-item-detail":
            bead_id = request.url.path.rsplit("/", 1)[-1]
            detail = daemon_contract.RemoteWorkItemDetail.model_validate(
                await self._source.work_item_detail(bead_id=bead_id)
            ).to_wire()
            return _json(detail)
        if operation == "refresh":
            return await self._refresh(request)
        raise AssertionError("unreachable private operation")

    async def _refresh(self, request: Request) -> Response:
        content_type = _header_values(request, b"content-type")
        if len(content_type) != 1 or content_type[0].split(";", 1)[0].strip() != "application/json":
            raise PrivateRequestError("invalid_request", 400, False)
        body = await request.body()
        try:
            value = _strict_json_object(body)
        except ValueError:
            raise PrivateRequestError("invalid_request", 400, False) from None
        if set(value) != {"schemaVersion", "expectedRevision", "correlationId"}:
            raise PrivateRequestError("invalid_request", 400, False)
        expected = value.get("expectedRevision")
        correlation = value.get("correlationId")
        if (
            value.get("schemaVersion") != 1
            or not isinstance(expected, str)
            or not 1 <= len(expected) <= 512
            or not isinstance(correlation, str)
            or not 1 <= len(correlation) <= 128
            or re.fullmatch(r"[A-Za-z0-9._~-]+", correlation) is None
        ):
            raise PrivateRequestError("invalid_request", 400, False)
        keys = _header_values(request, b"idempotency-key")
        if keys != [correlation]:
            raise PrivateRequestError("invalid_request", 400, False)
        now = self._now()
        snapshot = self._receipts.get(correlation, body, now=now)
        if snapshot is None:
            snapshot = await self._source.refresh(
                expected_revision=expected, correlation_id=correlation
            )
            self._receipts.remember(correlation, body, snapshot, now=now)
        return _json({"schemaVersion": 1, "correlationId": correlation, "snapshot": snapshot})

    async def _events(
        self,
        request: Request,
        *,
        deadline_ms: int,
        query: list[tuple[str, str]],
        release: Callable[[], None],
    ) -> Response:
        values = dict(query)
        after = values.get("after")
        last_event = _header_values(request, b"last-event-id")
        if after is None and last_event:
            after = last_event[0]
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                source_stream = await self._source.events(
                    subscription=values["subscription"], after=after
                )
        except TimeoutError:
            release()
            raise PrivateRequestError("upstream_timeout", 504, True) from None
        except BaseException:
            release()
            raise

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in source_stream:
                    yield chunk
            finally:
                release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-transform",
                "X-Content-Type-Options": "nosniff",
            },
        )


def build_private_frame_bridge_application(
    *,
    config: PrivateFrameBridgeConfig,
    source: FrameBridgeUpstreamSource,
    now: Callable[[], float] = time.time,
) -> Starlette:
    """Build the private app only; Factory service custody chooses the AF_UNIX listener."""

    implementation = _PrivateUpstreamApplication(config=config, source=source, now=now)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            await source.close()

    async def dispatch(request: Request) -> Response:
        return await implementation.dispatch(request)

    return Starlette(
        routes=[
            Route(_PREFIX, dispatch, methods=_PRIVATE_METHODS),
            Route(f"{_PREFIX}/{{path:path}}", dispatch, methods=_PRIVATE_METHODS),
        ],
        lifespan=lifespan,
    )
