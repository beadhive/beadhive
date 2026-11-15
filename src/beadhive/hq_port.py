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
    """Commits and a diff summary since the caller's floor."""

    commits: tuple[Mapping[str, Any], ...] = ()
    diff_summary: tuple[Mapping[str, Any], ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


# ---- protocols (design section 3.2) -----------------------------------------------------
# Types the design leaves to the SQL adapter's tuples (FrameComposite, FleetConfigSnapshot,
# HostManifest, AdmissionRequest) are ``Any`` here until bh-v3dyp fixes them.


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
        self, route: tuple, *, expected_revision: str, frame_address: str, deadline: float
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
        self, frame: str, request: Any, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str: ...


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


class HqOperator(Protocol):
    def rebind(
        self, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str: ...
    def release_policy(
        self, frame: str, state: dict, *, signer: Signer, expected_revision: str, deadline: float
    ) -> str: ...
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
    """``BH_HQ_ROUTE`` > host ``hq.route`` > ``sql``. An invalid value raises
    :class:`HqRouteError`; it never degrades to a default. *host_config* is the loaded host
    config (``config.load_host()``)."""
    value: Any = os.environ.get(ROUTE_ENV) or None
    source = ROUTE_ENV
    if value is None:
        hq = host_config.get("hq") if isinstance(host_config, Mapping) else None
        value = hq.get("route") if isinstance(hq, Mapping) else None
        source = "hq.route"
    if value is None:
        return DEFAULT_ROUTE
    if value not in ROUTES:
        raise HqRouteError(f"{source} must be one of {', '.join(ROUTES)}, not {value!r}")
    return str(value)
