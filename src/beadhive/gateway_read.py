"""Reusable ``gateway.read.v1`` read-source port and packaged Development catalog.

The operator-testkit package is an authoring dependency only.  Its generated JSON and manifest
are copied into this package at build time, pinned below, decoded once during application startup,
and then projected exclusively from immutable in-memory values.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Protocol

CONTRACT_VERSION = "gateway.read.v1"
SCHEMA_VERSION = 1
INSTANCE_ID = "dev/demo"
FACTORY_ID = "development"
SELECTED_SCENARIO_ID = "multi-hive"
CATALOG_FILE = "gateway-read-v1-development.json"
MANIFEST_FILE = "gateway-read-v1-development.manifest.json"
PINNED_CATALOG_SHA256 = "c45b4b80553f16d7973ff3758fae866415f624e4083c742c161bdc31fda5991b"
PINNED_MANIFEST_SHA256 = "894c31924af6706fb10191f89383e1439791bb09281c154274575e979dfd0d00"
SCENARIO_IDS = (
    "small",
    "dense",
    "multi-hive",
    "blocked-path",
    "gate-pending",
    "ready-kickoff",
)
_SNAPSHOT_COLLECTIONS = (
    "workItems",
    "dependencies",
    "epics",
    "gates",
    "agents",
    "assignments",
    "schedules",
    "evidence",
    "advertisedActions",
)
_SNAPSHOT_REQUIRED = frozenset(
    {
        "schemaVersion",
        "hive",
        "revision",
        "generatedAt",
        "cursor",
        "coverage",
        *_SNAPSHOT_COLLECTIONS,
    }
)
_EVENT_REQUIRED = frozenset(
    {
        "schemaVersion",
        "hiveId",
        "subscriptionId",
        "producerEpoch",
        "sequence",
        "baseSequence",
        "observedAt",
        "generatedAt",
        "source",
        "revision",
        "entity",
        "payload",
    }
)
_MAX_DOCUMENT_BYTES = 2_000_000
_MAX_COLLECTION_ITEMS = 10_000
_PAGE_CURSOR_TTL_SECONDS = 300


class CatalogValidationError(RuntimeError):
    """The packaged source cannot safely open readiness."""


class ReadSourceNotFound(Exception):
    """The principal cannot see the requested read-plane resource."""


class ReadSourceInvalidRequest(Exception):
    """A read-plane request is malformed."""


class ReadSourceResnapshotRequired(Exception):
    """A page or source cursor cannot be proven continuous."""


class GatewayReadSource(Protocol):
    """Substrate-neutral authenticated rich-read port used by bridge handlers."""

    @property
    def cache_boundary(self) -> str: ...

    async def list_hives(
        self, subject: str, *, limit: int, after: str | None
    ) -> Mapping[str, object]: ...

    async def snapshot(
        self, subject: str, *, factory_id: str, hive_id: str, detail: str
    ) -> Mapping[str, object]: ...

    async def events(
        self,
        subject: str,
        *,
        factory_id: str,
        hive_id: str,
        subscription: str,
        after: str | None,
    ) -> AsyncIterator[Mapping[str, object]]: ...


@dataclass(frozen=True)
class _CatalogHive:
    hive_id: str
    snapshot: Mapping[str, object]
    events: tuple[Mapping[str, object], ...]
    revision: str


@dataclass(frozen=True)
class _CatalogScenario:
    scenario_id: str
    hives: Mapping[str, _CatalogHive]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CatalogValidationError(f"packaged gateway read {label} must be an object")
    return value


def _array(value: object, label: str, *, maximum: int = _MAX_COLLECTION_ITEMS) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise CatalogValidationError(f"packaged gateway read {label} must be a bounded array")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**53 - 1:
        raise CatalogValidationError(f"packaged gateway read {label} must be a safe integer")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CatalogValidationError(f"packaged gateway read {label} must be bounded text")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _replace_hive_identity(value: object, old_hive_id: str, hive_id: str) -> object:
    if isinstance(value, list):
        return [_replace_hive_identity(item, old_hive_id, hive_id) for item in value]
    if isinstance(value, dict):
        projected: dict[str, object] = {}
        for key, item in value.items():
            if key == "hiveId" and item == old_hive_id:
                projected[key] = hive_id
            else:
                projected[key] = _replace_hive_identity(item, old_hive_id, hive_id)
        return projected
    return value


def _require_scoped_hive_ids(value: object, hive_id: str) -> None:
    if isinstance(value, list):
        for item in value:
            _require_scoped_hive_ids(item, hive_id)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "hiveId" and item != hive_id:
                raise CatalogValidationError(
                    "packaged gateway read nested reference is outside its canonical hive"
                )
            _require_scoped_hive_ids(item, hive_id)


def _validate_coverage(value: object, generated_at: int) -> None:
    coverage = _object(value, "snapshot coverage")
    if coverage.get("state") not in {"complete", "partial", "unavailable"}:
        raise CatalogValidationError("packaged gateway read coverage state is incompatible")
    _integer(coverage.get("generatedAt"), "coverage generatedAt")
    sources = _object(coverage.get("sources"), "coverage sources")
    for name, raw in sources.items():
        _text(name, "coverage source name", maximum=128)
        source = _object(raw, "coverage source")
        if source.get("state") not in {"complete", "partial", "unavailable"}:
            raise CatalogValidationError("packaged gateway read source coverage is incompatible")
        for key in ("requested", "returned"):
            if source.get(key) is not None:
                _integer(source.get(key), f"coverage {key}")
        _integer(source.get("fromCache"), "coverage fromCache")
        _integer(source.get("generatedAt"), "coverage source generatedAt")
        provenance = _object(source.get("provenance"), "coverage provenance")
        _text(provenance.get("system"), "coverage provenance system", maximum=256)
        for key in ("instance", "runId", "documentRef"):
            if provenance.get(key) is not None:
                _text(provenance.get(key), f"coverage provenance {key}", maximum=2048)
    if generated_at < coverage["generatedAt"]:  # type: ignore[operator]
        raise CatalogValidationError("packaged gateway read coverage is newer than its snapshot")


def _decode_snapshot(raw: object, scenario_id: str) -> tuple[str, Mapping[str, object]]:
    snapshot = _object(raw, f"{scenario_id} snapshot")
    if not _SNAPSHOT_REQUIRED <= set(snapshot) or snapshot.get("schemaVersion") != SCHEMA_VERSION:
        raise CatalogValidationError(
            f"packaged gateway read {scenario_id} snapshot is incompatible"
        )
    generated_at = _integer(snapshot.get("generatedAt"), "snapshot generatedAt")
    hive = _object(snapshot.get("hive"), "snapshot hive")
    old_hive_id = _text(hive.get("prefix"), "snapshot hive prefix")
    parts = [
        _text(hive.get(key), f"snapshot hive {key}", maximum=128)
        for key in ("provider", "org", "repo")
    ]
    if any("/" in part for part in parts):
        raise CatalogValidationError("packaged gateway read hive identity contains a separator")
    hive_id = "/".join(parts)
    _validate_coverage(snapshot.get("coverage"), generated_at)
    for collection in _SNAPSHOT_COLLECTIONS:
        values = _array(snapshot.get(collection), f"snapshot {collection}")
        if collection == "advertisedActions" and values:
            raise CatalogValidationError("generated gateway source must not advertise interactions")
    cursor = snapshot.get("cursor")
    if cursor is not None:
        cursor_value = _object(cursor, "snapshot cursor")
        _text(cursor_value.get("subscriptionId"), "snapshot subscription")
        _text(cursor_value.get("producerEpoch"), "snapshot producer epoch")
        _integer(cursor_value.get("sequence"), "snapshot sequence")
        _integer(cursor_value.get("observedAt"), "snapshot observedAt")
    projected = _replace_hive_identity(snapshot, old_hive_id, hive_id)
    assert isinstance(projected, dict)
    projected_hive = _object(projected["hive"], "projected snapshot hive")
    projected_hive["prefix"] = hive_id
    _require_scoped_hive_ids(projected, hive_id)
    revision = "sha256:" + _sha256(
        _canonical_bytes(
            {
                "catalog": PINNED_CATALOG_SHA256,
                "scenario": scenario_id,
                "hiveId": hive_id,
                "snapshot": projected,
            }
        )
    )
    projected["revision"] = revision
    return hive_id, projected


def _decode_event(
    raw: object, *, scenario_id: str, old_hive_id: str, hive_id: str
) -> Mapping[str, object]:
    frame = _object(raw, f"{scenario_id} event frame")
    delay = _integer(frame.get("afterMs"), "event delay")
    if delay > 60_000:
        raise CatalogValidationError("packaged gateway read event delay is unbounded")
    event = _object(frame.get("event"), f"{scenario_id} event")
    if not _EVENT_REQUIRED <= set(event) or event.get("schemaVersion") != SCHEMA_VERSION:
        raise CatalogValidationError(f"packaged gateway read {scenario_id} event is incompatible")
    if event.get("hiveId") != old_hive_id:
        raise CatalogValidationError("packaged gateway read event is outside its hive")
    sequence = _integer(event.get("sequence"), "event sequence")
    base_sequence = event.get("baseSequence")
    if base_sequence is not None and _integer(base_sequence, "event base sequence") >= sequence:
        raise CatalogValidationError("packaged gateway read event is not monotonic")
    _integer(event.get("observedAt"), "event observedAt")
    _integer(event.get("generatedAt"), "event generatedAt")
    _text(event.get("subscriptionId"), "event subscription")
    _text(event.get("producerEpoch"), "event producer epoch")
    _text(event.get("revision"), "event revision")
    payload = _object(event.get("payload"), "event payload")
    if payload.get("kind") not in {
        "entity-upsert",
        "entity-remove",
        "invalidate",
        "reset",
        "heartbeat",
    }:
        raise CatalogValidationError("packaged gateway read event kind is incompatible")
    projected = _replace_hive_identity(event, old_hive_id, hive_id)
    assert isinstance(projected, dict)
    _require_scoped_hive_ids(projected, hive_id)
    return projected


def _decode_catalog(
    artifact: bytes, manifest_bytes: bytes, *, expected_manifest_sha256: str
) -> tuple[dict[str, object], str, str]:
    if not artifact or len(artifact) > _MAX_DOCUMENT_BYTES:
        raise CatalogValidationError("packaged gateway read artifact is missing or unbounded")
    if _sha256(manifest_bytes) != expected_manifest_sha256:
        raise CatalogValidationError("packaged gateway read manifest digest does not match its pin")
    try:
        manifest = _object(json.loads(manifest_bytes), "manifest")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogValidationError("packaged gateway read manifest is not UTF-8 JSON") from exc
    expected_manifest_keys = {
        "artifact",
        "byteCount",
        "contractVersion",
        "encoding",
        "environment",
        "eventCount",
        "packageVersions",
        "scenarioNames",
        "selectedScenarioId",
        "schemaVersion",
        "sha256",
        "snapshotCount",
    }
    if set(manifest) != expected_manifest_keys:
        raise CatalogValidationError("packaged gateway read manifest shape is incompatible")
    if (
        manifest.get("schemaVersion") != SCHEMA_VERSION
        or manifest.get("contractVersion") != CONTRACT_VERSION
        or manifest.get("environment") != "Development"
        or manifest.get("artifact") != CATALOG_FILE
        or manifest.get("encoding") != "utf-8"
        or manifest.get("scenarioNames") != list(SCENARIO_IDS)
        or manifest.get("selectedScenarioId") not in SCENARIO_IDS
        or manifest.get("byteCount") != len(artifact)
        or manifest.get("sha256") != PINNED_CATALOG_SHA256
        or _sha256(artifact) != PINNED_CATALOG_SHA256
    ):
        raise CatalogValidationError("packaged gateway read artifact does not match its manifest")
    versions = _object(manifest.get("packageVersions"), "package versions")
    producer_version = _text(
        versions.get("@beadhive/operator-testkit"), "operator-testkit version", maximum=128
    )
    try:
        document = _object(json.loads(artifact), "artifact")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogValidationError("packaged gateway read artifact is not UTF-8 JSON") from exc
    if set(document) != {
        "schemaVersion",
        "contractVersion",
        "environment",
        "capabilities",
        "scenarios",
    }:
        raise CatalogValidationError("packaged gateway read artifact shape is incompatible")
    if (
        document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("contractVersion") != CONTRACT_VERSION
        or document.get("environment") != "Development"
        or document.get("capabilities") != ["snapshot", "event"]
    ):
        raise CatalogValidationError("packaged gateway read artifact version is incompatible")
    selected_scenario_id = _text(
        manifest.get("selectedScenarioId"), "selected scenario", maximum=128
    )
    return document, producer_version, selected_scenario_id


class GeneratedCatalogReadSource:
    """Immutable, startup-decoded source for one server-owned catalog selection."""

    def __init__(
        self,
        artifact: bytes,
        manifest: bytes,
        *,
        authorized_subjects: frozenset[str],
        expected_manifest_sha256: str = PINNED_MANIFEST_SHA256,
        now: Callable[[], float] = time.time,
    ) -> None:
        document, producer_version, selected_scenario_id = _decode_catalog(
            artifact, manifest, expected_manifest_sha256=expected_manifest_sha256
        )
        scenarios_raw = _array(document.get("scenarios"), "scenarios", maximum=len(SCENARIO_IDS))
        if len(scenarios_raw) != len(SCENARIO_IDS):
            raise CatalogValidationError("packaged gateway read catalog is incomplete")
        decoded: dict[str, _CatalogScenario] = {}
        snapshot_count = 0
        event_count = 0
        for expected_id, raw_scenario in zip(SCENARIO_IDS, scenarios_raw, strict=True):
            scenario = _object(raw_scenario, "scenario")
            if (
                set(scenario) != {"name", "snapshots", "events"}
                or scenario.get("name") != expected_id
            ):
                raise CatalogValidationError("packaged gateway read scenario order is incompatible")
            snapshots = _array(scenario.get("snapshots"), f"{expected_id} snapshots", maximum=3)
            if not snapshots:
                raise CatalogValidationError("packaged gateway read scenario has no snapshot")
            hives: dict[str, _CatalogHive] = {}
            old_ids: dict[str, str] = {}
            for raw_snapshot in snapshots:
                hive_id, snapshot = _decode_snapshot(raw_snapshot, expected_id)
                old_id = _object(raw_snapshot, "snapshot")["hive"]
                old_hive_id = _text(_object(old_id, "snapshot hive").get("prefix"), "hive prefix")
                if hive_id in hives or old_hive_id in old_ids:
                    raise CatalogValidationError("packaged gateway read scenario repeats a hive")
                old_ids[old_hive_id] = hive_id
                hives[hive_id] = _CatalogHive(
                    hive_id=hive_id,
                    snapshot=snapshot,
                    events=(),
                    revision=_text(snapshot.get("revision"), "projected revision"),
                )
                snapshot_count += 1
            by_hive: dict[str, list[Mapping[str, object]]] = {hive_id: [] for hive_id in hives}
            for raw_frame in _array(scenario.get("events"), f"{expected_id} events", maximum=1000):
                frame = _object(raw_frame, "event frame")
                event = _object(frame.get("event"), "event")
                old_hive_id = _text(event.get("hiveId"), "event hive")
                hive_id = old_ids.get(old_hive_id)
                if hive_id is None:
                    raise CatalogValidationError("packaged gateway read event has no scenario hive")
                by_hive[hive_id].append(
                    _decode_event(
                        raw_frame,
                        scenario_id=expected_id,
                        old_hive_id=old_hive_id,
                        hive_id=hive_id,
                    )
                )
                event_count += 1
            ordered: dict[str, _CatalogHive] = {}
            for hive_id in sorted(hives):
                hive = hives[hive_id]
                events = tuple(sorted(by_hive[hive_id], key=lambda value: value["sequence"]))
                raw_cursor = hive.snapshot.get("cursor")
                if events and not isinstance(raw_cursor, dict):
                    raise CatalogValidationError(
                        "packaged gateway read events have no snapshot cursor"
                    )
                previous = int(raw_cursor["sequence"]) if isinstance(raw_cursor, dict) else 0
                for event in events:
                    if (
                        event["sequence"] != previous + 1
                        or event["baseSequence"] != previous
                        or event["subscriptionId"] != raw_cursor["subscriptionId"]
                        or event["producerEpoch"] != raw_cursor["producerEpoch"]
                    ):
                        raise CatalogValidationError(
                            "packaged gateway read event script is not snapshot-relative"
                        )
                    previous += 1
                ordered[hive_id] = _CatalogHive(
                    hive_id=hive.hive_id,
                    snapshot=hive.snapshot,
                    events=events,
                    revision=hive.revision,
                )
            decoded[expected_id] = _CatalogScenario(expected_id, ordered)
        manifest_value = json.loads(manifest)
        if (
            manifest_value["snapshotCount"] != snapshot_count
            or manifest_value["eventCount"] != event_count
        ):
            raise CatalogValidationError("packaged gateway read manifest counts are incompatible")
        if selected_scenario_id not in decoded:
            raise CatalogValidationError("packaged gateway read selection is not in the catalog")
        self._scenarios = decoded
        self._selected = decoded[selected_scenario_id]
        self._authorized_subjects = authorized_subjects
        self._producer_version = producer_version
        self._now = now
        self._incarnation = secrets.token_urlsafe(18)
        self._cursor_key = secrets.token_bytes(32)

    @property
    def selected_scenario_id(self) -> str:
        return self._selected.scenario_id

    @property
    def cache_boundary(self) -> str:
        return self._incarnation

    @property
    def scenario_hive_ids(self) -> Mapping[str, tuple[str, ...]]:
        return {name: tuple(scenario.hives) for name, scenario in self._scenarios.items()}

    def _require_scope(self, subject: str, factory_id: str = FACTORY_ID) -> None:
        if subject not in self._authorized_subjects or factory_id != FACTORY_ID:
            raise ReadSourceNotFound

    def _subscription(self, subject: str, hive_id: str) -> str:
        value = f"{self._incarnation}\0{subject}\0{FACTORY_ID}\0{hive_id}\0live".encode()
        return "sub_" + hmac.new(self._cursor_key, value, hashlib.sha256).hexdigest()

    def _page_cursor(self, subject: str, limit: int, offset: int) -> str:
        payload = _canonical_bytes(
            {
                "exp": (int(self._now()) // _PAGE_CURSOR_TTL_SECONDS + 1)
                * _PAGE_CURSOR_TTL_SECONDS,
                "factoryId": FACTORY_ID,
                "generation": self._incarnation,
                "limit": limit,
                "offset": offset,
                "scenarioId": self.selected_scenario_id,
                "subject": subject,
            }
        )
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self._cursor_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def _page_offset(self, subject: str, limit: int, after: str | None) -> int:
        if after is None:
            return 0
        if not 1 <= len(after) <= 2048 or after.count(".") != 1:
            raise ReadSourceResnapshotRequired
        encoded, signature = after.split(".")
        expected = hmac.new(self._cursor_key, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ReadSourceResnapshotRequired
        try:
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReadSourceResnapshotRequired from exc
        if not isinstance(payload, dict) or payload != {
            "exp": payload.get("exp"),
            "factoryId": FACTORY_ID,
            "generation": self._incarnation,
            "limit": limit,
            "offset": payload.get("offset"),
            "scenarioId": self.selected_scenario_id,
            "subject": subject,
        }:
            raise ReadSourceResnapshotRequired
        if type(payload["exp"]) is not int or payload["exp"] < int(self._now()):
            raise ReadSourceResnapshotRequired
        offset = payload["offset"]
        if type(offset) is not int or not 0 <= offset < len(self._selected.hives):
            raise ReadSourceResnapshotRequired
        return offset

    async def list_hives(
        self, subject: str, *, limit: int, after: str | None
    ) -> Mapping[str, object]:
        self._require_scope(subject)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ReadSourceInvalidRequest
        offset = self._page_offset(subject, limit, after)
        hives = tuple(self._selected.hives.values())
        selected = hives[offset : offset + limit]
        items = []
        for hive in selected:
            snapshot = hive.snapshot
            generated_at = _integer(snapshot.get("generatedAt"), "snapshot generatedAt")
            repo = _object(snapshot.get("hive"), "snapshot hive")["repo"]
            items.append(
                {
                    "factoryId": FACTORY_ID,
                    "hiveId": hive.hive_id,
                    "displayName": f"Generated operator demo · {repo}",
                    "sourceMode": "generated",
                    "scenarioId": self.selected_scenario_id,
                    "availability": "online",
                    "freshness": {
                        "state": "fresh",
                        "asOf": generated_at,
                        "expiresAt": None,
                        "detail": "Immutable generated Development artifact.",
                    },
                    "capabilities": ["snapshot", "events"],
                }
            )
        next_offset = offset + len(selected)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "instanceId": INSTANCE_ID,
            "factoryId": FACTORY_ID,
            "detailLevel": "summary",
            "items": items,
            "nextCursor": (
                self._page_cursor(subject, limit, next_offset) if next_offset < len(hives) else None
            ),
        }

    def _hive(self, subject: str, factory_id: str, hive_id: str) -> _CatalogHive:
        self._require_scope(subject, factory_id)
        try:
            return self._selected.hives[hive_id]
        except KeyError as exc:
            raise ReadSourceNotFound from exc

    async def snapshot(
        self, subject: str, *, factory_id: str, hive_id: str, detail: str
    ) -> Mapping[str, object]:
        if detail != "live":
            raise ReadSourceInvalidRequest
        hive = self._hive(subject, factory_id, hive_id)
        snapshot = copy.deepcopy(hive.snapshot)
        generated_at = _integer(snapshot.get("generatedAt"), "snapshot generatedAt")
        raw_cursor = snapshot.get("cursor")
        sequence = 0
        if isinstance(raw_cursor, dict):
            sequence = _integer(raw_cursor.get("sequence"), "snapshot sequence")
        snapshot["cursor"] = {
            "subscriptionId": self._subscription(subject, hive_id),
            "producerEpoch": self._incarnation,
            "sequence": sequence,
            "observedAt": generated_at,
        }
        return {
            "schemaVersion": SCHEMA_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "instanceId": INSTANCE_ID,
            "factoryId": FACTORY_ID,
            "hiveId": hive_id,
            "detailLevel": "live",
            "source": {
                "mode": "generated",
                "revision": hive.revision,
                "generatedAt": generated_at,
                "artifactVersion": SCHEMA_VERSION,
                "provenance": {
                    "system": "@beadhive/operator-testkit",
                    "version": self._producer_version,
                    "scenario": self.selected_scenario_id,
                },
            },
            "snapshot": snapshot,
        }

    async def events(
        self,
        subject: str,
        *,
        factory_id: str,
        hive_id: str,
        subscription: str,
        after: str | None,
    ) -> AsyncIterator[Mapping[str, object]]:
        hive = self._hive(subject, factory_id, hive_id)
        expected_subscription = self._subscription(subject, hive_id)
        if subscription != expected_subscription:
            raise ReadSourceResnapshotRequired
        raw_cursor = hive.snapshot.get("cursor")
        snapshot_sequence = (
            _integer(raw_cursor.get("sequence"), "snapshot sequence")
            if isinstance(raw_cursor, dict)
            else 0
        )
        sequence = snapshot_sequence
        if after is not None:
            if not 1 <= len(after) <= 512 or after.count(":") != 1:
                raise ReadSourceResnapshotRequired
            epoch, raw_sequence = after.rsplit(":", 1)
            if (
                epoch != self._incarnation
                or not raw_sequence.isdigit()
                or str(int(raw_sequence)) != raw_sequence
            ):
                raise ReadSourceResnapshotRequired
            sequence = int(raw_sequence)
        maximum = max((int(event["sequence"]) for event in hive.events), default=snapshot_sequence)
        if not snapshot_sequence <= sequence <= maximum:
            raise ReadSourceResnapshotRequired
        result = []
        previous = sequence
        for raw in hive.events:
            event_sequence = int(raw["sequence"])
            if event_sequence <= sequence:
                continue
            if event_sequence != previous + 1 or raw.get("baseSequence") != previous:
                raise ReadSourceResnapshotRequired
            event = copy.deepcopy(raw)
            event["subscriptionId"] = expected_subscription
            event["producerEpoch"] = self._incarnation
            event["revision"] = hive.revision
            result.append(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "contractVersion": CONTRACT_VERSION,
                    "instanceId": INSTANCE_ID,
                    "factoryId": FACTORY_ID,
                    "hiveId": hive_id,
                    "detailLevel": "live",
                    "event": event,
                }
            )
            previous = event_sequence

        async def stream() -> AsyncIterator[Mapping[str, object]]:
            for envelope in result:
                yield envelope
            # A generated script describes future events, not the lifetime of the
            # subscription.  Keep an exhausted script open so the HTTP boundary can
            # send heartbeats, continuously reauthorize, and cancel it on disconnect
            # or shutdown.
            await asyncio.Event().wait()

        return stream()


def load_packaged_development_source(
    *, authorized_subjects: frozenset[str]
) -> GeneratedCatalogReadSource:
    """Verify and fully decode the package data before the gateway becomes ready."""
    package = resources.files("beadhive").joinpath("catalog")
    source = GeneratedCatalogReadSource(
        package.joinpath(CATALOG_FILE).read_bytes(),
        package.joinpath(MANIFEST_FILE).read_bytes(),
        authorized_subjects=authorized_subjects,
    )
    if source.selected_scenario_id != SELECTED_SCENARIO_ID:
        raise CatalogValidationError("packaged gateway read release selection is incompatible")
    return source
