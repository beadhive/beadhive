"""This frame's grant and route from hq-service, for liveness on an ``hq.route: api`` host
(bh-65s71, design gaps G1 and G3).

An api host has no SQL frame runtime. What the SQL runtime principal used to give the sender, the
anchor (principal, epoch, holder, instance, carrier) and the grant-bound beat fields, comes from
two hq-service reads instead:

* ``GET /eligibility/{frame}`` (hq-service v0.1.0, ``store/reads.go`` ``EligibilityRead``), bound
  by the adapter to a verified ``GET /authority`` read at the same head. The grant itself is taken
  from the **signed** authority record (``state.frames[frame].active``); the eligibility body must
  carry that same grant, and its ``principal`` and ``epoch`` must be the ones the grant derives
  (:func:`~beadhive.hq_sql_signatures.sql_principal`). Only the ``route`` (carrier and signer
  fingerprint, the principal registry row) is the service's alone, and its signer must be the
  granted key.
* ``GET /release-catalog/{catalog}?release_id=&release_digest=`` for the catalog row a release
  policy judges the measured release by (G3), at the verified head.

:class:`ApiFrameAuthority` serves those reads in the shape
:func:`beadhive.heartbeat_report.generate` reads from the SQL runtime
(``_runtime_authority().read_frame_composite()`` and ``read_release_catalog_row``), so the beat a
frame signs is built by the same generator on either route. It opens no SQL connection and never
falls back to SQL.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .hq_port import FrameAnchor, HqRefused, HqUnavailable

#: Seconds one api read of the frame grant may take end to end.
API_FRAME_TIMEOUT_S = 30.0

#: The carriers ``EligibilityRead.route.carrier`` names (``store/reads.go``).
CARRIERS = ("inbox", "service")


@dataclass(frozen=True)
class FrameRoute:
    """The frame's route in the attribute shape of the SQL ``PrincipalBinding``.

    ``inbox_table`` holds the service's carrier word (``inbox`` or ``service``), not a table
    name: an api host never addresses the inbox itself."""

    principal: str
    frame_id: str
    holder_identity: str
    instance_ref: str
    epoch: int
    inbox_table: str
    signer_fingerprint: str


@dataclass(frozen=True)
class FrameGrant:
    """One frame's verified active grant and its route, read at one authority head."""

    head: str
    config_head: str
    record: dict
    route: FrameRoute

    @property
    def anchor(self) -> FrameAnchor:
        route = self.route
        return FrameAnchor(
            principal=route.principal,
            frame=route.frame_id,
            epoch=route.epoch,
            holder=route.holder_identity,
            instance=route.instance_ref,
            carrier=route.inbox_table,
            head=self.head,
        )


def frame_grant(eligibility: Any, frame: str) -> FrameGrant:
    """The :class:`FrameGrant` in the adapter's ``read_eligibility`` result for *frame*.

    No active grant is a refusal (``no_current_grant``), and so is a grant with no registry route
    (``unprovisioned_principal``) or a route whose signer is not the granted key
    (``identity_mismatch``). A body that is not the verified authority's is unavailable."""
    from .hq_framelease_contracts import ObservationAuthority
    from .hq_sql_signatures import sql_principal

    try:
        if not isinstance(eligibility, Mapping) or eligibility.get("frame_id") != frame:
            raise HqUnavailable("hq-service eligibility is not this frame's")
        state = eligibility["authority"]["record"]["state"]
        entry = (state.get("frames") or {}).get(frame)
        signed = entry.get("active") if isinstance(entry, Mapping) else None
        served = eligibility.get("active")
        if signed is None and served is None:
            raise HqRefused("no_current_grant", "this frame has no active grant")
        if served != signed:
            raise HqUnavailable("the eligibility grant is not the verified authority's")
        authority = signed["authority"]
        principal = sql_principal(
            ObservationAuthority(
                **{
                    key: authority.get(key)
                    for key in ObservationAuthority.__dataclass_fields__
                    if key in authority
                }
            )
        )
        epoch = authority["epoch"]
        if eligibility.get("principal") != principal or eligibility.get("epoch") != epoch:
            raise HqUnavailable("the eligibility route is not the verified grant's incarnation")
        route = eligibility.get("route")
        if route is None:
            raise HqRefused("unprovisioned_principal", "the grant has no registry route")
        carrier, signer = route["carrier"], route["signer_fingerprint"]
        if carrier not in CARRIERS:
            raise HqUnavailable("hq-service names an unknown route carrier")
        if signer != authority["key_fingerprint"]:
            raise HqRefused("identity_mismatch", "the route signer is not the granted key")
        return FrameGrant(
            head=str(eligibility["head"]),
            config_head=str(eligibility["config_head"]),
            record=dict(signed),
            route=FrameRoute(
                principal=principal,
                frame_id=frame,
                holder_identity=str(authority["holder_identity"]),
                instance_ref=str(authority["instance_ref"]),
                epoch=int(epoch),
                inbox_table=str(carrier),
                signer_fingerprint=str(signer),
            ),
        )
    except (HqRefused, HqUnavailable):
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HqUnavailable("hq-service eligibility is not a frame grant") from None


@dataclass(frozen=True)
class _Snapshot:
    documents: tuple


class ApiFrameAuthority:
    """This frame's authority over the HQ port, in the SQL runtime's read shape.

    Stands where :func:`beadhive.heartbeat_report.generate` takes a SQL control plane: it is its
    own ``_runtime_authority()``. Every read is one port call under a fresh
    :data:`API_FRAME_TIMEOUT_S` deadline; any port error propagates (``generate`` reports it as a
    redacted category)."""

    def __init__(self, port: Any, frame: str, *, timeout: float = API_FRAME_TIMEOUT_S) -> None:
        self.port, self.frame = port, frame
        self.operation_timeout = timeout

    def _deadline(self) -> float:
        return time.monotonic() + self.operation_timeout

    def _runtime_authority(self) -> ApiFrameAuthority:
        return self

    def grant(self) -> FrameGrant:
        return frame_grant(
            self.port.read_eligibility(self.frame, deadline=self._deadline()), self.frame
        )

    def load_frame_binding(self, *, expected_head: str | None = None, **_kwargs: Any) -> FrameRoute:
        grant = self.grant()
        if expected_head is not None and grant.head != expected_head:
            raise HqUnavailable("authority moved from the expected head")
        return grant.route

    def read_frame_composite(self, **_kwargs: Any) -> tuple:
        """``(head, None, route, None, record, snapshot, None, None, None)``: the slots
        ``generate`` reads of the SQL composite. The committed config is read through the port
        and must be the one the grant's authority names (head and config head); the observation
        slot is ``None``, so the beat's ``seq`` is only a lower bound and the adapter's renew-seq
        store sets it (:mod:`beadhive.hq_renew_seq`)."""
        from .hq_control_plane import ApiControlPlane

        grant = self.grant()
        config = self.port.read_config(deadline=self._deadline())
        try:
            config_head = config["authority"]["head"]
        except (KeyError, TypeError):
            raise HqUnavailable("hq-service config carries no authority") from None
        snapshot = ApiControlPlane(self.port)._snapshot(config)
        if config_head != grant.head or snapshot.commit_revision != grant.config_head:
            raise HqUnavailable("authority moved between the eligibility and config reads")
        documents = _Snapshot(tuple(snapshot.documents))
        return grant.head, None, grant.route, None, grant.record, documents, None, None, None

    def read_release_catalog_row(self, head: str, catalog: str, measured: dict) -> dict | None:
        return self.port.read_release_catalog_row(
            head, catalog, measured, deadline=self._deadline()
        )
