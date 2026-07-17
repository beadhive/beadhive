"""`ws escalate <title> [--tool <name>] [--as <seat>]` — fire-and-forget HQ escalation.

An agent that hits a tool problem names the tool, hands it up to HQ, and **never blocks**.
Distinct from `ws report` (which needs a known hive): escalation is target-less — it always
lands in the Factory HQ store (kind=hq singleton), and routing is deliberately flat for now.

Write path (reuses `report.file_report` — one write path, no parallel bead-creation logic):
  1. Resolve the kind=hq hive via `registry.hive_of_kind` (the .3 resolver). Fail gracefully
     when no HQ is registered, pointing the user at `ws hq init`.
  2. Call `report.file_report` with the HQ prefix as the target and ``origin=ORIGIN_ESCALATION``
     (the new closed STATE_DIMENSIONS value,).
  3. `file_report` stamps `intake=untriaged` → the bead surfaces in `ws hub intake` /
     `ws hq intake` (the HQ aggregate read) immediately.
  4. After the bead lands, stamp the optional ``role:<derived>`` and ``tool:<name>`` metadata
     via ``bd set-state`` (best-effort; does not fail the escalation if these extra stamps fail).

Seat/role tagging
-----------------
The raiser's declared seat is derived from the ``--as`` / ``$BH_DEV`` (or deprecated
``$WS_DEV``/``$WS_CREW``) value via a simple
prefix map:  ``dev/`` → developer, ``disp/`` → dispatcher, the control-plane seats
(``super/`` → supervisor, ``dir/`` → director, ``cust/`` → custodian, ``ctrl/`` → controller),
``merge/`` → merger, ``review/`` → reviewer, and the Assurance/roadmap seats
(``warden/`` → warden, ``release/`` → releaser, ``ops/`` → operator).  Unrecognised prefixes
pass through unchanged.
The derived role is stamped as ``role:<value>`` via ``bd set-state`` (open dimension — no
validation gate, intentional).

No-HQ path (bh-ufne): consent-prompted auto-init, never lose the signal
-----------------------------------------------------------------------
Every host SHOULD have an HQ as part of initial setup, so when no kind=hq hive is registered
the command OFFERS to stand one up: on an interactive TTY it confirms
"no HQ store is registered — initialize one now?" (default yes) and, on consent, runs the
``hq init`` core (``hq.init_store`` — a direct call, never a subprocess) then files the
escalation there normally.  On decline — or in a non-interactive context where prompting is
impossible — the full escalation content (title, tool, actor/role) is printed with a clear
WARNING that it was NOT filed anywhere, and the command exits nonzero.  The signal is never
silently lost, and it is never silently filed somewhere unexpected: filing into the local
rig's own intake is explicitly REJECTED as a default.

Direction (future): escalation routing chains may become configurable (different escalation
parents per hive), but 'escalation parent: none' is intended to become INVALID hive
configuration — ``hive onboard`` / ``hive ready`` already surface the missing HQ.

Routing is FLAT → HQ only.  The up-chain auto-routing upgrade is deferred and is a
smart-target change to this verb, not a rewrite of the write path.
"""

from __future__ import annotations

import sys

import typer

from . import config, registry
from .identity import _env_actor
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


def _is_interactive() -> bool:
    """True when stdin is a TTY — the only context where a consent prompt is possible."""
    return sys.stdin.isatty()


def _offer_hq_init() -> bool:
    """Consent-prompted HQ auto-init (bh-ufne). True iff the HQ was stood up.

    Interactive TTY only; the prompt defaults to yes (every host SHOULD have an HQ from
    initial setup). On consent, calls the ``hq init`` core (``hq.init_store``) directly —
    never a subprocess. ``hub.sync`` failures inside ``init_store`` do not block filing:
    the durable store exists locally once it returns."""
    if not _is_interactive():
        return False
    if not typer.confirm("no HQ store is registered — initialize one now?", default=True):
        return False
    from . import hq as hq_mod  # lazy: hq imports hub (bd-touching); escalate stays light

    hq_mod.init_store()
    return True


def _print_unfiled(title: str, *, tool: str, actor: str, role: str) -> None:
    """Render the unfiled escalation to stderr so the signal is never silently lost."""
    typer.echo(
        "⚠ WARNING: this escalation was NOT filed anywhere — no HQ store is registered.",
        err=True,
    )
    typer.echo(f"  title: {title}", err=True)
    if tool:
        typer.echo(f"  tool:  {tool}", err=True)
    typer.echo(f"  actor: {actor}" + (f" (role: {role})" if role else ""), err=True)
    typer.echo(
        f"  run '{config.BINARY_ALIAS} hq init' and re-raise the escalation.", err=True
    )


def _stamp_extra(label_kv: str, new_id: str, hq_dir, actor: str) -> None:
    """Best-effort ``bd set-state`` for open-dimension metadata (role, tool).

    Failures are silently swallowed: the escalation bead is already filed and the raiser
    must not be blocked by a non-critical metadata stamp."""
    run(
        ["bd", "-C", str(hq_dir), "set-state", new_id, label_kv,
         "--reason", f"{config.BINARY_ALIAS} escalate metadata"],
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

    Reuses ``report.file_report`` with the kind=hq hive as the target and the closed
    ``origin=escalation`` channel (``ORIGIN_ESCALATION``).  Extra metadata (tool, role)
    is stamped best-effort after the bead lands.

    No HQ registered → offer a consent-prompted auto-init (interactive TTY only); on decline
    or in a non-interactive context, print the unfiled content with a WARNING and exit 1 —
    never silently lose the signal, never file into the local rig (see the module docstring).
    """
    from . import report as report_mod

    cfg = cfg if cfg is not None else config.load()

    # The actor for the audit trail is the raiser's seat identity.
    actor = seat or _env_actor()

    # No-HQ path (bh-ufne): consent-prompted auto-init, else print-and-refuse — before any
    # bd call. Local-same-repo filing is explicitly rejected as a default.
    hq_entry = registry.hive_of_kind(cfg, registry.HQ_KIND)
    if hq_entry is None and _offer_hq_init():
        cfg = config.load()  # init_store just registered the HQ — reload to pick it up
        hq_entry = registry.hive_of_kind(cfg, registry.HQ_KIND)
    if hq_entry is None:
        _print_unfiled(title, tool=tool, actor=actor, role=role_from_seat(seat))
        return (
            1,
            "escalation NOT filed — no HQ store is registered; "
            f"run '{config.BINARY_ALIAS} hq init' and re-raise (content printed above)",
            "",
        )

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
    hq_dir = registry.hive_dir(hq_entry)
    role = role_from_seat(seat)
    if role:
        _stamp_extra(f"role={role}", new_id, hq_dir, actor)
    if tool:
        _stamp_extra(f"tool={tool}", new_id, hq_dir, actor)

    return 0, "", new_id
