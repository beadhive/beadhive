"""Reviewed publication of a frame's optional release policy (SQL HQ only).

``bh hq authority release-policy plan|apply`` changes ``desired.release_policy`` on one frame's
grant through an operator-signed, CAS-guarded authority publication.  It never advances the frame
epoch and never touches the incarnation binding: eligibility judges each evidence row against the
policy at read time, so no evidence row carries over a decision made under another policy.  See
docs/design/frame-release-range-adr.md, sections 1 and 6.
"""

from __future__ import annotations

import copy
import hashlib
import json

from packaging.version import InvalidVersion, Version

from . import hq_authority_enforce
from . import hq_authority_guard as guard
from . import release_policy as policy_mod

DOMAIN = "beadhive/sql-release-policy/v1"
#: ``--spec none`` drops the policy, restoring exact-pin behaviour against ``release``.
DROP = "none"
#: First release whose authority reader accepts ``desired.release_policy``. Readers on 0.28.4 or
#: older require the exact ``desired`` key set and ``validate_state`` checks every record, so one
#: frame with a policy would make the whole authority state unreadable to them. Bump only if a
#: later reader-visible change to the policy shape ships.
MIN_RELEASE_POLICY_READER = "0.29.0"


def _slot(state, frame):
    entry = (state.get("frames") or {}).get(frame) or {}
    for name in ("active", "candidate"):
        if entry.get(name) is not None:
            return name
    raise ValueError(f"no active or candidate grant for frame {frame}")


def attested_release(record):
    """The release id the frame currently attests (accepted beat, else registration), or None."""
    receipt = record.get("receipt") or {}
    for carrier in (receipt.get("lease"), receipt.get("registration")):
        release = (carrier or {}).get("release")
        if isinstance(release, dict) and release.get("id"):
            return release["id"]
    return None


def unsafe_readers(state):
    """Frames that could not read a release_policy: ``(frame, release|None, kind)``.

    ``kind`` is ``"old"`` for a parseable release below the floor (never waivable) or
    ``"unknown"`` for no attested release or an unparseable one (waivable).
    """
    floor = Version(MIN_RELEASE_POLICY_READER)
    found = []
    for frame, entry in sorted((state.get("frames") or {}).items()):
        for slot in ("active", "candidate"):
            record = entry.get(slot)
            if record is None:
                continue
            release = attested_release(record)
            if release is None:
                found.append((frame, None, "unknown"))
                continue
            try:
                if Version(release) < floor:
                    found.append((frame, release, "old"))
            except InvalidVersion:
                found.append((frame, release, "unknown"))
    return found


def prepare(original, frame, head, spec, *, allow_downgrade=False, allow_unknown_readers=False):
    """Return ``(new_state, plan)`` for setting (or dropping) ``frame``'s policy."""
    guard.validate_state(original)
    name = _slot(original, frame)
    old = original["frames"][frame][name]
    desired = old["desired"]
    if spec.strip().lower() == DROP:
        new_policy = None
    else:
        new_policy = policy_mod.policy_for(policy_mod.normalize(spec))
        policy_mod.validate_policy(new_policy, desired["release"]["id"])
    attested = attested_release(old)
    downgrade = new_policy is not None and policy_mod.downgrades(new_policy["spec"], attested)
    if downgrade and not allow_downgrade:
        raise ValueError(
            f"release policy {new_policy['spec']!r} excludes the attested release {attested} "
            "while admitting lower releases (a downgrade); pass --allow-downgrade to accept"
        )
    if new_policy is not None:
        blocked = unsafe_readers(original)
        too_old = [(f, r) for f, r, kind in blocked if kind == "old"]
        unknown = [(f, r) for f, r, kind in blocked if kind == "unknown"]
        if too_old:
            names = ", ".join(f"{f} ({r})" for f, r in too_old)
            raise ValueError(
                f"release policy refused: these frames attest a release below "
                f"{MIN_RELEASE_POLICY_READER} and cannot read a release_policy: {names}; "
                "upgrade them first (no override)"
            )
        if unknown and not allow_unknown_readers:
            names = ", ".join(f"{f} ({r or 'no attested release'})" for f, r in unknown)
            raise ValueError(
                f"release policy refused: the release of these frames is unknown, so they may "
                f"not read a release_policy: {names}; pass --allow-unknown-readers to accept "
                "the risk"
            )
    state = copy.deepcopy(original)
    new_desired = state["frames"][frame][name]["desired"]
    if new_policy is None:
        new_desired.pop("release_policy", None)
    else:
        new_desired["release_policy"] = new_policy
    if state["frames"][frame][name]["desired"] == desired:
        raise ValueError("release policy unchanged")
    plan = {
        "domain": DOMAIN,
        "head": head,
        "frame_id": frame,
        "slot": name,
        "epoch": old["authority"]["epoch"],
        "reference_release": desired["release"],
        "previous_policy": desired.get("release_policy"),
        "release_policy": new_policy,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return state, {
        **plan,
        "plan_sha256": digest,
        "stored_spec": None if new_policy is None else new_policy["spec"],
        "attested_release": attested,
        "downgrade": downgrade,
        "epoch_advanced": False,
    }


def release_policy(
    plane,
    frame,
    action="plan",
    *,
    spec="",
    plan_sha256="",
    operator_key="",
    confirm=False,
    allow_downgrade=False,
    allow_unknown_readers=False,
):
    if action not in {"plan", "apply"}:
        raise ValueError("release-policy requires plan or apply")
    if not spec:
        raise ValueError("release-policy requires --spec")
    operator = plane._operator()
    budget = plane._operator_deadline()
    head, original, _crossref, _policies = operator.load(deadline=budget)
    state, result = prepare(
        original,
        frame,
        head,
        spec,
        allow_downgrade=allow_downgrade,
        allow_unknown_readers=allow_unknown_readers,
    )
    result = {"revision": head, **result}
    if action == "plan":
        return result
    if (
        not confirm
        or hq_authority_enforce.key_required(operator_key)
        or plan_sha256 != result["plan_sha256"]
    ):
        raise ValueError(
            "apply requires --confirm, approved operator key and exact reviewed plan digest"
        )
    state["revision"] += 1
    state["issued_at"] = plane.clock()
    state["expires_at"] = guard.operator_signed_expiry(original, state["issued_at"])
    guard.validate_state(state)
    result["revision"] = operator.publish(
        state, expected_revision=head, operator_key=operator_key, deadline=budget
    )
    return result
