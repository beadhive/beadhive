"""The grant transition rules as a pure, I/O-free function (bh-exm7k, bh-tn3c3 D3).

One place decides whether an admission (a candidate grant, or a trusted open admission when
``admitted``) may move the signed authority from ``original`` to a next state, and builds that
state. ``SqlControlPlane.grant`` and ``SqlHqAdapter.admit`` both call :func:`grant_transition`;
hq-service implements the same rules server-side, and the shared grant corpus
(``conformance/grant/v1`` in beadhive/hq-service, run by ``tests/test_hq_grant_conformance.py``)
keeps the two in lockstep.

Nothing here reads a clock, a database, the environment or process trust state: the caller
passes the loaded head and state, the instant ``now`` and the operator public key, then signs
and publishes the result itself. The trusted-fleet gate on open admission
(``hq_control_plane.open_admission_guard``) reads process state, so it stays with the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import hq_authority_guard as guard
from .hq_authority_payload import authority_payload
from .hq_sql_signatures import fingerprint, sql_principal

# Stable refusal codes -> the fixed message each carries (the corpus maps on these codes).
IDENTITY_OR_CAS_INVALID = "identity_or_cas_invalid"
CANDIDATE_EXISTS_OR_EPOCH_STALE = "candidate_exists_or_epoch_stale"
FRAME_ALREADY_ACTIVE = "frame_already_active"
REFUSALS = {
    IDENTITY_OR_CAS_INVALID: "candidate grant identity or original CAS invalid",
    CANDIDATE_EXISTS_OR_EPOCH_STALE: "candidate exists or epoch does not advance",
    FRAME_ALREADY_ACTIVE: "open admission: frame already has an active incarnation",
}


class GrantRefused(ValueError):
    """A grant rule refused the transition; ``code`` is stable, the message is fixed text."""

    def __init__(self, code: str) -> None:
        super().__init__(REFUSALS[code])
        self.code = code


@dataclass(frozen=True)
class GrantTransition:
    """The next authority state, unsigned, and the route its incarnation is provisioned on:
    ``(principal, frame_id, holder_identity, instance_ref, epoch, key_fingerprint)``."""

    state: dict
    provisioned_route: tuple


def _fresh_record(authority: Any, public_key: str, desired: Any, admitted: bool) -> dict:
    return {
        "authority": authority_payload(authority),
        "public_key": public_key.strip(),
        "state": "active" if admitted else "pending",
        "desired": desired,
        "cordoned": False,
        "drain_deadline": None,
        "receipt": {
            "sequence": 0,
            "sha": "",
            "first_seen": None,
            "consecutive": 0,
            "lease": None,
            "registration": None,
        },
    }


def grant_transition(
    original: dict,
    *,
    head: str,
    expected: str,
    authority: Any,
    public_key: str,
    desired: Any,
    admitted: bool,
    now: float,
    operator_public_key: str,
) -> GrantTransition:
    """Apply the grant rules to ``original`` (the verified state at ``head``).

    Refuses (:class:`GrantRefused`) unless: ``head`` is exactly ``expected``; a candidate
    (``admitted`` false) carries a bounded expiry later than ``now``; ``public_key`` is the key
    ``authority`` names; that key is neither the operator's nor any recorded incarnation's, and
    no recorded incarnation has the same holder identity; the frame has no pending candidate and
    ``authority.epoch`` advances its epoch floor; an open admission finds no active incarnation.

    The next state records the incarnation as ``active`` (admitted) or ``pending`` (candidate),
    raises the epoch floor, bumps the revision, stamps ``issued_at = now``, keeps the signed
    life (one hour floor), upgrades the domain to v2 once any record is HQ-ID bound, and is
    validated. ``original`` is not mutated. ``authority`` is an
    :class:`~beadhive.hq_framelease_contracts.ObservationAuthority`.
    """
    if (
        head != expected
        or not admitted
        and (authority.candidate_expires_at is None or authority.candidate_expires_at <= now)
        or fingerprint(public_key) != authority.key_fingerprint
        or authority.key_fingerprint == fingerprint(operator_public_key)
        or any(
            record["authority"]["key_fingerprint"] == authority.key_fingerprint
            or record["authority"]["holder_identity"] == authority.holder_identity
            for _, record in guard.records(original)
        )
    ):
        raise GrantRefused(IDENTITY_OR_CAS_INVALID)
    state = json.loads(json.dumps(original))
    entry = state["frames"].setdefault(
        authority.frame_id,
        {"active": None, "candidate": None, "retired": [], "epoch_floor": -1},
    )
    if entry["candidate"] is not None or authority.epoch <= entry["epoch_floor"]:
        raise GrantRefused(CANDIDATE_EXISTS_OR_EPOCH_STALE)
    if admitted and entry["active"] is not None:
        raise GrantRefused(FRAME_ALREADY_ACTIVE)
    entry["active" if admitted else "candidate"] = _fresh_record(
        authority, public_key, desired, admitted
    )
    entry["epoch_floor"] = authority.epoch
    state["revision"] += 1
    state["issued_at"] = now
    state["expires_at"] = guard.operator_signed_expiry(original, now)
    state["domain"] = (
        guard.DOMAIN_V2
        if any(row["authority"].get("beadyard_id") is not None for _, row in guard.records(state))
        else guard.DOMAIN
    )
    guard.validate_state(state)
    return GrantTransition(
        state=state,
        provisioned_route=(
            sql_principal(authority),
            authority.frame_id,
            authority.holder_identity,
            authority.instance_ref,
            authority.epoch,
            authority.key_fingerprint,
        ),
    )
