"""Beadhive Frame Bridge launcher backed by one loopback host daemon."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from joserfc.jwk import KeySet

from . import config as bh_config
from . import daemon_auth, gateway_read, operator_contract, otel
from .frame_bridge import (
    CLOUD_APP_ORIGIN,
    CLOUD_GATEWAY_ORIGIN,
    DEVELOPMENT_INSTANCE_ID,
    DEVELOPMENT_ISSUER,
    LOCAL_DESKTOP_APP_ORIGIN,
    LOCAL_DESKTOP_GATEWAY_ORIGIN,
    LOCAL_DESKTOP_SUBJECT,
    ClerkTokenVerifier,
    DevelopmentFrameBridgeConfig,
    DevelopmentInstanceRegistry,
    RemoteInstance,
    StaleCommandScope,
    StaleEventCursor,
    build_development_frame_bridge_application,
)

APP_ORIGIN = CLOUD_APP_ORIGIN
GATEWAY_ORIGIN = CLOUD_GATEWAY_ORIGIN
AUDIENCE = "beadhive-gateway-dev"
LOOPBACK_ORIGIN = "http://127.0.0.1:8420"
HIVE_ID = "github/beadhive/beadhive"
HIVE_SUBSCRIPTION_ID = operator_contract.hive_subscription_id(HIVE_ID)
_HIVE_PATH = "/api/v1/hives/github%2Fbeadhive%2Fbeadhive"
_SUBJECT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HIVE_ID = re.compile(r"[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+\Z")
_DEMO_STATUSES = frozenset({"open", "in_progress", "blocked"})
_INTERNAL_WORK_ITEM_TYPES = frozenset({"event", "gate"})
SOURCE_MODE_ENV = "BEADHIVE_FRAME_BRIDGE_SOURCE_MODE"
NETWORK_PROFILE_ENV = "BEADHIVE_FRAME_BRIDGE_NETWORK_PROFILE"


def network_origins(profile: str | None) -> tuple[str, str]:
    """Resolve one complete DEV network tuple; never compose origins independently."""
    if profile in {None, "cloud"}:
        return CLOUD_APP_ORIGIN, CLOUD_GATEWAY_ORIGIN
    if profile == "local-desktop":
        return LOCAL_DESKTOP_APP_ORIGIN, LOCAL_DESKTOP_GATEWAY_ORIGIN
    raise RuntimeError(f"{NETWORK_PROFILE_ENV} must be cloud or local-desktop")


class _LoopbackDaemonAuth(httpx.Auth):
    """Reveal the daemon bearer only while authorizing one fixed loopback request."""

    def __init__(self, bearer: daemon_auth.SecretBearer) -> None:
        self._bearer = bearer

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._bearer.reveal_for_authority()}"
        yield request


def _bounded_text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise RuntimeError("operator response is incompatible")
    return value


def _safe_timestamp(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise RuntimeError("operator response is incompatible")
    return value


@dataclass(frozen=True)
class _LiveSnapshotState:
    subscription: str
    producer_epoch: str
    sequence: int
    revision: str


class LoopbackGatewayReadSource:
    """Translate the authenticated host-daemon read API to ``gateway.read.v1``."""

    def __init__(
        self,
        *,
        daemon_bearer: daemon_auth.SecretBearer,
        authorized_subjects: frozenset[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(daemon_bearer, daemon_auth.SecretBearer):
            raise TypeError("daemon_bearer must be a SecretBearer")
        if not authorized_subjects:
            raise ValueError("authorized_subjects must not be empty")
        self._client = client or httpx.AsyncClient(
            base_url=LOOPBACK_ORIGIN,
            timeout=httpx.Timeout(5.0, read=None),
            trust_env=False,
        )
        if str(self._client.base_url).rstrip("/") != LOOPBACK_ORIGIN:
            raise ValueError("Frame Bridge daemon client must use the fixed loopback origin")
        self._daemon_auth = _LoopbackDaemonAuth(daemon_bearer)
        self._authorized_subjects = authorized_subjects
        self._cache_boundary = uuid.uuid4().hex
        self._states: dict[str, _LiveSnapshotState] = {}
        self._state_lock = asyncio.Lock()

    @property
    def cache_boundary(self) -> str:
        return self._cache_boundary

    def _require_scope(self, subject: str, factory_id: str = gateway_read.FACTORY_ID) -> None:
        if subject not in self._authorized_subjects or factory_id != gateway_read.FACTORY_ID:
            raise gateway_read.ReadSourceNotFound

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise gateway_read.ReadSourceNotFound
        if response.status_code == 400:
            raise gateway_read.ReadSourceInvalidRequest
        if response.status_code == 409:
            raise gateway_read.ReadSourceResnapshotRequired
        response.raise_for_status()

    async def list_hives(
        self, subject: str, *, limit: int, after: str | None
    ) -> Mapping[str, object]:
        self._require_scope(subject)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise gateway_read.ReadSourceInvalidRequest
        if after is not None and not 1 <= len(after) <= 2048:
            raise gateway_read.ReadSourceInvalidRequest
        params: dict[str, object] = {"limit": limit}
        if after is not None:
            params["cursor"] = after
        response = await self._client.get(
            "/api/v1/factory/hives",
            params=params,
            auth=self._daemon_auth,
        )
        self._raise_for_status(response)
        page = response.json()
        if not isinstance(page, dict) or page.get("schemaVersion") != 1:
            raise RuntimeError("operator hive directory is incompatible")
        generated_at = _safe_timestamp(page.get("generatedAt"))
        _bounded_text(page.get("revision"), maximum=256)
        raw_items = page.get("items")
        next_cursor = page.get("nextCursor")
        returned_count = page.get("returnedCount")
        reported_limit = page.get("limit")
        truncated = page.get("truncated")
        if (
            not isinstance(raw_items, list)
            or len(raw_items) > limit
            or returned_count != len(raw_items)
            or reported_limit != limit
            or type(truncated) is not bool
            or truncated != (next_cursor is not None)
            or (
                next_cursor is not None
                and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 2048)
            )
        ):
            raise RuntimeError("operator hive directory is incompatible")
        items: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise RuntimeError("operator hive directory is incompatible")
            hive_id = _bounded_text(raw.get("id"))
            if _HIVE_ID.fullmatch(hive_id) is None or hive_id in seen:
                raise RuntimeError("operator hive directory is incompatible")
            seen.add(hive_id)
            display_name = _bounded_text(raw.get("displayLabel"), maximum=256)
            availability = raw.get("availability")
            coverage = raw.get("coverage")
            if not isinstance(availability, dict) or not isinstance(coverage, dict):
                raise RuntimeError("operator hive directory is incompatible")
            availability_state = availability.get("state")
            if availability_state not in {"available", "unavailable"}:
                raise RuntimeError("operator hive directory is incompatible")
            as_of = raw.get("asOf")
            freshness_as_of = generated_at if as_of is None else _safe_timestamp(as_of)
            detail = coverage.get("reason") or availability.get("reason")
            if detail is not None and not isinstance(detail, str):
                raise RuntimeError("operator hive directory is incompatible")
            items.append(
                {
                    "factoryId": gateway_read.FACTORY_ID,
                    "hiveId": hive_id,
                    "displayName": display_name,
                    "sourceMode": "live",
                    "scenarioId": None,
                    "availability": ("online" if availability_state == "available" else "offline"),
                    "freshness": {
                        "state": "fresh" if availability_state == "available" else "unknown",
                        "asOf": freshness_as_of,
                        "expiresAt": None,
                        "detail": detail,
                    },
                    "capabilities": ["snapshot", "events"],
                }
            )
        return {
            "schemaVersion": gateway_read.SCHEMA_VERSION,
            "contractVersion": gateway_read.CONTRACT_VERSION,
            "instanceId": gateway_read.INSTANCE_ID,
            "factoryId": gateway_read.FACTORY_ID,
            "detailLevel": "summary",
            "items": items,
            "nextCursor": next_cursor,
        }

    async def snapshot(
        self, subject: str, *, factory_id: str, hive_id: str, detail: str
    ) -> Mapping[str, object]:
        self._require_scope(subject, factory_id)
        if detail != "live" or _HIVE_ID.fullmatch(hive_id) is None:
            raise gateway_read.ReadSourceInvalidRequest
        encoded_hive = quote(hive_id, safe="-._~")
        response = await self._client.get(
            f"/api/v1/hives/{encoded_hive}/snapshot",
            auth=self._daemon_auth,
        )
        self._raise_for_status(response)
        snapshot = response.json()
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
            raise RuntimeError("operator snapshot is incompatible")
        if set(snapshot) != gateway_read._SNAPSHOT_REQUIRED:
            raise RuntimeError("operator snapshot is incompatible")
        hive = snapshot.get("hive")
        if not isinstance(hive, dict):
            raise RuntimeError("operator snapshot is incompatible")
        identity = "/".join(_bounded_text(hive.get(name)) for name in ("provider", "org", "repo"))
        if identity != hive_id:
            raise RuntimeError("operator snapshot is incompatible")
        for name in gateway_read._SNAPSHOT_COLLECTIONS:
            value = snapshot.get(name)
            if not isinstance(value, list) or len(value) > gateway_read._MAX_COLLECTION_ITEMS:
                raise RuntimeError("operator snapshot is incompatible")
        revision = _bounded_text(snapshot.get("revision"), maximum=256)
        generated_at = _safe_timestamp(snapshot.get("generatedAt"))
        cursor = snapshot.get("cursor")
        if not isinstance(cursor, dict):
            raise RuntimeError("operator snapshot is incompatible")
        subscription = _bounded_text(cursor.get("subscriptionId"))
        producer_epoch = _bounded_text(cursor.get("producerEpoch"))
        sequence = cursor.get("sequence")
        if type(sequence) is not int or not 0 <= sequence <= 2**53 - 1:
            raise RuntimeError("operator snapshot is incompatible")
        _safe_timestamp(cursor.get("observedAt"))
        if not isinstance(snapshot.get("coverage"), dict):
            raise RuntimeError("operator snapshot is incompatible")
        envelope = {
            "schemaVersion": gateway_read.SCHEMA_VERSION,
            "contractVersion": gateway_read.CONTRACT_VERSION,
            "instanceId": gateway_read.INSTANCE_ID,
            "factoryId": gateway_read.FACTORY_ID,
            "hiveId": hive_id,
            "detailLevel": "live",
            "source": {
                "mode": "live",
                "revision": revision,
                "generatedAt": generated_at,
                "artifactVersion": None,
                "provenance": {
                    "system": "beadhive.host-daemon",
                    "version": "1",
                    "scenario": None,
                },
            },
            "snapshot": snapshot,
        }
        async with self._state_lock:
            self._states[hive_id] = _LiveSnapshotState(
                subscription=subscription,
                producer_epoch=producer_epoch,
                sequence=sequence,
                revision=revision,
            )
        return envelope

    async def events(
        self,
        subject: str,
        *,
        factory_id: str,
        hive_id: str,
        subscription: str,
        after: str | None,
    ) -> AsyncIterator[Mapping[str, object]]:
        self._require_scope(subject, factory_id)
        if (
            _HIVE_ID.fullmatch(hive_id) is None
            or not 1 <= len(subscription) <= gateway_read._EVENT_SUBSCRIPTION_MAX_LENGTH
            or (after is not None and not 1 <= len(after) <= gateway_read._EVENT_AFTER_MAX_LENGTH)
        ):
            raise gateway_read.ReadSourceInvalidRequest
        async with self._state_lock:
            state = self._states.get(hive_id)
        if state is None or subscription != state.subscription:
            raise gateway_read.ReadSourceResnapshotRequired
        cursor = after or f"{state.producer_epoch}:{state.sequence}"
        if cursor.count(":") != 1:
            raise gateway_read.ReadSourceResnapshotRequired
        cursor_epoch, raw_cursor_sequence = cursor.rsplit(":", 1)
        if (
            cursor_epoch != state.producer_epoch
            or not raw_cursor_sequence.isdigit()
            or str(int(raw_cursor_sequence)) != raw_cursor_sequence
        ):
            raise gateway_read.ReadSourceResnapshotRequired
        previous_sequence = int(raw_cursor_sequence)
        encoded_hive = quote(hive_id, safe="-._~")
        context = self._client.stream(
            "GET",
            f"/api/v1/hives/{encoded_hive}/events",
            params={"cursor": cursor, "subscription": subscription},
            auth=self._daemon_auth,
        )
        response = await context.__aenter__()
        try:
            self._raise_for_status(response)
        except BaseException:
            await context.__aexit__(None, None, None)
            raise

        async def stream() -> AsyncIterator[Mapping[str, object]]:
            event_id: str | None = None
            event_name: str | None = None
            data_lines: list[str] = []
            previous = previous_sequence

            async def require_current_snapshot() -> None:
                async with self._state_lock:
                    current = self._states.get(hive_id)
                if current != state:
                    raise gateway_read.ReadSourceResnapshotRequired

            def envelope() -> Mapping[str, object] | None:
                nonlocal event_id, event_name, data_lines, previous
                if event_id is None and event_name is None and not data_lines:
                    return None
                if event_id is None or event_name != "operator-event" or not data_lines:
                    raise RuntimeError("operator event stream is incompatible")
                try:
                    event = json.loads("\n".join(data_lines))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise RuntimeError("operator event stream is incompatible") from exc
                if not isinstance(event, dict) or event.get("schemaVersion") != 1:
                    raise RuntimeError("operator event stream is incompatible")
                epoch = _bounded_text(event.get("producerEpoch"))
                sequence = event.get("sequence")
                base_sequence = event.get("baseSequence")
                if (
                    event.get("hiveId") != hive_id
                    or event.get("subscriptionId") != subscription
                    or event_id != f"{epoch}:{sequence}"
                    or epoch != state.producer_epoch
                    or type(sequence) is not int
                    or type(base_sequence) is not int
                    or sequence != previous + 1
                    or base_sequence != previous
                ):
                    raise gateway_read.ReadSourceResnapshotRequired
                previous = sequence
                result = {
                    "schemaVersion": gateway_read.SCHEMA_VERSION,
                    "contractVersion": gateway_read.CONTRACT_VERSION,
                    "instanceId": gateway_read.INSTANCE_ID,
                    "factoryId": gateway_read.FACTORY_ID,
                    "hiveId": hive_id,
                    "detailLevel": "live",
                    "event": event,
                }
                event_id = None
                event_name = None
                data_lines = []
                return result

            try:
                async for line in response.aiter_lines():
                    if line == "":
                        value = envelope()
                        if value is not None:
                            await require_current_snapshot()
                            yield value
                    elif line.startswith(":"):
                        continue
                    elif line.startswith("id: "):
                        event_id = line.removeprefix("id: ")
                    elif line.startswith("event: "):
                        event_name = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        data_lines.append(line.removeprefix("data: "))
                    else:
                        raise RuntimeError("operator event stream is incompatible")
                value = envelope()
                if value is not None:
                    await require_current_snapshot()
                    yield value
            finally:
                await context.__aexit__(None, None, None)

        return stream()


def _remote_cursor(local: Mapping[str, object]) -> str:
    epoch = local.get("producerEpoch")
    sequence = local.get("sequence")
    if not isinstance(epoch, str) or type(sequence) is not int or sequence < 0:
        raise RuntimeError("operator cursor is incompatible")
    return f"{uuid.UUID(hex=epoch)}:{sequence}"


def _local_cursor(remote: str) -> str:
    epoch, sequence = remote.rsplit(":", 1)
    return f"{uuid.UUID(epoch).hex}:{int(sequence)}"


def _development_work_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise RuntimeError("operator work items are incompatible")
    selected: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("record"), Mapping):
            raise RuntimeError("operator work item is incompatible")
        record = item["record"]
        status = record.get("status")
        issue_type = record.get("issueType")
        if not isinstance(status, str) or not isinstance(issue_type, str):
            raise RuntimeError("operator work item is incompatible")
        if status in _DEMO_STATUSES and issue_type not in _INTERNAL_WORK_ITEM_TYPES:
            selected.append(item)
    return selected


class LoopbackDemoRuntime:
    """Redacted async adapter over the real host daemon's exact registered hive."""

    def __init__(
        self,
        *,
        daemon_bearer: daemon_auth.SecretBearer,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(daemon_bearer, daemon_auth.SecretBearer):
            raise TypeError("daemon_bearer must be a SecretBearer")
        self._client = client or httpx.AsyncClient(
            base_url=LOOPBACK_ORIGIN,
            timeout=httpx.Timeout(5.0, read=None),
            trust_env=False,
        )
        if str(self._client.base_url).rstrip("/") != LOOPBACK_ORIGIN:
            raise ValueError("Frame Bridge daemon client must use the fixed loopback origin")
        self._daemon_auth = _LoopbackDaemonAuth(daemon_bearer)

    @property
    def client(self) -> httpx.AsyncClient:
        """Shared daemon transport, closed with the legacy instance runtime."""

        return self._client

    async def online(self) -> bool:
        response = await self._client.get("/health")
        if response.status_code != 200:
            return False
        value = response.json()
        return (
            isinstance(value, dict) and value.get("status") == "live" and value.get("ready") is True
        )

    async def snapshot(self) -> Mapping[str, object]:
        response = await self._client.get(
            f"{_HIVE_PATH}/snapshot",
            auth=self._daemon_auth,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("cursor"), dict):
            raise RuntimeError("operator snapshot is incompatible")
        return {
            "schemaVersion": value.get("schemaVersion"),
            "revision": value.get("revision"),
            "generatedAt": value.get("generatedAt"),
            "workItems": _development_work_items(value.get("workItems")),
            "agents": value.get("agents"),
            "eventCursor": _remote_cursor(value["cursor"]),
        }

    async def refresh(self, expected_revision: str, _correlation_id: str) -> Mapping[str, object]:
        current = await self.snapshot()
        revision = current.get("revision")
        if revision != expected_revision:
            raise StaleCommandScope
        return {"status": "completed", "revision": revision}

    async def events(self, cursor: str) -> AsyncIterator[Mapping[str, object]]:
        context = self._client.stream(
            "GET",
            f"{_HIVE_PATH}/events",
            params={"cursor": _local_cursor(cursor), "subscription": HIVE_SUBSCRIPTION_ID},
            auth=self._daemon_auth,
        )
        response = await context.__aenter__()
        if response.status_code == 409:
            await context.__aexit__(None, None, None)
            raise StaleEventCursor
        if response.status_code != 200:
            await context.__aexit__(None, None, None)
            raise RuntimeError("operator event source is unavailable")

        async def stream() -> AsyncIterator[Mapping[str, object]]:
            event_id: str | None = None
            try:
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        event_id = line.removeprefix("id: ")
                    elif line.startswith("data: "):
                        payload = json.loads(line.removeprefix("data: "))
                        if not isinstance(payload, dict) or event_id is None:
                            raise RuntimeError("operator event is incompatible")
                        current = await self.snapshot()
                        revision = current.get("revision")
                        yield {
                            "cursor": _remote_cursor(
                                {
                                    "producerEpoch": event_id.rsplit(":", 1)[0],
                                    "sequence": int(event_id.rsplit(":", 1)[1]),
                                }
                            ),
                            "revision": revision,
                        }
                        event_id = None
            finally:
                await context.__aexit__(None, None, None)

        return stream()

    async def close(self) -> None:
        await self._client.aclose()


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("Frame Bridge credential files must exist with mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def _authorized_subjects(path: Path) -> frozenset[str]:
    value = _read_json(path)
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or any(not isinstance(item, str) or _SUBJECT.fullmatch(item) is None for item in value)
    ):
        raise RuntimeError("Frame Bridge subject policy is incompatible")
    return frozenset(value)


def create_application():
    """Uvicorn factory that verifies all immutable inputs before opening the listener."""
    credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials"))
    jwks_path = Path(
        os.environ.get("BEADHIVE_FRAME_BRIDGE_JWKS_FILE", credentials / "clerk-jwks.json")
    )
    subjects_path = Path(
        os.environ.get(
            "BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE",
            credentials / "authorized-subjects.json",
        )
    )
    daemon_bearer_path = Path(
        os.environ.get(
            "BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE",
            credentials / "daemon-bearer",
        )
    )
    app_origin, gateway_origin = network_origins(os.environ.get(NETWORK_PROFILE_ENV))
    config = DevelopmentFrameBridgeConfig(
        issuer=DEVELOPMENT_ISSUER,
        audience=AUDIENCE,
        app_origin=app_origin,
        gateway_origin=gateway_origin,
    )
    if config.is_local_desktop:
        authorized_subjects = frozenset({LOCAL_DESKTOP_SUBJECT})
        verifier = None
    else:
        authorized_subjects = _authorized_subjects(subjects_path)
        key_set = KeySet.import_key_set(_read_json(jwks_path))
        verifier = ClerkTokenVerifier(config=config, key=key_set)
    source_mode = os.environ.get(SOURCE_MODE_ENV)
    if source_mode not in {"generated", "live"}:
        raise RuntimeError(f"{SOURCE_MODE_ENV} must be explicitly set to generated or live")
    daemon_bearer = daemon_auth.load_bearer_file(daemon_bearer_path)
    runtime = LoopbackDemoRuntime(daemon_bearer=daemon_bearer)
    if source_mode == "generated":
        read_source = gateway_read.load_packaged_development_source(
            authorized_subjects=authorized_subjects
        )
        experience_source = gateway_read.load_packaged_development_experience_source(
            authorized_subjects=authorized_subjects
        )
    else:
        read_source = LoopbackGatewayReadSource(
            daemon_bearer=daemon_bearer,
            authorized_subjects=authorized_subjects,
            client=runtime.client,
        )
        experience_source = None
    instance = RemoteInstance(
        display_name="Development demo",
        authorized_subjects=authorized_subjects,
        snapshot=runtime.snapshot,
        online=runtime.online,
        refresh=runtime.refresh,
        events=runtime.events,
        close=runtime.close,
    )
    telemetry = None
    try:
        raw_config = bh_config.load()
        otel.init(
            raw_config,
            service_name="bh-frame-bridge",
            enrich_resource=False,
        )
        telemetry = otel.current_semantic_telemetry()
    except BaseException:
        # Frame Bridge correctness and listener construction never depend on observability.
        pass
    app = build_development_frame_bridge_application(
        config=config,
        verifier=verifier,
        registry=DevelopmentInstanceRegistry(instances={DEVELOPMENT_INSTANCE_ID: instance}),
        read_source=read_source,
        experience_source=experience_source,
        telemetry=telemetry,
    )
    app.state.source_mode = source_mode
    return app


def main() -> None:
    """Serve only on loopback; Cloudflared is the sole external transport."""
    import uvicorn

    uvicorn.run(
        "beadhive.frame_bridge_runtime:create_application",
        factory=True,
        host="127.0.0.1",
        port=8787,
        access_log=False,
        server_header=False,
    )
