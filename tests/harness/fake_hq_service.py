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
RETRYABLE = frozenset({"stale_revision"})

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
    "operator_signature_required": (409, "a signed fleet's publication needs the signature"),
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


def error_response(code: str) -> httpx.Response:
    """The service's error envelope for *code* (ADR bh-hq-pio.5), as a response."""
    status, message = {**CODES, **V0_CODES, **UNLISTED_CODES, **HIVE_LEASE_CODES}[code]
    body = {
        "error": {
            "code": code,
            "message": message,
            "retryable": code in RETRYABLE,
            "requestId": "req-fake",
            "details": None,
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
        #: The committed manifest bodies PUT published, by host id (bh-obhfx).
        self.manifest_bodies: dict[str, dict[str, Any]] = {}
        #: The operator authority state (``{revision, frames}``) an open admission writes into,
        #: as the service does (bh-obhfx). ``None`` keeps the canned admission answer.
        self.authority_state: dict[str, Any] | None = None

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
            self.seen.append(("write", refusal))
            return self._refuse(refusal)
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
        if (
            set(body) - {"kind", "expected_revision", "host_id", "public_key"}
            or kind not in ("open", "candidate")
            or not isinstance(body.get("expected_revision"), str)
            or not _REVISION.match(body["expected_revision"])
            or not isinstance(host_id, str)
            or (kind == "open" and (not host_id or not body.get("public_key")))
            or (kind == "candidate" and "public_key" in body)
        ):
            return "admission_invalid", None
        if body["expected_revision"] != self.head:
            return "stale_revision", None
        if self.authority_state is not None and kind == "open":
            return self._open_admission(frame, host_id, str(body["public_key"]).strip())
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

    def _open_admission(self, frame: str, host_id: str, key: str):
        """``store/admission.go`` ``open``: the ACTIVE grant hq-service builds and records."""
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
        frames[frame] = {
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
        state["revision"] = int(state.get("revision", 0)) + 1
        self.provision(
            principal,
            frame_id=frame,
            holder=host_id,
            instance=manifest["instance_ref"],
            epoch=epoch,
            public_key=key,
        )
        return None, {
            "outcome": "accepted",
            "revision": self._new_head("admission", frame, "open"),
            "frame_id": frame,
            "host_id": host_id,
            "kind": "open",
            "principal": principal,
            "epoch": epoch,
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
        if set(body) - plan_fields - (apply_fields if phase == "apply" else set()):
            return invalid, None
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
                "signature_needed": not self.trusted,
                "summary": {},
            }
        if body.get("expected_revision") != self.head:
            return "stale_revision", None
        if body.get("config_head") != self.config_head:
            return "stale_revision", None
        if "record" in body and body["record"] != self.operator_record:
            return "plan_mismatch", None
        signature = body.get("signature")
        if signature is None and not self.trusted:
            return "operator_signature_required", None
        if signature is not None and self.operator_key is not None:
            try:
                verify_authority(
                    self.operator_record, signature, granted_public_key=self.operator_key
                )
            except SqlSignatureError:
                return "operator_signature_invalid", None
        return None, {"outcome": "accepted", "revision": self._new_head(kind, signature)}

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
