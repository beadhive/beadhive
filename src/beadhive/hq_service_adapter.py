"""The hq-service adapter: the HQ port over the Gateway ``hq`` plane (``hq.lifecycle.v0``).

Each implemented port method issues exactly one request to exactly one route of the plane
(``beadhive-gateway`` ``docs/openapi/hq-lifecycle-v0.yaml``). The caller's identity is the
credential the Gateway authenticates (mTLS client certificate, or the auth proxy in front of
its socket), so no method carries a principal as an argument. Failures map onto the port's
taxonomy; a method with no route raises :class:`HqUnavailable` and never reaches SQL.

Wire-shape status (bh-21vpt): every route this adapter calls is pinned to hq-service v0.1.0
(commit 4e0fc6b; ``api/``, ``store/``, ``docs/DESIGN.md``): authority, the three signed
submissions, config, history, eligibility, the hive-lease read, the release-catalog read,
incarnations, manifests (PUT and DELETE), admissions, the rebind and release-policy plan/apply
pairs, and session renew and evidence. The write routes are caller-bound at v0.1.0 (the Gateway
mints the principal kind, frame and host, and a mismatch is ``caller_not_bound``); the adapter
sends nothing extra for that. The release-catalog import and revoke plan/apply routes are served
but have no port method yet (bh-lejfy). The
fixtures are in ``tests/test_hq_service_contract.py``, ``tests/test_hq_service_renew_contract.py``
and ``tests/test_hq_service_evidence_contract.py``. A shape the service does not serve yet would
be marked ``PROVISIONAL`` with the upstream gap; none remains. Methods with no route raise
:class:`HqUnavailable` (``_no_route``).

Verification of signed authority and the restore floor (bh-2r7qd, absorbing bh-j99c8) plugs in
through ``verifier``: authority-bearing reads (authority, eligibility, config) hand the response
to it and return what it returns. ``select_hq_port`` builds the default
:class:`~beadhive.hq_service_verifier.ServiceResponseVerifier` from the ``hq.api`` trust pins;
with no verifier those reads fail closed, because the
port promises verified results only.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .hq_port import (
    EvidenceReport,
    FrameAnchor,
    HistorySinceFloor,
    HqDeadline,
    HqNoCurrentBeat,
    HqOutcomeUnknown,
    HqPort,
    HqRefused,
    HqRouteError,
    HqUnavailable,
    SessionRoute,
    Signer,
    Submission,
    configured_route,
    sql_adapter_factory,
)
from .hq_sql_signatures import AUTHORITY_DOMAIN, SqlSignatureError, canonical

PREFIX = "/hq/lifecycle/v0"
DEADLINE_HEADER = "X-Beadhive-Deadline-Ms"
_DEFAULT_AUTHORITY = "http://hq"

_log = logging.getLogger(__name__)

Verifier = Callable[[str, Any], Any]
"""``verifier(method, payload) -> verified result`` for an authority-bearing read."""

BeatSource = Callable[[FrameAnchor], Any]
"""``beat_source(anchor) -> HeartbeatLease`` (or its mapping): the unsigned beat to renew with,
stamped with the sender's current ``renewTime`` and the next ``seq``."""

#: Refusal codes POST .../renew can return as a definite, non-retryable refusal
#: (``conformance/session/v1/refusal-codes.json`` plus the HTTP-layer pre-rule codes). Each
#: surfaces as :class:`HqRefused` with the code unchanged; an unlisted non-retryable code is
#: refused the same way, so a newer service code is never mistaken for an outage.
RENEW_REFUSALS = frozenset(
    {
        "authority_failed",
        "beat_not_fresh",
        "beat_schema_invalid",
        "beat_signature_invalid",
        "candidate_expired",
        "content_type_invalid",
        "epoch_future",
        "epoch_stale",
        "floor_unprovisioned",
        "grant_retired",
        "identity_mismatch",
        "incarnation_unprovisioned",
        "lease_duration_below_intervals",
        "lease_duration_out_of_range",
        "no_current_grant",
        "path_invalid",
        "path_principal_mismatch",
        "request_malformed",
        "request_not_canonical",
        "request_size_invalid",
        "seq_not_advanced",
        "unprovisioned_principal",
    }
)

#: Refusal codes POST .../evidence can return as a definite, non-retryable refusal: the report's
#: own (``no_current_beat``, ``evidence_digest_unmatched``, ``evidence_invalid``) and the path,
#: grant and HTTP-layer rules it shares with renew. ``no_current_beat`` surfaces as
#: :class:`HqNoCurrentBeat`; every other code as :class:`HqRefused` with the code unchanged (an
#: unlisted non-retryable code too, so a newer service code is never mistaken for an outage).
EVIDENCE_REFUSALS = frozenset(
    {
        "authority_failed",
        "content_type_invalid",
        "epoch_future",
        "epoch_stale",
        "evidence_digest_unmatched",
        "evidence_invalid",
        "grant_retired",
        "incarnation_unprovisioned",
        "no_current_beat",
        "no_current_grant",
        "path_invalid",
        "path_principal_mismatch",
        "request_malformed",
        "request_size_invalid",
        "unprovisioned_principal",
    }
)


#: Refusal codes hq-service v0.1.0 documents on a status other than 400/409/413/415
#: (``conformance/{admission,operator,http}/v1`` registries): definite verdicts on the request,
#: not a Gateway or version-skew status, so :class:`HqRefused` (bh-1bsvh's rule, revisited).
_DEFINITE_OTHER_4XX = frozenset(
    {"caller_not_bound", "operator_apply_disabled", "unknown_frame", "unknown_prefix"}
)


def _seg(value: object) -> str:
    """One path segment, percent-encoded so it can never add or alter a segment."""
    return quote(str(value), safe="")


def _digest(envelope: Mapping[str, Any]) -> str:
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _release_query(measured: Mapping[str, str] | None) -> str:
    """``?release_id=&release_digest=`` for a measured release, both once, percent-encoded."""
    if measured is None:
        return ""
    return f"?release_id={_seg(measured['id'])}&release_digest={_seg(measured['digest'])}"


def _no_route(method: str) -> HqUnavailable:
    return HqUnavailable(f"no hq-service route for {method}")


class HqServiceAdapter:
    """:class:`beadhive.hq_port.HqPort` over HTTP."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        verifier: Verifier | None = None,
        beat_source: BeatSource | None = None,
        signing_key: str | None = None,
        seq_store: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
        frame: str | None = None,
    ) -> None:
        self._client = client
        #: This host's ``host.frame_id``: the frame :meth:`read_frame_anchor` reads.
        self._frame = frame or ""
        self._verifier = verifier
        self._beat_source = beat_source
        self._signing_key = signing_key
        #: Zero-arg factory of the local renew-seq store (``hq_renew_seq.RenewSeqStore``); with
        #: none, the beat's own seq is sent as built (the caller then owns monotonicity).
        self._seq_store = seq_store
        self._clock = clock or (lambda: datetime.now(UTC).timestamp())

    # ---- transport ----------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        deadline: float,
        expected_revision: str | None = None,
        commit_write: bool = False,
        raw_body: bytes | None = None,
        envelope_errors: bool = False,
    ) -> tuple[int, Any]:
        """One request. Returns ``(status, json)`` for a 2xx; maps everything else.

        ``commit_write`` marks a non-idempotent write whose commit may precede the failure the
        caller sees, so a ``5xx`` raises :class:`HqOutcomeUnknown` rather than ``HqUnavailable``.
        ``raw_body`` sends exactly those bytes (a signed canonical document must not be
        re-encoded); ``envelope_errors`` reads a non-2xx body as the ``{"error": {...}}`` envelope.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HqDeadline("deadline spent before the request")
        headers = {DEADLINE_HEADER: str(max(1, int(remaining * 1000)))}
        kwargs: dict[str, Any] = {"headers": headers, "timeout": remaining}
        if raw_body is not None:
            kwargs["content"] = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            kwargs["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        write = method != "GET"
        try:
            response = self._client.request(method, PREFIX + path, **kwargs)
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
            if isinstance(exc, httpx.ConnectError) or time.monotonic() < deadline:
                raise HqUnavailable(f"hq-service unreachable: {exc}") from exc
            raise HqDeadline("deadline ran out connecting") from exc
        except httpx.TimeoutException as exc:  # read/write timeout: the body was sent
            if write:
                raise HqOutcomeUnknown(
                    expected_revision, "timeout after the request was sent"
                ) from exc
            raise HqDeadline("deadline ran out reading") from exc
        except httpx.HTTPError as exc:
            if write:
                raise HqOutcomeUnknown(expected_revision, f"connection lost: {exc}") from exc
            raise HqUnavailable(f"hq-service read failed: {exc}") from exc
        if envelope_errors and not 200 <= response.status_code < 300:
            self._raise_envelope(response, commit_write=commit_write)
        return response.status_code, self._decode(
            response, commit_write=commit_write, expected_revision=expected_revision
        )

    @staticmethod
    def _raise_envelope(response: httpx.Response, *, commit_write: bool = False) -> None:
        """Map an ADR bh-hq-pio.5 error envelope onto the port taxonomy; return when the body
        is not one (the caller's ordinary status mapping then applies).

        - ``deadline_exceeded`` -> :class:`HqDeadline` (the service ran out of our deadline).
        - ``retryable`` (503 transient, 409 CAS conflict) -> :class:`HqUnavailable`: nothing was
          committed, or the write is idempotent and the same bytes replay.
        - 401 -> :class:`HqUnavailable`: the credential/attestation, not the request, failed.
        - 405 -> :class:`HqUnavailable`: a route defect this client never causes.
        - any other 400/409/413/415 code -> :class:`HqRefused` carrying the code, and so are the
          documented refusals on other 4xx statuses (``_DEFINITE_OTHER_4XX``, hq-service v0.1.0:
          403 ``caller_not_bound`` / ``operator_apply_disabled``, 404 ``unknown_frame`` /
          ``unknown_prefix``).
        - a 5xx on a ``commit_write`` is left to the ordinary mapping, which reports it as
          :class:`HqOutcomeUnknown`: the commit may have landed before the failure.

        Decision (bh-1bsvh): an unknown non-retryable code on any other 4xx (403, 404, 422 ...)
        stays :class:`HqUnavailable`, not :class:`HqRefused`. The conformance corpus has no such
        code, so there is no contract for one; the status is the Gateway's or a version skew, not
        a verdict on this request, and a caller treats ``HqRefused`` as a definite, permanent
        refusal of the request. Unavailable is the fail-safe reading: nothing is assumed
        committed, the operation can be retried once the deployment is fixed. Revisit if
        hq-service ever documents a 4xx refusal code outside 400/409/413/415.
        """
        try:
            payload = response.json()
        except ValueError:
            return
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code:
            return
        message = str(error.get("message") or code)
        status = response.status_code
        if commit_write and status >= 500:
            return
        if code == "deadline_exceeded":
            raise HqDeadline(message)
        if error.get("retryable") is True or status in (401, 405) or status >= 500:
            raise HqUnavailable(f"hq-service answered {status} {code}: {message}")
        if status in (400, 409, 413, 415) or code in _DEFINITE_OTHER_4XX:
            raise HqRefused(code, message)

    @staticmethod
    def _decode(
        response: httpx.Response,
        *,
        commit_write: bool = False,
        expected_revision: str | None = None,
    ) -> Any:
        status = response.status_code
        try:
            payload: Any = response.json() if response.content else {}
        except ValueError:
            payload = None
        if 200 <= status < 300:
            if not isinstance(payload, (dict, list)):
                raise HqUnavailable(f"hq-service answered {status} with a non-JSON body")
            return payload
        message = payload.get("error") if isinstance(payload, dict) else None
        if status == 409 and isinstance(payload, dict) and payload.get("outcome") == "refused":
            code = str(payload.get("refusal") or "refused")
            raise HqRefused(code, str(payload.get("reason") or code))
        if status in (400, 413, 415):
            raise HqRefused(
                "bad_request", str(message or f"hq-service refused the request ({status})")
            )
        if commit_write and status >= 500:
            # A Gateway 502/503/504 can arrive after the service committed, and a service 500 can
            # be a post-commit error (a failed response build or commit acknowledgement), so
            # the write may have landed. All 5xx are outcome-unknown for a non-idempotent write.
            raise HqOutcomeUnknown(
                expected_revision,
                f"hq-service answered {status} to a write that may have committed"
                + (f": {message}" if message else ""),
            )
        # 401/403/404/405 come from the Gateway before the service; on a read, an idempotent
        # submission or a plan phase, 5xx means HQ could not be used and nothing was written.
        raise HqUnavailable(f"hq-service answered {status}" + (f": {message}" if message else ""))

    def _submission(
        self, method: str, path: str, envelope: dict, *, request_id: str | None, deadline: float
    ) -> Submission:
        digest = _digest(envelope)
        request = envelope.get("request") if isinstance(envelope, dict) else None
        rid = request_id or (request.get("request_id") if isinstance(request, dict) else None)
        rid = str(rid or digest)
        try:
            # v0.1.0 answers a refusal as its error envelope (409 + code, bh-h9dn1); the older
            # ``{outcome: refused}`` body still maps through ``_decode``.
            _, payload = self._call(method, path, envelope, deadline=deadline, envelope_errors=True)
        except HqRefused as exc:
            if exc.code == "bad_request":
                raise
            return Submission(rid, digest, "refused", refusal=exc.code)
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if outcome not in ("accepted", "replayed"):
            raise HqUnavailable(f"hq-service returned an unknown submission outcome: {outcome!r}")
        return Submission(rid, digest, outcome, revision=payload.get("revision"))

    def _revision(self, payload: Any) -> str:
        revision = payload.get("revision") if isinstance(payload, dict) else None
        if not isinstance(revision, str) or not revision:
            raise HqUnavailable("hq-service response carries no revision")
        return revision

    def _verified(self, method: str, payload: Any) -> Any:
        if self._verifier is None:
            raise HqUnavailable(
                f"{method}: no response verifier is configured; refusing to return an "
                "unverified result (bh-j99c8)"
            )
        return self._verifier(method, payload)

    # ---- liveness -----------------------------------------------------------------------

    def renew_session(self, anchor: FrameAnchor, *, deadline: float) -> SessionRoute:
        """Post one frame-signed canonical FrameLease to the session renew route.

        The body is the canonical (sorted, compact, ASCII) signed envelope, byte for byte: the
        service refuses any other encoding of the same document (``request_not_canonical``).
        ``renewTime`` and ``seq`` come from the beat source; the service stamps liveness with its
        own clock. ``replayed`` (the same beat sent again) is a success, like ``accepted``.
        """
        from . import hq_renew_seq
        from .hq_framelease_contracts import HeartbeatLease
        from .hq_sql_signatures import SqlSignatureError, canonical, sign_heartbeat

        if self._beat_source is None or not self._signing_key:
            raise HqUnavailable(
                "renew_session: no beat source and frame signing key are configured; "
                "refusing to send an unsigned renewal"
            )
        path = f"/sessions/{_seg(anchor.principal)}/{_seg(anchor.epoch)}/renew"
        store = self._seq_store() if self._seq_store is not None else None
        ident = (anchor.frame, anchor.holder, anchor.epoch)
        state = store.load(*ident) if store is not None else None
        now = self._clock()
        pending = None
        if state is not None and hq_renew_seq.resendable(state.pending, now):
            # An unanswered beat still fresh: resend the same seq and bytes (a replay, not a
            # second beat with another digest). No beat source read is needed.
            pending = state.pending
            body = pending.body.encode()
            seq = pending.seq
        else:
            if deadline - time.monotonic() <= 0:
                raise HqDeadline("deadline spent before the beat was built")
            try:
                lease = HeartbeatLease.model_validate(self._beat_source(anchor))
            except ValueError as exc:
                raise HqRefused(
                    "beat_schema_invalid", f"beat source built an invalid lease: {exc}"
                ) from exc
            if (lease.frame_id, lease.epoch) != (anchor.frame, anchor.epoch) or (
                (anchor.holder and lease.holderIdentity != anchor.holder)
                or (anchor.instance and lease.instance_ref != anchor.instance)
            ):
                raise HqRefused("identity_mismatch", "the beat is not for this frame anchor")
            if state is not None:
                lease = lease.model_copy(
                    update={"seq": hq_renew_seq.next_seq(state, lease.seq, now)}
                )
            try:
                body = canonical(sign_heartbeat(lease, signing_key=self._signing_key))
            except SqlSignatureError as exc:
                raise HqRefused("beat_signature_invalid", str(exc)) from exc
            seq = lease.seq
            if state is not None:
                pending = hq_renew_seq.Pending(
                    seq,
                    datetime.fromisoformat(lease.renewTime).timestamp(),
                    lease.leaseDurationSeconds,
                    body.decode(),
                )
                # Saved before sending: a crash or lost connection resends these exact bytes.
                store.save(
                    *ident, hq_renew_seq.SeqState(max(state.last_seq, seq), state.resync, pending)
                )
        try:
            _, payload = self._call(
                "POST", path, None, deadline=deadline, raw_body=body, envelope_errors=True
            )
        except (HqUnavailable, HqOutcomeUnknown, HqDeadline):
            raise  # outcome unknown or nothing sent: the pending beat stays for an identical resend
        except HqRefused as exc:
            if state is not None:
                # A verdict on that beat: it is never resent. After seq_not_advanced the floor is
                # above what this client holds, so the next fresh beat rebases above the clock.
                store.save(
                    *ident,
                    hq_renew_seq.SeqState(
                        max(state.last_seq, seq),
                        state.resync or exc.code == "seq_not_advanced",
                        None,
                    ),
                )
            raise
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        until = payload.get("accepted_until") if isinstance(payload, dict) else None
        if outcome not in ("accepted", "replayed") or type(until) not in (int, float):
            raise HqUnavailable(f"hq-service returned an unusable renew result: {payload!r}")
        if state is not None:
            store.save(*ident, hq_renew_seq.SeqState(max(state.last_seq, seq), False, None))
        return SessionRoute(
            anchor.principal,
            anchor.epoch,
            self._revision(payload),
            accepted_until=until,
            outcome=outcome,
            beat=_sent_beat(body),
        )

    def publish_evidence(
        self, anchor: FrameAnchor, report: EvidenceReport, *, deadline: float
    ) -> SessionRoute:
        """Post the beat's measurement report (``{method, checks, free_sessions}``) to the
        session evidence route.

        The route is scope ``lifecycle:session`` and the body is the report as unsigned JSON:
        the beat's signature already binds the report through ``report_digest``, which the
        service compares with the canonical sha256 of this report. Key order does not matter;
        the canonical bytes are sent so a re-post is byte-identical. ``no_current_beat`` (the
        service holds no accepted beat yet) raises :class:`HqNoCurrentBeat`: renew, then post
        again.

        Retry safety: the service stores a report once, keyed by its digest, and a repeat is
        ``200 replayed`` (the stored report untouched, the measurement re-stamped), so
        re-posting the same bytes after a 503, a timeout or a lost connection is safe.
        """
        try:
            body = json.dumps(
                dict(report.payload), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        except (TypeError, ValueError) as exc:
            raise HqRefused("evidence_invalid", f"report is not JSON-encodable: {exc}") from exc
        path = f"/sessions/{_seg(anchor.principal)}/{_seg(anchor.epoch)}/evidence"
        try:
            _, payload = self._call(
                "POST", path, None, deadline=deadline, raw_body=body, envelope_errors=True
            )
        except HqRefused as exc:
            if exc.code == "no_current_beat":
                raise HqNoCurrentBeat(exc.reason) from exc
            raise
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        digest = payload.get("digest") if isinstance(payload, dict) else None
        if outcome not in ("accepted", "replayed") or not isinstance(digest, str) or not digest:
            raise HqUnavailable(f"hq-service returned an unusable evidence result: {payload!r}")
        if digest != "sha256:" + hashlib.sha256(body).hexdigest():
            raise HqUnavailable("hq-service stored a different report digest than the one sent")
        return SessionRoute(anchor.principal, anchor.epoch, outcome=outcome, digest=digest)

    def submit_heartbeat(
        self, anchor: FrameAnchor, envelope: dict, *, request_id: str, deadline: float
    ) -> Submission:
        return self._submission(
            "PUT",
            f"/heartbeats/{_seg(request_id)}",
            envelope,
            request_id=request_id,
            deadline=deadline,
        )

    def submit_hive_lease(
        self, anchor: FrameAnchor, prefix: str, envelope: dict, *, deadline: float
    ) -> Submission:
        return self._submission(
            "POST", f"/hive-leases/{_seg(prefix)}", envelope, request_id=None, deadline=deadline
        )

    def read_result(self, submission: Submission, *, deadline: float) -> Submission:
        return submission  # the service answers with the final outcome

    # ---- admission ----------------------------------------------------------------------

    def describe_incarnation(
        self, principal: str, epoch: int, frame: str, *, target_route: str | None, deadline: float
    ) -> dict:
        raise _no_route("describe_incarnation")

    def preflight(self, *, route: str, deadline: float) -> dict:
        raise _no_route("preflight")

    def register_incarnation(
        self,
        route: tuple,
        *,
        expected_revision: str,
        frame_address: str,
        login_secret: Callable[[], str] | None = None,
        deadline: float,
    ) -> str:
        # frame_address and login_secret are SQL-adapter-only inputs: hq-service holds the only
        # database account, so no login is created and login_secret is never called. The body is
        # exactly {principal, epoch, expected_revision} (``store/incarnation.go``); the principal
        # is the route's own (``frame_`` + 20 hex of the authority identity) and the answer
        # echoes it with the unchanged head: nothing is committed.
        principal, _frame, _holder, _instance, epoch, _signer = route
        body = {"principal": principal, "epoch": epoch, "expected_revision": expected_revision}
        _, payload = self._call(
            "POST",
            "/incarnations",
            body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
            envelope_errors=True,
        )
        if (
            not isinstance(payload, dict)
            or payload.get("principal") != principal
            or payload.get("epoch") != epoch
        ):
            raise HqUnavailable("hq-service registered a different incarnation than the one sent")
        return self._revision(payload)

    def publish_manifest(self, manifest: Any, *, expected_revision: str, deadline: float) -> str:
        document = dict(manifest) if isinstance(manifest, Mapping) else dict(vars(manifest))
        host_id = document.get("host_id")
        if not host_id:
            raise HqRefused("bad_request", "manifest carries no host_id")
        # The body is the host manifest itself; the service reads the config head and CASes on
        # it (the host does not know it), so ``expected_revision`` is not sent. The 200 body is
        # {outcome, revision (the CONFIG head), host_id, path, sha256}.
        _, payload = self._call(
            "PUT",
            f"/manifests/{_seg(host_id)}",
            document,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
            envelope_errors=True,
        )
        if not isinstance(payload, dict) or payload.get("host_id") != host_id:
            raise HqUnavailable("hq-service published a different host manifest than the one sent")
        return self._revision(payload)

    def withdraw_manifest(self, host_id: str, *, expected_revision: str, deadline: float) -> str:
        # DELETE /manifests/{host_id}: no body, bound to the caller's own host like the PUT. The
        # service CASes the config head itself; withdrawing a host with no manifest is a replay
        # at the unchanged head. The 200 body is {outcome, revision (the CONFIG head), host_id,
        # path}.
        _, payload = self._call(
            "DELETE",
            f"/manifests/{_seg(host_id)}",
            None,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
            envelope_errors=True,
        )
        if not isinstance(payload, dict) or payload.get("host_id") != host_id:
            raise HqUnavailable("hq-service withdrew a different host manifest than the one named")
        return self._revision(payload)

    def submit_registration(
        self, anchor: FrameAnchor, envelope: dict, *, deadline: float
    ) -> Submission:
        return self._submission(
            "POST", "/registrations", envelope, request_id=None, deadline=deadline
        )

    def admit(
        self, frame: str, request: Any, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        # The service derives the principal from committed authority, and builds, stamps and
        # signs the grant itself under the service's own authority (design section 7 D3);
        # `signer` is not used on this route. The body is exactly
        # {kind, expected_revision, host_id, public_key} (``store/admission.go``): an open
        # admission (``request.admitted``) names the candidate's host and runtime key; a
        # candidate admission names neither (the committed candidate is promoted). The rest of
        # the request (desired policy, release, caps) is read by the service from the committed
        # host manifest.
        from .hq_port import AdmissionRequest

        if not isinstance(request, AdmissionRequest):
            raise HqRefused("bad_request", "admit takes an AdmissionRequest")
        body: dict[str, Any] = {
            "kind": "open" if request.admitted else "candidate",
            "expected_revision": expected_revision,
        }
        if request.admitted:
            body["host_id"] = str(getattr(request.authority, "holder_identity", "") or "")
            body["public_key"] = request.public_key
        _, payload = self._call(
            "POST",
            f"/admissions/{_seg(frame)}",
            body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
            envelope_errors=True,
        )
        if not isinstance(payload, dict) or payload.get("frame_id") != frame:
            raise HqUnavailable("hq-service admitted a different frame than the one sent")
        return self._revision(payload)

    # ---- reads --------------------------------------------------------------------------

    # GET /authority, /config, /history and /eligibility/{frame} are served at hq-service v0.1.0
    # (``api/api.go``, ``api/reads.go``; ``store/reads.go`` for the bodies).
    def read_authority(self, *, allow_expired: bool = False, deadline: float) -> Any:
        _, payload = self._call("GET", "/authority", None, deadline=deadline, envelope_errors=True)
        return self._verified("read_authority", payload)

    def read_frame_anchor(self, *, expected_head: str | None, deadline: float) -> FrameAnchor:
        """This host's frame route from ``GET /eligibility/{frame}`` (gap G1, bh-65s71).

        The frame is the host's own ``host.frame_id``. The grant is the verified authority's
        and the principal and epoch are the ones it derives
        (:func:`beadhive.hq_frame_grant.frame_grant`); a head other than *expected_head* is
        retryable. A host with no ``host.frame_id`` has no anchor (:class:`HqUnavailable`)."""
        from .hq_frame_grant import frame_grant

        if not self._frame:
            raise HqUnavailable("read_frame_anchor: this host has no host.frame_id")
        grant = frame_grant(self.read_eligibility(self._frame, deadline=deadline), self._frame)
        if expected_head is not None and grant.head != expected_head:
            raise HqUnavailable("read_frame_anchor: authority moved from the expected head")
        return grant.anchor

    def read_eligibility(
        self,
        frame: str,
        *,
        prefix: str | None = None,
        session_prefix: str | None = None,
        hive_lease_epoch: int | None = None,
        measured: Mapping[str, str] | None = None,
        deadline: float,
    ) -> Any:
        """``GET /eligibility/{frame}[?release_id=&release_digest=]``. ``measured`` (``{id,
        digest}``, adapter-only) asks for the release verdict against the active grant.

        The body (v0.1.0, ``store/reads.go`` ``EligibilityRead``) is ``{head, config_head,
        frame_id, active, principal, epoch, route, liveness, release}``: the last four are null
        without an active grant, and ``release`` without ``measured``. The service signs nothing
        on this route (still no ``authority`` envelope at v0.1.0), so the signature and the
        restore floor come from a verified GET /authority read, and the eligibility body is
        accepted only when it names that same authority (head and config head); a head that
        moved between the two reads is retryable. The result is the body plus that verified
        envelope under ``authority``. A hive-lease row is a separate read
        (:meth:`read_hive_lease`)."""
        if prefix is not None or session_prefix is not None or hive_lease_epoch is not None:
            raise _no_route("read_eligibility with a hive-lease row")  # read_hive_lease instead
        authority = self.read_authority(deadline=deadline)
        _, payload = self._call(
            "GET",
            f"/eligibility/{_seg(frame)}" + _release_query(measured),
            None,
            deadline=deadline,
            envelope_errors=True,
        )
        record = authority.get("record") if isinstance(authority, Mapping) else None
        if (
            not isinstance(payload, dict)
            or not isinstance(record, Mapping)
            or payload.get("frame_id") != frame
        ):
            raise HqUnavailable("hq-service eligibility is not the requested frame's")
        if payload.get("head") != authority.get("head") or payload.get("config_head") != record.get(
            "config_head"
        ):
            raise HqUnavailable("authority moved between the authority and eligibility reads")
        return {**payload, "authority": authority}

    def read_hive_lease(self, prefix: str, *, deadline: float) -> dict:
        """``GET /hive-leases?prefix=``: ``{head, prefix, revision, lease{host_id, label, epoch,
        adopted_at, expires_at}, held}`` (``HiveLeaseRead``). ``held`` is the server's verdict at
        its own clock. The row needs no authority and is returned as served; an unknown prefix
        is a 404 ``unknown_prefix`` (:class:`HqRefused`). The port's ``read_hive_lease``
        (bh-h9dn1); the api control plane qualifies the row."""
        _, payload = self._call(
            "GET",
            f"/hive-leases?prefix={_seg(prefix)}",
            None,
            deadline=deadline,
            envelope_errors=True,
        )
        if (
            not isinstance(payload, dict)
            or payload.get("prefix") != prefix
            or not isinstance(payload.get("lease"), dict)
            or not isinstance(payload.get("revision"), str)
        ):
            raise HqUnavailable("hq-service hive lease is not the requested prefix's")
        return payload

    def read_release_catalog_row(
        self, head: str, catalog: str, measured: dict, *, deadline: float
    ) -> dict | None:
        """The folded catalog row for ``measured`` (``{id, digest}``) or ``None`` when no row
        holds the digest: ``GET /release-catalog/{catalog}?release_id=&release_digest=``, whose
        ``release.row`` is ``{catalog, version, measured_digest, status, algorithm,
        active_digests}``, the SQL reader's own row. The service reads it AS OF its verified head;
        a head other than the caller's verified ``head`` is retryable."""
        payload = self._read_catalog(catalog, measured=measured, deadline=deadline)
        if payload.get("head") != head:
            raise HqUnavailable(
                "the release catalog was read at another head than the verified one"
            )
        release = payload.get("release")
        if not isinstance(release, dict) or "row" not in release:
            raise HqUnavailable("hq-service catalog verdict carries no row")
        row = release["row"]
        if row is not None and not isinstance(row, dict):
            raise HqUnavailable("hq-service catalog verdict row is malformed")
        return row

    def _read_catalog(
        self, catalog: str, *, measured: Mapping[str, str] | None = None, deadline: float
    ) -> dict:
        """``GET /release-catalog/{catalog}``: ``{head, catalog, rows[], release}``. Without
        ``measured``, ``rows`` lists every ``hq_release_catalog`` row whole; with it, only
        ``release`` ``{id, digest, verified, row}`` is set. The restore-floor verifier's catalog
        check reads through this."""
        _, payload = self._call(
            "GET",
            f"/release-catalog/{_seg(catalog)}" + _release_query(measured),
            None,
            deadline=deadline,
            envelope_errors=True,
        )
        if not isinstance(payload, dict) or payload.get("catalog") != catalog:
            raise HqUnavailable("hq-service catalog is not the requested catalog's")
        return payload

    def read_config(
        self, *, revision: str | None = None, cached_ok: bool = False, deadline: float
    ) -> Any:
        if revision is not None:
            raise _no_route("read_config at a pinned revision")
        _, payload = self._call("GET", "/config", None, deadline=deadline, envelope_errors=True)
        return self._verified("read_config", payload)

    def _read_history(self, *, since: str, deadline: float) -> HistorySinceFloor:
        """``GET /history?since={floor}``: ``{head, floor, revisions[]}`` (``store/reads.go``).

        ``revisions`` are those reachable from head but not from the floor, each ``{id, message,
        at, parents, changes[{table, data, schema}]}``, oldest first by commit time (rely on
        ``parents``). The service issues no ancestry verdict; the reader's check is the
        verifier's. An unknown floor is a 409 ``unknown_floor`` and more than 1000 revisions a
        409 ``history_too_long``, both :class:`HqRefused`."""
        _, payload = self._call(
            "GET", f"/history?since={_seg(since)}", None, deadline=deadline, envelope_errors=True
        )
        revisions = payload.get("revisions") if isinstance(payload, dict) else None
        if not isinstance(revisions, list) or not all(isinstance(r, dict) for r in revisions):
            raise HqUnavailable("hq-service history carries no revision list")
        return HistorySinceFloor(
            head=str(payload.get("head") or ""),
            floor=str(payload.get("floor") or ""),
            revisions=tuple(revisions),
            raw=payload,
        )

    # ---- operator -----------------------------------------------------------------------

    def _plan_apply(
        self,
        route: str,
        request: dict,
        *,
        signer: Signer,
        expected_revision: str,
        deadline: float,
    ) -> str:
        """Plan, sign, apply (``store/operator.go``, ``docs/DESIGN.md`` "Operator publication").

        ``POST {route}/plan`` takes only the request and writes nothing; it answers the exact
        ``record`` to sign, the ``signing_input`` (``base64(domain || canonical(record))``), the
        head it was made at (``revision``), the ``config_head`` it binds and the ``issued_at`` /
        ``expires_at`` it carries. This side checks ``signing_input`` against ``record`` and the
        head against ``expected_revision``, signs ``record`` with ``signer`` (skipped when the
        plan says ``signature_needed`` is false: a trusted fleet), then ``POST {route}/apply``
        re-sends the request with ``{expected_revision, config_head, issued_at, expires_at,
        signature, record}``; the service rebuilds the record itself and verifies the signature
        over its own bytes."""
        _, plan = self._call(
            "POST",
            route + "/plan",
            request,
            deadline=deadline,
            expected_revision=expected_revision,
            envelope_errors=True,
        )
        record = plan.get("record") if isinstance(plan, dict) else None
        if not isinstance(record, dict) or plan.get("outcome") != "planned":
            raise HqUnavailable("hq-service plan carries no record to sign")
        if plan.get("revision") != expected_revision:
            raise HqRefused(
                "stale_revision",
                f"the plan was made at {plan.get('revision')}, "
                f"not the expected {expected_revision}",
            )
        try:
            expected_input = base64.b64encode(
                AUTHORITY_DOMAIN + canonical(record, limit=4 * 1024 * 1024)
            ).decode()
        except SqlSignatureError:
            raise HqUnavailable("hq-service plan record is not canonical") from None
        if plan.get("signing_input") != expected_input:
            raise HqUnavailable("hq-service plan signing_input is not its record's signing input")
        apply_body: dict[str, Any] = {
            **request,
            "expected_revision": expected_revision,
            "config_head": plan.get("config_head"),
            "issued_at": plan.get("issued_at"),
            "expires_at": plan.get("expires_at"),
            "record": record,
        }
        if plan.get("signature_needed") is not False:
            apply_body["signature"] = signer(record)
        _, payload = self._call(
            "POST",
            route + "/apply",
            apply_body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
            envelope_errors=True,
        )
        return self._revision(payload)

    def rebind(
        self, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        """Rebind the authority (``/authority/rebind/{plan,apply}``). On this route ``state`` is
        the plan request, not a next state: ``{frame?, duration_s?}``."""
        return self._plan_apply(
            "/authority/rebind",
            dict(state),
            signer=signer,
            expected_revision=expected_revision,
            deadline=deadline,
        )

    def release_policy(
        self, frame: str, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        """Set or drop a frame's release policy
        (``/authority/release-policy/{frame}/{plan,apply}``). On this route ``state`` is
        the plan request: ``{spec, allow_downgrade?, allow_unknown_readers?}``; the frame is the
        path's."""
        return self._plan_apply(
            f"/authority/release-policy/{_seg(frame)}",
            dict(state),
            signer=signer,
            expected_revision=expected_revision,
            deadline=deadline,
        )

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
        raise _no_route("publish_authority")  # gap G6

    def read_evidence(
        self, frame: str, holder_identity: str, *, expected_revision: str, deadline: float
    ) -> tuple:
        raise _no_route("read_evidence")  # gap G7

    def publish_config(self, documents: tuple, *, expected_revision: str, deadline: float) -> Any:
        raise _no_route("publish_config")  # gap G8

    def recover_publication(
        self, publication_id: str, *, expected_revision: str, deadline: float
    ) -> Any:
        raise _no_route("recover_publication")  # gap G8


def adapter_from_config(
    host_config: Mapping[str, Any],
    *,
    verifier: Verifier | None = None,
    transport: httpx.BaseTransport | None = None,
    beat_source: BeatSource | None = None,
    signing_key: str | None = None,
    clock: Callable[[], float] | None = None,
) -> HqServiceAdapter:
    """Build the adapter from the host's ``hq.api`` section. A missing endpoint is an error,
    not a reason to try another adapter.

    Renewal signs a beat with the frame key. Unless injected (tests), the beat source is the
    sender's own builder (:func:`_frame_beat_source`) reading this frame's grant through this
    same adapter, and the key is the recorded per-host signing key (``host.yaml``
    ``signing_key``, the one ``heartbeat_sender beat`` signs with). The frame is the host's
    ``host.frame_id`` (:meth:`HqServiceAdapter.read_frame_anchor`). A missing key leaves renew
    raising :class:`HqUnavailable` before anything is sent.
    """
    hq = host_config.get("hq") if isinstance(host_config, Mapping) else None
    api = hq.get("api") if isinstance(hq, Mapping) else None
    api = api if isinstance(api, Mapping) else {}
    default_verifier = None
    if verifier is None and api.get("floor_path"):
        from .hq_service_verifier import ServiceResponseVerifier

        verifier = default_verifier = ServiceResponseVerifier(
            floor_path=str(api["floor_path"]),
            initial_revision=str(api.get("initial_revision") or ""),
            operator_public_key=str(api.get("operator_public_key") or ""),
            backend_identity=str(api.get("backend_identity") or ""),
            generation=str(api.get("generation") or ""),
        )
    socket = str(api.get("unix_socket") or "")
    url = str(api.get("url") or "")
    if not socket and not url:
        raise HqRouteError("hq.route is api but hq.api.url / hq.api.unix_socket is not set")
    cert, key = str(api.get("client_cert") or ""), str(api.get("client_key") or "")
    ca = str(api.get("ca_cert") or "")
    if transport is None and socket:
        transport = httpx.HTTPTransport(uds=socket)
    kwargs: dict[str, Any] = {
        "base_url": url or _DEFAULT_AUTHORITY,
        "follow_redirects": False,
        "trust_env": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = ca or True
        if cert:
            kwargs["cert"] = (cert, key)
    host = host_config.get("host") if isinstance(host_config, Mapping) else None
    frame = str(host.get("frame_id") or "") if isinstance(host, Mapping) else ""
    adapter = HqServiceAdapter(
        httpx.Client(**kwargs),
        verifier=verifier,
        beat_source=beat_source,
        signing_key=signing_key if signing_key is not None else _host_signing_key(),
        seq_store=_renew_seq_store,
        clock=clock,
        frame=frame,
    )
    if beat_source is None:
        adapter._beat_source = functools.partial(_frame_beat_source, port=adapter)
    if default_verifier is not None:
        default_verifier.bind_history(adapter._read_history)
        default_verifier.bind_catalog(adapter._read_catalog)
    return adapter


def _sent_beat(body: bytes) -> Any:
    """The ``HeartbeatLease`` inside the signed renew *body* the service accepted, or ``None``.

    Parsed from the bytes actually sent, never from the beat source: a pending beat that was
    resent byte for byte may digest an older conformance report than the cache holds now, and
    the evidence post must match the beat the service holds (bh-a3369)."""
    from .hq_framelease_contracts import HeartbeatLease

    try:
        spec = dict(json.loads(body)["spec"])
        spec.pop("signature", None)
        return HeartbeatLease.model_validate_json(json.dumps(spec))
    except (ValueError, KeyError, TypeError):
        return None


def _renew_seq_store() -> Any:
    """The local renew-seq store, under the sender's per-host heartbeat cache directory."""
    from . import heartbeat_report
    from .hq_renew_seq import RenewSeqStore

    return RenewSeqStore(heartbeat_report.bh_home() / "heartbeat")


def _host_signing_key() -> str | None:
    """The recorded per-host frame signing key path, or ``None`` when none is recorded or it
    is not an absolute path. Read-only; the path is never logged or put in an error."""
    from . import host

    try:
        key = host.signing_key()
    except Exception as exc:  # noqa: BLE001 - an unreadable host identity is "no key"
        # The class only: the text can carry paths.
        _log.debug("frame signing key unavailable (%s)", type(exc).__name__)
        return None
    return key if key and Path(key).is_absolute() else None


def _frame_beat_source(anchor: FrameAnchor, port: HqPort | None = None) -> Any:
    """The unsigned beat for *anchor*, built by the same generator ``heartbeat_sender beat``
    uses (``heartbeat_report.generate(..., cached=True)``): next ``seq``, ``renewTime`` now,
    ``key_id`` from the frame route and the ``report_digest`` of the cached conformance report.

    The grant-bound fields (``audience``, ``config_revision``, ``beadyard_id``, ``state_seen``,
    the grant ``profile`` and release) come from this frame's verified grant, read through
    *port* (``GET /eligibility/{frame}`` at a verified authority head, the committed host
    manifest from ``GET /config``, and under a release policy the catalog row from ``GET
    /release-catalog/{catalog}``: design gaps G1 and G3, bh-65s71); see
    :class:`beadhive.hq_frame_grant.ApiFrameAuthority`. No SQL frame runtime is read. With no
    port, or any failure building the beat, this is :class:`HqUnavailable` carrying only a fixed
    category (the underlying text can hold credential references). ``seq`` is NOT taken from
    here: it is only a lower bound, see :mod:`beadhive.hq_renew_seq`. Retry caching of a built
    beat (:meth:`HqServiceAdapter.renew_session`) needs no read at all.
    """
    from . import heartbeat_report
    from .hq_frame_grant import ApiFrameAuthority

    if port is None:
        raise HqUnavailable("renew_session: no beat available (no HQ port to read the grant)")
    try:
        return heartbeat_report.generate(ApiFrameAuthority(port, anchor.frame), cached=True)
    except Exception as exc:  # noqa: BLE001
        raise HqUnavailable(
            f"renew_session: no beat available ({heartbeat_report.refusal_category(exc)})"
        ) from None


def _load_sql_adapter() -> None:
    """Import the SQL adapter, which registers itself as the ``sql`` route on import (bh-v3dyp).
    Lazy so that choosing ``api`` never loads the SQL openers."""
    from . import hq_sql_adapter  # noqa: F401


def select_hq_port(
    host_config: Mapping[str, Any],
    *,
    route: str | None = None,
    verifier: Verifier | None = None,
) -> HqPort:
    """The adapter ``hq.route`` selects for this host. No fallback: an adapter that is not
    available raises :class:`HqRouteError` instead of trying the other one."""
    chosen = route or configured_route(host_config)
    if chosen == "sql":
        factory = sql_adapter_factory()
        if factory is None:
            _load_sql_adapter()
            factory = sql_adapter_factory()
        if factory is None:
            raise HqRouteError("hq.route is sql but no SQL adapter is registered (bh-v3dyp)")
        return factory()
    if chosen == "api":
        return adapter_from_config(host_config, verifier=verifier)  # type: ignore[return-value]
    raise HqRouteError(f"hq.route must be one of sql, api, not {chosen!r}")
