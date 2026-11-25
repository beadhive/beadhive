"""In-process fake of hq-service's session routes (renew and evidence), bh-zephr, canned read
routes (``reads``, bh-e437a) and the admission and operator write routes (bh-21vpt).

A transport-level stand-in for a staged hq-service, built from the hq-service conformance corpus
(``conformance/session/v1``, identical at v0.1.0 and 205fd5b, and ``conformance/http/v1``) and
the rules in its
``store/session.go`` ``renew()`` / ``evidence()``. It exists so the staged end-to-end test body
(``tests/test_hq_session_staged_e2e.py``) can be proven correct with no network: the test drives
the real adapter and sender code at an ``httpx`` transport, and this class answers.

Rules it ENFORCES, in the service's order:

* path: canonical epoch segment (``path_invalid``), known principal (``unprovisioned_principal``),
  epoch equal to the committed one (``epoch_stale`` / ``epoch_future``), grant state
  (``no_current_grant`` / ``grant_retired``), ``Content-Type: application/json``.
* renew: one JSON document (``request_malformed``); the body is byte for byte the canonical
  encoding (``request_not_canonical``); the FrameLease schema (``beat_schema_invalid``); the
  Ed25519 signature under the granted key (``beat_signature_invalid``); beat identity (frame,
  holder, instance, epoch) against the grant (``identity_mismatch``); first-observation
  freshness, ``-30 <= now - renewTime < leaseDurationSeconds`` (``beat_not_fresh``); an exact
  replay of an accepted ``(seq, digest)`` is ``replayed`` even after the floor moved; the floor
  must exist (``floor_unprovisioned``) and ``seq`` must be above it (``seq_not_advanced``).
  An accepted beat becomes the principal's CURRENT beat (observation) and advances the floor.
* evidence: one JSON document; the report shape ``{method, checks, free_sessions}``
  (``evidence_invalid``); a current beat must exist (``no_current_beat``); the report's canonical
  sha256 must equal that beat's ``report_digest`` (``evidence_digest_unmatched``); a repeat of a
  stored report is ``replayed``; evidence for a lapsed beat is still stored (staleness is the
  reader's call, as in the service).

The write routes serve the bodies of hq-service v0.1.0 (``api/writes.go``, ``store/``):

* ``POST /incarnations`` takes exactly ``{principal, epoch, expected_revision}`` and answers
  ``{outcome, revision, principal, epoch}`` (``revision`` is the unchanged head).
* ``PUT /manifests/{host_id}`` takes the host manifest and answers ``{outcome, revision (the
  CONFIG head), host_id, path, sha256}``; ``DELETE`` withdraws it (``{outcome, revision, host_id,
  path}``, a ``replayed`` at the unchanged head when there is none).
* ``POST /admissions/{frame}`` takes ``{kind, expected_revision, host_id?, public_key?}`` and
  answers ``{outcome, revision, frame_id, host_id, kind, principal, epoch}``.
* ``POST /authority/rebind/{plan,apply}`` and ``/authority/release-policy/{frame}/{plan,apply}``:
  a plan takes only its own fields and answers ``{outcome: planned, revision, config_head, record,
  signing_input, ...}`` for ``operator_record``; an apply takes the plan fields plus
  ``{expected_revision, config_head, issued_at, expires_at, signature?, record?}``, refuses a
  stale head, a mismatched echoed record, and a missing signature when ``signature_needed``.

* ``POST /manifests/{host_id}/publish/{plan,apply}`` (hq-service v0.1.3, bh-hq-0ci): the signed
  manifest publication that also rebinds. A plan takes ``{manifest, duration_s?}`` and answers
  the rebind plan over ``operator_record`` with its ``config_head`` set to the head the
  publication makes (``config_head_after``; the current head when the config already holds the
  manifest), plus ``manifest {host_id, path, content, sha256}`` and ``config_publication``. An
  apply commits the config, then the authority (``served_manifests`` becomes what is committed:
  the config a frame join then reads). ``crash_between_commits`` (a count) fails that many
  applies with a 503 after the config commit; the SAME apply sent again sees the config already
  at the signed head (its parent is the plan's) and publishes only the authority. An identical
  apply after completion is ``replayed``. ``operator_apply`` off refuses every apply
  (``operator_apply_disabled``).
* hq-service v0.1.4 (bh-hq-gr9): every operator plan takes ``can_sign`` (``can_sign_supported``
  off models v0.1.3, which refuses it as an unknown field) and answers ``signature_needed`` when
  the service is not ``trusted``, the authority is ``authority_signed`` or the request offers
  ``can_sign``; an unsigned apply is then ``operator_signature_required`` with the service's
  reason. A signed apply leaves the authority signed, an unsigned one unsigned. ``POST
  /admissions/{frame}`` (open, with ``authority_state``) refuses the unsigned admission over a
  signed authority (or with ``can_sign``); ``"plan": true`` answers the record to sign over the
  next ``authority_state`` (stamped at the server clock, expiry ``max(old, issued + 3600)``),
  and the planned apply (``issued_at``, ``config_head``, ``expires_at``, ``signature``,
  ``record``) is judged as the service does: config head, plan window (``plan_expired`` after
  900 s, ``operator_state_invalid`` 30 s ahead), ``plan_mismatch`` and the signature under
  ``operator_key``. ``refusals`` maps a ``(method, path)`` to an envelope refusal.
* ``GET /hive-leases?prefix=`` serves ``hive_leases[prefix]`` (``unknown_prefix`` otherwise) and
  ``GET /release-catalog/{catalog}`` lists ``catalog_rows`` or, with ``release_id`` and
  ``release_digest`` together, folds them into the verdict ``{id, digest, verified, row}``.
* With ``authority_state`` set (bh-obhfx), an open admission is the service's
  (``store/admission.go`` ``open``): it reads the manifest PUT committed, derives the authority
  (audience and profile from the first record, the epoch from the frame's floor, the config head)
  and its principal, refuses an incarnation POST /incarnations did not register
  (``incarnation_unprovisioned``), writes the ACTIVE grant into ``authority_state`` and provisions
  the principal, so the session routes then renew it. A ``reads`` value may be a zero-argument
  callable, evaluated per request (a read that follows the writes).
* ``POST /hive-leases/{prefix}`` (bh-h9dn1) takes the frame's signed hive-lease request envelope:
  the request's prefix must be the path's, its principal a provisioned one whose granted key
  signed it (``authority_failed``) and whose grant it names (``request_grant_mismatch``), its
  ``expected_revision`` the current row's (``""`` when absent; ``cas_conflict``), an adopt must
  advance the epoch, a renew or release must come from the incumbent. Accepted, the row becomes
  the request's lease at revision ``sha256(canonical(request))``; the same request again is
  ``replayed``. Not modelled: hive policy, receipts and incumbent eviction (``lease.Decide``).
* With ``caller`` set (``{kind, frame_id, host_id}``) the write routes are caller-bound as at
  v0.1.0: a caller not bound to the path's frame or host, or to the body's host, is a 403
  ``caller_not_bound``.
* With ``gateway_kinds`` set as well, the Gateway (beadhive-gateway v0.1.1 ``authorizeHQ``)
  refuses a caller of the wrong kind for ``POST /incarnations`` (operator) or ``POST
  /admissions/{frame}`` (frame) before the service sees it: a bare 403 ``{"error": "principal is
  not bound to this route"}``, no envelope (bh-garne). ``gateway_refusals`` maps a ``(method,
  path)`` to a bare Gateway 403 reason answered whatever the caller.

Every write CASes ``expected_revision`` on ``head`` (``stale_revision``, retryable). The history
read takes exactly one ``since`` (``missing_floor`` / ``invalid_floor``). ``requests`` records
every ``(method, path, query, body)`` the service saw, so a test can pin the wire shape.

Rules it does NOT enforce (the staged stack does): the gateway attestation and its scope, operator
authority verification (``authority_failed``), candidate-grant expiry, lease-duration range
codes beyond what the schema model rejects, request-size limits, the cross-principal checks, the
transactional CAS conflicts (``floor_cas_conflict``) and ``deadline_exceeded``. The server clock
is a caller-supplied callable, so a test controls time and nothing here sleeps.

The fake owns no I/O: ``transport()`` is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_service_adapter import PREFIX
from beadhive.hq_sql_signatures import (
    AUTHORITY_DOMAIN,
    CATALOG_DOMAIN,
    SqlSignatureError,
    canonical,
    fingerprint,
    sql_principal,
    verify_authority,
    verify_heartbeat,
    verify_hive_request,
)

#: ``(status, message)`` per refusal code the two session routes can answer.
CODES: dict[str, tuple[int, str]] = {
    "unprovisioned_principal": (409, "unprovisioned frame principal"),
    "no_current_grant": (409, "principal has no current operator-granted incarnation"),
    "grant_retired": (409, "retired incarnation cannot publish evidence"),
    "beat_not_fresh": (409, "heartbeat first observation expired or future-skewed"),
    "seq_not_advanced": (409, "heartbeat replays protected accepted floor"),
    "floor_unprovisioned": (409, "protected observer floor was not provisioned"),
    "identity_mismatch": (409, "the identity differs from the protected grant"),
    "request_not_canonical": (409, "the request payload is not canonical"),
    "epoch_stale": (409, "the path epoch is below the principal's committed epoch"),
    "epoch_future": (409, "the path epoch is above the principal's committed epoch"),
    "path_invalid": (400, "a path segment is not in canonical form"),
    "beat_signature_invalid": (409, "the beat is not signed by the granted key"),
    "beat_schema_invalid": (409, "the signed beat violates the FrameLease schema"),
    "request_malformed": (400, "the body is not one JSON document"),
    "content_type_invalid": (415, "Content-Type is not application/json"),
    "no_current_beat": (409, "evidence was posted before any beat was accepted for the principal"),
    "evidence_digest_unmatched": (
        409,
        "the report's canonical digest is not the report_digest of the principal's current beat",
    ),
    "evidence_invalid": (409, "the report is not a valid heartbeat measurement report"),
    "incarnation_unprovisioned": (409, "the incarnation is not registered with this service"),
}

#: Codes the registry marks retryable (``conformance/http/v1/codes.json``).
RETRYABLE = frozenset({"stale_revision", "storage_unavailable"})

_REVISION = re.compile(r"^[0-9a-v]{32}$")
_PRINCIPAL = re.compile(r"^frame_[0-9a-f]{20}$")
_FRAME = re.compile(r"^[a-z][a-z0-9-]{0,127}$")


#: ``(status, message)`` per code the admission, operator and read routes answer
#: (``conformance/admission/v1``, ``operator/v1`` and ``http/v1`` refusal registries, v0.1.0).
V0_CODES: dict[str, tuple[int, str]] = {
    "stale_revision": (409, "the head moved from the expected revision"),
    "operator_signature_invalid": (409, "the signature does not verify against the operator key"),
    "incarnation_invalid": (
        400,
        "the incarnation body is not principal, epoch and expected_revision",
    ),
    "incarnation_registered": (409, "the principal registry already routes this incarnation"),
    "manifest_invalid": (400, "the body is not a valid host manifest"),
    "host_id_mismatch": (409, "the manifest names another host than the path"),
    "admission_invalid": (400, "the admission request is invalid"),
    "rebind_invalid": (400, "the rebind request is not a frame and a duration"),
    "release_policy_invalid": (400, "the release policy request or its spec is invalid"),
    "catalog_invalid": (400, "the release catalog request is invalid"),
    "catalog_row_exists": (409, "the catalog already holds this release active"),
    "catalog_version_taken": (409, "the version already has another active digest"),
    "operator_signature_required": (409, "a signed fleet's publication needs the signature"),
    "operator_state_invalid": (409, "the authority it would publish is invalid"),
    "plan_mismatch": (409, "the apply is not what the plan request yields"),
    "missing_floor": (400, "exactly one since parameter is required"),
    "invalid_floor": (400, "since is not a revision id"),
    "unknown_floor": (409, "since names no known revision"),
    "invalid_frame": (400, "the frame path segment is not a frame id"),
    "unknown_frame": (404, "the authority has no such frame"),
    "caller_not_bound": (403, "the caller is not bound to the frame or host this request names"),
    "invalid_release": (400, "release_id and release_digest are given together, once each"),
    "missing_prefix": (400, "exactly one prefix parameter is required"),
    "invalid_prefix": (400, "prefix is not a hive prefix"),
    "unknown_prefix": (404, "no hive lease names that prefix"),
    "query_not_allowed": (400, "a query string where none is defined"),
    "manifest_frame_mismatch": (409, "the host's committed manifest names another frame"),
    "manifest_incomplete": (
        409,
        "admission needs a committed manifest with frame_id, instance_ref, release and "
        "capabilities",
    ),
    "admission_conflict": (409, "the frame already has an active or pending incarnation"),
    # hq-service v0.1.3 signed manifest publication (``conformance/operator/v1``, bh-hq-0ci).
    "operator_apply_disabled": (
        403,
        "this service does not write the authority table for operator publication",
    ),
    "plan_expired": (409, "the plan is older than its window; plan again"),
    "working_set_dirty": (409, "the runtime working set is not clean"),
    "committed_unreadable": (500, "the committed result could not be read back"),
    "storage_unavailable": (503, "storage is unavailable"),
}


#: Codes the service answers that no copied corpus registry lists with a status: admission's
#: missing committed manifest (``store/admission.go`` ``committedManifest``; the registration
#: registry names it without one).
UNLISTED_CODES: dict[str, tuple[int, str]] = {
    "host_manifest_unavailable": (409, "committed canonical host manifest unavailable"),
}

#: ``(status, message)`` per ``conformance/hive-lease/v1`` code the hive-lease submission answers
#: (bh-h9dn1): each a 409 rule refusal, as hq-service's registry loads a per-corpus code.
HIVE_LEASE_CODES: dict[str, tuple[int, str]] = {
    "authority_failed": (409, "hive request authentication or authority failed"),
    "request_grant_mismatch": (409, "signed hive request differs from current grant"),
    "cas_conflict": (409, "original protected hive lease CAS conflict"),
    "epoch_not_advanced": (409, "adopt hive epoch must advance"),
    "renew_not_current_incumbent": (409, "renew requires current exact incumbent"),
    "release_not_exact_incumbent": (409, "release requires exact incumbent incarnation/epoch"),
    "no_active_grant": (409, "no active operator-granted frame"),
}


#: ``POST /admissions/{frame}``'s body (v0.1.0) and the signed admission's fields (v0.1.4).
ADMISSION_FIELDS = frozenset({"kind", "expected_revision", "host_id", "public_key"})
SIGNED_ADMISSION_FIELDS = frozenset(
    {"plan", "can_sign", "issued_at", "config_head", "expires_at", "signature", "record"}
)


def error_response(code: str, reason: str | None = None) -> httpx.Response:
    """The service's error envelope for *code* (ADR bh-hq-pio.5), as a response: the message
    is the registry's, and ``reason`` (the service's own, e.g. why a signature is required) is
    ``details.reason`` as hq-service ``api/errors.go`` carries it."""
    status, message = {**CODES, **V0_CODES, **UNLISTED_CODES, **HIVE_LEASE_CODES}[code]
    body = {
        "error": {
            "code": code,
            "message": message,
            "retryable": code in RETRYABLE,
            "requestId": "req-fake",
            "details": {"reason": reason} if reason else None,
        }
    }
    return httpx.Response(status, json=body)


@dataclass
class Principal:
    """One provisioned frame principal: its grant and the service's protected state."""

    frame_id: str
    holder: str
    instance: str
    epoch: int
    public_key: str
    grant: str = "active"  # active | none | retired
    floor_seq: int | None = 0  # ``None``: the floor was never provisioned
    floor_digest: str = ""
    observation: dict[str, Any] | None = None  # the CURRENT beat: seq, digest, lease, first_seen
    receipts: dict[tuple[int, str], float] = field(default_factory=dict)
    evidence: dict[str, float] = field(default_factory=dict)  # report digest -> stored at
    renewed_at: float | None = None
    measured_at: float | None = None


class FakeHqService:
    """The session routes of hq-service, in process."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.principals: dict[str, Principal] = {}
        #: every request the service SAW (injected client-side faults never reach it):
        #: ``(kind, outcome_or_code)`` in arrival order.
        self.seen: list[tuple[str, str]] = []
        #: Read routes (bh-e437a): ``GET`` path under the plane prefix (``/authority``,
        #: ``/config``, ``/eligibility/<frame>``) -> the JSON body served. Bodies are opaque
        #: here; the client's response verifier gives them meaning. Unset paths are 404.
        self.reads: dict[str, Any] = {}
        #: The runtime head and the config head the write routes CAS on and answer with.
        self.head = "a" * 32
        self.config_head = "c" * 32
        #: ``(method, path under the plane prefix, query string, decoded JSON body or None)`` of
        #: every request seen, in order.
        self.requests: list[tuple[str, str, str, Any]] = []
        #: Incarnations ``POST /incarnations`` registered: ``(principal, epoch)``.
        self.incarnations: set[tuple[str, int]] = set()
        #: The record a rebind or release-policy plan hands out for signing.
        self.operator_record: dict[str, Any] = {"revision": 2, "state": {}, "hive_policies": {}}
        #: Operator public key an apply's signature must verify under; ``None`` accepts any
        #: signature text. ``trusted`` lets an apply publish with no signature.
        self.operator_key: str | None = None
        self.trusted = False
        #: Manifests (host ids) in the committed config; PUT adds, DELETE removes.
        self.manifests: set[str] = set()
        #: ``hq_release_catalog`` rows, whole (``CatalogEntry``), served by the catalog route.
        self.catalog_rows: list[dict[str, Any]] = []
        #: ``GET /hive-leases`` bodies by prefix.
        self.hive_leases: dict[str, dict[str, Any]] = {}
        #: The attested caller the Gateway minted: ``{kind, frame_id, host_id}``; ``None`` skips
        #: the caller-binding rules (an unbound test).
        self.caller: dict[str, str] | None = None
        #: Gateway v0.1.1 kind/scope enforcement on the join's two routes (bh-garne).
        self.gateway_kinds = False
        #: ``(method, path under the plane prefix)`` -> a bare Gateway 403 reason.
        self.gateway_refusals: dict[tuple[str, str], str] = {}
        #: The committed manifest bodies PUT published, by host id (bh-obhfx).
        self.manifest_bodies: dict[str, dict[str, Any]] = {}
        #: The operator authority state (``{revision, frames}``) an open admission writes into,
        #: as the service does (bh-obhfx). ``None`` keeps the canned admission answer.
        self.authority_state: dict[str, Any] | None = None
        #: Manifest publication (bh-hb75g): the manifests the authority's pinned config head
        #: serves, set by each completed publication apply; whether applies are enabled; how many
        #: applies fail between the config and the authority commit; the base head each host's
        #: publication was committed on; and the last completed apply body, for replay.
        self.served_manifests: dict[str, dict[str, Any]] = {}
        self.operator_apply = True
        self.crash_between_commits = 0
        self._published_on: dict[str, str] = {}
        self._completed_publications: list[dict[str, Any]] = []
        #: hq-service v0.1.4 (bh-hq-gr9): whether the current authority carries a verifying
        #: operator signature (an operator publication or admission over it then needs one), and
        #: whether plans know ``can_sign`` (``False`` models v0.1.3: an unknown field).
        self.authority_signed = False
        self.can_sign_supported = True
        #: ``(method, path under the plane prefix)`` -> ``(code, message)``: an envelope refusal
        #: the service answers whatever the body.
        self.refusals: dict[tuple[str, str], tuple[str, str]] = {}

    def provision(self, principal: str, **kwargs: Any) -> Principal:
        record = Principal(**kwargs)
        self.principals[principal] = record
        return record

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- routing ------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        plane = path.removeprefix(PREFIX)
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        self.requests.append((request.method, plane, request.url.query.decode(), body))
        refusal = self._gateway(request.method, plane)
        if refusal:
            return httpx.Response(403, json={"error": refusal})
        if (request.method, plane) in self.refusals:
            code, message = self.refusals[(request.method, plane)]
            self.seen.append(("write", code))
            return error_response(code, message)
        if path.startswith(PREFIX):
            routed = self._route_v0(request, plane, body)
            if routed is not None:
                return routed
        if request.method == "GET" and path.removeprefix(PREFIX) in self.reads:
            self.seen.append(("read", path.removeprefix(PREFIX)))
            served = self.reads[path.removeprefix(PREFIX)]
            return httpx.Response(200, json=served() if callable(served) else served)
        parts = path.removeprefix(PREFIX).split("/")
        if (
            request.method != "POST"
            or not path.startswith(PREFIX + "/sessions/")
            or len(parts) != 5
            or parts[4] not in ("renew", "evidence")
        ):
            return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})
        kind = parts[4]
        code, response = self._decide(request, parts[2], parts[3], kind)
        self.seen.append((kind, code or str(json.loads(response.content).get("outcome"))))
        return response

    # ---- reads and writes of hq-service v0.1.0 (api/reads.go, api/writes.go) ---------------

    def _route_v0(self, request: httpx.Request, plane: str, body: Any) -> httpx.Response | None:
        """The history read and the write routes; ``None`` leaves the path to the older routes."""
        method = request.method
        if method == "GET" and plane == "/history" and plane in self.reads:
            return self._history(request)
        if method == "GET" and plane.startswith("/eligibility/") and plane in self.reads:
            if not _FRAME.match(plane.removeprefix("/eligibility/")):
                return self._refuse("invalid_frame")
            if self._measured(request) is False:
                return self._refuse("invalid_release")
            return None
        if method == "GET" and plane == "/hive-leases":
            return self._hive_lease(request)
        parts = plane.split("/")[1:]
        if method == "POST" and len(parts) == 2 and parts[0] == "hive-leases":
            return self._write(request, body, lambda b: self._submit_hive_lease(parts[1], b))
        if method == "GET" and len(parts) == 2 and parts[0] == "release-catalog":
            return self._catalog(request, parts[1])
        if method == "DELETE" and len(parts) == 2 and parts[0] == "manifests":
            return self._write(request, None, lambda b: self._withdraw(parts[1]))
        if method == "POST" and parts == ["incarnations"]:
            return self._write(request, body, self._incarnation)
        if method == "PUT" and len(parts) == 2 and parts[0] == "manifests":
            return self._write(request, body, lambda b: self._manifest(parts[1], b))
        if method == "POST" and len(parts) == 2 and parts[0] == "admissions":
            return self._write(request, body, lambda b: self._admission(parts[1], b))
        if (
            method == "POST"
            and len(parts) == 4
            and parts[0] == "release-catalog"
            and parts[2] == "import"
            and parts[3] in ("plan", "apply")
            and _FRAME.match(parts[1])
        ):
            return self._write(request, body, lambda b: self._catalog_import(parts[1], b, parts[3]))
        if (
            method == "POST"
            and len(parts) == 4
            and parts[0] == "manifests"
            and parts[2] == "publish"
            and parts[3] in ("plan", "apply")
        ):
            return self._write(
                request, body, lambda b: self._manifest_publish(parts[1], b, parts[3])
            )
        if method == "POST" and parts[:2] == ["authority", "rebind"] and len(parts) == 3:
            if parts[2] in ("plan", "apply"):
                return self._write(request, body, lambda b: self._operate("rebind", b, parts[2]))
        if method == "POST" and parts[:2] == ["authority", "release-policy"] and len(parts) == 4:
            if parts[3] in ("plan", "apply") and _FRAME.match(parts[2]):
                return self._write(
                    request, body, lambda b: self._operate("release-policy", b, parts[3])
                )
        return None

    @staticmethod
    def _measured(request: httpx.Request) -> dict[str, str] | None | bool:
        """The measured release from the query: ``None`` without one, ``False`` when invalid."""
        items = request.url.params.multi_items()
        if not items:
            return None
        params = dict(items)
        if len(items) != 2 or set(params) != {"release_id", "release_digest"}:
            return False
        return {"id": params["release_id"], "digest": params["release_digest"]}

    def _hive_lease(self, request: httpx.Request) -> httpx.Response:
        keys = [key for key, _ in request.url.params.multi_items()]
        if keys != ["prefix"]:
            return self._refuse("missing_prefix")
        prefix = request.url.params["prefix"]
        if prefix not in self.hive_leases:
            return self._refuse("unknown_prefix")
        self.seen.append(("read", "/hive-leases"))
        return httpx.Response(200, json=self.hive_leases[prefix])

    def _submit_hive_lease(self, prefix: str, envelope: dict):
        request = envelope.get("request")
        if not isinstance(request, dict) or request.get("prefix") != prefix:
            return "request_malformed", None  # request.prefix must match the path
        record = self.principals.get(str(request.get("principal")))
        if record is None:
            return "unprovisioned_principal", None
        try:
            verify_hive_request(envelope, granted_public_key=record.public_key)
        except SqlSignatureError:
            return "authority_failed", None
        if record.grant != "active":
            return "no_active_grant", None
        if (
            request.get("frame_id"),
            request.get("holder_identity"),
            request.get("instance_ref"),
            request.get("epoch"),
        ) != (record.frame_id, record.holder, record.instance, record.epoch):
            return "request_grant_mismatch", None
        revision = hashlib.sha256(canonical(request)).hexdigest()
        current = self.hive_leases.get(prefix)
        if current is not None and current["revision"] == revision:
            return None, {"outcome": "replayed", "revision": revision}
        if request.get("expected_revision") != (current["revision"] if current else ""):
            return "cas_conflict", None
        lease, operation = request.get("lease") or {}, request.get("operation")
        prior = current["lease"] if current else None
        holder = request.get("holder_identity")
        if operation == "adopt" and prior is not None and lease.get("epoch", 0) <= prior["epoch"]:
            return "epoch_not_advanced", None
        if operation == "renew" and (prior is None or prior["host_id"] != holder):
            return "renew_not_current_incumbent", None
        if operation == "release" and (
            prior is None or prior["host_id"] != holder or lease.get("epoch") != prior["epoch"]
        ):
            return "release_not_exact_incumbent", None
        self.hive_leases[prefix] = {
            "head": self.head,
            "prefix": prefix,
            "revision": revision,
            "lease": dict(lease),
            "held": bool(lease.get("host_id")),
        }
        return None, {"outcome": "accepted", "revision": revision}

    def _catalog(self, request: httpx.Request, catalog: str) -> httpx.Response:
        if not _FRAME.match(catalog):
            return self._refuse("path_invalid")
        measured = self._measured(request)
        if measured is False:
            return self._refuse("invalid_release")
        rows = [r for r in self.catalog_rows if r["catalog"] == catalog]
        body: dict[str, Any] = {
            "head": self.head,
            "catalog": catalog,
            "rows": rows,
            "release": None,
        }
        if measured:
            body["rows"] = None
            body["release"] = self._verdict(rows, catalog, measured)
        self.seen.append(("read", "/release-catalog"))
        return httpx.Response(200, json=body)

    @staticmethod
    def _verdict(rows: list[dict], catalog: str, measured: dict[str, str]) -> dict[str, Any]:
        """``release.Fold`` as ``store/catalog.go`` judges it: a digest is active with an
        ``active`` row and no ``revoked`` tombstone, and verified when also the version's only
        active digest, measured by the catalog's algorithm, and named by the measured id."""
        digest, version = measured["digest"], measured["id"]
        versions = sorted({r["version"] for r in rows if r["measured_digest"] == digest})
        verdict = {"id": version, "digest": digest, "verified": False, "row": None}
        if not versions:
            return verdict
        chosen = version if version in versions else versions[0]
        scoped = [r for r in rows if r["version"] == chosen]
        tombstoned = {r["measured_digest"] for r in scoped if r["status"] == "revoked"}
        active = {r["measured_digest"] for r in scoped if r["status"] == "active"} - tombstoned
        algorithm = next((r["algorithm"] for r in scoped if r["measured_digest"] == digest), "")
        row = {
            "catalog": catalog,
            "version": chosen,
            "measured_digest": digest,
            "status": "active" if digest in active else "revoked",
            "algorithm": algorithm,
            "active_digests": len(active),
        }
        verdict["row"] = row
        verdict["verified"] = (
            row["status"] == "active"
            and algorithm == "beadhive-tree-sha256-v1"
            and len(active) == 1
            and chosen == version
        )
        return verdict

    def _gateway(self, method: str, plane: str) -> str | None:
        """The bare Gateway 403 reason for this request, or ``None`` to forward it."""
        if (method, plane) in self.gateway_refusals:
            return self.gateway_refusals[(method, plane)]
        if not self.gateway_kinds or self.caller is None or method != "POST":
            return None
        if plane == "/incarnations":
            need = "operator"
        elif plane.startswith("/admissions/"):
            need = "frame"
        else:
            return None
        return None if self.caller.get("kind") == need else "principal is not bound to this route"

    def _bound(self, kind: str, frame: str | None, host_id: str | None) -> str | None:
        """The ``bind*`` rules of ``api/writes.go``: a reason when the caller is not bound."""
        caller = self.caller
        if caller is None:
            return None
        if caller.get("kind") != kind:
            return f"the caller is not bound as {kind}"
        if frame is not None and caller.get("frame_id") != frame:
            return "the caller is bound to another frame than the path"
        if host_id is not None and caller.get("host_id") != host_id:
            return "the caller is bound to another host"
        return None

    @staticmethod
    def _refuse(code: str) -> httpx.Response:
        return error_response(code)

    def _history(self, request: httpx.Request) -> httpx.Response:
        keys = [key for key, _ in request.url.params.multi_items()]
        if keys != ["since"]:
            return self._refuse("missing_floor")
        if not _REVISION.match(request.url.params["since"]):
            return self._refuse("invalid_floor")
        self.seen.append(("read", "/history"))
        return httpx.Response(200, json=self.reads["/history"])

    def _write(self, request: httpx.Request, body: Any, handler) -> httpx.Response:
        if request.url.query:
            return self._refuse("query_not_allowed")
        json_body = request.headers.get("content-type", "").split(";")[0].strip()
        if request.method != "DELETE" and json_body != "application/json":
            return self._refuse("content_type_invalid")
        if request.method != "DELETE" and not isinstance(body, dict):
            return self._refuse("request_malformed")
        refusal, facts = handler(body)
        if refusal:
            code, message = refusal if isinstance(refusal, tuple) else (refusal, None)
            self.seen.append(("write", code))
            return error_response(code, message)
        self.seen.append(("write", str(facts["outcome"])))
        return httpx.Response(200, json=facts)

    def _new_head(self, *parts: Any) -> str:
        self.head = hashlib.sha256(
            json.dumps([self.head, *parts], default=str).encode()
        ).hexdigest()[:32]
        return self.head

    def _incarnation(self, body: dict):
        if self._bound("operator", None, None):
            return "caller_not_bound", None
        epoch = body.get("epoch")
        if (
            set(body) != {"principal", "epoch", "expected_revision"}
            or not isinstance(body["principal"], str)
            or not _PRINCIPAL.match(body["principal"])
            or type(epoch) is not int
            or not 0 <= epoch <= 9_999_999_999
            or not isinstance(body["expected_revision"], str)
            or not _REVISION.match(body["expected_revision"])
        ):
            return "incarnation_invalid", None
        if body["expected_revision"] != self.head:
            return "stale_revision", None
        key = (body["principal"], epoch)
        outcome = "replayed" if key in self.incarnations else "accepted"
        self.incarnations.add(key)
        return None, {
            "outcome": outcome,
            "revision": self.head,
            "principal": body["principal"],
            "epoch": epoch,
        }

    def _withdraw(self, host_id: str):
        if self._bound("host", None, host_id):
            return "caller_not_bound", None
        facts = {"host_id": host_id, "path": f"hosts/{host_id}.yaml"}
        if host_id not in self.manifests:
            return None, {"outcome": "replayed", "revision": self.config_head, **facts}
        self.manifests.discard(host_id)
        self.manifest_bodies.pop(host_id, None)
        self.config_head = hashlib.sha256((self.config_head + "-" + host_id).encode()).hexdigest()[
            :32
        ]
        return None, {"outcome": "accepted", "revision": self.config_head, **facts}

    def _manifest(self, host_id: str, body: dict):
        if self._bound("host", None, host_id):
            return "caller_not_bound", None
        if not body.get("host_id") or not isinstance(body["host_id"], str):
            return "manifest_invalid", None
        if body["host_id"] != host_id:
            return "host_id_mismatch", None
        document = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        self.manifests.add(host_id)
        self.manifest_bodies[host_id] = body
        self.config_head = hashlib.sha256(self.config_head.encode() + document).hexdigest()[:32]
        return None, {
            "outcome": "accepted",
            "revision": self.config_head,
            "host_id": host_id,
            "path": f"hosts/{host_id}.yaml",
            "sha256": hashlib.sha256(document).hexdigest(),
        }

    def _admission(self, frame: str, body: dict):
        if not _FRAME.match(frame):
            return "path_invalid", None
        if self._bound("frame", frame, body.get("host_id")):
            return "caller_not_bound", None
        kind, host_id = body.get("kind"), body.get("host_id", "")
        planned = "issued_at" in body
        apply_fields = set(body) & (SIGNED_ADMISSION_FIELDS - {"plan", "can_sign"})
        if (
            set(body) - ADMISSION_FIELDS - SIGNED_ADMISSION_FIELDS
            or kind not in ("open", "candidate")
            or not isinstance(body.get("expected_revision"), str)
            or not _REVISION.match(body["expected_revision"])
            or not isinstance(host_id, str)
            or (kind == "open" and (not host_id or not body.get("public_key")))
            or (kind == "candidate" and "public_key" in body)
            or any(not isinstance(body.get(f, False), bool) for f in ("plan", "can_sign"))
            or (body.get("plan") and apply_fields)
            or (not planned and apply_fields)
        ):
            return "admission_invalid", None
        if body["expected_revision"] != self.head:
            return "stale_revision", None
        if self.authority_state is not None and kind == "open":
            return self._open_admission(frame, host_id, str(body["public_key"]).strip(), body)
        principal = "frame_" + hashlib.sha256(f"{frame}/{host_id}".encode()).hexdigest()[:20]
        revision = self._new_head("admission", frame, kind)
        return None, {
            "outcome": "accepted",
            "revision": revision,
            "frame_id": frame,
            "host_id": host_id,
            "kind": kind,
            "principal": principal,
            "epoch": 0,
        }

    def _open_admission(self, frame: str, host_id: str, key: str, body: dict):
        """``store/admission.go`` ``open``: the ACTIVE grant hq-service builds and records,
        unsigned, or planned and signed over an operator-signed authority (v0.1.4)."""
        from beadhive.hq_framelease_contracts import ObservationAuthority

        state = self.authority_state
        assert state is not None
        frames = state.setdefault("frames", {})
        records = [
            record
            for entry in frames.values()
            for record in (entry.get("active"), entry.get("candidate"))
            if record
        ]
        entry = frames.get(frame)
        if entry is not None:
            active = entry.get("active")
            if (
                active
                and active["authority"]["holder_identity"] == host_id
                and active["public_key"] == key
                and active["state"] != "retired"
            ):
                return None, {
                    "outcome": "replayed",
                    "revision": self.head,
                    "frame_id": frame,
                    "host_id": host_id,
                    "kind": "open",
                }
            if entry.get("active") or entry.get("candidate"):
                return "admission_conflict", None
        manifest = self.manifest_bodies.get(host_id)
        if manifest is None:
            return "host_manifest_unavailable", None
        if manifest.get("frame_id") != frame:
            return "manifest_frame_mismatch", None
        if not all(manifest.get(k) for k in ("instance_ref", "release", "capabilities")):
            return "manifest_incomplete", None
        audience, profile = manifest.get("beadyard_id") or "beadhive", "open-admission"
        if records:
            audience = records[0]["authority"]["audience"]
            profile = records[0]["desired"]["profile"]
        epoch = 0 if entry is None else int(entry["epoch_floor"]) + 1
        authority = ObservationAuthority(
            frame_id=frame,
            holder_identity=host_id,
            instance_ref=manifest["instance_ref"],
            key_fingerprint=fingerprint(key),
            epoch=epoch,
            audience=audience,
            config_revision=self.config_head,
            beadyard_id=manifest.get("beadyard_id"),
        )
        principal = sql_principal(authority)
        if (principal, epoch) not in self.incarnations:
            return "incarnation_unprovisioned", None
        granted = {
            "active": {
                "authority": {
                    k: v
                    for k, v in vars(authority).items()
                    if v is not None or k == "candidate_expires_at"
                },
                "public_key": key,
                "state": "active",
                "desired": {
                    "declared": True,
                    "release": manifest["release"],
                    "caps": manifest["capabilities"],
                    "profile": profile,
                },
                "cordoned": False,
            },
            "candidate": None,
            "retired": [],
            "epoch_floor": epoch,
        }
        facts = {
            "frame_id": frame,
            "host_id": host_id,
            "kind": "open",
            "principal": principal,
            "epoch": epoch,
        }
        needed, why = self.authority_signed or body.get("can_sign") is True, ""
        if needed:
            why = (
                "the current authority is operator-signed: an unsigned publication would unsign "
                "it and break every signed-mode host; sign the plan's signing_input"
                if self.authority_signed
                else "the request offers the operator's signature (can_sign); sign the plan's "
                "signing_input"
            )
        if not body.get("plan") and "issued_at" not in body:
            if needed:
                return (
                    "operator_signature_required",
                    why + '; plan the admission with "plan": true and apply it with the plan\'s '
                    "issued_at, config_head, expires_at and signature",
                ), None
            frames[frame] = granted
            state["revision"] = int(state.get("revision", 0)) + 1
            self.authority_signed = False
            return None, self._admitted(principal, granted, manifest, facts, signature=None)
        now = self._clock()
        issued = now
        if "issued_at" in body:
            if body.get("config_head") != self.config_head:
                return "stale_revision", None
            issued = float(body["issued_at"])
            if issued > now + 30:
                return "operator_state_invalid", None
            if now - issued > 900:
                return "plan_expired", None
        expires = max(float(state.get("expires_at") or 0), issued + 3600)
        after = json.loads(json.dumps(state))
        after["frames"][frame] = granted
        after.update(revision=int(state.get("revision", 0)) + 1, issued_at=issued)
        after["expires_at"] = expires
        record = {
            "revision": after["revision"],
            "config_head": self.config_head,
            "state": after,
            "hive_policies": {},
        }
        signing_input = AUTHORITY_DOMAIN + canonical(record)
        if body.get("plan"):
            return None, {
                "outcome": "planned",
                "revision": self.head,
                "config_head": self.config_head,
                "record": record,
                "signing_input": base64.b64encode(signing_input).decode(),
                "signing_sha256": hashlib.sha256(signing_input).hexdigest(),
                "issued_at": issued,
                "expires_at": expires,
                "plan_expires_at": issued + 900,
                "signature_needed": needed,
                "summary": facts,
            }
        if body.get("expires_at") != expires:
            return "plan_mismatch", None
        if "record" in body and body["record"] != record:
            return "plan_mismatch", None
        signature = body.get("signature")
        if signature is None:
            if needed:
                return ("operator_signature_required", why), None
        elif self.operator_key is not None:
            try:
                verify_authority(record, signature, granted_public_key=self.operator_key)
            except SqlSignatureError:
                return "operator_signature_invalid", None
        self.authority_state = after
        self.authority_signed = signature is not None
        return None, {
            **self._admitted(principal, granted, manifest, facts, signature=signature),
            "signed": signature is not None,
        }

    def _admitted(self, principal, granted, manifest, facts, *, signature):
        """Provision the admitted principal (its route) and answer the accepted admission."""
        authority = granted["active"]["authority"]
        self.provision(
            principal,
            frame_id=authority["frame_id"],
            holder=authority["holder_identity"],
            instance=manifest["instance_ref"],
            epoch=authority["epoch"],
            public_key=granted["active"]["public_key"],
        )
        return {
            "outcome": "accepted",
            "revision": self._new_head("admission", authority["frame_id"], "open", signature),
            **facts,
        }

    def _catalog_import(self, catalog: str, body: dict, phase: str):
        """``POST /release-catalog/{catalog}/import/{plan,apply}`` (``store/catalog.go``): a
        plan answers the active row and the record to sign under the catalog domain; an apply
        carries ``{expected_revision, issued_at, signature}`` only."""
        fields = {"version", "measured_digest", "wheel_sha256", "commit", "bundle"}
        apply_fields = {"expected_revision", "issued_at", "signature"}
        if set(body) - fields - (apply_fields if phase == "apply" else set()):
            return "catalog_invalid", None
        if not all(isinstance(body.get(key), str) and body[key] for key in fields - {"bundle"}):
            return "catalog_invalid", None
        scoped = [r for r in self.catalog_rows if r["catalog"] == catalog]
        mine = [r for r in scoped if r["version"] == body["version"]]
        revoked = {r["measured_digest"] for r in mine if r["status"] == "revoked"}
        if phase == "apply" and body.get("expected_revision") != self.head:
            return "stale_revision", None
        if any(r["measured_digest"] == body["measured_digest"] for r in mine):
            return "catalog_row_exists", None
        if any(r["status"] == "active" and r["measured_digest"] not in revoked for r in mine):
            return "catalog_version_taken", None
        issued = body["issued_at"] if phase == "apply" else 1793000000.0
        row = {
            "catalog": catalog,
            "version": body["version"],
            "measured_digest": body["measured_digest"],
            "status": "active",
            "algorithm": "beadhive-tree-sha256-v1",
            "wheel_sha256": body["wheel_sha256"],
            "commit": body["commit"],
            "bundle": body.get("bundle", ""),
            "reason": "",
            "recorded_at": issued,
        }
        record = {"domain": "beadhive/hq-catalog/v1", "op": "import", "head": self.head, "row": row}
        signing_input = CATALOG_DOMAIN + canonical(record)
        if phase == "plan":
            return None, {
                "outcome": "planned",
                "revision": self.head,
                "record": record,
                "signing_input": base64.b64encode(signing_input).decode(),
                "signing_sha256": hashlib.sha256(signing_input).hexdigest(),
                "issued_at": issued,
                "plan_expires_at": issued + 900,
                "signature_needed": True,
                "row": row,
            }
        signature = body.get("signature")
        if not signature:
            return "operator_signature_required", None
        if self.operator_key is not None:
            try:
                verify_authority(
                    record, signature, granted_public_key=self.operator_key, domain=CATALOG_DOMAIN
                )
            except SqlSignatureError:
                return "operator_signature_invalid", None
        self.catalog_rows.append(row)
        return None, {
            "outcome": "accepted",
            "revision": self._new_head("catalog-import", row["version"]),
            "op": "import",
            "catalog": catalog,
            "row": row,
        }

    def _operate(self, kind: str, body: dict, phase: str):
        invalid = "rebind_invalid" if kind == "rebind" else "release_policy_invalid"
        plan_fields = (
            {"frame", "duration_s"}
            if kind == "rebind"
            else {"spec", "allow_downgrade", "allow_unknown_readers"}
        )
        apply_fields = {
            "expected_revision", "config_head", "issued_at", "expires_at", "signature", "record",
        }  # fmt: skip
        if unknown := self._unknown_field(body, plan_fields, apply_fields, phase, invalid):
            return unknown, None
        needed, why = self._signature_needed(body)
        if kind == "release-policy" and not str(body.get("spec") or "").strip():
            return invalid, None
        signing_input = AUTHORITY_DOMAIN + canonical(self.operator_record)
        if phase == "plan":
            return None, {
                "outcome": "planned",
                "revision": self.head,
                "config_head": self.config_head,
                "record": self.operator_record,
                "signing_input": base64.b64encode(signing_input).decode(),
                "signing_sha256": hashlib.sha256(signing_input).hexdigest(),
                "issued_at": 1793000000.0,
                "expires_at": 4102444800.0,
                "plan_expires_at": 1793000900.0,
                "signature_needed": needed,
                "summary": {},
            }
        if body.get("expected_revision") != self.head:
            return "stale_revision", None
        if body.get("config_head") != self.config_head:
            return "stale_revision", None
        if "record" in body and body["record"] != self.operator_record:
            return "plan_mismatch", None
        signature = body.get("signature")
        if signature is None and needed:
            return ("operator_signature_required", why), None
        if signature is not None and self.operator_key is not None:
            try:
                verify_authority(
                    self.operator_record, signature, granted_public_key=self.operator_key
                )
            except SqlSignatureError:
                return "operator_signature_invalid", None
        self.authority_signed = signature is not None
        return None, {"outcome": "accepted", "revision": self._new_head(kind, signature)}

    def _unknown_field(self, body, plan_fields, apply_fields, phase, invalid):
        """The ``*_invalid`` refusal of a body field the route does not take, naming it as
        hq-service does (``unknown field "<name>"``); ``can_sign`` only from v0.1.4."""
        known = set(plan_fields) | ({"can_sign"} if self.can_sign_supported else set())
        extra = sorted(set(body) - known - (set(apply_fields) if phase == "apply" else set()))
        if not extra:
            return None
        return invalid, f'unknown field "{extra[0]}"'

    def _signature_needed(self, body) -> tuple[bool, str]:
        """hq-service v0.1.4 ``signatureNeeded``: whether the publication must be signed, and
        the refusal reason when it is not."""
        if not self.trusted:
            return True, (
                "this service publishes unsigned authority only in a trusted fleet (service mode "
                "and the committed hq.default_authority_mode); sign the plan's signing_input"
            )
        if self.authority_signed:
            return True, (
                "the current authority is operator-signed: an unsigned publication would unsign "
                "it and break every signed-mode host; sign the plan's signing_input"
            )
        if body.get("can_sign") is True:
            return True, (
                "the request offers the operator's signature (can_sign); sign the plan's "
                "signing_input"
            )
        return False, ""

    def _manifest_publish(self, host_id: str, body: dict, phase: str):
        """``store/operator_manifest.go`` ``operateManifest``, reduced to what a client sees."""
        apply_fields = {
            "expected_revision", "config_head", "issued_at", "expires_at", "signature", "record",
        }  # fmt: skip
        plan_fields = {"manifest", "duration_s"}
        if unknown := self._unknown_field(
            body, plan_fields, apply_fields, phase, "manifest_invalid"
        ):
            return unknown, None
        needed, why = self._signature_needed(body)
        manifest = body.get("manifest")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("host_id"), str):
            return "manifest_invalid", None
        if manifest["host_id"] != host_id:
            return "host_id_mismatch", None
        if phase == "apply" and not self.operator_apply:
            return "operator_apply_disabled", None
        document = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        written = self.manifest_bodies.get(host_id) == manifest
        path = f"hosts/{host_id}.yaml"
        if phase == "plan":
            base = self.config_head
            after = base if written else hashlib.sha256(base.encode() + document).hexdigest()[:32]
            record = {**self.operator_record, "config_head": after}
            signing_input = AUTHORITY_DOMAIN + canonical(record)
            sha = hashlib.sha256(document).hexdigest()
            return None, {
                "outcome": "planned",
                "revision": self.head,
                "config_head": base,
                "config_head_after": after,
                "record": record,
                "signing_input": base64.b64encode(signing_input).decode(),
                "signing_sha256": hashlib.sha256(signing_input).hexdigest(),
                "issued_at": 1793000000.0,
                "expires_at": 4102444800.0,
                "plan_expires_at": 1793000900.0,
                "signature_needed": needed,
                "manifest": {
                    "host_id": host_id,
                    "path": path,
                    "content": document.decode(),
                    "sha256": sha,
                },
                "config_publication": {
                    "publish": not written,
                    "publication_id": hashlib.sha256(base.encode() + document).hexdigest()[:36],
                    "date": "2026-10-26T08:53:20Z",
                    "base_head": base,
                    "resulting_head": after,
                },
                "summary": {
                    "config_head_before": base,
                    "config_head_after": after,
                    "config_moved": after != base,
                    "host_id": host_id,
                    "path": path,
                    "sha256": sha,
                    "config_published": not written,
                },
            }
        if body in self._completed_publications:
            return None, {"outcome": "replayed", "revision": self.head}
        if body.get("expected_revision") != self.head:
            return "stale_revision", None
        if body.get("config_head") == self.config_head:
            after = (
                self.config_head
                if written
                else hashlib.sha256(self.config_head.encode() + document).hexdigest()[:32]
            )
        elif written and self._published_on.get(host_id) == body.get("config_head"):
            after = self.config_head  # a half-done apply, sent again: only the authority is left
        else:
            return "stale_revision", None
        record = {**self.operator_record, "config_head": after}
        if "record" in body and body["record"] != record:
            return "plan_mismatch", None
        signature = body.get("signature")
        if signature is None and needed:
            return ("operator_signature_required", why), None
        if signature is not None and self.operator_key is not None:
            try:
                verify_authority(record, signature, granted_public_key=self.operator_key)
            except SqlSignatureError:
                return "operator_signature_invalid", None
        published = after != self.config_head
        if published:
            self.manifests.add(host_id)
            self.manifest_bodies[host_id] = manifest
            self._published_on[host_id] = self.config_head
            self.config_head = after
        if self.crash_between_commits > 0:
            self.crash_between_commits -= 1
            return "storage_unavailable", None
        self.served_manifests = dict(self.manifest_bodies)
        self._completed_publications.append(body)
        self.authority_signed = signature is not None
        return None, {
            "outcome": "accepted",
            "revision": self._new_head("manifest", host_id, signature),
            "kind": "manifest",
            "signed": signature is not None,
            "host_id": host_id,
            "path": path,
            "sha256": hashlib.sha256(document).hexdigest(),
            "config_head": after,
            "config_published": published,
        }

    def _decide(
        self, request: httpx.Request, principal: str, epoch_text: str, kind: str
    ) -> tuple[str | None, httpx.Response]:
        def refuse(code: str) -> tuple[str, httpx.Response]:
            return code, error_response(code)

        if not epoch_text.isdigit() or (epoch_text != "0" and epoch_text.startswith("0")):
            return refuse("path_invalid")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return refuse("content_type_invalid")
        record = self.principals.get(principal)
        if record is None:
            return refuse("unprovisioned_principal")
        epoch = int(epoch_text)
        if epoch < record.epoch:
            return refuse("epoch_stale")
        if epoch > record.epoch:
            return refuse("epoch_future")
        if record.grant == "none":
            return refuse("no_current_grant")
        if record.grant == "retired":
            return refuse("grant_retired")
        now = self._clock()
        body = request.content
        code, payload = (
            self._renew(record, body, now) if kind == "renew" else self._evidence(record, body, now)
        )
        if code:
            return refuse(code)
        return None, httpx.Response(200, json=payload)

    # ---- renew (store/session.go renew) -------------------------------------------------

    def _renew(self, record: Principal, body: bytes, now: float):
        try:
            value = json.loads(body)
        except ValueError:
            return "request_malformed", None
        if not isinstance(value, dict):
            return "beat_schema_invalid", None
        try:
            if canonical(value) != body:
                return "request_not_canonical", None
        except SqlSignatureError:
            return "request_not_canonical", None
        spec = value.get("spec")
        try:
            lease = HeartbeatLease.model_validate(
                {k: v for k, v in (spec or {}).items() if k != "signature"}
            )
        except ValueError:
            return "beat_schema_invalid", None
        try:
            _, digest = verify_heartbeat(value, granted_public_key=record.public_key)
        except SqlSignatureError:
            return "beat_signature_invalid", None
        if (
            lease.frame_id != record.frame_id
            or lease.holderIdentity != record.holder
            or lease.instance_ref != record.instance
            or lease.epoch != record.epoch
            or lease.key_id != fingerprint(record.public_key)
        ):
            return "identity_mismatch", None
        seconds = lease.leaseDurationSeconds
        age = now - lease.observed_at
        if age < -30 or age >= seconds:
            return "beat_not_fresh", None
        seq = lease.seq
        first_seen = record.receipts.get((seq, digest))
        if first_seen is not None:  # an exact replay, even if a newer beat advanced the floor
            return None, {
                "outcome": "replayed",
                "revision": digest,
                "accepted_until": first_seen + seconds,
            }
        if record.floor_seq is None:
            return "floor_unprovisioned", None
        if seq <= record.floor_seq:
            return "seq_not_advanced", None
        record.receipts[(seq, digest)] = now
        record.floor_seq, record.floor_digest = seq, digest
        record.observation = {
            "seq": seq,
            "digest": digest,
            "report_digest": lease.report_digest,
            "first_seen": now,
            "accepted_until": now + seconds,
        }
        record.renewed_at = now
        return None, {"outcome": "accepted", "revision": digest, "accepted_until": now + seconds}

    # ---- evidence (store/session.go evidence) -------------------------------------------

    def _evidence(self, record: Principal, body: bytes, now: float):
        try:
            report = json.loads(body)
        except ValueError:
            return "request_malformed", None
        if not _valid_report(report):
            return "evidence_invalid", None
        digest = "sha256:" + hashlib.sha256(canonical(report)).hexdigest()
        if record.observation is None or record.observation["seq"] < 1:
            return "no_current_beat", None
        if record.observation["report_digest"] != digest:
            return "evidence_digest_unmatched", None
        outcome = "replayed" if digest in record.evidence else "accepted"
        record.evidence.setdefault(digest, now)  # a replay keeps the stored report
        record.measured_at = now  # ... but the measurement is re-stamped
        return None, {"outcome": outcome, "digest": digest}


def _valid_report(report: Any) -> bool:
    """``validReport``: exactly ``{method, checks, free_sessions}``."""
    if not isinstance(report, dict) or set(report) != {"method", "checks", "free_sessions"}:
        return False
    free = report["free_sessions"]
    if not isinstance(report["method"], str) or type(free) is not int or not 0 <= free <= 1024:
        return False
    if not isinstance(report["checks"], list):
        return False
    for check in report["checks"]:
        if not isinstance(check, dict) or set(check) - {"id", "status", "evidence"}:
            return False
        if not check.get("id") or check.get("status") not in ("pass", "fail", "not-applicable"):
            return False
        evidence = check.get("evidence")
        if "evidence" in check and (not isinstance(evidence, str) or len(evidence) > 1024):
            return False
    return True
