"""The HQ lifecycle port: the contract between bh's HQ callers and an HQ adapter.

Defined in ``docs/design/hq-lifecycle-port.md`` (bh-tekmw). One adapter object per host
implements :class:`HqPort`, and the ``hq.route`` setting (``sql`` | ``api``) selects it. This
module holds the value objects, the one error taxonomy and the selector. The hq-service adapter
lives in :mod:`beadhive.hq_service_adapter`; the SQL adapter registers itself here with
:func:`register_sql_adapter` (bh-v3dyp), so switching a host back to SQL is the config change
alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ROUTE_ENV = "BH_HQ_ROUTE"
ROUTES = ("sql", "api")
DEFAULT_ROUTE = "sql"

Signer = Callable[[dict], str]
"""Signs a canonical authority record in-process. Keys never cross the port."""


# ---- errors (design section 3.3) -------------------------------------------------------


class HqError(Exception):
    """Base of the port's error taxonomy."""


class HqRefused(HqError):
    """A definite, stable refusal. Nothing was written."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(f"{code}: {reason}" if reason else code)
        self.code = code
        self.reason = reason or code


class HqNoCurrentBeat(HqRefused):
    """Evidence was refused because the service holds no accepted beat to bind it to
    (``no_current_beat``). Nothing was written. The caller can act on it: renew the session,
    then post the same report again."""

    def __init__(self, reason: str = "") -> None:
        super().__init__("no_current_beat", reason)


class HqUnavailable(HqError):
    """HQ could not be reached or read. Nothing was written."""


class HqDeadline(HqError):
    """The caller's deadline ran out (never reported as :class:`HqUnavailable`)."""


class HqOutcomeUnknown(HqError):
    """The write may have committed. Re-read before retrying."""

    def __init__(self, expected_revision: str | None, detail: str = "") -> None:
        super().__init__(detail or "the write may have committed")
        self.expected_revision = expected_revision


class HqRouteError(HqError):
    """``hq.route`` is invalid, or names an adapter that is not available here. Raised in
    place of a fallback: the other adapter is never tried."""


# ---- value objects ---------------------------------------------------------------------


@dataclass(frozen=True)
class FrameAnchor:
    """This caller's registry route, and the authority head it was read at."""

    principal: str
    frame: str
    epoch: int
    holder: str = ""
    instance: str = ""
    carrier: str = ""
    head: str | None = None


@dataclass(frozen=True)
class EvidenceReport:
    """One incarnation's evidence row, as the frame reports it."""

    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SessionRoute:
    """The server-stamped result of a session renewal or an evidence update."""

    principal: str
    epoch: int
    revision: str | None = None
    #: Renew only: the server-stamped lease expiry (epoch seconds) and ``accepted`` | ``replayed``.
    accepted_until: float | None = None
    outcome: str | None = None
    #: Evidence only: the canonical sha256 digest of the stored report (``accepted`` | ``replayed``
    #: is in ``outcome``); evidence carries no revision.
    digest: str | None = None
    #: Renew only (the hq-service adapter): the validated ``HeartbeatLease`` the service accepted,
    #: parsed from the exact signed bytes sent (a resent pending beat included), so a caller
    #: can post the evidence of that very beat (bh-a3369). ``None`` where an adapter has none.
    beat: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Submission:
    """A submitted envelope and its verdict. ``outcome`` is ``pending`` (SQL),
    ``accepted``, ``replayed`` or ``refused``; ``refusal`` is a stable code."""

    request_id: str
    digest: str
    outcome: Literal["pending", "accepted", "replayed", "refused"]
    revision: str | None = None
    refusal: str | None = None
    #: What a stateless ``read_result`` needs to rebuild the receiver's public-result read
    #: (SQL route, filled by submit): the envelope audience, the head the submit was
    #: anchored at, and the submitting route. ``None`` when the route does not need them.
    audience: str | None = None
    expected_head: str | None = None
    anchor: FrameAnchor | None = None


@dataclass(frozen=True)
class HistorySinceFloor:
    """The revisions after the caller's floor (hq-service ``GET /history?since=``).

    ``revisions`` are those reachable from ``head`` but not from ``floor``, each ``{id, message,
    at, parents, changes[{table, data, schema}]}``; the order is by commit time, so a reader
    walks ``parents``."""

    head: str = ""
    floor: str = ""
    revisions: tuple[Mapping[str, Any], ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionRequest:
    """Admission intent (design section 7 D3): admit ``authority`` to ``frame``.

    ``authority`` is the candidate's validated
    :class:`~beadhive.hq_framelease_contracts.ObservationAuthority` and ``public_key`` the
    OpenSSH Ed25519 key it names. ``admitted`` asks for a trusted (open) admission, recorded
    active; otherwise a pending candidate grant. ``desired`` is the frame's desired policy.
    ``provisioned_route`` optionally names the incarnation route the write provisions; absent,
    the adapter derives it from ``authority``.

    The request carries no next state: the grant rules
    (:func:`beadhive.hq_grant_rules.grant_transition`) build it on the SQL route, and
    hq-service builds and signs it on the api route.
    """

    frame: str
    authority: Any
    public_key: str
    desired: Mapping[str, Any]
    admitted: bool = False
    provisioned_route: tuple | None = None


# ---- protocols (design section 3.2) -----------------------------------------------------
# Types the design leaves to the SQL adapter's tuples (FrameComposite, FleetConfigSnapshot,
# HostManifest) are ``Any`` here until bh-v3dyp fixes them.


class HqLiveness(Protocol):
    def renew_session(self, anchor: FrameAnchor, *, deadline: float) -> SessionRoute: ...
    def publish_evidence(
        self, anchor: FrameAnchor, report: EvidenceReport, *, deadline: float
    ) -> SessionRoute: ...
    def submit_heartbeat(
        self, anchor: FrameAnchor, envelope: dict, *, request_id: str, deadline: float
    ) -> Submission: ...
    def submit_hive_lease(
        self, anchor: FrameAnchor, prefix: str, envelope: dict, *, deadline: float
    ) -> Submission: ...
    def read_result(self, submission: Submission, *, deadline: float) -> Submission: ...


class HqAdmission(Protocol):
    def describe_incarnation(
        self, principal: str, epoch: int, frame: str, *, target_route: str | None, deadline: float
    ) -> dict: ...
    def preflight(self, *, route: str, deadline: float) -> dict: ...
    def register_incarnation(
        self,
        route: tuple,
        *,
        expected_revision: str,
        frame_address: str,
        login_secret: Callable[[], str] | None = None,
        deadline: float,
    ) -> str: ...
    def publish_manifest(
        self, manifest: Any, *, expected_revision: str, deadline: float
    ) -> str: ...
    def withdraw_manifest(
        self, host_id: str, *, expected_revision: str, deadline: float
    ) -> str: ...
    def submit_registration(
        self, anchor: FrameAnchor, envelope: dict, *, deadline: float
    ) -> Submission: ...
    def admit(
        self,
        frame: str,
        request: AdmissionRequest,
        *,
        signer: Signer,
        expected_revision: str,
        deadline: float,
    ) -> str:
        """Admit ``request`` and return the new authority head. ``signer`` signs the next
        state on the SQL route; on the api route hq-service builds and signs the grant itself
        and ``signer`` is unused."""
        ...


class HqReads(Protocol):
    def read_authority(self, *, allow_expired: bool = False, deadline: float) -> Any: ...
    def read_frame_anchor(self, *, expected_head: str | None, deadline: float) -> FrameAnchor: ...
    def read_eligibility(
        self,
        frame: str,
        *,
        prefix: str | None = None,
        session_prefix: str | None = None,
        hive_lease_epoch: int | None = None,
        deadline: float,
    ) -> Any: ...
    def read_release_catalog_row(
        self, head: str, catalog: str, measured: dict, *, deadline: float
    ) -> dict | None: ...
    def read_config(
        self, *, revision: str | None = None, cached_ok: bool = False, deadline: float
    ) -> Any: ...
    def read_hive_lease(self, prefix: str, *, deadline: float) -> Any:
        """The hive's lease row, unqualified (bh-h9dn1). hq-service answers ``{head, prefix,
        revision, lease, held}`` (an unknown prefix is :class:`HqRefused` ``unknown_prefix``);
        the SQL adapter answers the plane's ``(revision, lease)``."""
        ...

    def read_release_catalog(self, catalog: str, *, deadline: float) -> list[dict]:
        """Every row of ``catalog`` whole (api route only; the SQL route reads through the
        release-catalog store binding)."""
        ...


class HqOperator(Protocol):
    def rebind(
        self, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str: ...
    def release_policy(
        self, frame: str, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str: ...
    def plan_release_policy(self, frame: str, request: dict, *, deadline: float) -> dict:
        """Plan only (api route): the service's plan, ``record`` unsigned, nothing written."""
        ...

    def plan_rebind(self, request: dict, *, deadline: float) -> dict:
        """Plan a rebind only (api route): the service's plan, nothing written (bh-hb75g)."""
        ...

    def plan_manifest_publish(self, host_id: str, request: dict, *, deadline: float) -> dict:
        """Plan a signed manifest publication + rebind (api route, hq-service v0.1.3):
        ``request`` is ``{manifest, duration_s?}``; nothing written."""
        ...

    def apply_manifest_publish(self, host_id: str, body: dict, *, deadline: float) -> dict:
        """Apply a planned manifest publication with the exact apply ``body`` (api route). The
        same body is safe to send again: it completes a half-done publication or replays."""
        ...

    def import_release_catalog(
        self, catalog: str, request: dict, *, signer: Signer, deadline: float
    ) -> str:
        """Import one release row (api route): plan, sign under the catalog domain, apply.
        Returns the new head."""
        ...

    def publish_authority(
        self,
        state: dict,
        *,
        signer: Signer,
        expected_revision: str,
        provisioned_route: tuple | None = None,
        release_upgrade: dict | None = None,
        deadline: float,
    ) -> str: ...
    def read_evidence(
        self, frame: str, holder_identity: str, *, expected_revision: str, deadline: float
    ) -> tuple: ...
    def publish_config(
        self, documents: tuple, *, expected_revision: str, deadline: float
    ) -> Any: ...
    def recover_publication(
        self, publication_id: str, *, expected_revision: str, deadline: float
    ) -> Any: ...


def operator_apply_body(request: Mapping[str, Any], plan: Mapping[str, Any], *, signer) -> dict:
    """The apply body of an operator plan/apply pair: the plan request plus the plan's
    ``{expected_revision (its head), config_head, issued_at, expires_at, record}`` and, unless the
    plan says no signature is needed (a trusted fleet), ``signer(record)``. Deterministic for one
    plan (Ed25519 signs deterministically), so the same plan always yields the same bytes."""
    record = plan["record"]
    body: dict[str, Any] = {
        **request,
        "expected_revision": plan.get("revision"),
        "config_head": plan.get("config_head"),
        "issued_at": plan.get("issued_at"),
        "expires_at": plan.get("expires_at"),
        "record": record,
    }
    if plan.get("signature_needed") is not False:
        body["signature"] = signer(record)
    return body


class HqPort(HqLiveness, HqAdmission, HqReads, HqOperator, Protocol):
    """What ``hq.route`` selects: one adapter object per host."""


# ---- SQL adapter registration -------------------------------------------------------------

_sql_factory: Callable[[], HqPort] | None = None


def register_sql_adapter(factory: Callable[[], HqPort] | None) -> None:
    """Install (or clear, with ``None``) the SQL adapter's factory. bh-v3dyp calls this."""
    global _sql_factory
    _sql_factory = factory


def sql_adapter_factory() -> Callable[[], HqPort] | None:
    return _sql_factory


def configured_route(host_config: Mapping[str, Any]) -> str:
    """``BH_HQ_ROUTE`` > host ``hq.route`` > ``api`` when host ``hq.mode`` is ``api`` > ``sql``.
    An invalid value raises :class:`HqRouteError`; it never degrades to a default.
    *host_config* is the loaded host config (``config.load_host()``)."""
    value: Any = os.environ.get(ROUTE_ENV) or None
    source = ROUTE_ENV
    hq = host_config.get("hq") if isinstance(host_config, Mapping) else None
    if value is None:
        value = hq.get("route") if isinstance(hq, Mapping) else None
        source = "hq.route"
    if value is None and isinstance(hq, Mapping) and hq.get("mode") == "api":
        # ``hq.mode: api`` names the same host binding as ``hq.route: api`` (bh-e437a).
        return "api"
    if value is None:
        return DEFAULT_ROUTE
    if value not in ROUTES:
        raise HqRouteError(f"{source} must be one of {', '.join(ROUTES)}, not {value!r}")
    return str(value)
