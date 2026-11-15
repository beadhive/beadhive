"""The hq-service adapter: the HQ port over the Gateway ``hq`` plane (``hq.lifecycle.v0``).

Each implemented port method issues exactly one request to exactly one route of the plane
(``beadhive-gateway`` ``docs/openapi/hq-lifecycle-v0.yaml``). The caller's identity is the
credential the Gateway authenticates (mTLS client certificate, or the auth proxy in front of
its socket), so no method carries a principal as an argument. Failures map onto the port's
taxonomy; a method with no route raises :class:`HqUnavailable` and never reaches SQL.

Verification of signed authority and the restore floor is a separate step (bh-j99c8). It plugs
in through ``verifier``: authority-bearing reads (authority, eligibility, config) hand the
response to it and return what it returns. With no verifier those reads fail closed, because the
port promises verified results only.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .hq_port import (
    EvidenceReport,
    FrameAnchor,
    HistorySinceFloor,
    HqDeadline,
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

PREFIX = "/hq/lifecycle/v0"
DEADLINE_HEADER = "X-Beadhive-Deadline-Ms"
_DEFAULT_AUTHORITY = "http://hq"

Verifier = Callable[[str, Any], Any]
"""``verifier(method, payload) -> verified result`` for an authority-bearing read."""


def _seg(value: object) -> str:
    """One path segment, percent-encoded so it can never add or alter a segment."""
    return quote(str(value), safe="")


def _digest(envelope: Mapping[str, Any]) -> str:
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _no_route(method: str) -> HqUnavailable:
    return HqUnavailable(f"no hq-service route for {method}")


class HqServiceAdapter:
    """:class:`beadhive.hq_port.HqPort` over HTTP."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        verifier: Verifier | None = None,
    ) -> None:
        self._client = client
        self._verifier = verifier

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
    ) -> tuple[int, Any]:
        """One request. Returns ``(status, json)`` for a 2xx; maps everything else.

        ``commit_write`` marks a non-idempotent write whose commit may precede the failure the
        caller sees, so a ``5xx`` raises :class:`HqOutcomeUnknown` rather than ``HqUnavailable``.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HqDeadline("deadline spent before the request")
        headers = {DEADLINE_HEADER: str(max(1, int(remaining * 1000)))}
        kwargs: dict[str, Any] = {"headers": headers, "timeout": remaining}
        if body is not None:
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
        return response.status_code, self._decode(
            response, commit_write=commit_write, expected_revision=expected_revision
        )

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
            _, payload = self._call(method, path, envelope, deadline=deadline)
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
        path = f"/sessions/{_seg(anchor.principal)}/{_seg(anchor.epoch)}/renew"
        _, payload = self._call("POST", path, {}, deadline=deadline)
        return SessionRoute(anchor.principal, anchor.epoch, payload.get("revision"))

    def publish_evidence(
        self, anchor: FrameAnchor, report: EvidenceReport, *, deadline: float
    ) -> SessionRoute:
        path = f"/sessions/{_seg(anchor.principal)}/{_seg(anchor.epoch)}/evidence"
        _, payload = self._call("POST", path, dict(report.payload), deadline=deadline)
        return SessionRoute(anchor.principal, anchor.epoch, payload.get("revision"))

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
        self, route: tuple, *, expected_revision: str, frame_address: str, deadline: float
    ) -> str:
        # frame_address and any password are SQL-adapter-only inputs: no login, no secret here.
        body = {"route": list(route), "expected_revision": expected_revision}
        _, payload = self._call(
            "POST",
            "/incarnations",
            body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
        )
        return self._revision(payload)

    def publish_manifest(self, manifest: Any, *, expected_revision: str, deadline: float) -> str:
        document = dict(manifest) if isinstance(manifest, Mapping) else dict(vars(manifest))
        host_id = document.get("host_id")
        if not host_id:
            raise HqRefused("bad_request", "manifest carries no host_id")
        body = {"manifest": document, "expected_revision": expected_revision}
        _, payload = self._call(
            "PUT",
            f"/manifests/{_seg(host_id)}",
            body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
        )
        return self._revision(payload)

    def withdraw_manifest(self, host_id: str, *, expected_revision: str, deadline: float) -> str:
        raise _no_route("withdraw_manifest")

    def submit_registration(
        self, anchor: FrameAnchor, envelope: dict, *, deadline: float
    ) -> Submission:
        return self._submission(
            "POST", "/registrations", envelope, request_id=None, deadline=deadline
        )

    def admit(
        self, frame: str, request: Any, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        # The service derives the principal from committed authority, and stamps and signs the
        # grant itself under the service's own authority; `signer` is not used on this route.
        document = dict(request) if isinstance(request, Mapping) else dict(vars(request))
        body = {"request": document, "expected_revision": expected_revision}
        _, payload = self._call(
            "POST",
            f"/admissions/{_seg(frame)}",
            body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
        )
        return self._revision(payload)

    # ---- reads --------------------------------------------------------------------------

    def read_authority(self, *, allow_expired: bool = False, deadline: float) -> Any:
        _, payload = self._call("GET", "/authority", None, deadline=deadline)
        return self._verified("read_authority", payload)

    def read_frame_anchor(self, *, expected_head: str | None, deadline: float) -> FrameAnchor:
        raise _no_route("read_frame_anchor")  # gap G1

    def read_eligibility(
        self,
        frame: str,
        *,
        prefix: str | None = None,
        session_prefix: str | None = None,
        hive_lease_epoch: int | None = None,
        deadline: float,
    ) -> Any:
        if prefix is not None or session_prefix is not None or hive_lease_epoch is not None:
            raise _no_route("read_eligibility with a hive-lease row")  # gap G2
        _, payload = self._call("GET", f"/eligibility/{_seg(frame)}", None, deadline=deadline)
        return self._verified("read_eligibility", payload)

    def read_release_catalog_row(
        self, head: str, catalog: str, measured: dict, *, deadline: float
    ) -> dict | None:
        raise _no_route("read_release_catalog_row")  # gap G3

    def read_config(
        self, *, revision: str | None = None, cached_ok: bool = False, deadline: float
    ) -> Any:
        if revision is not None:
            raise _no_route("read_config at a pinned revision")
        _, payload = self._call("GET", "/config", None, deadline=deadline)
        return self._verified("read_config", payload)

    def _read_history(self, *, since: str, deadline: float) -> HistorySinceFloor:
        # The plane refuses query strings, so the floor is a path segment (not `?since=`).
        _, payload = self._call("GET", f"/history/{_seg(since)}", None, deadline=deadline)
        if not isinstance(payload, dict):
            raise HqUnavailable("hq-service history is not an object")
        return HistorySinceFloor(
            commits=tuple(payload.get("commits") or ()),
            diff_summary=tuple(payload.get("diff_summary") or ()),
            raw=payload,
        )

    # ---- operator -----------------------------------------------------------------------

    def _plan_apply(
        self, path: str, request: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        """Plan/apply (bh-hq-mz3): the signed body includes the config head and the hive-policy
        projection only the service reads, so fetch the body to sign, sign it here, then apply.
        The ``phase`` envelope is provisional with the route."""
        plan_body = {**request, "phase": "plan", "expected_revision": expected_revision}
        _, plan = self._call(
            "POST", path, plan_body, deadline=deadline, expected_revision=expected_revision
        )
        record = plan.get("plan") if isinstance(plan, dict) else None
        if not isinstance(record, dict):
            raise HqUnavailable("hq-service plan carries no record to sign")
        signature = signer(record)
        apply_body = {
            **request,
            "phase": "apply",
            "expected_revision": expected_revision,
            "record": record,
            "signature": signature,
        }
        _, payload = self._call(
            "POST",
            path,
            apply_body,
            deadline=deadline,
            expected_revision=expected_revision,
            commit_write=True,
        )
        return self._revision(payload)

    def rebind(
        self, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        return self._plan_apply(
            "/authority/rebind",
            {"state": state},
            signer=signer,
            expected_revision=expected_revision,
            deadline=deadline,
        )

    def release_policy(
        self, frame: str, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str:
        return self._plan_apply(
            "/release-policy",
            {"frame": frame, "state": state},
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
) -> HqServiceAdapter:
    """Build the adapter from the host's ``hq.api`` section. A missing endpoint is an error,
    not a reason to try another adapter."""
    hq = host_config.get("hq") if isinstance(host_config, Mapping) else None
    api = hq.get("api") if isinstance(hq, Mapping) else None
    api = api if isinstance(api, Mapping) else {}
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
    return HqServiceAdapter(httpx.Client(**kwargs), verifier=verifier)


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
