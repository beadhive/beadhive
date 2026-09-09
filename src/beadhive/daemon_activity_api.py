"""Authenticated durable publication adapter for exact outer-run activity."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from pydantic import ValidationError

from . import daemon_activity
from .daemon_auth import AuthenticatedPrincipal
from .daemon_contract import ActivityAppendRequest, ActivityAppendResponse
from .operator_sources import OperatorSourceError, OperatorSources
from .public_readers import Coverage, RunJournalFrame


def _error(code: str, message: str, status_code: int, *, retryable: bool = False):
    return OperatorSourceError(code, message, status_code=status_code, retryable=retryable)


_STORE_ERRORS: dict[str, tuple[int, str, bool]] = {
    "activity_store_unavailable": (
        503,
        "The durable activity store is unavailable.",
        True,
    ),
    "activity_store_schema_unsupported": (
        503,
        "The durable activity store is unavailable.",
        False,
    ),
    "record_too_large": (413, "The activity exceeds the configured size limit.", False),
    "run_unknown": (404, "The exact outer run was not found.", False),
    "run_identity_conflict": (409, "The exact run identity conflicts with prior state.", False),
    "run_identity_mismatch": (409, "The activity names a different outer run.", False),
    "payload_identity_mismatch": (
        409,
        "The activity payload conflicts with the authoritative run identity.",
        False,
    ),
    "payload_identity_noncanonical": (
        400,
        "The activity payload uses a noncanonical identity field.",
        False,
    ),
    "source_identity_mismatch": (
        409,
        "The activity source conflicts with the authoritative publisher identity.",
        False,
    ),
    "idempotency_conflict": (
        409,
        "The idempotency key is already bound to a different activity.",
        False,
    ),
    "activity_expired": (409, "The activity retry window has expired.", False),
    "expiry_window_exceeded": (
        400,
        "The activity retry window exceeds the configured bound.",
        False,
    ),
}


def _store_error(error: daemon_activity.ActivityStoreError) -> OperatorSourceError:
    status, message, retryable = _STORE_ERRORS.get(
        error.code,
        (400, "The activity request is invalid.", False),
    )
    return _error(error.code, message, status, retryable=retryable)


class ActivityPublicationService:
    """Resolve journal identity, then register and durably append in a worker thread."""

    def __init__(self, *, sources: OperatorSources, store: daemon_activity.DurableActivityStore):
        self.sources = sources
        self.store = store

    @staticmethod
    def _request(body: bytes) -> ActivityAppendRequest:
        try:
            return ActivityAppendRequest.model_validate_json(body)
        except (ValidationError, ValueError) as exc:
            raise _error(
                "invalid_activity_schema",
                "The activity body does not match the v1 contract.",
                400,
            ) from exc

    @staticmethod
    def _publisher_identity(principal: AuthenticatedPrincipal) -> tuple[str, str]:
        parts = principal.principal.split(":")
        if len(parts) != 3 or parts[0] != "service" or not all(parts[1:]):
            raise _error(
                "source_identity_mismatch",
                "The activity source conflicts with the authoritative publisher identity.",
                409,
            )
        return parts[1], parts[2]

    @staticmethod
    def _journal_identity(
        run_id: str,
        request: ActivityAppendRequest,
        journal: RunJournalFrame,
        principal: AuthenticatedPrincipal,
    ) -> daemon_activity.ActivityRunIdentity:
        if journal.run_id != run_id:
            raise _error(
                "run_identity_conflict",
                "The authoritative journal names a different outer run.",
                409,
            )
        if journal.coverage is not Coverage.COMPLETE or not journal.records:
            raise _error(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                503,
                retryable=True,
            )
        first = journal.records[0]
        source, seat = ActivityPublicationService._publisher_identity(principal)
        if request.source != source:
            raise _error(
                "source_identity_mismatch",
                "The activity source conflicts with the authoritative publisher identity.",
                409,
            )
        allowed_writers = daemon_activity.source_writers(source)
        writer = next(
            (
                candidate
                for record in reversed(journal.records)
                if isinstance((candidate := record.get("writer")), str)
                and candidate in allowed_writers
            ),
            None,
        )
        if writer is None:
            raise _error(
                "source_identity_mismatch",
                "The activity source conflicts with the authoritative publisher identity.",
                409,
            )
        try:
            return daemon_activity.ActivityRunIdentity(
                run_id=run_id,
                hive_id=first["hive"],
                bead_id=first["bead"],
                seat=seat,
                provider=first["provider"],
                writer=writer,
                source=request.source,
                manifest_digest=first["manifest_digest"],
            )
        except (KeyError, TypeError, daemon_activity.ActivityStoreError) as exc:
            if isinstance(exc, daemon_activity.ActivityStoreError):
                raise _store_error(exc) from exc
            raise _error(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                503,
                retryable=True,
            ) from exc

    @staticmethod
    def _validate_request_identity(
        identity: daemon_activity.ActivityRunIdentity,
        request: ActivityAppendRequest,
    ) -> None:
        payload: Mapping[str, object] = request.payload
        expected = {
            "runId": identity.run_id,
            "hiveId": identity.hive_id,
            "beadId": identity.bead_id,
            "seat": identity.seat,
            "provider": identity.provider,
            "writer": identity.writer,
            "source": identity.source,
            "manifestDigest": identity.manifest_digest,
        }
        if request.source != identity.source:
            raise _error(
                "source_identity_mismatch",
                "The activity source conflicts with the authoritative publisher identity.",
                409,
            )
        if any(name in payload and payload[name] != value for name, value in expected.items()):
            raise _error(
                "payload_identity_mismatch",
                "The activity payload conflicts with the authoritative run identity.",
                409,
            )

    def _publish(
        self, run_id: str, body: bytes, principal: AuthenticatedPrincipal
    ) -> ActivityAppendResponse:
        request = self._request(body)
        if request.run_id != run_id:
            raise _error(
                "run_id_payload_mismatch",
                "The activity payload names a different outer run.",
                409,
            )
        hive, source = self.sources.locate_run(run_id)
        journal = self.sources.read_run(hive, source, run_id)
        identity = self._journal_identity(run_id, request, journal, principal)
        self._validate_request_identity(identity, request)
        try:
            return self.store.append(identity, request, register_unknown=True)
        except daemon_activity.ActivityStoreError as exc:
            raise _store_error(exc) from exc

    async def publish(
        self, run_id: str, body: bytes, principal: AuthenticatedPrincipal
    ) -> ActivityAppendResponse:
        """Do not acknowledge until the synchronous FULL commit or dedupe finishes."""

        return await asyncio.to_thread(self._publish, run_id, body, principal)

    def durable_records(
        self, run_id: str, journal: RunJournalFrame, offset: int = 0
    ) -> tuple[tuple[Mapping[str, object], ...], str | None, bool, int]:
        """Project one bounded committed page into the activity-feed record boundary."""

        try:
            identity, view, complete, total = self.store.read_registered_page(run_id, offset=offset)
        except daemon_activity.ActivityStoreError as exc:
            if exc.code == "run_unknown":
                return (), None, True, 0
            raise _store_error(exc) from exc
        first = journal.records[0]
        expected = (
            run_id,
            first.get("hive"),
            first.get("bead"),
            first.get("provider"),
            first.get("manifest_digest"),
        )
        actual = (
            identity.run_id,
            identity.hive_id,
            identity.bead_id,
            identity.provider,
            identity.manifest_digest,
        )
        if actual != expected:
            raise _error(
                "run_identity_conflict",
                "The durable activity identity conflicts with the authoritative journal.",
                409,
            )
        continuation = next(
            (
                record.get("provider_continuation")
                for record in reversed(journal.records)
                if record.get("provider_continuation") is not None
            ),
            None,
        )
        records = tuple(
            {
                "version": "beadhive.daemon-activity/v1",
                "source_revision": activity.revision,
                "timestamp_ms": activity.occurred_at,
                "run_id": run_id,
                "hive": identity.hive_id,
                "bead": identity.bead_id,
                "driver": first["driver"],
                "provider": identity.provider,
                "manifest_digest": identity.manifest_digest,
                "provider_continuation": continuation,
                "writer": identity.writer,
                "activity": {
                    "kind": activity.kind,
                    "source": activity.source,
                    "seat": identity.seat,
                    "activityId": activity.activity_id,
                    "idempotencyKey": activity.idempotency_key,
                    "payload": activity.payload,
                },
            }
            for activity in view.activities
        )
        return records, view.revision, complete, total


__all__ = ["ActivityPublicationService"]
