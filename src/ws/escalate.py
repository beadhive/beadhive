"""`ws escalate <title> [--tool <name>] [--as <seat>]` — fire-and-forget HQ escalation.

An agent that hits a tool problem names the tool, hands it up to HQ, and **never blocks**.
Distinct from `ws report` (which needs a known rig): escalation is target-less — it always
lands in the Factory HQ store (kind=hq singleton), and routing is deliberately flat for now.

Write path (reuses `report.file_report` — one write path, no parallel bead-creation logic):
  1. Resolve the kind=hq rig via `registry.rig_of_kind` (the .3 resolver). Fail gracefully
     when no HQ is registered, pointing the user at `ws hq init`.
  2. Call `report.file_report` with the HQ prefix as the target and ``origin=ORIGIN_ESCALATION``
     (the new closed STATE_DIMENSIONS value,).
  3. `file_report` stamps `intake=untriaged` → the bead surfaces in `ws hub intake` /
     `ws hq intake` (the HQ aggregate read) immediately.
  4. After the bead lands, stamp the optional ``role:<derived>`` and ``tool:<name>`` metadata
     via ``bd set-state`` (best-effort; does not fail the escalation if these extra stamps fail).

Seat/role tagging
-----------------
The raiser's declared seat is derived from the ``--as`` / ``$WS_CREW`` value via a simple
prefix map:  ``dev/`` → developer, ``disp/`` → dispatcher, the control-plane seats
(``super/`` → supervisor, ``dir/`` → director, ``cust/`` → custodian, ``ctrl/`` → controller),
``merge/`` → merger, ``review/`` → reviewer, and the Assurance/roadmap seats
(``warden/`` → warden, ``release/`` → releaser, ``ops/`` → operator).  Unrecognised prefixes
pass through unchanged.
The derived role is stamped as ``role:<value>`` via ``bd set-state`` (open dimension — no
validation gate, intentional).

Graceful no-HQ path
--------------------
When no kind=hq rig is registered the command prints a clear pointer at ``ws hq init`` and
exits 1.  ``report.file_report`` is never called.

Routing is FLAT → HQ only.  The up-chain auto-routing upgrade is deferred and is a
smart-target change to this verb, not a rewrite of the write path.
"""

from __future__ import annotations

import os

from . import config, registry
from .run import run
from .state import ORIGIN_ESCALATION

# Seat-prefix → canonical role label value.  Extend as new seat prefixes land.
# Aligned to the roles/RBAC matrix (docs/design/roles-rbac-matrix.md): coord/->disp/,
# crew/->dev/, the superintendent split into four control-plane seats, plus the
# Assurance (warden) and roadmap (releaser/operator) seats.  contrib/ is intentionally
# left unmapped (passes through) — the contributor seat name is unchanged and out of scope.
_SEAT_ROLES: dict[str, str] = {
    # Integration plane
    "dev": "developer",
    "disp": "dispatcher",
    "review": "reviewer",
    "merge": "merger",
    # Control plane (superintendent split → four seats)
    "super": "supervisor",
    "dir": "director",
    "cust": "custodian",
    "ctrl": "controller",
    # Assurance plane
    "warden": "warden",
    # Release / Delivery (roadmap)
    "release": "releaser",
    "ops": "operator",
}

# Escalations are control-plane signals, filed as chores (not user-facing bugs/features).
ESCALATION_TYPE = "chore"


def role_from_seat(seat: str) -> str:
    """Derive the canonical role from a seat identifier (``crew/name`` → ``developer``).

    The prefix before the first ``/`` is mapped via ``_SEAT_ROLES``; unrecognised prefixes
    return the raw seat value so no information is silently dropped.  An empty seat returns
    an empty string (no role label stamped).
    """
    if not seat:
        return ""
    prefix = seat.split("/", 1)[0]
    return _SEAT_ROLES.get(prefix, seat)


def _stamp_extra(label_kv: str, new_id: str, hq_dir, actor: str) -> None:
    """Best-effort ``bd set-state`` for open-dimension metadata (role, tool).

    Failures are silently swallowed: the escalation bead is already filed and the raiser
    must not be blocked by a non-critical metadata stamp."""
    run(
        ["bd", "-C", str(hq_dir), "set-state", new_id, label_kv,
         "--reason", "ws escalate metadata"],
        check=False,
        capture=True,
    )


def file_escalation(
    title: str,
    *,
    tool: str = "",
    seat: str = "",
    cfg=None,
) -> tuple[int, str, str]:
    """File a fire-and-forget escalation bead into HQ.

    Returns ``(exit_code, error_message, new_id)`` — callers render ``error_message``.

    Reuses ``report.file_report`` with the kind=hq rig as the target and the closed
    ``origin=escalation`` channel (``ORIGIN_ESCALATION``).  Extra metadata (tool, role)
    is stamped best-effort after the bead lands.

    No HQ registered → exits 1 with a pointer at ``ws hq init``; no bead is written.
    """
    from . import report as report_mod

    cfg = cfg if cfg is not None else config.load()

    # Fail gracefully when HQ is not set up — before any bd call.
    hq_entry = registry.rig_of_kind(cfg, registry.HQ_KIND)
    if hq_entry is None:
        return (
            1,
            "no HQ store is registered — run 'ws hq init' to set one up before escalating",
            "",
        )

    # The actor for the audit trail is the raiser's seat identity.
    actor = seat or os.environ.get("WS_CREW", "")

    # Delegate to the shared write path — one bead-creation path, no duplication.
    # file_report resolves the HQ entry by its prefix and stamps origin=escalation +
    # intake=untriaged.
    code, error, new_id = report_mod.file_report(
        registry.HQ_PREFIX,
        title,
        ESCALATION_TYPE,
        actor,
        cfg=cfg,
        origin=ORIGIN_ESCALATION,
    )
    if error:
        return code, error, new_id

    # Stamp optional open-dimension metadata best-effort (no validation gate; not a closed dim).
    hq_dir = registry.rig_dir(hq_entry)
    role = role_from_seat(seat)
    if role:
        _stamp_extra(f"role={role}", new_id, hq_dir, actor)
    if tool:
        _stamp_extra(f"tool={tool}", new_id, hq_dir, actor)

    return 0, "", new_id
