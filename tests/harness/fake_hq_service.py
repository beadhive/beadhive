"""In-process fake of hq-service's session routes (renew and evidence), bh-zephr.

A transport-level stand-in for a staged hq-service, built from the hq-service conformance corpus
(``conformance/session/v1`` and ``conformance/http/v1`` at commit 205fd5b) and the rules in its
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

Rules it does NOT enforce (the staged stack does): the gateway attestation and its scope, operator
authority verification (``authority_failed``), candidate-grant expiry, lease-duration range
codes beyond what the schema model rejects, request-size limits, the cross-principal checks, the
transactional CAS conflicts (``floor_cas_conflict``) and ``deadline_exceeded``. The server clock
is a caller-supplied callable, so a test controls time and nothing here sleeps.

The fake owns no I/O: ``transport()`` is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_service_adapter import PREFIX
from beadhive.hq_sql_signatures import SqlSignatureError, canonical, fingerprint, verify_heartbeat

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
}


def error_response(code: str) -> httpx.Response:
    """The service's error envelope for *code* (ADR bh-hq-pio.5), as a response."""
    status, message = CODES[code]
    body = {
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
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

    def provision(self, principal: str, **kwargs: Any) -> Principal:
        record = Principal(**kwargs)
        self.principals[principal] = record
        return record

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- routing ------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
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
