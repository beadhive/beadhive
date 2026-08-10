"""`ws work` — the integration-plane driver.

Takes a single bead assigned → merged through the Agentic Git Flow lifecycle
(brief → claim → check → submit → resume → abandon, plus orchestrator-only assign),
so an agent drives the lifecycle through `ws` instead of improvising raw git. It is a
thin facade: each verb composes `bd` (Beads), `ws` managed worktrees, and per-agent
identity primitives that already exist. Raw git is for the change *inside* the worktree
only — never the lifecycle around it.

Test seam: this module shells out to **`bd` only** (via `bd.run`); every git / worktree
operation goes through `worktree` / `identity`. Tests use a real git repo and fake just
`bd` by patching `ws.work.run`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from . import (
    adopt,
    bd,
    claim_authority,
    config,
    ghpr,
    git_linkage,
    guard,
    host,
    identity,
    jsonout,
    otel,
    registry,
    release_order,
    validation_ledger,
    work_group,
    work_logic,
    work_next,
    work_show,
    worktree,
)
from . import schedule as schedule_mod
from .run import run
from .work_logic import (
    _MARKER,
    _guard_holds_claim,
    _guard_not_other,
    _guard_open,
    _history_ok,
    _simulate,
    _stamp,
    build_todo,
    plan_from_since,
    validate_plan,
)

# Re-exported for the public/test surface (used by callers, not within this module).
auto_message = work_logic.auto_message
flag_rows = work_logic.flag_rows
ensure_review_gate = work_logic.ensure_review_gate  # shared gate seam (single-bead + batch submit)
ensure_container = work_logic.ensure_container  # shared epic-container provisioning

app = typer.Typer(no_args_is_help=True, help="Drive a bead assigned→merged (integration plane).")


class WorkError(Exception):
    """An integration-plane operation failed. Typer-free; the CLI maps it to stderr + exit 1.

    Carries the stderr `messages` to render and, once a refine backup exists, its `backup`
    branch name (so the CLI reports it the same on the success and the restore paths)."""

    def __init__(self, messages: list[str], backup: str = ""):
        self.messages = messages
        self.backup = backup
        super().__init__("; ".join(messages))


@dataclass
class RefineResult:
    """Outcome of `refine_branch`: a dry-run preview, or the applied rewrite's report."""

    base: str
    dry_run: bool = False
    subjects: list[str] = field(default_factory=list)  # dry-run: the would-be subjects
    backup: str = ""  # applied: the backup branch left behind
    branch: str = ""  # applied: the refined branch
    log: str = ""  # applied: the rendered log range
    target: Path | None = None  # applied: worktree path (for the restore hint)


# ---- bd plumbing: the shared helpers now live in bd.py / registry.py --------


def _forward_read(sub_args, cwd):
    """Forward a read-only `bd` subcommand (ready / show / list) and stream its output through
    verbatim, propagating the exit code. Capture-then-write keeps bd's bytes (incl. `--json`)
    byte-identical to the `ws bd` passthrough, so the coordinator loop's consumed shapes are
    unchanged once the bd passthrough is gated off. Raises typer.Exit with bd's return code."""
    res = bd.run(sub_args, cwd, capture=True)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    raise typer.Exit(res.returncode)


def _maybe_open_molecule(cfg, hive, bead, main):
    """Lazily open the epic's container branch (the coordinator seat `wt/bead/epic/<epic>`) when a
    child of a KICKED-OFF epic is first provisioned, BEFORE `worktree.ensure` for the child, so the
    child forks off the container (not main). Kickoff moved out of the planning plane (`ws plan
    approve` no longer creates the branch), so the integration plane opens the container on the
    first assign/claim of a child — idempotently via `ensure()` (which, under the collapsed
    container==seat model, opens the branch off `integration_base` AND attaches the seat worktree;
    the coordinator's own `start`/`assign` re-attaches + identity-stamps it). Gated on the epic
    being `kickoff=approved`, so a dotted bead whose epic was never kicked off still targets `main`
    (backward-compatible).

    The container is then REFRESHED from its integration base: it opens once,
    on the first child's dispatch, and would otherwise pin every later child to that stale base —
    fixes landing on main mid-molecule stayed invisible. Refresh is best-effort (warns, never
    blocks dispatch) and lands on the container only, so submit's `base..child` rules hold.

    Thin dotted-id wrapper over `work_logic.ensure_container` (bh-n5z3.2): parse the epic off the
    dotted bead id, then delegate the kickoff-gate + open + refresh to the shared helper (which the
    collapsed/group claim paths also call, so a batch lands into the container too)."""
    epic, sep, _ = bead.rpartition(".")
    if not sep or not epic:
        return
    work_logic.ensure_container(cfg, hive, epic, main)


def _first(data, *keys):
    """First present, truthy value among keys (bd JSON field-name drift insurance)."""
    return next((data[k] for k in keys if data.get(k)), None)


# ---- at-merge flow metrics (hqfy.2): best-effort, skew-guarded bd reads ------
#
# Everything below feeds the commit-flow metrics emitted at the merge seam. EVERY bd read here is
# best-effort: the caller wraps the emission in try/except so a slow/failing read NEVER blocks a
# merge, and each individual metric is skipped when its inputs are missing or its delta is negative
# (clock skew / out-of-order data). Attributes are bounded — no bead/epic ids on the metric points.


def _hive(entry) -> str:
    """The low-cardinality hive name for a metric attribute (the managed-repo prefix)."""
    return str(entry.get("prefix", "") or "")


def _vres(rc: int) -> str:
    """The bounded ``ws.validation.result`` attribute value for a validation exit code."""
    return "pass" if rc == 0 else "fail"


def _parse_ts(value):
    """Parse a bd RFC3339/ISO timestamp into an aware UTC datetime, or None when absent/unparseable
    (so a missing field just skips its metric rather than raising)."""
    if not value:
        return None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)
    except (ValueError, TypeError):
        return None


def _emit_delta(record_fn, end, start, attrs) -> None:
    """Record ``(end-start)`` seconds via ``record_fn`` iff both timestamps are present and the
    delta is non-negative — a negative delta (clock skew / out-of-order data) is skipped, never
    recorded."""
    if end is None or start is None:
        return
    delta = (end - start).total_seconds()
    if delta < 0:
        return  # skew guard: never record a negative duration
    record_fn(delta, attrs)


def _flow_events(bead, cwd):
    """The bead's lifecycle event records (``type=event`` infra children), or None on read failure
    (so the caller can tell 'no events' from 'couldn't read')."""
    rows = bd.json(["list", "--parent", bead, "--include-infra"], cwd)
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict) and str(r.get("issue_type") or "") == "event"]


def _event_text(ev) -> str:
    """Lower-cased haystack of an event's human/text fields for transition matching."""
    return " ".join(
        str(ev.get(k) or "") for k in ("title", "description", "reason", "to_state", "state")
    ).lower()


def _is_review_pending(ev) -> bool:
    t = _event_text(ev)
    return "review" in t and "pending" in t


def _is_changes_requested(ev) -> bool:
    t = _event_text(ev)
    return "changes-requested" in t or "changes_requested" in t


def _review_pending_at(events):
    """created_at of the FIRST review→pending event (the submit moment), or None."""
    for ev in events:
        if _is_review_pending(ev):
            return _parse_ts(_first(ev, "created_at", "created"))
    return None


def _clear_review_label(bead, data, main, actor="") -> None:
    """Strip any stale ``review:*`` dimension label once the review lifecycle is over (approved /
    merged / closed). ``bd set-state`` only ever *replaces* a dimension label, never clears it, so
    without this a "what's awaiting review" query (``review:pending``) surfaces long-closed beads
    fleet-wide. Best-effort — a label already gone is fine."""
    labels = data.get("labels") if isinstance(data, dict) else None
    for lbl in labels or []:
        if str(lbl).startswith("review:"):
            bd.run(["label", "remove", bead, str(lbl)], main, actor=actor)


def _strip_review_pending(row, main, actor) -> int:
    """Remove ``review:pending`` from one closed-bead row (a `bd list` result); 1 if cleared, 0 if
    the row had no id or the removal failed. Per-bead so a partial failure never masks the others'
    outcome in the returned count."""
    bid = str(row.get("id") or "") if isinstance(row, dict) else ""
    if not bid:
        return 0
    res = bd.run(["label", "remove", bid, "review:pending"], main, actor=actor)
    return int(res.returncode == 0)


def backfill_stale_review_labels(main, actor="") -> int:
    """One-time cleanup: strip ``review:pending`` from every already-closed bead — the label was
    never cleared on close/merge before this fix, so it lingers on historical work and pollutes
    review queries. Returns the count cleaned; idempotent (safe to re-run). A data migration tool,
    not a lifecycle verb — invoke once per hive (`from beadhive.work import
    backfill_stale_review_labels`)."""
    rows = bd.json(["list", "--status", "closed", "--label", "review:pending"], main)
    if not isinstance(rows, list):
        return 0
    return sum(_strip_review_pending(r, main, actor) for r in rows)


def _open_gates(cwd) -> list:
    """Every gate (open + resolved) in `cwd`'s hive — the one full-window `bd gate list --all`
    fetch `_security_gate` and `_release_hold_gate` both filter, so a caller checking both (e.g.
    `approve`) spawns it once instead of twice."""
    gates = bd.json(["gate", "list", "--all", "--limit", "0"], cwd)
    return gates if isinstance(gates, list) else []


def _match_gate(gates, bead, matcher):
    """First gate in `gates` naming `bead` in its description and satisfying `matcher`, or None."""
    for g in gates:
        desc = str(g.get("description") or "").lower()
        if bead.lower() in desc and matcher(g):
            return g
    return None


def _security_gate(gates, bead):
    """The Assurance `security:*` gate for `bead` (a `security:` marker in its description), or
    None — the warden-owned gate that blocks the merge in parallel with review (bead .33). Matched
    like `work_logic.review_gates` (description-based) but on `guard.is_security_gate`, so
    kickoff/review gates don't match. `gates` is a pre-fetched `_open_gates` result."""
    return _match_gate(gates, bead, guard.is_security_gate)


def _release_hold_gate(gates, bead):
    """The `release-hold:` gate for `bead` (a `release-hold:` marker in its description), or None —
    the releaser-owned gate that holds a release:breaking change out of the current release window
    (bh-k2j8) and blocks the merge like any open gate. Matched like `_security_gate` but on
    `guard.is_release_hold_gate`, so review/security/kickoff gates don't match. `gates` is a
    pre-fetched `_open_gates` result."""
    return _match_gate(gates, bead, guard.is_release_hold_gate)


def _stage_recorder(stage):
    """A ``(seconds, attrs)`` recorder bound to one flow ``stage`` (for ``_emit_delta``)."""
    return lambda seconds, attrs: otel.record_stage(stage, seconds, attrs)


def _emit_cycle(data, attrs) -> None:
    """Emit cycle_time (now−created_at) + cycle_time.active (now−started_at) for a bead/epic.
    Shared by the bead and molecule merge paths (molecule emits ONLY this + slot, no stage)."""
    now = datetime.datetime.now(datetime.UTC)
    created = _parse_ts(_first(data or {}, "created_at", "created"))
    started = _parse_ts(_first(data or {}, "started_at", "started"))
    _emit_delta(otel.record_cycle_time, now, created, attrs)
    _emit_delta(otel.record_cycle_time_active, now, started, attrs)


def _emit_bead_flow(bead, data, main, attrs) -> None:
    """At-merge cycle + stage + rework metrics for one bead (NOT the molecule path). Best-effort
    + skew-guarded throughout; the caller wraps this in try/except so it never blocks the merge.

    Decomposition: coding = started→review_pending, review_wait = review_pending→gate_closed,
    merge_latency = gate_closed→now; rework = count of review→changes-requested events."""
    _emit_cycle(data, attrs)
    now = datetime.datetime.now(datetime.UTC)
    started = _parse_ts(_first(data or {}, "started_at", "started"))

    events = _flow_events(bead, main)
    event_pending_at = None
    if events is not None:
        event_pending_at = _review_pending_at(events)
        otel.record_rework(sum(1 for e in events if _is_changes_requested(e)), attrs)

    open_gates, resolved_gates = work_logic.review_gates(bead, main)
    # At merge every review gate is resolved; superseded duplicates resolve earlier, so the
    # LAST resolved gate (creation order) is the approved one — the submit/approve moments.
    gate = open_gates[0] if open_gates else (resolved_gates[-1] if resolved_gates else None)
    gate_closed_at = _parse_ts(_first(gate or {}, "closed_at", "resolved_at")) if gate else None
    # The submit moment: `bd set-state review=pending` materializes no infra event child, so the
    # event scan is empty in practice and coding/review_wait never emitted. The review gate is
    # opened at that same submit, so fall back to its created_at — resurrecting both stages with
    # zero new writes (event scan stays primary for when an event is present).
    gate_opened_at = _parse_ts(_first(gate or {}, "created_at", "created")) if gate else None
    review_pending_at = event_pending_at or gate_opened_at

    _emit_delta(_stage_recorder("coding"), review_pending_at, started, attrs)
    _emit_delta(_stage_recorder("review_wait"), gate_closed_at, review_pending_at, attrs)
    _emit_delta(_stage_recorder("merge_latency"), now, gate_closed_at, attrs)


# ---- guards & shared steps ---------------------------------------------------


# Identity namespaces: dispatchers drive molecules (container beads), developers implement leaves.
# Prefixes + returned seat literals follow the roles/RBAC matrix (docs/design/roles-rbac-matrix.md):
# dispatcher (disp/) coordinates a set of beads on a long-lived branch; developer (dev/) implements
# ONE bead on an ephemeral bead branch.
_DISP_PREFIX = "disp/"
_DEV_PREFIX = "dev/"

# Back-compat shim: legacy seat prefixes (pre roles/RBAC matrix) still resolve during the
# migration window, mapped legacy -> (seat, canonical replacement prefix). A legacy identity keeps
# working (with a one-line deprecation warning) so in-flight coord//crew/ sessions don't break;
# removed later per the limn/kkke sequencing.
_LEGACY_SEAT_PREFIXES = {
    "coord/": ("dispatcher", _DISP_PREFIX),
    "crew/": ("developer", _DEV_PREFIX),
}

# Orchestrator seats (roles/RBAC matrix §2.2, bead .38): only a dispatcher (disp/) — the
# Integration-plane seat that assigns work — or a director (dir/) — the Control-plane routing
# seat — may run `ws work assign`. A developer/reviewer/merger/… can't dispatch work to itself
# or anyone else; a bare human/supervised operator (no recognized seat prefix) is exempt.
_DIRECTOR_PREFIX = "dir/"

# Every canonical seat prefix (roles/RBAC matrix §2), plus the legacy coord//crew/ shim. An
# identity carrying one of these is a *seat* bound by the seat conventions; anything else is a
# bare human / supervised operator, exempt from the seat-only guards. Kept local (not sourced from
# escalate._SEAT_ROLES, which keys on a role's *word* e.g. 'review', not the 'rev/' prefix).
_KNOWN_SEAT_PREFIXES = frozenset(
    {
        # Control
        "super/",
        "dir/",
        "cust/",
        "ctrl/",
        # Planning
        "plan/",
        "analyst/",
        # Integration
        "disp/",
        "dev/",
        "rev/",
        "merge/",
        # Assurance
        "warden/",
        "verify/",
        # Release / Contribution / Delivery (roadmap)
        "release/",
        "contrib/",
        "ops/",
        # Legacy migration shim
        "coord/",
        "crew/",
    }
)


def _is_epic(data) -> bool:
    """True iff the bead's declared issue_type is `epic` (a container/molecule, not a leaf)."""
    return str((data or {}).get("issue_type") or "") == "epic"


def _kind_of(data) -> str:
    """The `wt/bead/<type>/` namespace segment for a bead: `epic` for a container (dispatcher
    seat), else `issue` (leaf). Threaded into `worktree.ensure`/`locate` so a bead branch is
    provisioned under the right namespace even before it exists (nothing to probe yet)."""
    return "epic" if _is_epic(data) else "issue"


def _push_state(cfg, main, actor, message) -> None:
    """Best-effort publish of local bead state to the hive's remote (bh-dw3e.6, closing the
    BEADS-SYNC gap): `assign`/`submit` mutate the local DB first, then push so a developer on
    another host actually sees it. `Engine.push_state`'s own `bd dolt push` already exits 0 with
    nothing to do on a solo/no-remote hive (matches the no-remote no-block goal for free); any
    OTHER failure is surfaced as a warning — not raised — so a flaky remote can't turn a already-
    -successful local mutation into a blocked verb."""
    from . import engine

    res = engine.get_engine(cfg).push_state(main, actor=actor, message=message)
    if res.returncode != 0:
        typer.echo(f"⚠ state push failed: {bd.err_line(res)}", err=True)


def _pull_state(cfg, main) -> None:
    """Best-effort refresh of local bead state from the hive's remote (bh-dw3e.6) — `claim`/
    `resume` pull first so they act on the latest assignment/feedback rather than only the local
    DB. A hive with no Dolt remote configured is a normal single-host setup (`bd dolt pull`
    errors 'no remote' there) and is skipped without noise; any other pull failure is a warning,
    not a hard stop, so a flaky remote can't block a developer from claiming/resuming their own
    bead."""
    from . import engine

    res = engine.get_engine(cfg).pull_state(main)
    if res.returncode != 0 and "no remote" not in bd.err_line(res).lower():
        typer.echo(f"⚠ state pull failed: {bd.err_line(res)}", err=True)


def _work_preview(cfg, hive, bead, stamp_actor, op) -> dict:
    """Side-effect-free 'what would claim/assign provision + stamp': `worktree.preview()`'s
    contract plus the identity `_stamp` would apply for `stamp_actor` (the actor for `claim`,
    `--to` for `assign`). No `bd` write, no git write — a read-only `bd.show` + `locate` only."""
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    data = bd.show(bead, main)
    result = worktree.preview(cfg, hive, bead=bead, kind=_kind_of(data), op=op)
    prof = config.work_identity(cfg, entry, stamp_actor)
    result["identity"] = {
        "mode": prof["mode"],
        "name": stamp_actor or prof["name"] or "",
        "email": prof["email"] or "",
        "signing_key": prof["signing_key"] or "",
        "sign": prof["sign"],
    }
    return result


def _print_work_preview(cfg, hive, bead, stamp_actor, op, as_json) -> None:
    """Render `_work_preview` as JSON (orchestrator input) or a short human summary."""
    result = _work_preview(cfg, hive, bead, stamp_actor, op)
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"{op} preview: {bead} → {result['branch']}  ({result['would']})")
    typer.echo(f"  path {result['path']}")
    ident = result["identity"]
    typer.echo(f"  identity {ident['name']} <{ident['email']}> (mode={ident['mode']})")


def _seat_of(name: str) -> str:
    """The seat an identity names: 'dispatcher' (disp/<name>), 'developer' (dev/<name>),
    or '' when neither prefix matches. Legacy coord//crew/ prefixes still resolve
    (dispatcher/developer) via the back-compat shim, with a one-line deprecation warning."""
    if name.startswith(_DISP_PREFIX):
        return "dispatcher"
    if name.startswith(_DEV_PREFIX):
        return "developer"
    for legacy, (seat, replacement) in _LEGACY_SEAT_PREFIXES.items():
        if name.startswith(legacy):
            from . import log  # lazy: avoid a hard log import at module load

            log.get_logger(__name__).warning(
                "legacy_seat_prefix_deprecated",
                deprecated=legacy,
                replacement=replacement,
                seat=seat,
                reason="seat prefixes renamed per roles/RBAC matrix (coord/->disp/, crew/->dev/)",
            )
            return seat
    return ""


def _guard_seat(data, name, bead, *, verb):
    """Type-driven seat enforcement: an epic (container) may only be worked by a dispatcher
    (disp/<name>), any other bead only by a developer (dev/<name>) — so a dispatcher drives a
    molecule and a developer implements a leaf, and the two agent seats never cross wires (also
    lets Claude bash-prefix permissions gate them). A non-seat identity (a human/supervised
    operator, no disp//dev/ prefix) is exempt — humans aren't bound by the seat convention.
    `verb` tails the message ('assigned to' / 'claimed by')."""
    want = "dispatcher" if _is_epic(data) else "developer"
    if _seat_of(name) in ("", want):
        return
    kind = "epic" if _is_epic(data) else "issue"
    pfx = _DISP_PREFIX if want == "dispatcher" else _DEV_PREFIX
    typer.echo(
        f"✗ {bead} is an {kind} — it may only be {verb} a {want} ({pfx}<name>), not {name!r}",
        err=True,
    )
    raise typer.Exit(1)


def _is_orchestrator(name: str) -> bool:
    """Whether `name` is an orchestrator seat allowed to dispatch work: a dispatcher (disp/) or a
    director (dir/). Legacy coord/ still resolves (→ dispatcher) via the back-compat shim."""
    if name.startswith(_DISP_PREFIX) or name.startswith(_DIRECTOR_PREFIX):
        return True
    for legacy, (seat, _replacement) in _LEGACY_SEAT_PREFIXES.items():
        if name.startswith(legacy):
            return seat == "dispatcher"
    return False


def _names_a_seat(name: str) -> bool:
    """Whether `name` carries a recognized seat prefix (so it's bound by the seat convention). A
    bare human / supervised operator with no recognized prefix is NOT a seat and stays exempt —
    the same carve-out `_guard_seat` and the control-plane guards make for humans."""
    return any(name.startswith(pfx) for pfx in _KNOWN_SEAT_PREFIXES)


def _guard_orchestrator(actor, bead):
    """`ws work assign` is orchestrator-only (roles/RBAC matrix §2.2, bead .38): stamping an
    assignee + provisioning a worktree is a dispatch action, reserved for a dispatcher (disp/) or
    director (dir/). A recognized non-orchestrator seat (developer, reviewer, merger, warden, …) is
    hard-denied — a leaf worker cannot dispatch work. A non-seat identity (human/supervised
    operator, no recognized prefix) is exempt, so existing supervised flows are unaffected."""
    if _is_orchestrator(actor) or not _names_a_seat(actor):
        return
    typer.echo(
        f"✗ {bead}: `{config.BINARY_ALIAS} work assign` is orchestrator-only — "
        "only a dispatcher (disp/<name>) or "
        f"director (dir/<name>) may assign work, not {actor!r}.",
        err=True,
    )
    raise typer.Exit(1)


def _epic_of(data, bead) -> str:
    """The molecule (epic) a dispatch acts on: an epic is its own molecule; a child's molecule is
    its parent epic (the `parent` field, falling back to the dotted-id stem like
    _maybe_open_molecule does). '' when there's no molecule to gate (an orphan/ad-hoc leaf)."""
    if _is_epic(data):
        return bead
    parent = str((data or {}).get("parent") or "").strip()
    if parent:
        return parent
    stem, sep, _ = bead.rpartition(".")
    return stem if sep else ""


def _guard_conventions(cfg, data, bead, main, *, action):
    """Dispatch gate: refuse to route work off a MALFORMED molecule, surfacing the plan-plane
    validator's specific problem list (not a cryptic refusal / silent main fork). Resolve the
    parent epic first, then reuse `plan.verify_epic` via `plan.enforce_epic_conventions` (BH_DEBUG
    overrides for humans). No-op when there's no molecule to verify."""
    from . import plan  # lazy: keep the plan<->work seam import-cycle-safe (mirrors work_group)

    epic = _epic_of(data, bead)
    if not epic:
        return
    plan.enforce_epic_conventions(epic, cfg, main, action=action)


def _print_brief(cfg, entry, bead, data):
    if not data:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    typer.echo(f"# {data.get('id', bead)}  {data.get('title', '')}")
    desc = _first(data, "description")
    if desc:
        typer.echo(f"\n## Requirements / goals\n{desc}")
    acc = _first(data, "acceptance_criteria", "acceptance")
    if acc:
        typer.echo(f"\n## Acceptance\n{acc}")
    design = _first(data, "design")
    if design:
        typer.echo(f"\n## Design\n{design}")
    typer.echo(f"\n## Validate with\n{config.validate_cmd(cfg, entry)}")


# ---- verbs ------------------------------------------------------------------

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_BEAD = typer.Argument(..., metavar="<id>", help="bead id")
_BEAD_OPT = typer.Argument("", metavar="<id>", help="bead id (omit when using --group)")
_AS = typer.Option("", "--as", help="dev/<name> identity (default: config/$BH_DEV/git)")
_GROUP = typer.Option(
    "", "--group", help="batch mode: comma-separated member ids sharing a batch:<group> label"
)
_COLLAPSE = typer.Option(
    "", "--collapse", help="collapsed mode: <epic> — run its ready children as one grouped session"
)
_BOUNCE_MSG = typer.Option("", "-m", "--message", help="changes-requested reason for the developer")
# Annotated (not a bare `typer.Option(False, ...)` default): claim/assign are called directly as
# plain Python functions throughout the test suite, and a bare OptionInfo default is truthy when
# no CLI parsing runs — Annotated keeps the real runtime default `False` while still wiring the
# flag.
_Preview = Annotated[
    bool, typer.Option("--preview", help="read-only: print what this call would provision + stamp")
]
_PreviewJson = Annotated[
    bool, typer.Option("--json", help="render --preview as machine-readable JSON")
]
# Same Annotated reasoning as above: `next` is called as a plain function in the tests.
_NextJson = Annotated[bool, typer.Option("--json", help="emit the machine-readable envelope")]


@app.command("brief")
@otel.trace_verb("work.brief")
def brief(bead: str = _BEAD, hive: str = _HIVE):
    """Print the bead's requirements/goals and the repo's validation command. Read-only."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    _print_brief(cfg, entry, bead, bd.show(bead, main))


# ---- first-class bead reads (replace `ws bd ready|show|list` in the loops) ---
#
# The coordinator/developer loops read ready work, one issue, and filtered issue lists — today via
# the `ws bd` passthrough (`ws bd ready --json`, `ws bd show <id> --json`). These verbs surface the
# same reads first-class so those loops never invoke `ws bd`, and stay byte/JSON-shape stable by
# forwarding straight to `bd` (capture-then-stream) — no reshaping — so the passthrough can later be
# gated off without touching a consumer. Each accepts arbitrary trailing `bd` flags (`--json`,
# `--gated`, `--status …`) via `ignore_unknown_options`, on top of the ws `--hive`.

_READ_CTX = {"allow_extra_args": True, "ignore_unknown_options": True}


def _reorder_ready_lines(text: str, ordered_ids) -> str:
    """Reorder the bead lines of a `bd ready` table by `ordered_ids` (advisory merge order), leaving
    header/footer/blank lines in place. A bead line is any line carrying a known id as a token; the
    lines matching ids are re-sequenced among their own slots, everything else stays put — so bd's
    framing is preserved and only the rows move."""
    pos = {bid: i for i, bid in enumerate(ordered_ids)}
    lines = text.splitlines(keepends=True)

    def id_of(line: str):
        return next((t for t in line.split() if t in pos), None)

    slots = [i for i, ln in enumerate(lines) if id_of(ln) is not None]
    reordered = sorted((lines[i] for i in slots), key=lambda ln: pos[id_of(ln)])
    for slot, line in zip(slots, reordered, strict=True):
        lines[slot] = line
    return "".join(lines)


def _count_avoided_conflicts(beads, order, estimator, strategy) -> None:
    """Counter of conflicts the release merge-order scorer avoided (bh-k2j8.8): for each pair of
    beads ADJACENT in the natural/FCFS read order (`beads`, as `bd` returned them), ask the
    `ConflictEstimator` (same `estimator`/threshold the start-gate uses,
    `schedule_mod.DEFERRAL_LIKELIHOOD`) whether the pair is conflict-likely. When it is, AND the
    scorer's `order` does NOT keep the pair adjacent (something else got sequenced between them),
    record one `record_conflict_avoided` — the re-sequencing kept two conflict-likely beads from
    landing back-to-back. Advisory telemetry only: an O(n) scan over adjacent pairs, never a new
    decision surface."""
    pos = {bid: i for i, bid in enumerate(order)}
    for a, b in zip(beads, beads[1:], strict=False):
        verdict = release_order.start_verdict(b, [a], estimator=estimator)
        if verdict.likelihood < schedule_mod.DEFERRAL_LIKELIHOOD:
            continue
        ai = pos.get(str(a.get("id") or ""))
        bi = pos.get(str(b.get("id") or ""))
        if ai is None or bi is None or abs(ai - bi) == 1:
            continue  # still adjacent (or unranked) in the scorer's order — not avoided
        otel.record_conflict_avoided({"bh.release.strategy": strategy})


def _forward_ready_ordered(args, cwd, strategy, fix_churn_budget, estimator) -> None:
    """`work ready --gated` under a configured `release.strategy`: forward `bd ready` re-sequenced
    by the advisory scorer (release_order) instead of FCFS. Reads the bead set once as JSON to
    derive the order, then emits it in the shape the caller asked for — the JSON array reordered, or
    the table's rows reordered in place. On any read miss it falls back to a plain verbatim forward
    (no behavior change), so the advisory layer never breaks the base read."""
    beads = bd.json(["ready", *[a for a in args if a != "--json"]], cwd)
    if not isinstance(beads, list) or not beads:
        _forward_read(["ready", *args], cwd)
        return
    order = release_order.merge_sequence(
        beads, strategy=strategy, fix_churn_budget=fix_churn_budget
    )
    _count_avoided_conflicts(beads, order, estimator, strategy)
    pos = {bid: i for i, bid in enumerate(order)}
    if "--json" in args:
        ordered = sorted(beads, key=lambda b: pos.get(str(b.get("id") or ""), len(order)))
        sys.stdout.write(json.dumps(ordered, indent=2) + "\n")
        return
    res = bd.run(["ready", *args], cwd, capture=True)
    if res.stdout:
        sys.stdout.write(_reorder_ready_lines(res.stdout, order))
    if res.stderr:
        sys.stderr.write(res.stderr)
    raise typer.Exit(res.returncode)


# ---- bh-i0p1.2: `ready`'s truncation must never be silent --------------------
#
# bd's own `-n`/`--limit` defaults to 100 (bd ready --help). Below the cap the read is complete;
# above it bd ALREADY says so — inside the table's own footer for a plain render, on bd's OWN
# stderr for `--json` (so the array itself stays clean). Confirmed live: bd never prints the
# "Showing X of Y" line at all when the read isn't actually truncated, so its mere presence is a
# reliable signal — no second bd call needed to confirm a total. Two gaps remain: the table
# footer lives in stdout, exactly what `bh work ready | grep <id>` throws away; and a `--json`
# caller who parses stdout and checks $? for success never learns bd put anything on stderr at
# all. Neither needs new data, just making bd's own signal impossible to miss.

_READY_LIMIT_FLAGS = {"-n", "--limit"}
_READY_NARROWING_FLAGS = {
    "-l",
    "--label",
    "--label-any",
    "--exclude-label",
    "-t",
    "--type",
    "--exclude-type",
    "-p",
    "--priority",
    "-a",
    "--assignee",
    "-u",
    "--unassigned",
    "--parent",
    "--mol",
    "--mol-type",
    "--has-metadata-key",
    "--metadata-field",
}
_READY_SHOWING_RE = re.compile(r"Showing (\d+) of (\d+) ready issues")

# Distinct, documented exit code: a VALID but partial default-capped `--json` read the caller
# never asked to cap. 0 stays "complete"; bd's own non-zero codes (1 general error, 2 for
# --max-rows exceeded) are untouched — this is layered on top, only for `ready`, only when bd
# itself already flagged the read as truncated.
READY_TRUNCATED_EXIT = 3


def _ready_arg_name(tok: str) -> str:
    """The flag name of one arg token, stripping a `--flag=value` suffix."""
    return tok.split("=", 1)[0]


def _ready_has_flag(args, names) -> bool:
    return any(_ready_arg_name(a) in names for a in args)


def _widen_narrowed_ready_args(args: list[str]) -> list[str]:
    """A narrowed `ready` read (any filter flag, no explicit -n/--limit) is widened to `-n 0`
    (unbounded) here — a narrow question ("what's ready with label X?") should get a complete
    answer, never a silently-capped one. An unfiltered listing is left untouched: raising ITS cap
    would only move the cliff, not remove it (bh-i0p1.2's design note)."""
    if _ready_has_flag(args, _READY_LIMIT_FLAGS):
        return args  # the caller's own explicit cap — never second-guessed
    if not _ready_has_flag(args, _READY_NARROWING_FLAGS):
        return args  # unfiltered listing — cap stays bd's default, see _forward_ready_plain
    return [*args, "-n", "0"]


def _ready_truncated_exit(args, res, *, as_json: bool) -> int:
    """`res.returncode` unless bd's own output shows this default-capped read (no explicit
    -n/--limit) was truncated, in which case `--json` gets READY_TRUNCATED_EXIT (a caller
    checking $? — not just stdout bytes — can then tell a partial read from a complete one) and
    plain-table mode gets bd's "Showing X of Y" line mirrored onto stderr, where a `| grep`/pipe
    of stdout can't make it disappear the way it does when that line is the table's own last
    row."""
    if res.returncode != 0 or _ready_has_flag(args, _READY_LIMIT_FLAGS):
        return res.returncode
    haystack = res.stderr if as_json else res.stdout
    m = _READY_SHOWING_RE.search(haystack or "")
    if not m or m.group(1) == m.group(2):
        return res.returncode
    if as_json:
        return READY_TRUNCATED_EXIT
    typer.echo(f"⚠ {m.group(0)} — pass -n 0 for the full list", err=True)
    return res.returncode


def _forward_ready_plain(args, cwd) -> None:
    """Forward a plain `bd ready` read (no --gated, no release start-gate annotation) — the SAME
    bytes bd would produce for these exact args, on both stdout and stderr — then apply
    `_ready_truncated_exit` on top so a truncated default-capped read is never silent."""
    res = bd.run(["ready", *args], cwd, capture=True)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    raise typer.Exit(_ready_truncated_exit(args, res, as_json="--json" in args))


@app.command("ready", context_settings=_READ_CTX)
@otel.trace_verb("work.ready")
def ready(ctx: typer.Context, hive: str = _HIVE):
    """List ready (unblocked, dependency-ordered) work — first-class `bd ready`. Read-only.

    Pass `--json` for the coordinator loop's machine shape, `--gated` for beads whose review gate
    just closed. Extra flags forward to `bd ready` unchanged.

    When `--gated` is passed and `release.strategy` is configured, the gated set is re-sequenced by
    the advisory release scorer (the strategy-preferred merge order) rather than FCFS; with no
    strategy set the forward is byte-verbatim (no behavior change).

    Truncation (bh-i0p1.2): bd's own `-n`/`--limit` defaults to 100. A narrowed read (any filter
    flag — --label/--type/--priority/--assignee/--parent/--mol/…) with no explicit -n/--limit is
    widened to unbounded here, since a narrow question should get a complete answer. An unfiltered
    listing keeps bd's own default cap, but a truncated result is never silent: the table forward
    mirrors bd's "Showing X of Y" line onto stderr too (a `| grep`/pipe of stdout can otherwise
    make it disappear), and a truncated `--json` read exits READY_TRUNCATED_EXIT (3) instead of 0
    so a caller checking $? can tell a partial read from a complete one. An explicit -n/--limit is
    the caller's own deliberate cap and is never second-guessed."""
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    args = _widen_narrowed_ready_args(list(ctx.args))
    # Opt-in release start-gating (bh-k2j8.6): only a plain `--json` read on a hive that set
    # `release.strategy` gets deferred beads annotated; the merger's scorer-sorted `--gated` view is
    # the sibling merge-order bead's (.7), handled below. Every other call — and every
    # default-config hive — falls through to the verbatim `bd ready` forward, so today's byte-shape
    # is untouched.
    if "--json" in args and "--gated" not in args:
        entry = registry.entry_for_dir(cfg, cwd)
        if str(config.release_value(cfg, entry, "strategy", "") or ""):
            _emit_start_gated_ready(cfg, entry, cwd, args)
            return
    # Merge-slot advisory ordering (bh-k2j8.7): a `--gated` read on a hive that set
    # `release.strategy` is re-sequenced by the advisory release scorer (the strategy-preferred
    # merge order) rather than FCFS. Scoped out of the truncation fix above — `--gated` answers a
    # different question (molecules ready for gate-resume dispatch / the merger's scored subset),
    # not the plain "what's ready" listing bh-i0p1.2 was filed against.
    if "--gated" in args:
        entry = registry.entry_for_dir(cfg, cwd)
        strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
        if strategy:
            _forward_ready_ordered(
                args,
                cwd,
                strategy,
                config.release_fix_churn_budget(cfg, entry),
                config.release_conflict_estimator(cfg, entry),
            )
            return
    _forward_ready_plain(args, cwd)


def _emit_start_gated_ready(cfg, entry, cwd, args) -> None:
    """Emit `bd ready --json` with each bead annotated `deferred` by the release start-gate: a ready
    bead the scorer would hold — likely to conflict with work ranked ahead of it — is flagged so
    the dispatcher doesn't start it into a suboptimal merge slot. The queue order is the ready
    list's own dependency/FCFS order. Only reached when the hive opted into `release.strategy`; on
    any non-list `bd` output it forwards the raw bytes + exit code unchanged."""
    res = bd.run(["ready", *args], cwd, capture=True)
    try:
        beads = json.loads(res.stdout) if res.stdout.strip() else []
    except json.JSONDecodeError:
        beads = None
    if not isinstance(beads, list):
        if res.stdout:
            sys.stdout.write(res.stdout)
        if res.stderr:
            sys.stderr.write(res.stderr)
        raise typer.Exit(res.returncode)
    order = [str(b.get("id") or "") for b in beads]
    deferrals = schedule_mod.start_gate(
        beads, order, estimator=config.release_conflict_estimator(cfg, entry)
    )
    strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
    for _d in deferrals:
        otel.record_deferred_start({"bh.release.strategy": strategy})
    deferred = {d.id for d in deferrals}
    for bead in beads:
        bead["deferred"] = str(bead.get("id") or "") in deferred
    sys.stdout.write(json.dumps(beads, indent=2) + "\n")
    if res.stderr:
        sys.stderr.write(res.stderr)
    # bh-i0p1.2: the start-gate is a `--json` ready read too — same truncation risk, same signal.
    raise typer.Exit(_ready_truncated_exit(args, res, as_json=True))


@app.command("issue", context_settings=_READ_CTX)
@otel.trace_verb("work.issue")
def issue(ctx: typer.Context, bead: str = _BEAD, hive: str = _HIVE):
    """Show a single issue's fields — first-class `bd show <id>`. Read-only.

    Pass `--json` for the machine shape the router reads `model:` / `harness:` labels from. Extra
    flags forward to `bd show` unchanged."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    _forward_read(["show", bead, *ctx.args], registry.hive_dir_for(cfg, hive))


@app.command("list", context_settings=_READ_CTX)
@otel.trace_verb("work.list")
def list_(ctx: typer.Context, hive: str = _HIVE):
    """List / filter issues (e.g. `--status <state>`) — first-class `bd list`. Read-only.

    Pass `--json` for the machine shape. Extra flags forward to `bd list` unchanged."""
    cfg = config.load()
    _forward_read(["list", *ctx.args], registry.hive_dir_for(cfg, hive))


# ---- intake triage --------------------------------------
#
# The hive manager's fielding surface: `ws work intake` lists this hive's untriaged intake queue
# (source-agnostic — keyed on the shared `intake:untriaged` state, distinguished by the closed
# `origin` CHANNEL: report|github|import) and surfaces likely dupes via `bd find-duplicates`; the
# four disposition verbs (accept/reject/reroute/promote) dispose of a queued report, type-aware. The
# logic lives in `ws/triage.py`; these are thin CLI wrappers (hive-scoped like the read verbs).

_SOURCE = typer.Option(
    "", "--source", help="narrow to one intake channel (origin): report | github | import"
)
_INTAKE_JSON = typer.Option(False, "--json", help="emit {rows, dupes} as JSON")
_NO_DUPES = typer.Option(False, "--no-dupes", help="skip the bd find-duplicates pass")


def _render_disposition(code, error, message):
    """Render a triage disposition's (exit, error, message): echo the message, or fail with the
    error on a non-zero exit."""
    if error:
        typer.echo(f"✗ {error}", err=True)
        raise typer.Exit(code)
    typer.echo(message)


@app.command("intake")
@otel.trace_verb("work.intake")
def intake_cmd(
    hive: str = _HIVE,
    source: str = _SOURCE,
    as_json: bool = _INTAKE_JSON,
    no_dupes: bool = _NO_DUPES,
):
    """List this hive's untriaged intake queue (source-agnostic) + surface likely dupes. Read-only.

    A report lands as `intake:untriaged` no matter its channel; the resolved `origin` channel
    (report|github|import — the `origin:` label for reports, else derived from `source_system` for
    imports) rides each row. Dispose with `ws work accept|reject|reroute|promote`."""
    from . import triage

    cfg = config.load()
    triage.print_intake(
        registry.hive_dir_for(cfg, hive), source=source, dupes=not no_dupes, as_json=as_json
    )


@app.command("accept")
@otel.trace_verb("work.accept")
def accept_cmd(
    bead: str = _BEAD,
    issue_type: str = typer.Option("", "--type", "-t", help="set the accepted type (type-aware)"),
    priority: str = typer.Option("", "--priority", "-p", help="set priority (0-4 / P0-P4)"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Accept an intake report into backlog: set type/priority (both optional) + clear intake."""
    from . import triage

    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    _render_disposition(*triage.accept(cwd, bead, actor, issue_type=issue_type, priority=priority))


@app.command("reject")
@otel.trace_verb("work.reject")
def reject_cmd(
    bead: str = _BEAD,
    reason: str = typer.Option(..., "--reason", help="reporter-visible reason (recorded on close)"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Reject an intake report: clear intake + close it with a reporter-visible reason."""
    from . import triage

    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    _render_disposition(*triage.reject(cwd, bead, actor, reason=reason))


@app.command("reroute")
@otel.trace_verb("work.reroute")
def reroute_cmd(
    bead: str = _BEAD,
    to: str = typer.Option("", "--to", help="re-file the report into this hive"),
    super_: str = typer.Option("", "--super", help="bounce to this superintendent seat"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Reroute a mis-routed report: re-file into the right hive (`--to`), or bounce it to the
    superintendent (`--super`) to keep it in the fleet-wide inbox. Exactly one destination."""
    from . import triage

    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    _render_disposition(
        *triage.reroute(cwd, bead, actor, to_hive=to, superintendent=super_, cfg=cfg)
    )


@app.command("promote")
@otel.trace_verb("work.promote")
def promote_cmd(bead: str = _BEAD, as_: str = _AS, hive: str = _HIVE):
    """Promote an intake report to the planner (hand-off only; the adopt path is
    ). Sets `intake:promoted` — the planner's adopt queue key."""
    from . import triage

    otel.set_bead(bead)
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = identity.resolve_actor(as_)
    _render_disposition(*triage.promote(cwd, bead, actor))


@app.command("assign")
@otel.trace_verb("work.assign")
def assign(
    bead: str = _BEAD,
    to: str = typer.Option(..., "--to", help="dev/<name> to assign + provision for"),
    as_: str = _AS,
    hive: str = _HIVE,
    preview: _Preview = False,
    as_json: _PreviewJson = False,
):
    """Orchestrator-only: stamp the assignee and provision the worktree with that identity.
    Leaves status `open` — the worker's `claim` is the ack that flips it to in_progress.

    The acting identity (`--as` > config > $BH_DEV > git) must be an orchestrator seat — a
    dispatcher (disp/<name>) or director (dir/<name>); a non-orchestrator seat is hard-denied
    (bead .38), while a bare human/supervised operator is exempt.

    `--preview` (read-only): print the worktree provisioning + `--to` identity this call would
    stamp, without touching `bd` or git — the machine-readable pre-flight for an external
    orchestrator (`--json` for the schema)."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    if preview:
        _print_work_preview(cfg, hive, bead, to, op="assign", as_json=as_json)
        return  # --preview is read-only: never gated ("gate writes, never reads")
    guard.guard_primary(hive, cfg=cfg, verb="work assign")
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _guard_orchestrator(actor, bead)  # assign is orchestrator-only (disp//dir/); humans exempt
    data = bd.show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, to, bead)
    _guard_seat(data, to, bead, verb="assigned to")
    _guard_conventions(cfg, data, bead, main, action="dispatch")
    # EXPERIMENTAL (cit.5): the coordinator->developer dispatch seam. The coordinator agent loop
    # hands this bead to a developer crew — emit it as a GenAI `invoke_agent` span, with the brief
    # carried as a droppable span EVENT (gated no-op when otel is off; see ws.otel).
    brief_text = _first(data, "description")
    with otel.record_agent_dispatch(
        agent=to,
        model=config.otel_genai_model(cfg),
        system=config.otel_genai_system(cfg, entry),
        brief=brief_text,
        attributes={"bh.bead": bead},
    ):
        res = bd.run(["assign", bead, to], main)
        if res.returncode != 0:
            raise typer.Exit(res.returncode)
        _push_state(cfg, main, actor, f"assign {bead} -> {to}")
        _maybe_open_molecule(cfg, hive, bead, main)
        entry, target, _branch = worktree.ensure(cfg, hive, bead, kind=_kind_of(data))
        _stamp(cfg, entry, target, to)
    otel.count_bead_transition("assigned")  # bead id rides the span (set_bead), not the metric
    typer.echo(f"✓ assigned {bead} → {to}; worktree {target}")


def _claim_fence(cfg, hive) -> tuple[str, int]:
    """This host's `(host_id, epoch)` — the fencing token stamped into a fresh `ClaimRecord`
    (bh-ytbb.10). Both resolve LOCALLY: `host.host_id()` reads `~/.beadhive/host.yaml`, and
    `guard.live_epoch` reads the cached host lease (no network — see its docstring). Claiming
    must stay cheap; a worker that had to poll a remote to take a bead would be exactly the
    design this molecule rejects.

    Degrades to the unfenced `("", 0)` rather than failing the claim: a host that never ran
    `bh config init`, or a factory that never adopted anything, has no token to mint and must
    keep working exactly as before."""
    try:
        this_host = host.host_id()
    except FileNotFoundError:
        this_host = ""  # identity not minted (never ran `bh config init`): unfenced
    return this_host, guard.live_epoch(hive, cfg=cfg)


def _issue_claim(cfg, entry, bead, actor, target, hive="") -> None:
    """Mint + persist a `ClaimRecord` naming `actor` as this worktree's claim holder (bh-ejlq),
    through the configured `ClaimAuthority` (default Tier 0 `local`). `submit` reads this back to
    default its actor when `--as` is omitted, instead of re-deriving identity from ambient env/git
    and risking a mismatch against what `claim`/`resume` actually recorded.

    The record is also stamped with this host's fencing token (bh-ytbb.10) so `submit` can catch
    a claim that outlived the host lease it was taken under — see `_guard_claim_fence`."""
    authority = claim_authority.get_authority(config.claim_authority(cfg, entry))
    this_host, epoch = _claim_fence(cfg, hive)
    authority.issue(bead, actor, target, host_id=this_host, epoch=epoch)


@app.command("claim")
@otel.trace_verb("work.claim")
def claim(
    bead: str = _BEAD_OPT,
    as_: str = _AS,
    group: str = _GROUP,
    collapse: str = _COLLAPSE,
    hive: str = _HIVE,
    preview: _Preview = False,
    as_json: _PreviewJson = False,
):
    """Ack that you're starting: re-attach/provision the worktree with your identity, refuse
    if it's someone else's, then `bd update --claim` as your actor (→ in_progress).

    With `--group <ids>` this is the work-group ack: provision the ONE shared `wt/batch/<group>`
    worktree (members read from their `batch:<group>` labels), stamp it with your identity once,
    and claim every member — one agent owns the whole batch.

    With `--collapse <epic>` this is the collapsed ack: synthesize a `batch:<epic>` label on the
    epic's un-batched ready children, then claim them as one group — batching an epic the planner
    never labelled.

    `--preview` (read-only, single bead only): print the worktree provisioning + identity this
    call would stamp, without touching `bd` or git — the machine-readable pre-flight for an
    external orchestrator (`--json` for the schema)."""
    cfg = config.load()
    group = work_logic.opt_str(group)
    collapse = work_logic.opt_str(collapse)
    if preview:
        if collapse or group:
            typer.echo("✗ --preview supports a single <id> only (no --group/--collapse)", err=True)
            raise typer.Exit(1)
        if not bead:
            typer.echo("✗ pass a bead <id>", err=True)
            raise typer.Exit(1)
        entry, _main, _target, _branch = worktree.locate(cfg, hive, bead)
        actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
        _print_work_preview(cfg, hive, bead, actor, op="claim", as_json=as_json)
        return  # --preview is read-only: never gated ("gate writes, never reads")
    guard.guard_primary(hive, cfg=cfg, verb="work claim")
    if collapse:
        if bead or group:
            typer.echo("✗ pass either <id>, --group, or --collapse — not more than one", err=True)
            raise typer.Exit(1)
        work_group.claim_collapsed(cfg, hive, collapse, as_)
        return
    if group:
        if bead:
            typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise typer.Exit(1)
        work_group.claim_group(cfg, hive, group, as_)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise typer.Exit(1)
    _claim_single_bead(cfg, hive, bead, as_)


def _claim_single_bead(cfg, hive, bead, as_) -> None:
    """The single-bead claim: re-attach/provision the worktree with `actor`'s identity, refuse
    if it's someone else's or the wrong seat, then `bd update --claim` (→ in_progress)."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    _pull_state(cfg, main)  # see current state first — an assignment may have landed elsewhere
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = bd.show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, actor, bead)
    _guard_seat(data, actor, bead, verb="claimed by")
    _guard_conventions(cfg, data, bead, main, action="dispatch")
    _maybe_open_molecule(cfg, hive, bead, main)
    entry, target, _branch = worktree.ensure(cfg, hive, bead, kind=_kind_of(data))
    _stamp(cfg, entry, target, actor)
    _issue_claim(cfg, entry, bead, actor, target, hive)
    res = bd.run(["update", bead, "--claim"], main, actor=actor)
    if res.returncode != 0:
        raise typer.Exit(res.returncode)
    otel.count_bead_transition("claimed")  # bead id rides the span (set_bead), not the metric
    typer.echo(f"✓ claimed {bead} as {actor}; worktree {target}")
    _print_brief(cfg, entry, bead, data)
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"\nWARNING: cwd is not the bead worktree — edits here target the wrong tree.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )


def _batch_member_procedure_msg(bead, grp) -> str:
    """The error a per-bead `submit`/`check` on a BATCH member gets instead of the misleading
    "claim it first": a batch member has no per-bead worktree — the whole batch lives in the ONE
    shared `wt/batch/<grp>` worktree and completes as a UNIT (bh-n5z3.7)."""
    alias = config.BINARY_ALIAS
    return (
        f"✗ {bead} is a batch member (batch:{grp}) — it has no per-bead worktree.\n"
        f"  Batch work happens in the ONE shared worktree wt/batch/{grp} and completes as a UNIT:\n"
        f"      {alias} work submit --group <ids>   # one review gate for the whole batch\n"
        f"      {alias} work merge --group <ids>    # after approval"
    )


def _batch_worktree(cfg, hive, bead, main):
    """`(group, shared worktree)` for `bead`'s `batch:<group>` label — the ONE seam per-bead verbs
    use to refuse to act on a batch member's own dir (bh-c3nf).

    `("", None)` when it carries no batch label. `(grp, None)` when it does but `wt/batch/<grp>`
    is absent. A batch member's artifact is ALWAYS the shared worktree: any `wt/bead/<type>/<id>`
    dir is a stray from a per-bead verb and holds none of the group's work, so callers must key on
    the returned group and never on that dir's existence."""
    grp = work_group.batch_label(bd.show(bead, main))
    if not grp:
        return "", None
    target = worktree.locate(cfg, hive, branch=f"{work_group.BATCH_PREFIX}{grp}")[2]
    return grp, (target if target.exists() else None)


# ---- next: the optimistic pick → claim → re-verify loop ----------------------
#
# `bd update --claim` is NOT a hard compare-and-swap: two drivers racing for the same bead can
# both see exit 0, and the last write wins. Every external driver that picks a bead off
# `bd ready` and then claims it therefore reimplements the same race — badly, and separately.
# This verb is the ONE safe entry point: pick optimistically, claim, then RE-READ the bead and
# verify we are the holder, moving to the next candidate when we lost. Losing a race is the
# normal case under an unattended dispatcher, not an error, so it is never surfaced as a failure.
#
# SCOPE (bh-qczj.1): the transition only. No worktree is provisioned here — `worktree` reports
# null and bh-qczj.2 fills it in, along with the richer `--json` envelope.

#: Version of the `bh work next --json` contract (`_next_payload`).
NEXT_SCHEMA = 1

#: Exit code for a clean decline — nothing eligible, or every candidate lost its race. DISTINCT
#: from 1 on purpose: "nothing to do" is a normal poll result an unattended driver must be able to
#: tell apart from "the call failed", without parsing stderr.
NEXT_DECLINE_EXIT = 3


def _next_payload(hive, actor, claimed, rows, tried) -> dict:
    """The `bh work next` envelope — one stable key set for both outcomes, so a consumer branches
    on `status` rather than on which keys happen to be present."""
    return jsonout.envelope(
        "work next",
        NEXT_SCHEMA,
        {
            "status": "claimed" if claimed else "declined",
            "bead": claimed,
            "actor": actor,
            "seat": _seat_of(actor),
            "hive": hive,
            "worktree": None,  # bh-qczj.2 provisions it; this slice has no worktree side effects
            "reason": "" if claimed else work_next.decline(rows, tried),
            "tried": list(tried),
        },
    )


def _try_claim(bead, actor, main) -> bool:
    """Claim `bead` for `actor`, then RE-VERIFY by re-reading it. True only when we hold it.

    The re-read is the whole point (see the block comment above): a non-zero claim means we lost
    outright, but a ZERO claim proves nothing — `bd` will happily hand the same bead to a second
    caller. `work_next.claim_won` decides from the re-read row, so the verdict is a pure function
    of what the store actually says rather than of an exit code."""
    res = bd.run(["update", bead, "--claim"], main, actor=actor)
    if res.returncode != 0:
        return False
    return work_next.claim_won(bd.show(bead, main), actor)


@app.command("next")
@otel.trace_verb("work.next")
def next_(as_: str = _AS, hive: str = _HIVE, as_json: _NextJson = False):
    """Atomically take the next ready bead: pick, claim, re-verify — retrying the next candidate
    when another worker won the race. The safe entry point for an unattended driver.

    Walks `bh work ready` order (dependency-ordered, and release-scored when the hive configured a
    strategy) and claims the first candidate it can PROVE it holds. Declines cleanly — exit 3 and
    a `status: declined` envelope reasoned `empty_queue` / `none_eligible` / `all_lost` — when
    nothing is takeable; that is a normal poll result, not a failure.

    This slice performs NO worktree side effects: the bead transitions to in_progress under your
    identity and `worktree` reports null. Run `bh work claim <id>` to get the worktree until
    bh-qczj.2 folds provisioning in."""
    cfg = config.load()
    guard.guard_primary(hive, cfg=cfg, verb="work next")
    main = registry.hive_dir_for(cfg, hive)
    # `or {}`: a hive dir that resolves to no registered entry still has a queue to serve — fall
    # back to the global work defaults rather than failing a poll on a config lookup.
    entry = registry.entry_for_dir(cfg, main) or {}
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _pull_state(cfg, main)  # see the CURRENT queue: a claim may have landed on another host
    rows = [r for r in (bd.json(["ready"], main) or []) if isinstance(r, dict)]
    tried: list[str] = []
    claimed = ""
    for bead in work_next.eligible(rows, actor):
        tried.append(bead)
        if _try_claim(bead, actor, main):
            claimed = bead
            otel.set_bead(bead)  # stamp ws.bead/ws.epic once this tick knows which bead it took
            otel.count_bead_transition("claimed")
            break
    if as_json:
        jsonout.emit(_next_payload(_hive(entry), actor, claimed, rows, tried))
    elif claimed:
        typer.echo(f"✓ claimed {claimed} as {actor} (no worktree — run `bh work claim {claimed}`)")
    else:
        typer.echo(f"— nothing to claim ({work_next.decline(rows, tried)})", err=True)
    if not claimed:
        raise typer.Exit(NEXT_DECLINE_EXIT)


@app.command("check")
@otel.trace_verb("work.check")
def check(bead: str = _BEAD, hive: str = _HIVE):
    """Run the hive's validation command against the worktree; propagate its exit code.

    A green run against a CLEAN tree also seeds the verdict ledger `submit` reuses from
    (bh-i0p1.4), so the ordinary check-then-submit sequence pays for validation once, not
    twice — see `_record_check_verdict`."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, target, _branch = worktree.locate(cfg, hive, bead)
    # Batch membership is probed BEFORE `target.exists()`, not inside it (bh-c3nf). The old order
    # let ANY per-bead dir shadow the redirect, and `resume` used to create exactly that: a stray
    # `wt/bead/issue/<id>` at the container tip holding none of the group's work. `check` then
    # validated the stray tree and — via `_record_check_verdict` — seeded that FALSE GREEN into the
    # ledger `submit` reuses, for a sha that never contained the change.
    grp, batch_target = _batch_worktree(cfg, hive, bead, main)
    if grp:
        # A batch member: check is read-only, so redirect to the shared batch worktree when it
        # exists rather than erroring; otherwise name the batch procedure (bh-n5z3.7).
        if batch_target is None:
            typer.echo(_batch_member_procedure_msg(bead, grp), err=True)
            raise typer.Exit(1)
        target = batch_target
    elif not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"WARNING: cwd is not the bead worktree — uncommitted edits here are invisible.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )
    # Telemetry-neutral env so `check` agrees with `submit`'s clean-checkout validation regardless
    # of the hive's otel config (the worktree overlay seeds OTEL_* into os.environ otherwise).
    cmd = config.validate_cmd(cfg, entry)
    v_start = time.perf_counter()
    rc = run(
        shlex.split(cmd),
        cwd=str(target),
        check=False,
        env=otel.telemetry_neutral_env(),
    ).returncode
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"bh.work.phase": "check", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "check"})
    _record_check_verdict(entry, target, cmd, rc)
    if rc != 0:
        raise typer.Exit(rc)


def _record_check_verdict(entry, target, cmd, rc) -> None:
    """Feed a green `check` into the same verdict ledger `submit` reuses from (bh-i0p1.4): a
    clean-checkout validation and a `check` against a CLEAN worktree prove the exact same thing
    for the exact same sha, so there is no reason the second (submit's) has to re-pay the ~6
    minute run the first (check's, run moments earlier in the ordinary check-then-submit flow —
    see the `work` skill) already proved green. Recording is keyed on `target`'s own HEAD, so it
    is only trustworthy — and only attempted — when the tree is clean (no uncommitted delta):
    a dirty tree's HEAD would misrepresent what `cmd` actually ran against. Best-effort, silent,
    and skipped outright on a red run — `validation_ledger.record` never reuses a non-green
    verdict anyway, so there's nothing to gain recording one from here."""
    if rc != 0 or not worktree.is_clean(target):
        return
    sha = worktree.head_full_sha(target)
    if sha:
        validation_ledger.record(entry, sha, cmd, rc)


def _merged_batch_groups(cfg, entry, main, beads) -> set[str]:
    """The `batch:<group>` names among `beads` whose group branch `wt/batch/<group>` already merged
    into integration — dead labels a re-parent/split can leave behind (bh-bfoy). Scheduling must not
    resurrect these as a batch. A group with no branch yet (never claimed) is live, not merged."""
    integration = config.integration_branch(cfg, entry)
    groups = {schedule_mod.batch_group(b) for b in beads}
    groups.discard("")
    merged: set[str] = set()
    for g in groups:
        branch = f"{worktree.WT_PREFIX}{worktree.BATCH_BRANCH_PREFIX}{g}"
        if worktree._branch_exists(main, branch) and worktree.is_merged(entry, branch, integration):
            merged.add(g)
    return merged


def schedule_payload(epic: str, cfg, entry, main) -> dict:
    """Core payload for ``ws work schedule --json`` and ``beadhive://work/schedule/{epic}``.

    Returns ``{groups, singletons, coordinators, max_depth}`` — the cost-model dispatch
    plan enriched with per-group tier labels and coordinator model/dispatch strings.
    Wraps ``schedule_mod.plan_schedule`` + the ``_tier`` / ``_coord_model`` enrichment;
    raises ``ValueError`` when ``epic`` is not found in this hive so callers can map the
    error to the appropriate surface (``typer.Exit`` or MCP ``ResourceError``).
    """
    children = bd.json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        raise ValueError(f"cannot list children of {epic} — is it an epic in this hive?")
    beads = [c for c in children if str(c.get("status", "")) != "closed"]
    by_id = {str(b.get("id")): b for b in beads if b.get("id")}
    # Honor work.dispatch.mode: fanout (default, one-per-worktree) stays the plain plan; collapsed
    # forces a single group past the guards; auto asks the cost model whether to collapse.
    mode = config.dispatch_mode(cfg, entry)
    max_size = config.batch_max_size(cfg, entry)
    collapse = mode == "collapsed" or (
        mode == "auto"
        and schedule_mod.auto_should_collapse(beads, budget=config.dispatch_auto_budget(cfg, entry))
    )
    if collapse:
        sched = schedule_mod.plan_schedule(
            beads,
            max_size=max_size,
            force_single_group=True,
            max_beads_per_session=config.dispatch_max_beads_per_session(cfg, entry),
        )
    else:
        merged_groups = _merged_batch_groups(cfg, entry, main, beads)
        sched = schedule_mod.plan_schedule(beads, max_size=max_size, merged_groups=merged_groups)

    def _tier(g):
        # The tier a grouped session must run at to cover its hardest member (haiku<sonnet<opus).
        return schedule_mod.max_model_tier([by_id[i] for i in g.ids if i in by_id])

    # Dispatch-by-type (xn3o.8): child epics dispatch to nested COORDINATORS, one seat each, at
    # their own model tier. Live Task nesting is bounded by work.dispatch.max_depth — at depth 0 a
    # nested coordinator can't be a Task, so a child epic runs as a SEPARATE supervised session.
    max_depth = config.dispatch_max_depth(cfg, entry)
    coord_dispatch = "nested-coordinator Task" if max_depth >= 1 else "separate supervised session"

    def _coord_model(cid):
        return schedule_mod.max_model_tier([by_id[cid]] if cid in by_id else [])

    groups = [
        {"kind": g.kind, "ids": list(g.ids), "reason": g.reason, "model": _tier(g)}
        for g in sched.groups
    ]
    coordinators = [
        {"id": c, "dispatch": coord_dispatch, "model": _coord_model(c)} for c in sched.coordinators
    ]
    payload = {
        "groups": groups,
        "singletons": list(sched.singletons),
        "coordinators": coordinators,
        "max_depth": max_depth,
    }
    _apply_start_gating(payload, beads, cfg, entry)
    return payload


def _apply_start_gating(payload: dict, beads: list, cfg, entry) -> None:
    """Opt-in release-strategy start-gating (bh-k2j8.6). When the hive has set `release.strategy`,
    surface the scorer's merge order and mark any bead the start-gate would DEFER — one that would
    finish only to wait behind higher-priority work it's likely to conflict with — under a `release`
    key. When `release.strategy` is UNSET (the default / every hive today) this is a no-op, so the
    payload stays byte-identical to the pre-release FCFS/dep-order plan. Mutates `payload` in place.
    """
    strategy = str(config.release_value(cfg, entry, "strategy", "") or "")
    if not strategy:
        return  # not opted in — today's FCFS behavior, output unchanged
    ordering = release_order.order_beads(
        beads,
        strategy=strategy,
        fix_churn_budget=config.release_fix_churn_budget(cfg, entry),
    )
    deferrals = schedule_mod.start_gate(
        beads, ordering.order, estimator=config.release_conflict_estimator(cfg, entry)
    )
    for _d in deferrals:
        otel.record_deferred_start({"bh.release.strategy": strategy})
    payload["release"] = {
        "strategy": strategy,
        "order": list(ordering.order),
        "deferred": [
            {"id": d.id, "likelihood": d.likelihood, "reason": d.reason} for d in deferrals
        ],
    }


@app.command("schedule")
@otel.trace_verb("work.schedule")
def schedule(
    epic: str = typer.Argument(..., metavar="<epic>", help="molecule epic id"),
    hive: str = _HIVE,
    as_json: bool = typer.Option(False, "--json", help="emit the plan as JSON"),
):
    """Cost-model dispatch plan for a molecule: which open children to run as ONE grouped agent
    (a planner `batch:<group>` or an auto-detected linear chain) vs as singletons (parallel
    wall-time, the default one-per-worktree). Read-only — surfaces the decision; you still
    `bh work claim --group` / `assign` to act on it. See the coordinator skill for the model."""
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, epic)
    try:
        payload = schedule_payload(epic, cfg, entry, main)
    except ValueError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not payload["groups"] and not payload["singletons"] and not payload["coordinators"]:
        typer.echo("(no open children to schedule)")
        return
    for c in payload["coordinators"]:
        typer.echo(f"◆ coordinator {c['id']}  — child epic → {c['dispatch']} (model: {c['model']})")
    for g in payload["groups"]:
        typer.echo(
            f"▸ group [{g['kind']}] {', '.join(g['ids'])}  — {g['reason']} (model: {g['model']})"
        )
        # A scheduler-forced collapsed group carries no batch:<group> label yet — print the exact
        # claim it implies so the operator doesn't have to self-label first (bh-n5z3.5); claim
        # self-heals the label from the shared parent epic.
        if g["kind"] == "collapsed":
            typer.echo(f"    → {config.BINARY_ALIAS} work claim --group {','.join(g['ids'])}")
    deferred = {d["id"]: d for d in payload.get("release", {}).get("deferred", [])}
    for s in payload["singletons"]:
        if s in deferred:
            typer.echo(f"⏸ deferred {s}  — {deferred[s]['reason']} (start-gate: hold behind queue)")
        else:
            typer.echo(f"· single {s}")


def _guard_fork_remote(entry, remote) -> None:
    """Defense in depth alongside `worktree.push_branch`'s own pull-only rail (bh-uxam.1): an
    external hive's push target must never resolve to `upstream`, whatever produced `remote` —
    catch a misconfiguration here, at the caller, before ever reaching the git-shelling seam."""
    if str((entry or {}).get("kind", "")) == "external" and remote == worktree.UPSTREAM_REMOTE:
        typer.echo(
            "✗ refusing to push an external hive's branch to 'upstream' — it's the fork "
            "(origin) or nothing; check work.push_remote",
            err=True,
        )
        raise typer.Exit(1)


@app.command("submit")
@otel.trace_verb("work.submit")
def submit(bead: str = _BEAD_OPT, as_: str = _AS, hive: str = _HIVE, group: str = _GROUP):
    """Hand off to async review: verify the branch is clean conventional digests, validate the
    proposed hash from a clean checkout, (publish for out-of-process review,) then open a gate.
    Not 'done' — leaves the worktree intact and returns immediately.

    With `--group <ids>`, submits a whole work-group from the shared `wt/batch/<group>` worktree:
    validate it once and open exactly ONE review gate whose reason names every member, so a single
    `approve` on any member clears it before `merge --group`."""
    cfg = config.load()
    guard.guard_primary(hive, cfg=cfg, verb="work submit")
    group = work_logic.opt_str(group)
    if group:
        if bead:
            typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise typer.Exit(1)
        work_group.submit_group(cfg, hive, group, as_)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise typer.Exit(1)
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    entry, main, target, branch = worktree.locate(cfg, hive, bead)
    _guard_submit_worktree(bead, main, target)
    actor = _resolve_submit_actor(cfg, entry, target, bead, main, as_)
    _guard_claim_fence(cfg, entry, target, hive)
    base = _guard_submit_ready(entry, target, branch, bead, cfg)
    _warn_submit_release_hint(bead, main, entry, branch, base)
    _validate_submit_checkout(entry, branch, cfg)

    sha = worktree.head_sha(target)
    _record_submit_commits(bead, main, entry, branch, base)
    gate, reuse = _open_submit_gate(cfg, entry, bead, branch, main, sha)
    _push_state(cfg, main, actor, f"submit {bead} @ {sha}")
    otel.count_bead_transition("review_pending", {"bh.review.gate": gate})
    verb = "reused open" if reuse else "opened"
    typer.echo(f"✓ submitted {bead} @ {sha} — {verb} {gate} review gate (worktree left intact)")


def _record_submit_commits(bead, main, entry, branch, base) -> None:
    """Append this submit's own branch commits (`base..branch`, oldest-first) onto the bead's
    `git.commits` linkage (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md) — every
    commit on the branch, not just the tip, because the eventual `--no-ff` merge preserves each
    one verbatim into history. Non-fatal by construction: a metadata write must never fail (or
    strand) a submit whose code already landed on the branch — a failure is surfaced as a
    warning, never swallowed silently and never raised."""
    try:
        shas = worktree.commit_shas(entry, branch, base)
        if shas:
            git_linkage.record_commits(bead, main, shas)
    except Exception as exc:  # best-effort: linkage must never fail a submit
        typer.echo(f"⚠ failed to record commit linkage for {bead}: {exc}", err=True)


def _guard_submit_worktree(bead, main, target) -> None:
    """Refuse when there's no worktree for `bead` — routes a batch member to `submit --group`
    (bh-n5z3.7) instead of a bare 'claim it first'."""
    if target.exists():
        return
    grp = work_group.batch_label(bd.show(bead, main))
    if grp:  # a batch member submits as a UNIT via submit --group, not per-bead (bh-n5z3.7)
        typer.echo(_batch_member_procedure_msg(bead, grp), err=True)
    else:
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
    raise typer.Exit(1)


def _resolve_submit_actor(cfg, entry, target, bead, main, as_) -> str:
    """Resolve the submitting actor and guard the claim: no explicit `--as` defaults to the seat
    `claim`/`resume` actually recorded (bh-ejlq) — NOT a fresh env/git re-derivation, which is
    exactly what used to diverge from the held claim across separate shells/tool-calls. An
    explicit `--as` still wins outright; `_guard_holds_claim` refuses a mismatch or an unclaimed
    bead either way. Also warns (non-fatal) when cwd isn't the bead worktree."""
    authority = claim_authority.get_authority(config.claim_authority(cfg, entry))
    record = authority.read(target)
    claim_holder = record.seat if authority.verify(record, "submit", "") else ""
    actor = identity.resolve_actor(
        work_logic.opt_str(as_), claim_holder or config.work_identity(cfg, entry)["name"] or ""
    )
    _guard_holds_claim(bd.show(bead, main), actor, bead)
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"WARNING: cwd is not the bead worktree — ensure all changes are committed.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )
    return actor


def _guard_claim_fence(cfg, entry, target, hive) -> None:
    """Verify the claim's FENCING TOKEN at the write boundary (bh-ytbb.10): refuse the submit
    when the host lease was lost and re-adopted while this work was in flight, so the recorded
    epoch is behind the generation now in force.

    Deliberately separate from `_resolve_submit_actor` above, and run AFTER it: that function
    owns seat verification and is unchanged by this bead, so an unclaimed bead or a seat
    mismatch still produces exactly the error it always did. This adds a second, orthogonal
    check on a different axis (generation, not identity) — see `guard.guard_claim_epoch`."""
    authority = claim_authority.get_authority(config.claim_authority(cfg, entry))
    guard.guard_claim_epoch(authority.read(target), hive, cfg=cfg, verb="work submit")


def _guard_submit_ready(entry, target, branch, bead, cfg) -> str:
    """Guard the worktree is clean, on the expected branch, and a small clean conventional
    history — returns the resolved integration base."""
    if not worktree.is_clean(target):
        typer.echo("✗ working tree not clean — commit or discard changes first", err=True)
        raise typer.Exit(1)
    cur = worktree.current_branch(target)
    if cur != branch:
        typer.echo(f"✗ on branch {cur or '(detached)'}, expected {branch}", err=True)
        raise typer.Exit(1)
    base = worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg}", err=True)
        raise typer.Exit(1)
    return base


def _warn_submit_release_hint(bead, main, entry, branch, base) -> None:
    """Release-hint reconcile (bh-k2j8.5): a NON-BLOCKING cross-check of the planner's `release:`
    hint against what the branch actually landed — a `release:feature`/`fix` bead that ships a
    breaking commit gets a warning so the label (or the commit) is fixed before release-order
    scoring reads a stale hint. Advisory only; never aborts the submit."""
    warn = work_logic.reconcile_release_hint(
        work_logic.release_hint(bd.show(bead, main)),
        worktree.commit_messages(entry, branch, base),
    )
    if warn:
        typer.echo(f"⚠ {warn}", err=True)


def _validate_submit_checkout(entry, branch, cfg) -> None:
    """Clean-checkout validation — the result must not depend on dirty local state. Submit is
    the trusted-local opt-in to the verdict ledger (bh-dfx0): a fresh green verdict for this
    exact (sha, cmd) skips the redundant checkout, so a re-submit of an unchanged sha is a
    true end-to-end no-op. Landing-boundary validations (merge/postland/finish) never reuse."""
    v_start = time.perf_counter()
    rc = worktree.clean_checkout(
        entry, branch, config.validate_cmd(cfg, entry, "submit"), reuse=True
    )
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"bh.work.phase": "submit", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "submit"})
    if rc != 0:
        typer.echo(f"✗ clean-checkout validation failed (exit {rc}) — nothing submitted", err=True)
        raise typer.Exit(1)


def _open_submit_gate(cfg, entry, bead, branch, main, sha) -> tuple[str, bool]:
    """Publish + open (or reuse) the review gate: push BEFORE set-state so a failed push blocks
    the gate too (no half-submitted bead) — out-of-process reviewers (GitHub CI) can't see a
    branch we don't push, and a `kind=external` (contribution) hive always pushes to its fork
    whatever the gate (bh-uxam.6). Opens the gate FIRST, then flips state, so we never leave a
    bead review=pending with nothing blocking it. Returns (gate type, reused an open gate)."""
    gate = config.review_gate(cfg, entry)
    if gate.startswith("gh:") or str(entry.get("kind", "")) == "external":
        remote = config.push_remote(cfg, entry)
        _guard_fork_remote(entry, remote)
        if worktree.push_branch(entry, branch, remote) != 0:
            typer.echo("✗ failed to push branch for review — nothing submitted", err=True)
            raise typer.Exit(1)
    # The reuse/supersede/create logic lives in the shared `ensure_review_gate` seam (bh-c3il),
    # so single-bead and batch submit open the gate identically.
    reuse = work_logic.ensure_review_gate(main, bead, sha, gate)
    sres = bd.run(["set-state", bead, "review=pending", "--reason", f"submitted {sha}"], main)
    if sres.returncode != 0:
        typer.echo("✗ failed to set review state — nothing submitted", err=True)
        raise typer.Exit(1)
    return gate, reuse


def _person_of(name: str) -> str:
    """The person part of a seat identity ('dev/alice' -> 'alice'); a bare name maps to itself. Used
    to spot a cross-seat self-review — the SAME person wearing both an author and a reviewer hat."""
    return name.split("/", 1)[1] if "/" in name else name


def _guard_self_review(cfg, entry, data, actor, bead) -> None:
    """Reviewer cross-seat policy (roles/RBAC matrix §3, bead .39; default flipped by bh-e5kv):
    approving a `type:human` review gate on a bead you authored is a rubber-stamp risk — the same
    leak whether the approver is a human wearing two hats or an agent self-approving its own
    dispatched work. Under `hard` (the default) this BLOCKS deterministically, so the human
    sign-off a `type:human` gate exists for can't be skipped by self-approval; under `advise`
    (explicit opt-out) it only WARNS and lets the approval through. Self-review is judged by
    PERSON, not seat — dev/alice authoring and rev/alice (or dev/alice) approving both count.
    No-op when the approver differs from the author, or either is unknown."""
    author = str((data or {}).get("assignee") or "").strip()
    if not author or not actor or _person_of(actor) != _person_of(author):
        return
    mode = config.dispatch_reviewer_cross_seat(cfg, entry)
    if mode != "advise":
        typer.echo(
            f"✗ {bead}: self-review blocked — {actor!r} authored this bead (as {author!r}); the "
            "reviewer cross-seat policy is `hard` (default). A different seat/person must "
            "approve; set `work.dispatch.reviewer_cross_seat: advise` to opt back into a warning.",
            err=True,
        )
        raise typer.Exit(1)
    from . import log  # lazy: keep work free of a load-time log import

    log.get_logger(__name__).warning(
        "reviewer_cross_seat_self_review",
        bead=bead,
        actor=actor,
        author=author,
        policy=mode,
        reason="approver authored the bead (rubber-stamp risk); advise warns, hard (default) "
        "blocks",
    )
    typer.echo(
        f"⚠ {bead}: self-review — {actor!r} authored this bead (as {author!r}). Advisory only "
        "(reviewer cross-seat policy explicitly set to `advise`); the default `hard` policy would "
        "block this.",
        err=True,
    )


@app.command("approve")
@otel.trace_verb("work.approve")
def approve(bead: str = _BEAD, as_: str = _AS, hive: str = _HIVE):
    """Reviewer/coordinator: resolve a submitted bead's HUMAN review gate through the bh
    convention layer — the first-class approve step that replaces the gated
    `bh bd gate resolve <id>` (which needs BH_BD_PASS_ENABLED=1). It attributes the actor
    (`--as` > config > $BH_DEV > git) on the audit trail and wraps `bd gate resolve` internally,
    so no `bh bd` passthrough override is needed on the normal drive path.

    Guards: refuses when there's no open *review* gate for the bead (a non-review gate such as a
    kickoff gate is ignored, so it can't be cleared here), and refuses an anonymous / out-of-process
    gate (`gh:*` / `timer`) that isn't a human's to approve — resolve those through their own
    channel (CI / PR merge). On success the gate closes and the bead is unblocked for the Merger.

    Assurance (bead .33): an open `security:*` gate is the warden's to clear — this same verb
    resolves it when run as a warden (`--as warden/<name>`), and refuses a non-warden that targets
    it. The security gate runs in PARALLEL with review: both block the merge until they clear.

    Release (bh-k2j8): an open `release-hold:` gate is the releaser's to clear — resolved here when
    run as a releaser (`--as releaser/<name>`) and refused for any other seat, so a release:breaking
    change can't be self-released into the wrong version window."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = bd.show(bead, main)
    _guard_open(data, bead)

    # One shared `bd gate list --all` fetch for both the security and release-hold lookups below
    # (was two identical spawns) — read-only, so reordering ahead of `review_gates`'s own fetch
    # (a different query) changes nothing.
    gates = _open_gates(main)
    open_review, _resolved = work_logic.review_gates(bead, main)
    if _approve_security_gate(gates, bead, main, actor, open_review):
        return
    if _approve_release_hold_gate(gates, bead, main, actor, open_review):
        return

    if not open_review:
        typer.echo(f"✗ no open review gate for {bead} — nothing to approve", err=True)
        raise typer.Exit(1)
    _guard_human_review_gate(open_review, bead)
    _guard_self_review(cfg, entry, data, actor, bead)  # cross-seat policy: advise (warn) | hard
    resolved_ids = _resolve_review_gates(open_review, bead, main, actor)
    _clear_stale_review_state(bead, data, main, actor)
    otel.count_bead_transition("approved", {"bh.review.gate": "human"})
    typer.echo(f"✓ approved {bead}: resolved review gate(s) {', '.join(resolved_ids)} as {actor}")


def _approve_security_gate(gates, bead, main, actor, open_review) -> bool:
    """Assurance (bead .33): a security:* gate is warden-only to resolve and runs in PARALLEL with
    review. Resolved here when a warden is clearing it, or when it's the only open gate (so a
    non-warden targeting it hits the warden-only refusal, not a misleading "no review gate").
    Returns True iff it handled (and reported) the approve — the caller returns immediately."""
    security = _security_gate(gates, bead)
    if (
        security is None
        or str(security.get("status")) != "open"
        or not (guard.is_warden(actor) or not open_review)
    ):
        return False
    guard.guard_security_gate_resolution(security, actor)  # raises for a non-warden
    sec_id = str(security.get("id") or "")
    sres = bd.run(
        ["gate", "resolve", sec_id, "--reason", f"security cleared by {actor}"], main, actor=actor
    )
    if sres.returncode != 0:
        typer.echo(f"✗ failed to resolve security gate {sec_id} for {bead}", err=True)
        raise typer.Exit(sres.returncode or 1)
    otel.count_bead_transition("security_cleared", {"bh.assurance.gate": "security"})
    typer.echo(f"✓ cleared {bead}: resolved security gate {sec_id} as {actor}")
    return True


def _approve_release_hold_gate(gates, bead, main, actor, open_review) -> bool:
    """Release (bh-k2j8): a release-hold: gate is releaser-only to resolve and blocks the merge
    like any open gate. Resolved here when a releaser is clearing it, or when it's the only open
    gate (so a non-releaser targeting it hits the releaser-only refusal, not a misleading "no
    review gate"). Mirrors `_approve_security_gate`. Returns True iff it handled the approve."""
    hold = _release_hold_gate(gates, bead)
    if (
        hold is None
        or str(hold.get("status")) != "open"
        or not (guard.is_releaser(actor) or not open_review)
    ):
        return False
    guard.guard_release_hold_gate_resolution(hold, actor)  # raises for a non-releaser
    hold_id = str(hold.get("id") or "")
    hres = bd.run(
        ["gate", "resolve", hold_id, "--reason", f"release-hold cleared by {actor}"],
        main,
        actor=actor,
    )
    if hres.returncode != 0:
        typer.echo(f"✗ failed to resolve release-hold gate {hold_id} for {bead}", err=True)
        raise typer.Exit(hres.returncode or 1)
    typer.echo(f"✓ cleared {bead}: resolved release-hold gate {hold_id} as {actor}")
    return True


def _guard_human_review_gate(open_review, bead) -> None:
    """Refuse when `bead`'s open review gate is out-of-process (`gh:*`/`timer`) — resolve those
    through their own channel (CI / PR merge), not `bh work approve`."""
    non_human = next(
        (g for g in open_review if str(g.get("await_type") or "human") != "human"), None
    )
    if non_human is not None:
        await_type = str(non_human.get("await_type"))
        typer.echo(
            f"✗ {bead}'s review gate is a {await_type} gate — resolve it through its own channel "
            f"(CI / PR merge), not `{config.BINARY_ALIAS} work approve`",
            err=True,
        )
        raise typer.Exit(1)


def _resolve_review_gates(open_review, bead, main, actor) -> list[str]:
    """Resolve EVERY open review gate — never first-match a possibly-stale one (bh-c3il): a
    duplicate left by an older submit would otherwise deadlock approve against merge. `bd gate
    resolve` only ever takes ONE gate id, so this stays a per-gate spawn (not batchable). Returns
    the resolved gate ids."""
    resolved_ids = []
    for gate in open_review:
        gate_id = str(gate.get("id") or "")
        res = bd.run(
            ["gate", "resolve", gate_id, "--reason", f"approved by {actor}"], main, actor=actor
        )
        if res.returncode != 0:
            typer.echo(f"✗ failed to resolve review gate {gate_id} for {bead}", err=True)
            raise typer.Exit(res.returncode or 1)
        resolved_ids.append(gate_id)
    return resolved_ids


def _clear_stale_review_state(bead, data, main, actor) -> None:
    """Clear a stale review=changes-requested left by a raw `bd set-state` bounce (bh-n5z3.6): once
    the gate is resolved, an approval must also flip the review state out of changes-requested,
    else `_merge_bead` refuses forever. review=approved is a new value nothing reads (merge only
    refuses changes-requested), so this is a pure unblock. Otherwise drop the stale
    review:pending label — review passed."""
    if bd.state(bead, "review", main) == "changes-requested":
        bd.run(
            [
                "set-state",
                bead,
                "review=approved",
                "--reason",
                f"approved by {actor} (clears stale changes-requested)",
            ],
            main,
            actor=actor,
        )
    else:
        _clear_review_label(bead, data, main, actor)


@app.command("bounce")
@otel.trace_verb("work.bounce")
def bounce(bead: str = _BEAD, message: str = _BOUNCE_MSG, as_: str = _AS, hive: str = _HIVE):
    """Reviewer: bounce a submitted bead back for changes. Resolves every OPEN review gate (so no
    orphan is left blocking a later merge while `approve` says "no open review gate"), then sets
    review=changes-requested. With no open gate it warns and still records the bounce. Points the
    developer at `bh work resume`. Batch behavior falls out free — the one batch gate names every
    member, so bouncing any member resolves it and blocks `merge --group` (bh-n5z3.6)."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = bd.show(bead, main)
    _guard_open(data, bead)
    reason = work_logic.opt_str(message).strip()
    open_review, _resolved = work_logic.review_gates(bead, main)
    if not open_review:
        typer.echo(
            f"⚠ {bead}: no open review gate to resolve — recording the bounce anyway", err=True
        )
    gate_reason = f"changes requested by {actor}" + (f": {reason}" if reason else "")
    for gate in open_review:
        gate_id = str(gate.get("id") or "")
        res = bd.run(["gate", "resolve", gate_id, "--reason", gate_reason], main, actor=actor)
        if res.returncode != 0:
            typer.echo(f"✗ failed to resolve review gate {gate_id} for {bead}", err=True)
            raise typer.Exit(res.returncode or 1)
    sres = bd.run(
        ["set-state", bead, "review=changes-requested", "--reason", gate_reason], main, actor=actor
    )
    if sres.returncode != 0:
        typer.echo(f"✗ failed to set review state on {bead}", err=True)
        raise typer.Exit(sres.returncode or 1)
    otel.count_bead_transition("changes_requested", {"bh.review.gate": "human"})
    typer.echo(
        f"✓ bounced {bead} (review=changes-requested) as {actor} — "
        f"developer picks it up with `{config.BINARY_ALIAS} work resume {bead}`"
    )


def _delete_branch(main, branch) -> None:
    """Best-effort delete of a landed molecule branch. The molecule already landed, so a failure
    here only warns (leaving a stale ref the coordinator can drop). GIT_* dir-pointing env is
    scrubbed so our explicit `-C <main>` always wins."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    res = run(["git", "-C", str(main), "branch", "-d", branch], check=False, capture=True, env=env)
    if res.returncode != 0:
        typer.echo(f"⚠ landed but failed to delete {branch} — delete it manually", err=True)


def _teardown_coordinator_seat(cfg, hive, epic) -> None:
    """Best-effort removal of a coordinator seat worktree after its molecule lands (mirrors
    `merge --rm`). Runs BEFORE `_delete_branch` so the container branch isn't checked out (a
    `git branch -d` on a still-attached branch fails). No-op when the seat was never provisioned
    (a Phase-A / separate-merger land drove from the main clone) — a removal failure only warns,
    never blocks the completed land."""
    _entry, _main, target, _branch = worktree.locate(cfg, hive, epic, kind="epic")
    if not target.exists():
        return
    try:
        worktree.remove(hive, epic, force=True)
    except typer.Exit:
        typer.echo(
            f"⚠ landed but failed to remove coordinator seat {target} — remove it manually",
            err=True,
        )


def _rollback_or_keep(entry, main, base, pre, slot_attrs) -> bool:
    """Handle a RED post-merge re-validation while still holding the slot: roll `base` back to its
    pre-merge sha `pre` IFF the branch is safe to rewrite (local/unpushed), else leave the merge
    bubble standing (a shared/pushed branch is fixed FORWARD, never reset). Emits the
    rolled_back/red_kept merge-outcome metric. Returns True iff the tip was rolled back — the caller
    renders the (site-specific) message and any bead bounce."""
    base_clone = worktree.clone_for_branch(entry, base)
    rolled = worktree.safe_to_rewrite(main, base) and worktree.reset_hard(base_clone, pre) == 0
    how = "rolled_back" if rolled else "red_kept"
    otel.count_merge_outcome({**slot_attrs, "bh.merge.how": how})
    return rolled


def _pr_ref(pr) -> str:
    """The human/bd-facing 'PR #<n> <url>' handle for a gh PR row."""
    num = str((pr or {}).get("number") or "").strip()
    url = str((pr or {}).get("url") or "").strip()
    return " ".join(x for x in ((f"PR #{num}" if num else "PR"), url) if x)


def _close_swarm_bead(epic, main) -> None:
    """Close the swarm orchestration bead(s) created over `epic` at kickoff (bh-7tno): without
    this every landed molecule leaves one permanent open type:molecule bead behind, silting up
    `work list` until a manual groom sweep. Best-effort — a failure warns, never unwinds a
    completed land. Batched into ONE `bd close` for every still-open match (`bd close` accepts
    multiple ids) instead of a subprocess-per-swarm loop."""
    data = bd.json(["swarm", "list"], main)
    swarms = data.get("swarms") if isinstance(data, dict) else None
    ids = [
        str(sw.get("id") or "")
        for sw in swarms or []
        if str(sw.get("epic_id")) == epic and str(sw.get("status", "")) != "closed" and sw.get("id")
    ]
    if not ids:
        return
    if bd.run(["close", *ids, "--reason", f"molecule {epic} landed"], main).returncode != 0:
        typer.echo(
            f"⚠ landed but failed to close swarm bead(s) {', '.join(ids)} — close manually",
            err=True,
        )


def _pr_merge_gates(bead, main) -> list[dict]:
    """The OPEN `pr-merge` gates blocking `bead` — the landing-PR analog of `review_gates`
    (same description-marker selector convention, bh-c3il)."""
    return [
        g
        for g in work_logic._bead_gates(bead, main)
        if str(g.get("status")) == "open" and "pr-merge" in str(g.get("description") or "").lower()
    ]


def _ensure_pr_gate(main, bead, ref) -> None:
    """Idempotently open the bd `gh:pr` gate that blocks `bead` until its landing PR merges —
    bd's own gate check/discover watcher machinery can resolve it, and `work land` resolves any
    survivor at close. Reuses an already-open pr-merge gate on re-runs (submit's reuse rule)."""
    gates = _pr_merge_gates(bead, main)
    if gates:
        typer.echo(f"• gh:pr gate {gates[0].get('id')} already open for {bead} — reusing it")
        return
    g = bd.run(
        ["gate", "create", "--blocks", bead, "--type", "gh:pr", "--reason", f"pr-merge {ref}"],
        main,
    )
    if g.returncode != 0:
        # Same create-then-refuse shape as submit's review gate: bd opens the gate bead, then
        # refuses the blocking dep onto an EPIC — accept the dep-less gate it left behind.
        opened = [
            gg
            for gg in _pr_merge_gates(bead, main)
            if f"pr-merge {ref}" in str(gg.get("description") or "")
        ]
        if not opened:
            typer.echo(
                "✗ PR opened but failed to open the gh:pr gate — re-run the merge to retry",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(
            "· gh:pr gate opened without a blocking dep (bd refuses blocks edges onto epics)"
        )


def _open_landing_pr(cfg, entry, main, bead, data, branch, base):
    """The `work.landing: pr` boundary — landing onto the SHARED integration branch of a
    PR-only-main repo. Instead of a local --no-ff merge: push the branch (work.push_remote) and
    open a GitHub PR against `base` (title from the bead digest, body carries id + acceptance),
    record the PR on the bead, and leave the bead/epic OPEN in a `landing=pr-pending` condition
    behind a `gh:pr` gate. CI on the PR takes over the postland-validation role; the close (with
    the squash-proof close_reason) fires from `work land` once GitHub reports the PR merged.
    Idempotent: a re-run reuses the open PR and its gate."""
    if not ghpr.available():
        typer.echo(
            "✗ work.landing is 'pr' but `gh` is not on PATH — install gh or set landing: local",
            err=True,
        )
        raise typer.Exit(1)
    remote = config.push_remote(cfg, entry)
    _guard_fork_remote(entry, remote)
    if worktree.push_branch(entry, branch, remote) != 0:
        typer.echo(f"✗ failed to push {branch} to {remote} — nothing landed", err=True)
        raise typer.Exit(1)
    pr = ghpr.open_pr_for(entry, branch)
    if pr:
        typer.echo(f"• {_pr_ref(pr)} already open for {branch} — reusing it")
    else:
        title = str(_first(data, "title") or bead)
        acceptance = _first(data, "acceptance_criteria", "acceptance") or ""
        body = f"Lands {bead} ({branch} → {base}) via `work.landing: pr`."
        if acceptance:
            body += f"\n\n## Acceptance\n{acceptance}"
        rc, out = ghpr.create_pr(entry, base, branch, title, body)
        if rc != 0:
            typer.echo(f"✗ `gh pr create` failed — nothing landed:\n{out}", err=True)
            raise typer.Exit(1)
        pr = ghpr.pr_from_url(out)
    ref = _pr_ref(pr)
    _ensure_pr_gate(main, bead, ref)
    if bd.run(["set-state", bead, "landing=pr-pending", "--reason", ref], main).returncode != 0:
        typer.echo("⚠ PR opened but failed to record landing=pr-pending — set it by hand", err=True)
    otel.count_bead_transition("pr_pending")
    typer.echo(
        f"✓ opened {ref} for {bead} ({branch} → {base}); bead stays OPEN (pr-pending) — "
        f"`{config.BINARY_ALIAS} work land {bead}` once the PR merges"
    )


def _guard_molecule_children(epic, main) -> list[dict]:
    """Guard the molecule is complete — every child closed, except an adopted origin report,
    linked child-of the epic as PROVENANCE, not molecule work — it carries no acceptance and never
    gets worked/closed on its own, so it must never gate the land. Returns the origin-report
    children (the intended jf5k/jey0 behavior: the report rides the epic to completion) for the
    caller to auto-close once the epic lands."""
    children = bd.json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        typer.echo(f"✗ cannot list children of {epic} — refusing to land", err=True)
        raise typer.Exit(1)
    origin_reports = [c for c in children if adopt.is_origin_report(c.get("labels"))]
    open_kids = [
        str(c.get("id"))
        for c in children
        if str(c.get("status", "")) != "closed" and not adopt.is_origin_report(c.get("labels"))
    ]
    if open_kids:
        typer.echo(
            f"✗ molecule {epic} incomplete — open child issue(s): {', '.join(open_kids)}", err=True
        )
        raise typer.Exit(1)
    return origin_reports


def _guard_molecule_land_base(entry, epic, integration) -> str:
    """Recursive land (xn3o.7): resolve the land target one tier up via the integration_base climb,
    so `finish <container>` lands wt/bead/epic/<container> onto its nearest container ancestor —
    a top-level epic onto main (byte-identical to the old hardcoded target), a nested epic
    <ws>.<epic> onto its workstream container. Guards a container parent-link ambiguity and a
    closed-epic land target before resolving."""
    conflict = worktree.container_conflict(entry, epic, integration)
    if conflict:
        id_base, link_base = conflict
        typer.echo(
            f"✗ {epic}: container ambiguity — the dotted id resolves to {id_base} but the "
            f"parent-child link resolves to {link_base}. A re-parent/split left both containers "
            f"live; refusing to land onto a guessed container. Reconcile the parent link, retry.",
            err=True,
        )
        raise typer.Exit(1)
    base = worktree.integration_base(entry, epic, integration)
    if worktree.container_epic_closed(entry, base):
        typer.echo(
            f"✗ {epic}: land target {base} belongs to a CLOSED epic — refusing to resurrect a "
            f"landed container. Re-parent {epic} onto a live container and retry.",
            err=True,
        )
        raise typer.Exit(1)
    return base


def _open_molecule_pr(cfg, entry, main, epic, epic_data, mol_branch, base, mode) -> None:
    """PR-only-main landing (work.landing: pr): a molecule landing onto the SHARED integration
    branch publishes as a PR instead of local-merging. The assembled molecule is still validated
    from a clean checkout first (a red molecule never reaches the PR either); the
    postland/combined validation role passes to CI on the PR."""
    if mode != "loose":
        rc = worktree.clean_checkout(entry, mol_branch, config.validate_cmd(cfg, entry, "molecule"))
        otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
        if rc != 0:
            typer.echo(f"✗ molecule validation failed (exit {rc}) — no PR opened", err=True)
            raise typer.Exit(rc)
    _open_landing_pr(cfg, entry, main, epic, epic_data, mol_branch, base)


def _validate_molecule_checkout(entry, mol_branch, cfg, mode) -> None:
    """Validate the ASSEMBLED molecule from a clean checkout before landing — the land must not
    depend on dirty local state, and a red molecule never reaches the integration line. `loose`
    trusts the per-bead submits and skips even this. Raises on a red result."""
    if mode == "loose":
        return
    v_start = time.perf_counter()
    rc = worktree.clean_checkout(entry, mol_branch, config.validate_cmd(cfg, entry, "molecule"))
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"bh.work.phase": "molecule", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
    if rc != 0:
        typer.echo(f"✗ molecule validation failed (exit {rc}) — nothing landed", err=True)
        raise typer.Exit(rc)


def _postland_revalidate_molecule(
    cfg, entry, main, base, pre, mode, stale, epic, mol_branch, slot_attrs
) -> None:
    """Post-land re-validation of the integration tip. Runs under `conservative` always, and as a
    correctness backstop under `relaxed` when main moved (stale). Still holding the slot, so a red
    tip is reset to its pre-land sha before release — no one ever sees a broken main. Raises on an
    unrecoverable red result."""
    if mode == "conservative" or (mode != "loose" and stale):
        vrc = worktree.clean_checkout(entry, base, config.validate_cmd(cfg, entry, "postland"))
        otel.count_validation(vrc == 0, {"bh.work.phase": "postland"})
        if vrc != 0:
            # Only rewrite a branch that's safe to rewrite (unpushed). A shared integration
            # branch is fixed FORWARD, never reset — the land was intentional. Roll back where
            # `base` lives: the main clone for a top-level land, a seat for a nested tier.
            if _rollback_or_keep(entry, main, base, pre, slot_attrs):
                typer.echo(  # lossless: mol branch + epic preserved
                    f"✗ post-land validation failed (exit {vrc}) — the integration tip is RED "
                    f"after landing {epic} (main moved underneath it). Rolled {base} back to "
                    f"{pre[:7]}; {mol_branch} preserved, epic still open. Rebase the molecule "
                    f"on {base} and re-run the wrap-up.",
                    err=True,
                )
            else:
                typer.echo(
                    f"✗✗ post-land validation failed (exit {vrc}) — {base} is RED after "
                    f"landing {epic} (main moved underneath it), and {base} is shared "
                    f"(pushed) so it is NOT rewritten. The merge bubble stands; epic left "
                    f"open. Fix forward: revert the bubble or land a follow-up fix.",
                    err=True,
                )
            raise typer.Exit(vrc)
    elif mode == "loose" and stale:
        typer.echo(
            f"⚠ main advanced under {epic}; skipping post-land revalidation per loose mode — "
            f"{base} may be red",
            err=True,
        )


def _close_molecule_origin_reports(origin_reports, epic, main) -> None:
    """Auto-close any adopted origin report now that its epic has landed: the report is
    provenance that rides the epic to completion, so it closes WITH the molecule rather than
    lingering open forever. Best-effort — a close failure only warns, never unwinds a completed
    land. Batched into ONE `bd close` for every still-open report (`bd close` accepts multiple
    ids) instead of a subprocess-per-report loop."""
    ids = [str(r.get("id")) for r in origin_reports if str(r.get("status", "")) != "closed"]
    if not ids:
        return
    if bd.run(["close", *ids, "--reason", f"adopted epic {epic} landed"], main).returncode != 0:
        typer.echo(
            f"⚠ landed but failed to close origin report(s) {', '.join(ids)} — close manually",
            err=True,
        )


def _reconcile_landed_molecule(cfg, entry, main, epic, epic_data, mol_branch, base, hive) -> None:
    """Finish the bookkeeping half of a molecule land whose CODE already landed (bh-lvqs).

    The molecule twin of `_reconcile_landed_bead`, and the one with the least forgiving failure
    mode: `merge_no_ff` over an already-merged container succeeds with "Already up to date", so
    the old path could re-run forever, reporting nothing wrong while the epic stayed open and its
    seat worktree and container branch stayed alive. Reconcile does the tail the first run missed —
    close the epic, ride the origin reports and swarm bead down with it, tear the seat down, delete
    the container — and exits 0."""
    origin_reports = _guard_molecule_children(epic, main)
    with work_group.merge_slot(main, {"bh.merge.kind": "molecule", "bh.hive": _hive(entry)}):
        closed = work_logic.close_merged(epic, main, "molecule landed", data=epic_data)
        _close_molecule_origin_reports(origin_reports, epic, main)
        _close_swarm_bead(epic, main)
        _teardown_coordinator_seat(cfg, hive, epic)
        _delete_branch(worktree.clone_for_branch(entry, base), mol_branch)
    if not closed:
        assignee = str(epic_data.get("assignee") or "").strip()
        typer.echo(
            f"✗ molecule {epic} is ALREADY LANDED ({mol_branch} → {base}) but {epic} could not be "
            f"closed{f' (assignee {assignee!r})' if assignee else ''} — close it manually; the "
            f"molecule is on {base}, do NOT re-land it",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(
        f"✓ molecule {epic} was already landed ({mol_branch} → {base}) — reconciled bookkeeping "
        f"(closed {epic}, tore down the seat, deleted the container; no re-merge)"
    )


def _merge_molecule(cfg, epic, hive):
    """The molecule wrap-up / land: collapse a whole assembled `mol/<epic>` onto the hive
    integration branch as ONE `--no-ff` bubble (the bead merges live inside it). Guards the
    molecule is complete (every child closed) + clean, holds the hive merge slot, validates the
    assembled branch from a clean checkout, lands it, closes the epic, and deletes the branch.
    On conflict / validation failure it aborts and releases the slot — never drops work."""
    entry, main, _target, _branch = worktree.locate(cfg, hive, epic)
    epic_data = bd.show(epic, main)
    _guard_open(epic_data, epic)

    mol_branch = f"{worktree._BEAD_PREFIX}epic/{epic}"
    if not worktree._branch_exists(main, mol_branch):
        typer.echo(f"✗ no container branch {mol_branch} — was {epic} kicked off?", err=True)
        raise typer.Exit(1)

    origin_reports = _guard_molecule_children(epic, main)

    if not worktree.is_clean(main):
        typer.echo(f"✗ main clone {main} not clean — cannot land molecule", err=True)
        raise typer.Exit(1)

    integration = config.integration_branch(cfg, entry)
    base = _guard_molecule_land_base(entry, epic, integration)
    if already_landed(entry, mol_branch, base):
        # ALREADY LANDED — the container merged and the epic never closed (bh-lvqs). Without this
        # the run falls through to merge_no_ff, which reports "Already up to date" and leaves the
        # epic open with no indication anything is wrong, so `finish` can never complete.
        _reconcile_landed_molecule(cfg, entry, main, epic, epic_data, mol_branch, base, hive)
        return
    # The container carries every bead commit plus bh's own merge bubbles; the gate covers the
    # whole range, so an unsigned merge commit bh made is caught here too, not just bead work.
    _guard_signed_history(entry, mol_branch, base, cfg)
    mode = config.validation_mode(cfg, entry)
    if base == integration and config.work_landing(cfg, entry) == "pr":
        _open_molecule_pr(cfg, entry, main, epic, epic_data, mol_branch, base, mode)
        return

    slot_attrs = {"bh.merge.kind": "molecule", "bh.hive": _hive(entry)}
    started = time.perf_counter()
    with work_group.merge_slot(main, slot_attrs):
        _validate_molecule_checkout(entry, mol_branch, cfg, mode)

        # Staleness: did the integration branch advance since the molecule forked? If so the
        # --no-ff land combines validated-mol with newer-main — a clean textual merge can still be
        # a logical conflict, and that tree was never validated. `pre` is the rollback target.
        pre = worktree._ref_sha(main, base)
        stale = worktree.base_of(entry, mol_branch, base) != pre

        prof = config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        mrc, out = worktree.merge_no_ff(
            entry,
            mol_branch,
            base,
            name=(prof["name"] or "") if agent else "",
            email=(prof["email"] or "") if agent else "",
            signing_key=(prof["signing_key"] or "") if agent else "",
            sign=prof["sign"] if agent else False,
            message=f"chore(merge): molecule {epic}",
        )
        if mrc != 0:
            otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "conflict"})
            # The merger has no write authority to hand-resolve this (bh-2p6w — merger is "not
            # implement" per docs/design/roles-rbac-matrix.md), so the escalation is made
            # RECORDED + ROUTABLE state on the epic, not just this stderr transcript.
            where = work_logic.record_merge_conflict(
                entry, mol_branch, base, main, [epic], "molecule land"
            )
            typer.echo(
                f"✗ molecule merge failed — aborted, nothing landed; bounced {epic} to "
                f"review=changes-requested (conflict in: {where}) — resolve in the {mol_branch} "
                f"seat, then re-run `{config.BINARY_ALIAS} work finish {epic}`:\n{out}",
                err=True,
            )
            raise typer.Exit(mrc)

        _postland_revalidate_molecule(
            cfg, entry, main, base, pre, mode, stale, epic, mol_branch, slot_attrs
        )

        # The container land bubble's own sha, recorded onto the epic — per-bead merge commits
        # were already recorded onto their own beads as each child landed onto mol/<epic>
        # (_merge_bead's _record_merge_commit); this is the OUTER bubble the whole molecule lands
        # as (bh-1b0rc.2).
        _record_merge_commit(epic, main, base)

        otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "no_ff"})
        # Close AS THE EPIC'S ASSIGNEE, not the merging actor (bh-r8el) — see `_merge_bead`'s
        # matching fix. `closed` drives the final message + exit code below (bh-3nuo).
        closed = work_logic.close_merged(epic, main, "molecule landed", data=epic_data)
        _close_molecule_origin_reports(origin_reports, epic, main)
        _close_swarm_bead(epic, main)  # the kickoff swarm bead rides the epic down too (bh-7tno)
        _teardown_coordinator_seat(cfg, hive, epic)  # remove seat worktree BEFORE deleting branch
        # Delete the container in the clone where `base` lives — its HEAD now includes the landed
        # container, so the safe `branch -d` succeeds. For a nested land base is the workstream seat
        # (main clone's HEAD, still on `main`, does NOT include the child container merged one tier
        # up); for a top-level land it's the main clone. clone_for_branch resolves either.
        _delete_branch(worktree.clone_for_branch(entry, base), mol_branch)

    otel.record_merge_duration(time.perf_counter() - started, {"bh.merge.kind": "molecule"})
    # Molecule asymmetry: emit cycle_time (+ slot, above) ONLY — never coding/review_wait/rework,
    # which are per-bead concepts. Best-effort, never blocks the land (it already succeeded).
    try:
        _emit_cycle(epic_data, {"bh.merge.kind": "molecule", "bh.hive": _hive(entry)})
    except Exception:  # best-effort: a metric read/parse must never fail a completed land
        pass
    otel.count_bead_transition("molecule_landed")
    if not closed:
        assignee = str(epic_data.get("assignee") or "").strip()
        typer.echo(
            f"✗ landed molecule {epic} ({mol_branch} --no-ff → {base}) but FAILED to close "
            f"{epic}{f' (assignee {assignee!r})' if assignee else ''} — close it manually",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"✓ landed molecule {epic} ({mol_branch} --no-ff → {base}); closed {epic}")


@app.command("start")
@otel.trace_verb("work.start")
def start(epic: str = _BEAD, as_: str = _AS, hive: str = _HIVE):
    """Dispatcher entrypoint: take the seat on a kicked-off epic. Epic-only alias of `claim` —
    guards the bead is an epic, planning-approved (`bh plan approve`), and that you act as a
    dispatcher (`--as disp/<name>`); provisions the dispatcher seat worktree on the container
    branch `wt/bead/epic/<epic>` (forked off `integration_base` — main for a top-level epic, the
    workstream for a nested one), stamps it with your `disp/<name>` identity, and marks the epic
    in_progress. This is the same `ensure()` op as a developer seat, differing only in the `<type>`
    segment + identity — so opening the container and attaching the seat worktree are one step
    (the retired `ensure_integration_branch`). Child beads assigned afterward fork off the
    container; `finish` lands it and tears the seat down."""
    otel.set_bead(epic)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, epic, kind="epic")
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = bd.show(epic, main)
    _guard_open(data, epic)
    if not _is_epic(data):
        typer.echo(
            f"✗ {epic} is not an epic — use `{config.BINARY_ALIAS} work claim` for a leaf bead",
            err=True,
        )
        raise typer.Exit(1)
    if bd.state(epic, "kickoff", main) != "approved":
        typer.echo(
            f"✗ {epic} is not kicked off — run `{config.BINARY_ALIAS} plan approve {epic}` first",
            err=True,
        )
        raise typer.Exit(1)
    _guard_not_other(data, actor, epic)
    _guard_seat(data, actor, epic, verb="started by")
    _guard_conventions(cfg, data, epic, main, action="dispatch")
    entry, target, branch = worktree.ensure(cfg, hive, bead=epic, kind="epic")
    _stamp(cfg, entry, target, actor)
    res = bd.run(["update", epic, "--claim"], main, actor=actor)
    if res.returncode != 0:
        raise typer.Exit(res.returncode)
    otel.count_bead_transition("started")  # bead id rides the span (set_bead), not the metric
    typer.echo(
        f"✓ started {epic} as {actor}; opened container {branch}; seat worktree {target} — "
        f"assign children onto it"
    )


@app.command("finish")
@otel.trace_verb("work.finish")
def finish(epic: str = _BEAD, hive: str = _HIVE):
    """Coordinator/merger wrap-up: land a whole assembled molecule. Epic-only alias of
    `merge --molecule` — guards the bead is an epic, then validates the assembled `mol/<epic>`,
    lands it onto the integration branch as ONE `--no-ff` bubble, closes the epic, and deletes the
    branch. `merge --molecule <epic>` remains the equivalent."""
    otel.set_bead(epic)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    _entry, main, _target, _branch = worktree.locate(cfg, hive, epic)
    data = bd.show(epic, main)
    _guard_open(data, epic)
    if not _is_epic(data):
        typer.echo(f"✗ {epic} is not an epic — nothing to finish", err=True)
        raise typer.Exit(1)
    _merge_molecule(cfg, epic, hive)


@app.command("land")
@otel.trace_verb("work.land")
def land(bead: str = _BEAD, hive: str = _HIVE):
    """Complete a `work.landing: pr` landing after GitHub merges the PR: confirm a MERGED PR
    with head `wt/bead/<type>/<id>` (`gh pr list --state merged --head …`), resolve the gh:pr
    gate, and close the bead with the squash-proof close_reason (`merged`; `molecule landed`
    for an epic) that `worktree prune`'s landed detection honors. Refuses while the PR is
    unmerged — completion is driven by PR STATE, never asserted (the operator escape hatch for
    an out-of-band landing is `worktree mark-landed`). For an epic it also closes adopted
    origin reports and tears down the coordinator seat, mirroring the local land; the pushed
    branch itself is left for `worktree prune` to reap."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, branch = worktree.locate(cfg, hive, bead)
    data = bd.show(bead, main)
    _guard_open(data, bead)
    _guard_land_pr_pending(bead, main)
    pr = _resolve_merged_land_pr(entry, branch)
    ref = _pr_ref(pr)
    _resolve_land_pr_merge_gates(bead, main, ref)
    reason = "molecule landed" if _is_epic(data) else "merged"
    if bd.run(["close", bead, "--reason", reason], main).returncode != 0:
        typer.echo(f"✗ PR merged but failed to close {bead} — close it manually", err=True)
        raise typer.Exit(1)
    _clear_review_label(bead, data, main)  # landed → drop any stale review:pending label
    if _is_epic(data):
        # Epic parity with the local land: adopted origin reports ride the epic to completion,
        # and the coordinator seat comes down. Best-effort — never unwinds a completed land.
        _close_land_origin_reports(bead, main)
        _close_swarm_bead(bead, main)  # the kickoff swarm bead rides the epic down too (bh-7tno)
        _teardown_coordinator_seat(cfg, hive, bead)
    otel.count_bead_transition("pr_landed")
    typer.echo(
        f"✓ {ref} merged — closed {bead} (close_reason: {reason}); "
        f"`{config.BINARY_ALIAS} worktree prune` reaps the seat + branch"
    )


def _guard_land_pr_pending(bead, main) -> None:
    """Refuse `land` on a bead that isn't `pr-pending` — it only completes a `work.landing: pr`
    landing opened by `merge`/`finish`."""
    if bd.state(bead, "landing", main) != "pr-pending":
        typer.echo(
            f"✗ {bead} is not pr-pending — `land` completes a `work.landing: pr` landing "
            f"opened by merge/finish",
            err=True,
        )
        raise typer.Exit(1)


def _resolve_merged_land_pr(entry, branch) -> dict:
    """The MERGED PR for `branch`, or refuse — completion is driven by PR STATE, never asserted
    (the operator escape hatch for an out-of-band landing is `worktree mark-landed`)."""
    pr = ghpr.merged_pr_for(entry, branch)
    if pr:
        return pr
    cur = ghpr.pr_for_branch(entry, branch)
    state = str((cur or {}).get("state") or "not found")
    typer.echo(f"✗ PR for {branch} is {state}, not MERGED — nothing landed", err=True)
    raise typer.Exit(1)


def _resolve_land_pr_merge_gates(bead, main, ref) -> None:
    """Resolve any still-open pr-merge gate — bd's own gh:pr gate watcher may already have (both
    orders are fine); a resolve failure only warns, the merge already happened on GitHub. `bd
    gate resolve` only ever takes ONE gate id, so this stays a per-gate spawn (not batchable)."""
    for g in _pr_merge_gates(bead, main):
        gid = str(g.get("id") or "")
        if bd.run(["gate", "resolve", gid, "--reason", f"{ref} merged"], main).returncode != 0:
            typer.echo(f"⚠ failed to resolve gh:pr gate {gid} — resolve it manually", err=True)


def _close_land_origin_reports(bead, main) -> None:
    """Epic parity with the local land: adopted origin reports ride the epic to completion.
    Best-effort — never unwinds a completed land. Batched into ONE `bd close` for every
    still-open report (`bd close` accepts multiple ids) instead of a subprocess-per-report loop."""
    children = bd.json(["list", "--parent", bead], main)
    ids = [
        str(r.get("id"))
        for r in (children if isinstance(children, list) else [])
        if adopt.is_origin_report(r.get("labels")) and str(r.get("status", "")) != "closed"
    ]
    if not ids:
        return
    if bd.run(["close", *ids, "--reason", f"adopted epic {bead} landed"], main).returncode != 0:
        typer.echo(f"⚠ landed but failed to close origin report(s) {', '.join(ids)}", err=True)


@app.command("merge")
@otel.trace_verb("work.merge")
def merge(
    bead: str = _BEAD_OPT,
    hive: str = _HIVE,
    rm: bool = typer.Option(False, "--rm", help="remove the worktree after a clean merge"),
    molecule: bool = typer.Option(
        False, "--molecule", help="land the whole molecule mol/<epic> (arg is the epic id)"
    ),
    group: str = _GROUP,
):
    """Merger-only: serialize integration of an *approved* bead onto the integration branch.
    Holds the hive merge slot, re-verifies a small clean conventional history, merges `--no-ff`
    (history preserved, never squashed at the boundary), closes the bead, releases the slot.
    Refuses unless the review gate is resolved; on conflict it aborts and releases — never drops
    work. (No worker-side ack: this is the merge owner, not the developer.)

    With `--molecule`, the positional arg is an *epic* and this lands the assembled `mol/<epic>`
    onto the integration branch as ONE `--no-ff` bubble (the wrap-up verb): guard the molecule is
    complete + clean, validate it, land it, close the epic, delete the branch.

    With `--group <ids>`, lands a whole work-group: validate the shared `wt/batch/<group>` branch
    once, merge it `--no-ff` into the members' molecule as ONE bubble (per-bead commits preserved
    inside, so it stays bisectable), then close every member — release the slot either way."""
    cfg = config.load()
    # ONE gate, up front, before the slot is held or anything is merged. Deliberately NOT
    # wrapped around the post-merge `bd close` below: that close is bookkeeping on a merge that
    # already succeeded, and re-gating it (or re-gating the operator's `bd close --force`
    # retry — bh-r8el) would block cleanup for a merge nobody can undo. See guard_primary.
    guard.guard_primary(hive, cfg=cfg, verb="work merge")
    group = work_logic.opt_str(group)
    if group:
        # Refuse `<id>` alongside `--group` — `submit` already does, and `merge` silently ignoring
        # it is what UNDERCOUNTED bh-hsus (bh-c3nf). `--group` is ONE value: written space- rather
        # than comma-separated, the shell binds only the first id to it and the rest land in this
        # positional, so the batch BRANCH merged whole (every member's commits landed) while the
        # member LIST held one id — and only that one was closed. Silence made a partial close
        # look like a complete one, which is the failure worth refusing over.
        if bead:
            typer.echo(
                f"✗ pass either <id> or --group, not both (got <id>={bead}, --group={group}).\n"
                f"  --group takes ONE comma-separated value — ids must not be space-separated:\n"
                f"      {config.BINARY_ALIAS} work merge --group {group},{bead}   # correct\n"
                f"      {config.BINARY_ALIAS} work merge --group {group} {bead}   # drops {bead}",
                err=True,
            )
            raise typer.Exit(1)
        work_group.merge_group(cfg, group, hive, rm)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> / --molecule <epic>)", err=True)
        raise typer.Exit(1)
    otel.set_bead(bead)  # ws.bead/ws.epic on this verb span (bead is the epic when --molecule)
    if molecule:
        _merge_molecule(cfg, bead, hive)
        return
    _merge_bead(cfg, bead, hive, rm)


def _guard_bead_merge_gates(bead, main, landing_pr) -> None:
    """Guard `bead` is mergeable: not changes-requested, and no open gate blocks it (broad on
    purpose — the warden's security:* gate blocks in parallel with review); the refusal
    enumerates each open gate by kind so the merger knows who clears what (bh-c3il). Under
    `landing: pr` the ONE exception is the landing path's own `pr-merge` gate — it must not block
    an idempotent re-run of that same path (which reuses the open PR + gate rather than opening
    duplicates)."""
    if bd.state(bead, "review", main) == "changes-requested":
        typer.echo(f"✗ {bead} has changes-requested — resume & resubmit, don't merge", err=True)
        raise typer.Exit(1)
    gate_lines = work_logic.open_gate_lines(
        bead, main, skip_marker="pr-merge" if landing_pr else ""
    )
    if gate_lines:
        typer.echo(f"✗ {bead}: open gate(s) block the merge:\n" + "\n".join(gate_lines), err=True)
        raise typer.Exit(1)


def _guard_bead_land_base(entry, bead, integration) -> str:
    """Recursive land (xn3o.7): guard a container parent-link ambiguity and a closed-epic land
    target, then resolve `bead`'s land base one tier up via the integration_base climb."""
    conflict = worktree.container_conflict(entry, bead, integration)
    if conflict:
        id_base, link_base = conflict
        typer.echo(
            f"✗ {bead}: container ambiguity — the dotted id resolves to {id_base} but the "
            f"parent-child link resolves to {link_base}. A re-parent/split left both containers "
            f"live; refusing to guess. Reconcile the parent link (or retire the stale container) "
            f"and retry.",
            err=True,
        )
        raise typer.Exit(1)
    base = worktree.integration_base(entry, bead, integration)
    if worktree.container_epic_closed(entry, base):
        typer.echo(
            f"✗ {bead}: {base} belongs to a CLOSED epic — refusing to land on (or resurrect) a "
            f"landed container. Re-parent {bead} onto a live epic and retry.",
            err=True,
        )
        raise typer.Exit(1)
    return base


def already_landed(entry, branch: str, base: str) -> bool:
    """Merge-verb alias for :func:`worktree.landed_via_merge` — the branch's commits are on `base`
    because they were merged there, not because the branch was never implemented (bh-lvqs)."""
    return worktree.landed_via_merge(entry, branch, base)


def _guard_bead_clean_history(entry, branch, base, cfg) -> bool:
    """Guard the branch is a small clean conventional history before it's allowed to merge —
    reuses submit's `_history_ok` check as a merge-time backstop.

    Returns True when the branch is ALREADY LANDED, so the caller reconciles bookkeeping instead
    of merging (bh-lvqs); False on the ordinary path. A genuinely empty branch — no commits over
    base and NOT an ancestor of it — still takes the self-refine bounce unchanged."""
    count, subjects = worktree.history(entry, branch, base)
    if count == 0 and already_landed(entry, branch, base):
        return True
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)
    return False


def _reconcile_landed_bead(cfg, entry, main, bead, bead_data, branch, base, hive, rm) -> None:
    """Finish the bookkeeping half of a merge whose CODE already landed (bh-lvqs).

    Reached when the branch is an ancestor of the base — the merge happened, but the run that did
    it died before closing the bead, so the tracker still says in-progress while main carries the
    work. Re-running merge used to report that as "nothing to submit". This makes the verb
    IDEMPOTENT instead: do exactly the steps the first run missed and exit 0.

    Deliberately does NOT re-merge, re-validate, or re-emit merge-outcome metrics — the merge is
    not happening now, it happened then, and counting it twice would corrupt the very telemetry an
    operator would use to spot this failure. The merge slot is still held around the close so a
    concurrent merger cannot interleave with the reconcile."""
    slot_attrs = {"bh.merge.kind": "bead", "bh.hive": _hive(entry)}
    with work_group.merge_slot(main, slot_attrs):
        closed = work_logic.close_merged(bead, main, "merged", data=bead_data)
        _clear_review_label(bead, bead_data, main)
    if rm:
        # Tolerant: see `work_group._reconcile_landed_group` — the tree may already be gone,
        # and reconcile must stay idempotent.
        try:
            worktree.remove(hive, bead, force=True)
        except Exception:
            pass
    if not closed:
        assignee = str(bead_data.get("assignee") or "").strip()
        typer.echo(
            f"✗ {bead} is ALREADY MERGED ({branch} → {base}) but could not be closed"
            f"{f' (assignee {assignee!r})' if assignee else ''} — close it manually; "
            "the code is on the integration branch, do NOT re-implement it",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(
        f"✓ {bead} was already merged ({branch} → {base}) — reconciled bookkeeping "
        "(closed the bead; no re-merge)"
    )


def _guard_signed_history(entry, branch, base, cfg) -> None:
    """The enforce-signing gate (bh-ijd4), beside the clean-history check because that is
    already the pre-merge guard: with `work.enforce_signing` on, EVERY commit in the merge
    range must verify as trusted, not just the tip. A no-op when the flag is off, so default
    behaviour is byte-identical to before."""
    if not config.enforce_signing(cfg, entry):
        return
    ok, msg = work_logic._signing_ok(worktree.signature_status(entry, branch, base), branch, base)
    if not ok:
        typer.echo(f"✗ {msg}", err=True)
        raise typer.Exit(1)


def _merge_bead_no_ff(entry, branch, base, target, cfg, bead, main, slot_attrs) -> str:
    """rebase-then-retry the merge: a replay-resolvable conflict (a coupled sibling's change
    already landed on the base — e.g. both beads added the same boilerplate line) is recovered by
    rebasing this bead onto the newer base; a genuinely divergent conflict still fails cleanly
    with the bead branch restored, so the merger bounces it for rework. Returns `how`
    ('merged'/'rebased'/'union') on success; raises Exit on a real conflict.

    On a real conflict the merger has no write authority to hand-resolve it (bh-2p6w — the
    merger seat is 'not implement' per `docs/design/roles-rbac-matrix.md`), so the bounce is
    made RECORDED + ROUTABLE state (`work_logic.record_merge_conflict`: a note + bounce to
    `review=changes-requested` naming the conflicted paths), not just this stderr transcript."""
    prof = config.work_identity(cfg, entry)
    agent = prof["mode"] == "agent"
    rc, out, how = worktree.try_merge_rebase(
        entry,
        branch,
        base,
        target,
        name=(prof["name"] or "") if agent else "",
        email=(prof["email"] or "") if agent else "",
        signing_key=(prof["signing_key"] or "") if agent else "",
        sign=prof["sign"] if agent else False,
        message=f"chore(merge): bead {bead}",
        union_globs=tuple(config.union_globs(cfg, entry)),
        validate_cmd=config.validate_cmd(cfg, entry, "union"),
    )
    if rc != 0:
        otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "conflict"})
        where = work_logic.record_merge_conflict(entry, branch, base, main, [bead], "merge")
        typer.echo(
            f"✗ real conflict merging {bead} — rebase retry failed, bead branch restored; "
            f"bounced {bead} to review=changes-requested (conflict in: {where}) — "
            f"`{config.BINARY_ALIAS} work resume {bead}`, rebase onto {base}, resolve, "
            f"resubmit:\n{out}",
            err=True,
        )
        raise typer.Exit(rc)
    return how


def _postland_revalidate_bead(cfg, entry, main, base, pre, bead, slot_attrs, on_main) -> None:
    """Re-test the integration tip after a clean bead merge — green in isolation at submit, but
    the COMBINATION with what's already on the tip may be red. Still holding the slot, so on red
    we reset a safe-to-rewrite tip (the private mol/<epic>, or an unpushed main) to its pre-merge
    sha and bounce the bead to changes-requested; a shared (pushed) tip is left standing and
    fixed forward. Raises on an unrecoverable red result."""
    vrc = worktree.clean_checkout(
        entry, base, config.validate_cmd(cfg, entry, "merge", main_gate=on_main)
    )
    otel.count_validation(vrc == 0, {"bh.work.phase": "merge"})
    if vrc == 0:
        return
    # Roll back `base` where it lives — the coordinator seat for a container base, else the main
    # clone (a top-level land onto main).
    rolled = _rollback_or_keep(entry, main, base, pre, slot_attrs)
    bd.run(
        [
            "set-state",
            bead,
            "review=changes-requested",
            "--reason",
            "combined-state red after merge — may be an interaction with "
            "already-merged siblings; rebase on the current tip and fix",
        ],
        main,
    )
    if rolled:
        typer.echo(
            f"✗ {bead} merged clean but the {base} tip is RED in combination (exit "
            f"{vrc}) — rolled {base} back to {pre[:7]} and bounced the bead to "
            f"changes-requested.",
            err=True,
        )
    else:
        typer.echo(
            f"✗✗ {bead} merged clean but {base} is RED in combination (exit {vrc}) and "
            f"{base} is shared (pushed) so it is NOT rewritten — the merge stands. "
            f"Bounced the bead; fix forward.",
            err=True,
        )
    raise typer.Exit(vrc)


def _record_merge_commit(bead, main, base) -> None:
    """Append the just-landed merge commit's own sha onto `bead`'s `git.commits` linkage
    (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md). Read `base`'s tip AFTER the merge
    lands and (when this run re-validates) AFTER `_postland_revalidate_bead` has returned —
    that call either returns clean or raises `typer.Exit` on a red re-validation that ROLLS THE
    MERGE BACK, so calling this only once control reaches here means a rolled-back sha is never
    recorded. Non-fatal by construction: a metadata write must never fail a merge that already
    landed — a failure is surfaced as a warning, never swallowed silently and never raised."""
    try:
        merge_sha = worktree._ref_sha(main, base)
        if merge_sha:
            git_linkage.record_commits(bead, main, [merge_sha])
    except Exception as exc:  # best-effort: linkage must never fail a completed merge
        typer.echo(f"⚠ failed to record commit linkage for {bead}: {exc}", err=True)


def _merge_bead(cfg, bead, hive, rm):
    """Serialize the land of a single approved bead onto its integration base: guard open + review
    resolved + a small clean conventional history, hold the merge slot, rebase-retry merge
    `--no-ff`, re-validate the combined tip on a main-gate, close the bead. The single-bead
    sibling of `_merge_molecule` / `merge_group`; `merge` is the thin 3-way dispatch over them."""
    started = time.perf_counter()
    entry, main, target, branch = worktree.locate(cfg, hive, bead)
    bead_data = bd.show(bead, main)  # reused for the at-merge cycle/stage flow metrics below
    _guard_open(bead_data, bead)

    landing_pr = config.work_landing(cfg, entry) == "pr"
    _guard_bead_merge_gates(bead, main, landing_pr)

    integration = config.integration_branch(cfg, entry)
    base = _guard_bead_land_base(entry, bead, integration)
    if _guard_bead_clean_history(entry, branch, base, cfg):
        # Already on the base: finish the bookkeeping the first run missed, don't re-merge and
        # don't bounce it for rework (bh-lvqs).
        _reconcile_landed_bead(cfg, entry, main, bead, bead_data, branch, base, hive, rm)
        return
    _guard_signed_history(entry, branch, base, cfg)

    # PR-only-main landing (work.landing: pr): the SHARED-branch boundary is PR-governed — push
    # + open a PR instead of local-merging, and leave the bead open (pr-pending) until the PR
    # merges. A bead landing into its molecule container stays a local merge in any mode.
    if base == integration and landing_pr:
        _open_landing_pr(cfg, entry, main, bead, bead_data, branch, base)
        return

    slot_attrs = {"bh.merge.kind": "bead", "bh.hive": _hive(entry)}
    mode = config.validation_mode(cfg, entry)
    # An ad-hoc bead (no molecule) merges straight into the shared integration branch — that land is
    # a main-merge gate just like the molecule pre-land, so it gets a final re-validation in every
    # mode except `loose` (which trusts submits and skips main-gate checks, as it does for a
    # molecule). A bead → mol/<epic> merge stays fast (the mol→main land is its backstop).
    on_main = base == integration
    revalidate = mode == "conservative" or (on_main and mode != "loose")
    pre = worktree._ref_sha(main, base) if revalidate else ""
    with work_group.merge_slot(main, slot_attrs):
        how = _merge_bead_no_ff(entry, branch, base, target, cfg, bead, main, slot_attrs)

        if revalidate:
            _postland_revalidate_bead(cfg, entry, main, base, pre, bead, slot_attrs, on_main)

        _record_merge_commit(bead, main, base)

        otel.count_merge_outcome({**slot_attrs, "bh.merge.how": how})
        # Close AS THE BEAD'S ASSIGNEE, not the merging actor (bh-r8el) — the seat that did the
        # work is never the merger's own identity in the normal dispatcher flow, so `bd close`'s
        # actor guard refused every time until now. `closed` is the TRUE outcome and drives the
        # final message + exit code below, never assumed (bh-3nuo).
        #
        # PAST THIS POINT THE CODE IS ON THE BASE (bh-lvqs). Anything that raises from here is a
        # bookkeeping failure over a COMPLETED merge, and the operator's only dangerous mistake is
        # to read the traceback as "the merge failed" and re-do the work. The original incident
        # died exactly here and said nothing, leaving main carrying the change while the tracker
        # said in-progress. So: name what is unreconciled, say plainly that the code landed, and
        # re-raise rather than swallowing — this reports, it does not recover.
        try:
            closed = work_logic.close_merged(bead, main, "merged", data=bead_data)
            _clear_review_label(bead, bead_data, main)  # merged → drop stale review:pending
        except Exception:
            assignee = str(bead_data.get("assignee") or "").strip()
            typer.echo(
                f"✗ {bead} MERGED SUCCESSFULLY ({branch} --no-ff → {base}) but its bookkeeping "
                f"did not complete.\n"
                f"  THE CODE IS ON {base} — do not re-implement or re-submit it.\n"
                f"  Unreconciled: bead {bead}"
                f"{f', assignee {assignee!r}' if assignee else ''}, branch {branch}.\n"
                f"  Re-run `{config.BINARY_ALIAS} work merge {bead}` — it is idempotent over an "
                f"already-landed branch and will finish the reconcile.",
                err=True,
            )
            raise

    otel.record_merge_duration(
        time.perf_counter() - started, {"bh.merge.kind": "bead", "bh.merge.how": how}
    )
    # At-merge cycle/stage/rework from bd — best-effort + skew-guarded; the bead already merged, so
    # a slow/failing read or a negative delta must never turn a successful land into a failure.
    try:
        _emit_bead_flow(bead, bead_data, main, {"bh.merge.kind": "bead", "bh.hive": _hive(entry)})
    except Exception:  # best-effort: a metric read/parse must never fail a completed merge
        pass
    otel.count_bead_transition("merged")
    note = ""
    if how == "rebased":
        note = " (rebased onto a newer base first)"
    elif how == "union":
        note = " (landed via union conflict resolution)"
    if rm:
        worktree.remove(hive, bead, force=True)
    if not closed:
        assignee = str(bead_data.get("assignee") or "").strip()
        typer.echo(
            f"✗ merged {bead} ({branch} --no-ff → {base}){note} but FAILED to close it"
            f"{f' (assignee {assignee!r})' if assignee else ''} — close it manually",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"✓ merged {bead} ({branch} --no-ff → {base}){note} and closed it")


@app.command("resume")
@otel.trace_verb("work.resume")
def resume(
    bead: str = _BEAD,
    as_: str = _AS,
    hive: str = _HIVE,
):
    """After review returns changes-requested: re-attach a fresh worktree on the bead branch,
    print the feedback, and re-assert the claim. Address the feedback and `submit` again."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    _pull_state(cfg, main)  # see current state first — bounce feedback may have landed elsewhere
    state = bd.state(bead, "review", main)
    if state != "changes-requested":
        typer.echo(f"✗ {bead} not in review:changes-requested (now: {state or 'none'})", err=True)
        raise typer.Exit(1)
    # GC any review gate a RAW `bd set-state` bounce left open (bh-n5z3.6): resolve it here so a
    # same-sha resubmit can't resurrect a stale gate that would deadlock merge against approve.
    open_review, _resolved = work_logic.review_gates(bead, main)
    for gate in open_review:
        bd.run(
            [
                "gate",
                "resolve",
                str(gate.get("id") or ""),
                "--reason",
                "orphaned by bounce — cleared on resume",
            ],
            main,
        )
    # A BATCH member re-attaches to the shared `wt/batch/<grp>` worktree and NEVER provisions its
    # own (bh-c3nf). `worktree.ensure(bead)` would create `wt/bead/<type>/<id>` forked off the
    # container tip — a tree holding none of the group's work — which then shadowed `check`'s
    # batch redirect and poisoned the verdict ledger. No `_issue_claim` here, matching
    # `work_group.claim_group`: a batch is claimed as a unit, and the group's own claim stands.
    grp, batch_target = _batch_worktree(cfg, hive, bead, main)
    if grp:
        if batch_target is None:
            typer.echo(_batch_member_procedure_msg(bead, grp), err=True)
            raise typer.Exit(1)
        target = batch_target
    else:
        entry, target, _branch = worktree.ensure(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _stamp(cfg, entry, target, actor)
    if not grp:
        _issue_claim(cfg, entry, bead, actor, target, hive)
    typer.echo("── review feedback ──")
    bd.run(["comments", bead], main)
    bd.run(["update", bead, "--claim"], main, actor=actor)
    typer.echo(f"✓ resumed {bead} as {actor}; worktree {target}")


@app.command("abandon")
@otel.trace_verb("work.abandon")
def abandon(
    bead: str = _BEAD,
    hive: str = _HIVE,
    rm: bool = typer.Option(False, "--rm", help="also remove the worktree (default: keep it)"),
):
    """Release the claim and record the abandon. Recovery path for stalls."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor("", config.work_identity(cfg, entry)["name"] or "")
    # Recovery path: deliberately no refuse-if-other guard (the point is to release a bead a
    # stalled/dead agent left claimed). Surface bd failures instead of always reporting success.
    r1 = bd.run(["set-state", bead, "review=abandoned", "--reason", "abandoned"], main, actor=actor)
    r2 = bd.run(["update", bead, "--status", "open", "--assignee", ""], main, actor=actor)
    if rm and target.exists():
        worktree.remove(hive, bead, force=True)
    if r1.returncode or r2.returncode:
        typer.echo(f"⚠ abandoned {bead} with bd errors (see above)", err=True)
        raise typer.Exit(1)
    otel.count_bead_transition("abandoned")  # bead id rides the span (set_bead), not the metric
    typer.echo(f"✓ abandoned {bead}" + ("; worktree removed" if rm else "; worktree kept"))


# ---- show / review (read-only render verbs; bodies live in work_show) -------
# Registered onto this app from work_show so the rendering surface sits in one file while the
# command names stay `ws work show` / `ws work review`. Re-bound here (show = …) so existing
# callers/tests that invoke `work.show(...)` / `work.review(...)` keep working.

show = app.command("show")(otel.trace_verb("work.show")(work_show.show))
review = app.command("review")(otel.trace_verb("work.review")(work_show.review))


# ---- refine (squash local checkpoint noise) ---------------------------------


def _load_plan(plan_arg: str) -> dict:
    """Read a squash-plan from a file path or '-' (stdin). Raises on read/JSON errors."""
    text = sys.stdin.read() if plan_arg == "-" else Path(plan_arg).read_text()
    return json.loads(text)


def _restore(target, backup) -> None:
    """Abort any in-progress rebase and hard-reset the branch back to its pre-refine tip."""
    worktree.rebase_abort(target)
    worktree.reset_hard(target, backup)


def refine_branch(
    cfg,
    *,
    hive: str,
    bead: str,
    plan: str = "",
    autosquash: bool = False,
    since: str = "",
    dry_run: bool = False,
) -> RefineResult:
    """Squash local checkpoint noise into conventional digests, behind a backup branch and a
    byte-identical gate (the net tree never changes). Typer-free core shared by the CLI and the
    future MCP entrypoint; returns a RefineResult and raises WorkError on any failure.

    Exactly one input mode (--plan | --autosquash | --since). On a real refine the backup
    branch is created before the rebase and surfaced via RefineResult.backup (success) or
    WorkError.backup (restore paths) so callers can report it identically."""
    entry, _main, target, branch = worktree.locate(cfg, hive, bead)
    _guard_refine_mode(target, bead, plan, autosquash, since)
    base = _resolve_refine_base(cfg, entry, bead, branch)
    base, rows, groups = _build_refine_plan(entry, base, branch, plan, autosquash, since)

    # --dry-run: simulate; make NO changes (no clean-tree requirement — read-only).
    if dry_run:
        subjects = (
            [r["subject"] for r in rows if not _MARKER.match(r["subject"])]
            if autosquash
            else _simulate(rows, groups)
        )
        return RefineResult(base=base, dry_run=True, subjects=subjects)

    backup = _apply_refine_rebase(entry, target, branch, base, autosquash, rows, groups)
    return RefineResult(
        base=base,
        backup=backup,
        branch=branch,
        log=worktree.log_range(entry, base, branch),
        target=target,
    )


def _guard_refine_mode(target, bead, plan, autosquash, since) -> None:
    """Guard exactly one input mode (--plan | --autosquash | --since) is given and the worktree
    exists."""
    if sum([bool(plan), autosquash, bool(since)]) != 1:
        raise WorkError(["✗ pass exactly one of --plan / --autosquash / --since"])
    if not target.exists():
        raise WorkError([f"✗ no worktree for {bead} — claim it first"])


def _resolve_refine_base(cfg, entry, bead, branch) -> str:
    """Resolve the refine base (the integration base climbed onto the branch's actual fork
    point), or raise when it can't be computed."""
    base = worktree.base_of(
        entry, branch, worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    )
    if not base:
        raise WorkError(["✗ cannot compute base (is the integration branch present locally?)"])
    return base


def _build_refine_plan(entry, base, branch, plan, autosquash, since) -> tuple[str, list, list]:
    """Build the squash plan + resolve commit rows/groups (autosquash lets git build its own
    todo, so no plan). Returns (base — possibly overridden by an explicit plan `base`, commit
    rows, groups)."""
    if autosquash:
        return base, worktree.commit_rows(entry, base, branch), []
    if since:
        plan_dict = plan_from_since(worktree.commit_rows(entry, since, branch))
    else:
        try:
            plan_dict = _load_plan(plan)
        except (OSError, json.JSONDecodeError) as e:
            raise WorkError([f"✗ cannot read plan: {e}"]) from None
    if isinstance(plan_dict, dict) and plan_dict.get("base"):
        base = plan_dict["base"]  # explicit base override
    rows = worktree.commit_rows(entry, base, branch)
    ok, errors, groups = validate_plan(plan_dict, rows)
    if not ok:
        raise WorkError([f"✗ {e}" for e in errors])
    return base, rows, groups


def _apply_refine_rebase(entry, target, branch, base, autosquash, rows, groups) -> str:
    """Real refine: require a clean tree on the expected branch, snapshot a backup branch,
    rebase (autosquash or an explicit squash-plan todo), and gate on a byte-identical net tree —
    restoring from the backup on any rebase failure or tree drift. Returns the backup branch."""
    if not worktree.is_clean(target):
        raise WorkError(["✗ working tree not clean — commit or discard changes first"])
    cur = worktree.current_branch(target)
    if cur != branch:
        raise WorkError([f"✗ on branch {cur or '(detached)'}, expected {branch}"])

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = worktree.backup_branch(entry, branch, ts)

    if autosquash:
        rc, out = worktree.rebase_autosquash(target, base)
    else:
        rc, out = worktree.rebase_squash(target, base, build_todo(rows, groups))

    if rc != 0:
        _restore(target, backup)
        messages = [f"✗ refine rebase failed (exit {rc}) — restored from {backup}"]
        if out.strip():
            messages.append(out.strip())
        messages.append(
            "  keep a keep's folds contiguous, or refine-as-you-go with `git commit --fixup`"
        )
        raise WorkError(messages, backup=backup)

    # Byte-identical gate — the net change must be untouched (guarantees a pure rewrite).
    if not worktree.same_tree(entry, backup, branch):
        worktree.reset_hard(target, backup)
        raise WorkError([f"✗ refine changed the tree — restored from {backup}"], backup=backup)

    return backup


@app.command("refine")
@otel.trace_verb("work.refine")
def refine(
    bead: str = _BEAD,
    plan: str = typer.Option("", "--plan", help="squash-plan JSON file or '-' for stdin"),
    autosquash: bool = typer.Option(False, "--autosquash", help="fold fixup!/squash! into targets"),
    since: str = typer.Option("", "--since", help="fold <ref>..tip into a single digest"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the would-be log; change nothing"),
    hive: str = _HIVE,
):
    """Squash local checkpoint noise into conventional digests behind a backup branch and a
    byte-identical gate (the net tree never changes). Retains per-digest author dates. Exactly
    one input mode: --plan | --autosquash | --since."""
    cfg = config.load()
    try:
        result = refine_branch(
            cfg,
            hive=hive,
            bead=bead,
            plan=plan,
            autosquash=autosquash,
            since=since,
            dry_run=dry_run,
        )
    except WorkError as e:
        if e.backup:
            typer.echo(f"backup branch: {e.backup}")
        for line in e.messages:
            typer.echo(line, err=True)
        raise typer.Exit(1) from None

    if result.dry_run:
        typer.echo(f"would produce {len(result.subjects)} commit(s) over {result.base[:7]}:")
        for s in result.subjects:
            typer.echo(f"  {s}")
        return

    typer.echo(f"backup branch: {result.backup}")
    typer.echo(f"✓ refined {bead} ({result.branch}) — backup left at {result.backup}:")
    typer.echo(result.log)
    typer.echo(f"restore with: git -C {result.target} reset --hard {result.backup}")
