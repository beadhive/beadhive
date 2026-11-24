"""The SQL adapter of the HQ lifecycle port (bh-v3dyp): today's HQ SQL code behind ``HqPort``.

A thin class over the existing openers (``docs/design/hq-lifecycle-port.md`` section 5). Every
method is one of today's transactions, called unchanged, so each fence, floor check and fixed
message stays where it is; the adapter only (a) maps the port's value objects onto the SQL
tuples and (b) maps the SQL error classes onto the port's one taxonomy. It is the default
route (``hq.route: sql``) and registers itself with :func:`beadhive.hq_port.register_sql_adapter`
on import.

The adapter is built from the validated ``hq.sql`` settings (the same dict
``SqlControlPlane`` holds) and reuses ``SqlControlPlane`` as the single place that constructs
the openers (``_runtime_authority``, ``_operator``, ``config_store``), so the frame-compatibility
guard and the broker/clock wiring are not duplicated.

``SqlControlPlane`` keeps its business rules and is not re-pointed at the port here; the
hq-service adapter is not wired into callers in this bead.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping
from io import StringIO
from typing import Any

from .hq_port import (
    AdmissionRequest,
    EvidenceReport,
    FrameAnchor,
    HqDeadline,
    HqError,
    HqOutcomeUnknown,
    HqRefused,
    HqUnavailable,
    SessionRoute,
    Signer,
    Submission,
    register_sql_adapter,
)

_SHA_PREFIX = "sha256:"


def _classify(error: BaseException) -> HqError | None:
    """Map a SQL-side error onto the port taxonomy, or ``None`` for an unrelated exception.

    Order matters: the specific unknown/deadline/unavailable classes subclass the generic
    ``ValueError`` families, so they are tested before the generic refusal."""
    from .hq_authority_enforce import AuthorityEnforcementError
    from .hq_control_plane import ControlPlaneError, HqLeaseUnknown
    from .hq_grant_rules import GrantRefused
    from .hq_sql_capabilities import CapabilityProbeError
    from .hq_sql_config import (
        PublicationUnknown,
        SqlConfigDeadline,
        SqlConfigError,
        SqlConfigUnavailable,
    )
    from .hq_sql_operator import AuthorityPublicationUnknown, SqlOperatorError
    from .hq_sql_placement import PlacementError, PlacementUnknown
    from .hq_sql_receiver import ReceiverError, ReceiverUnknown
    from .hq_sql_runtime import (
        InboxUnknown,
        SqlRuntimeCapabilityMissing,
        SqlRuntimeDeadline,
        SqlRuntimeError,
        SqlRuntimeFloorRefusal,
        SqlRuntimeUnavailable,
    )
    from .hq_sql_runtime_schema import RuntimeSchemaError
    from .hq_sql_session import SessionError
    from .hq_sql_signatures import SqlSignatureError
    from .hq_sql_transport import SqlTransportError

    if isinstance(error, AuthorityPublicationUnknown):
        return HqOutcomeUnknown(getattr(error, "expected_revision", None), str(error))
    if isinstance(error, PublicationUnknown):
        return HqOutcomeUnknown(getattr(error, "expected_revision", None), str(error))
    if isinstance(error, InboxUnknown):
        return HqOutcomeUnknown(None, str(error))
    if isinstance(error, HqLeaseUnknown):
        return HqOutcomeUnknown(getattr(error, "expected_revision", None), str(error))
    if isinstance(error, ReceiverUnknown):
        return HqOutcomeUnknown(getattr(error, "request_id", None), str(error))
    if isinstance(error, PlacementUnknown):
        return HqOutcomeUnknown(getattr(error, "expected", None), str(error))
    if isinstance(error, SqlRuntimeDeadline | SqlConfigDeadline):
        return HqDeadline(str(error))
    if isinstance(error, SqlRuntimeUnavailable | SqlConfigUnavailable | SqlTransportError):
        return HqUnavailable(str(error))
    if isinstance(error, SqlRuntimeFloorRefusal):
        return HqRefused("restore_floor", str(error))
    if isinstance(error, SqlRuntimeCapabilityMissing):
        return HqRefused("capability_missing", str(error))
    if isinstance(error, SqlOperatorError):
        return HqRefused("operator_refused", str(error))
    if isinstance(error, SqlConfigError):
        return HqRefused("config_refused", str(error))
    if isinstance(error, ControlPlaneError):
        return HqRefused("control_plane_refused", str(error))
    if isinstance(error, GrantRefused):
        return HqRefused(error.code, str(error))
    if isinstance(error, SqlRuntimeError | SessionError):
        return HqRefused("runtime_refused", str(error))
    # Reachability: the placement/receiver/probe/schema/signature errors surface through the
    # runtime and operator openers the adapter delegates to; none is caught by a parent above
    # (they subclass ValueError directly), so each needs its own stable code. Every
    # ValueError family defined in ``hq_sql_*`` is covered here or by a parent class above
    # (see tests/test_hq_sql_adapter.py, which enumerates them).
    if isinstance(error, ReceiverError):
        return HqRefused("receiver_refused", str(error))
    if isinstance(error, PlacementError):
        return HqRefused("placement_refused", str(error))
    if isinstance(error, CapabilityProbeError):
        return HqRefused("capability_probe_failed", str(error))
    if isinstance(error, RuntimeSchemaError):
        return HqRefused("runtime_schema_refused", str(error))
    if isinstance(error, SqlSignatureError):
        return HqRefused("signature_refused", str(error))
    if isinstance(error, AuthorityEnforcementError):
        return HqRefused("authority_enforcement_refused", str(error))
    return None


def _mapped(method: Callable) -> Callable:
    """Run one adapter method, translating SQL errors onto the port taxonomy (chained)."""

    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except HqError:
            raise
        except Exception as error:  # noqa: BLE001 - unrelated errors are re-raised untouched
            translated = _classify(error)
            if translated is None:
                raise
            raise translated from error

    return call


def _binding(anchor: FrameAnchor):
    from .hq_sql_runtime import PrincipalBinding

    return PrincipalBinding(
        principal=anchor.principal,
        frame_id=anchor.frame,
        holder_identity=anchor.holder,
        instance_ref=anchor.instance,
        epoch=anchor.epoch,
        inbox_table=anchor.carrier,
        signer_fingerprint="",
    )


class SqlHqAdapter:
    """``HqPort`` over today's HQ SQL openers."""

    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        broker: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from .hq_control_plane import SqlControlPlane

        self._plane = SqlControlPlane(dict(settings), broker=broker, clock=clock)

    # ---- openers (the existing seams) ----------------------------------------------------

    def _runtime(self):
        return self._plane._runtime_authority()

    def _operator(self):
        return self._plane._operator()

    def _store(self):
        return self._plane.config_store()

    def _session_connection(self, deadline: float):
        from .hq_sql_transport import FnoxBroker, connect

        binding = self._plane.settings["runtime"]
        return (
            connect(binding, self._plane.broker or FnoxBroker(), deadline=deadline),
            binding["database"],
        )

    # ---- HqLiveness ----------------------------------------------------------------------

    @_mapped
    def renew_session(self, anchor: FrameAnchor, *, deadline: float) -> SessionRoute:
        from .hq_sql_session import SessionRenewer

        database = self._plane.settings["runtime"]["database"]
        route = SessionRenewer(
            lambda: self._session_connection(deadline)[0], database=database
        ).tick()
        return SessionRoute(principal=route.principal, epoch=route.epoch)

    @_mapped
    def publish_evidence(
        self, anchor: FrameAnchor, report: EvidenceReport, *, deadline: float
    ) -> SessionRoute:
        from .hq_sql_session import EvidenceReport as SqlEvidenceReport
        from .hq_sql_session import publish_evidence

        payload = dict(report.payload)
        connection, database = self._session_connection(deadline)
        try:
            route = publish_evidence(
                connection,
                SqlEvidenceReport(
                    epoch=int(payload["epoch"]),
                    release_id=str(payload["release_id"]),
                    release_digest=str(payload["release_digest"]),
                    profile=str(payload["profile"]),
                    status=str(payload["status"]),
                    report_digest=str(payload.get("report_digest", "")),
                ),
                database=database,
            )
        finally:
            connection.close()
        return SessionRoute(principal=route.principal, epoch=route.epoch)

    def _submit(
        self, kind: str, anchor: FrameAnchor, envelope: dict, request_id: str | None, deadline
    ) -> Submission:
        binding = _binding(anchor)
        request_id, digest = self._runtime().publish_inbox(
            kind,
            envelope,
            binding=binding,
            expected_head=anchor.head,
            request_id=request_id,
            deadline=deadline,
        )
        return Submission(
            request_id=request_id,
            digest=digest,
            outcome="pending",
            revision=anchor.head,
            audience=str(envelope.get("audience", "")),
            expected_head=anchor.head,
            anchor=anchor,
        )

    @_mapped
    def submit_heartbeat(
        self, anchor: FrameAnchor, envelope: dict, *, request_id: str, deadline: float
    ) -> Submission:
        return self._submit("heartbeat", anchor, envelope, request_id, deadline)

    @_mapped
    def submit_hive_lease(
        self, anchor: FrameAnchor, prefix: str, envelope: dict, *, deadline: float
    ) -> Submission:
        return self._submit("hive_lease", anchor, envelope, None, deadline)

    @_mapped
    def read_result(self, submission: Submission, *, deadline: float) -> Submission:
        """One read of the receiver's public verdict. ``pending`` stays ``pending`` until the
        receiver has answered; the caller polls, as ``publish_hive_lease`` does today."""
        if submission.outcome != "pending":
            return submission
        if submission.anchor is None or submission.audience is None:
            raise HqRefused("unknown_submission", "submission carries no SQL read context")
        binding = _binding(submission.anchor)
        audience, expected = submission.audience, submission.expected_head
        result = self._runtime().read_public_result(
            submission.request_id,
            request_sha256=submission.digest.removeprefix(_SHA_PREFIX),
            principal=binding,
            audience=audience,
            expected_revision=expected,
            deadline=deadline,
        )
        if result is None:
            return submission
        status, revision = result
        if status == "accepted" and revision:
            return Submission(
                submission.request_id, submission.digest, "accepted", revision=revision
            )
        return Submission(
            submission.request_id,
            submission.digest,
            "refused",
            revision=submission.revision,
            refusal=str(revision or "rejected"),
        )

    # ---- HqAdmission ---------------------------------------------------------------------

    @_mapped
    def describe_incarnation(
        self, principal: str, epoch: int, frame: str, *, target_route: str | None, deadline: float
    ) -> dict:
        return self._operator().describe_release_route(
            principal, epoch, frame, target_route=target_route, deadline=deadline
        )

    @_mapped
    def preflight(self, *, route: str, deadline: float) -> dict:
        return self._operator().preflight(route=route, deadline=deadline)

    @_mapped
    def register_incarnation(
        self,
        route: tuple,
        *,
        expected_revision: str,
        frame_address: str,
        login_secret: Callable[[], str] | None = None,
        deadline: float,
    ) -> str:
        if login_secret is None:
            raise HqRefused(
                "provisioning_password_required",
                "the SQL adapter creates the incarnation login and needs a login_secret source",
            )
        return self._operator().provision_release_route(
            route,
            expected_revision=expected_revision,
            frame_address=frame_address,
            password=login_secret(),  # the one call, inside provisioning
            deadline=deadline,
        )

    def _publish_documents(self, change: Callable[[list], list], expected_revision, deadline):
        store = self._store()
        snapshot = store.load_snapshot(deadline=deadline)
        if snapshot.commit_revision != expected_revision:
            raise HqRefused("revision_conflict", "the committed config revision changed")
        return store.publish_snapshot(
            tuple(change(list(snapshot.documents))),
            expected_revision=expected_revision,
            deadline=deadline,
        ).commit_revision

    @_mapped
    def publish_manifest(self, manifest: Any, *, expected_revision: str, deadline: float) -> str:
        from ruamel.yaml import YAML

        from .hq_sql_config import FleetConfigDocument

        stream = StringIO()
        yaml = YAML()
        yaml.indent(mapping=2, sequence=4, offset=2)
        yaml.dump(manifest.model_dump(mode="json"), stream)
        path = f"hosts/{manifest.host_id}.yaml"

        def change(documents: list) -> list:
            kept = [item for item in documents if item.path != path]
            return [*kept, FleetConfigDocument(path, stream.getvalue())]

        return self._publish_documents(change, expected_revision, deadline)

    @_mapped
    def withdraw_manifest(self, host_id: str, *, expected_revision: str, deadline: float) -> str:
        path = f"hosts/{host_id}.yaml"

        def change(documents: list) -> list:
            kept = [item for item in documents if item.path != path]
            if len(kept) == len(documents):
                raise HqRefused("manifest_not_found", f"no manifest published for {host_id}")
            return kept

        return self._publish_documents(change, expected_revision, deadline)

    @_mapped
    def submit_registration(
        self, anchor: FrameAnchor, envelope: dict, *, deadline: float
    ) -> Submission:
        return self._submit("registration", anchor, envelope, None, deadline)

    @_mapped
    def admit(
        self,
        frame: str,
        request: AdmissionRequest,
        *,
        signer: Signer,
        expected_revision: str,
        deadline: float,
    ) -> str:
        """Compute the next authority state from the intent with the shared grant rules
        (:func:`beadhive.hq_grant_rules.grant_transition`) and publish it signed by *signer*.
        A trusted (``admitted``) request first passes ``open_admission_guard``, as
        ``SqlControlPlane.admit_open`` does."""
        from .hq_control_plane import open_admission_guard
        from .hq_grant_rules import grant_transition

        if not isinstance(request, AdmissionRequest):
            raise HqRefused("admission_request_invalid", "admit takes an AdmissionRequest")
        if request.frame != frame or request.authority.frame_id != frame:
            raise HqRefused("admission_frame_mismatch", "admission request names another frame")
        desired = open_admission_guard(request.desired) if request.admitted else request.desired
        operator = self._operator()
        head, original, _crossref, _policies = operator.load(deadline=deadline)
        transition = grant_transition(
            original,
            head=head,
            expected=expected_revision,
            authority=request.authority,
            public_key=request.public_key,
            desired=desired,
            admitted=request.admitted,
            now=self._plane.clock(),
            operator_public_key=self._plane.settings["runtime_operator_public_key"],
        )
        return operator.publish(
            transition.state,
            expected_revision=expected_revision,
            authority_signer=signer,
            provisioned_route=request.provisioned_route or transition.provisioned_route,
            deadline=deadline,
        )

    # ---- HqReads -------------------------------------------------------------------------

    @_mapped
    def read_authority(self, *, allow_expired: bool = False, deadline: float) -> Any:
        """``(head, state, config crossref, hive policies)``. A frame reads through its runtime
        login; a host with only the operator's writer binding reads through that (expired
        authority allowed, as ``SqlRuntimeOperator.load`` always does)."""
        if self._plane.settings.get("runtime") is None:
            return self._operator().load(deadline=deadline)
        return self._runtime().load_state(deadline=deadline, allow_expired=allow_expired)

    @_mapped
    def read_frame_anchor(self, *, expected_head: str | None, deadline: float) -> FrameAnchor:
        binding = self._runtime().load_frame_binding(expected_head=expected_head, deadline=deadline)
        return FrameAnchor(
            principal=binding.principal,
            frame=binding.frame_id,
            epoch=binding.epoch,
            holder=binding.holder_identity,
            instance=binding.instance_ref,
            carrier=binding.inbox_table,
            head=expected_head,
        )

    @_mapped
    def read_eligibility(
        self,
        frame: str,
        *,
        prefix: str | None = None,
        session_prefix: str | None = None,
        hive_lease_epoch: int | None = None,
        deadline: float,
    ) -> Any:
        """The frame composite. *frame* is implied by the runtime login on SQL."""
        return self._runtime().read_frame_composite(
            prefix=prefix,
            session_prefix=session_prefix,
            hive_lease_epoch=hive_lease_epoch,
            session_liveness=True,
            deadline=deadline,
        )

    @_mapped
    def read_release_catalog_row(
        self, head: str, catalog: str, measured: dict, *, deadline: float
    ) -> dict | None:
        return self._runtime().read_release_catalog_row(head, catalog, measured, deadline=deadline)

    def read_release_catalog(self, catalog: str, *, deadline: float) -> list[dict]:
        raise HqUnavailable("read_release_catalog is the api route's; SQL reads the catalog store")

    def plan_release_policy(self, frame: str, request: dict, *, deadline: float) -> dict:
        raise HqUnavailable("plan_release_policy is the api route's; SQL plans in the plane")

    def plan_rebind(self, request: dict, *, deadline: float) -> dict:
        raise HqUnavailable("plan_rebind is the api route's; hq-service plans the rebind")

    def plan_manifest_publish(self, host_id: str, request: dict, *, deadline: float) -> dict:
        raise HqUnavailable("plan_manifest_publish is the api route's (hq-service v0.1.3)")

    def apply_manifest_publish(self, host_id: str, body: dict, *, deadline: float) -> dict:
        raise HqUnavailable("apply_manifest_publish is the api route's (hq-service v0.1.3)")

    def import_release_catalog(
        self, catalog: str, request: dict, *, signer, deadline: float
    ) -> str:
        raise HqUnavailable("import_release_catalog is the api route's; SQL imports via the store")

    def plan_admission(
        self, frame: str, request: AdmissionRequest, *, expected_revision: str, deadline: float
    ) -> dict:
        raise HqUnavailable("plan_admission is the api route's (hq-service v0.1.4)")

    def apply_admission(self, frame: str, body: dict, *, deadline: float) -> str:
        raise HqUnavailable("apply_admission is the api route's (hq-service v0.1.4)")

    @_mapped
    def read_config(
        self, *, revision: str | None = None, cached_ok: bool = False, deadline: float
    ) -> Any:
        store = self._store()
        if cached_ok and revision is None:
            return store.load_snapshot_or_cached()
        return store.load_snapshot(revision=revision, deadline=deadline)

    @_mapped
    def read_hive_lease(self, prefix: str, *, deadline: float) -> Any:
        """``(revision, lease)``: the unqualified protected hive-lease record (``("", None)``
        when never adopted), as :meth:`SqlControlPlane.read_hive_lease_record` reads it."""
        return self._plane.read_hive_lease_record(prefix)

    # ---- HqOperator ----------------------------------------------------------------------

    def _publish(self, state, signer, expected_revision, deadline, **extra) -> str:
        return self._operator().publish(
            state,
            expected_revision=expected_revision,
            authority_signer=signer,
            deadline=deadline,
            **extra,
        )

    @_mapped
    def rebind(
        self, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        return self._publish(state, signer, expected_revision, deadline)

    @_mapped
    def release_policy(
        self, frame: str, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        return self._publish(state, signer, expected_revision, deadline)

    @_mapped
    def publish_authority(
        self,
        state: dict,
        *,
        signer: Signer,
        expected_revision: str,
        provisioned_route: tuple | None = None,
        release_upgrade: dict | None = None,
        deadline: float,
    ) -> str:
        return self._publish(
            state,
            signer,
            expected_revision,
            deadline,
            provisioned_route=provisioned_route,
            release_upgrade=release_upgrade,
        )

    @_mapped
    def read_evidence(
        self, frame: str, holder_identity: str, *, expected_revision: str, deadline: float
    ) -> tuple:
        return self._operator().evidence(
            frame, holder_identity, expected_revision=expected_revision, deadline=deadline
        )

    @_mapped
    def publish_config(self, documents: tuple, *, expected_revision: str, deadline: float) -> Any:
        return self._store().publish_snapshot(
            documents, expected_revision=expected_revision, deadline=deadline
        )

    @_mapped
    def recover_publication(
        self, publication_id: str, *, expected_revision: str, deadline: float
    ) -> Any:
        return self._store().recover_publication(
            publication_id, expected_revision=expected_revision, deadline=deadline
        )


def _default_factory() -> SqlHqAdapter:
    """The adapter over this host's validated ``hq.sql`` settings (read by the control plane,
    which already owns the host-config import)."""
    from .hq_control_plane import host_sql_settings

    return SqlHqAdapter(host_sql_settings())


register_sql_adapter(_default_factory)
