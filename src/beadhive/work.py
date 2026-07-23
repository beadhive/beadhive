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
    config,
    ghpr,
    guard,
    identity,
    otel,
    registry,
    work_group,
    work_logic,
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


def backfill_stale_review_labels(main, actor="") -> int:
    """One-time cleanup: strip ``review:pending`` from every already-closed bead — the label was
    never cleared on close/merge before this fix, so it lingers on historical work and pollutes
    review queries. Returns the count cleaned; idempotent (safe to re-run). A data migration tool,
    not a lifecycle verb — invoke once per hive (`from beadhive.work import
    backfill_stale_review_labels`)."""
    rows = bd.json(["list", "--status", "closed", "--label", "review:pending"], main)
    if not isinstance(rows, list):
        return 0
    cleaned = 0
    for r in rows:
        bid = str(r.get("id") or "") if isinstance(r, dict) else ""
        if not bid:
            continue
        if bd.run(["label", "remove", bid, "review:pending"], main, actor=actor).returncode == 0:
            cleaned += 1
    return cleaned


def _security_gate(bead, cwd):
    """The Assurance `security:*` gate for `bead` (a `security:` marker in its description), or
    None — the warden-owned gate that blocks the merge in parallel with review (bead .33). Matched
    like `work_logic.review_gates` (description-based) but on `guard.is_security_gate`, so
    kickoff/review gates don't match."""
    gates = bd.json(["gate", "list", "--all", "--limit", "0"], cwd)
    if not isinstance(gates, list):
        return None
    for g in gates:
        desc = str(g.get("description") or "").lower()
        if bead.lower() in desc and guard.is_security_gate(g):
            return g
    return None


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


@app.command("ready", context_settings=_READ_CTX)
@otel.trace_verb("work.ready")
def ready(ctx: typer.Context, hive: str = _HIVE):
    """List ready (unblocked, dependency-ordered) work — first-class `bd ready`. Read-only.

    Pass `--json` for the coordinator loop's machine shape, `--gated` for beads whose review gate
    just closed. Extra flags forward to `bd ready` unchanged."""
    cfg = config.load()
    _forward_read(["ready", *ctx.args], registry.hive_dir_for(cfg, hive))


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
        return
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
        return
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


@app.command("check")
@otel.trace_verb("work.check")
def check(bead: str = _BEAD, hive: str = _HIVE):
    """Run the hive's validation command against the worktree; propagate its exit code."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, target, _branch = worktree.locate(cfg, hive, bead)
    if not target.exists():
        grp = work_group.batch_label(bd.show(bead, main))
        if grp:
            # A batch member: check is read-only, so redirect to the shared batch worktree when it
            # exists rather than erroring; otherwise name the batch procedure (bh-n5z3.7).
            batch_target = worktree.locate(cfg, hive, branch=f"{work_group.BATCH_PREFIX}{grp}")[2]
            if batch_target.exists():
                target = batch_target
            else:
                typer.echo(_batch_member_procedure_msg(bead, grp), err=True)
                raise typer.Exit(1)
        else:
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
    v_start = time.perf_counter()
    rc = run(
        shlex.split(config.validate_cmd(cfg, entry)),
        cwd=str(target),
        check=False,
        env=otel.telemetry_neutral_env(),
    ).returncode
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"bh.work.phase": "check", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "check"})
    if rc != 0:
        raise typer.Exit(rc)


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
    return {
        "groups": groups,
        "singletons": list(sched.singletons),
        "coordinators": coordinators,
        "max_depth": max_depth,
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
    for s in payload["singletons"]:
        typer.echo(f"· single {s}")


def _guard_fork_remote(entry, remote) -> None:
    """Defense in depth alongside `worktree.push_branch`'s own pull-only rail (bh-uxam.1): an
    external hive's push target must never resolve to `upstream`, whatever produced `remote` —
    catch a misconfiguration here, at the caller, before ever reaching the git-shelling seam."""
    if str((entry or {}).get("kind", "")) == "external" and remote == worktree.UPSTREAM_REMOTE:
        typer.echo(
            "✗ refusing to push an external hive's branch to 'upstream' — it's the fork "
            "(origin) or nothing; check work.push_remote", err=True,
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
    if not target.exists():
        grp = work_group.batch_label(bd.show(bead, main))
        if grp:  # a batch member submits as a UNIT via submit --group, not per-bead (bh-n5z3.7)
            typer.echo(_batch_member_procedure_msg(bead, grp), err=True)
        else:
            typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
    # Re-check claim ownership: `abandon` can't stop an already-running agent, but submit
    # must not open a review gate on a bead the submitter no longer holds (abandoned/reassigned).
    actor = identity.resolve_actor(
        work_logic.opt_str(as_), config.work_identity(cfg, entry)["name"] or ""
    )
    _guard_holds_claim(bd.show(bead, main), actor, bead)
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"WARNING: cwd is not the bead worktree — ensure all changes are committed.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )

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

    # Clean-checkout validation — the result must not depend on dirty local state. Submit is
    # the trusted-local opt-in to the verdict ledger (bh-dfx0): a fresh green verdict for this
    # exact (sha, cmd) skips the redundant checkout, so a re-submit of an unchanged sha is a
    # true end-to-end no-op. Landing-boundary validations (merge/postland/finish) never reuse.
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

    sha = worktree.head_sha(target)
    gate = config.review_gate(cfg, entry)
    # Out-of-process reviewers (GitHub CI) can't see a branch we don't push. Push BEFORE
    # set-state so a failed push blocks the gate too (no half-submitted bead). A `kind=external`
    # (contribution) hive always pushes to its fork, whatever the gate — the branch has to exist
    # on the fork before a PR can ever target upstream (bh-uxam.6).
    if gate.startswith("gh:") or str(entry.get("kind", "")) == "external":
        remote = config.push_remote(cfg, entry)
        _guard_fork_remote(entry, remote)
        if worktree.push_branch(entry, branch, remote) != 0:
            typer.echo("✗ failed to push branch for review — nothing submitted", err=True)
            raise typer.Exit(1)

    # Open the gate FIRST, then flip state — so we never leave a bead review=pending with
    # nothing blocking it (which would let the scheduler re-pick it). The reuse/supersede/create
    # logic lives in the shared `ensure_review_gate` seam (bh-c3il), so single-bead and batch
    # submit open the gate identically.
    reuse = work_logic.ensure_review_gate(main, bead, sha, gate)
    sres = bd.run(["set-state", bead, "review=pending", "--reason", f"submitted {sha}"], main)
    if sres.returncode != 0:
        typer.echo("✗ failed to set review state — nothing submitted", err=True)
        raise typer.Exit(1)
    _push_state(cfg, main, actor, f"submit {bead} @ {sha}")
    otel.count_bead_transition("review_pending", {"bh.review.gate": gate})
    verb = "reused open" if reuse else "opened"
    typer.echo(f"✓ submitted {bead} @ {sha} — {verb} {gate} review gate (worktree left intact)")


def _person_of(name: str) -> str:
    """The person part of a seat identity ('dev/alice' -> 'alice'); a bare name maps to itself. Used
    to spot a cross-seat self-review — the SAME person wearing both an author and a reviewer hat."""
    return name.split("/", 1)[1] if "/" in name else name


def _guard_self_review(cfg, entry, data, actor, bead) -> None:
    """Reviewer cross-seat policy (roles/RBAC matrix §3, bead .39): approving a review gate on a
    bead you authored is a rubber-stamp risk. Under `advise` (the default) this WARNS but lets the
    approval through; under `hard` it BLOCKS, so a hive that wants the split-review guarantee gets
    it. Self-review is judged by PERSON, not seat — dev/alice authoring and rev/alice (or dev/alice)
    approving both count. No-op when the approver differs from the author, or either is unknown."""
    author = str((data or {}).get("assignee") or "").strip()
    if not author or not actor or _person_of(actor) != _person_of(author):
        return
    mode = config.dispatch_reviewer_cross_seat(cfg, entry)
    if mode == "hard":
        typer.echo(
            f"✗ {bead}: self-review blocked — {actor!r} authored this bead (as {author!r}); the "
            "reviewer cross-seat policy is `hard`. A different seat/person must approve.",
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
        reason="approver authored the bead (rubber-stamp risk); advise warns, hard blocks",
    )
    typer.echo(
        f"⚠ {bead}: self-review — {actor!r} authored this bead (as {author!r}). Advisory only "
        "(reviewer cross-seat policy is `advise`); set it to `hard` to block self-approval.",
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
    it. The security gate runs in PARALLEL with review: both block the merge until they clear."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = bd.show(bead, main)
    _guard_open(data, bead)

    # Assurance (bead .33): a security:* gate is warden-only to resolve and runs in PARALLEL with
    # review. Route it here when a warden is clearing it, or when it's the only open gate (so a
    # non-warden targeting it hits the warden-only refusal, not a misleading "no review gate").
    security = _security_gate(bead, main)
    open_review, _resolved = work_logic.review_gates(bead, main)
    if (
        security is not None
        and str(security.get("status")) == "open"
        and (guard.is_warden(actor) or not open_review)
    ):
        guard.guard_security_gate_resolution(security, actor)  # raises for a non-warden
        sec_id = str(security.get("id") or "")
        sres = bd.run(
            ["gate", "resolve", sec_id, "--reason", f"security cleared by {actor}"],
            main,
            actor=actor,
        )
        if sres.returncode != 0:
            typer.echo(f"✗ failed to resolve security gate {sec_id} for {bead}", err=True)
            raise typer.Exit(sres.returncode or 1)
        otel.count_bead_transition("security_cleared", {"bh.assurance.gate": "security"})
        typer.echo(f"✓ cleared {bead}: resolved security gate {sec_id} as {actor}")
        return

    if not open_review:
        typer.echo(f"✗ no open review gate for {bead} — nothing to approve", err=True)
        raise typer.Exit(1)
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
    _guard_self_review(cfg, entry, data, actor, bead)  # cross-seat policy: advise (warn) | hard
    # Resolve EVERY open review gate — never first-match a possibly-stale one (bh-c3il): a
    # duplicate left by an older submit would otherwise deadlock approve against merge.
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
    # Clear a stale review=changes-requested left by a raw `bd set-state` bounce (bh-n5z3.6): once
    # the gate is resolved, an approval must also flip the review state out of changes-requested,
    # else `_merge_bead` refuses forever. review=approved is a new value nothing reads (merge only
    # refuses changes-requested), so this is a pure unblock.
    if bd.state(bead, "review", main) == "changes-requested":
        bd.run(
            ["set-state", bead, "review=approved", "--reason",
             f"approved by {actor} (clears stale changes-requested)"],
            main,
            actor=actor,
        )
    else:
        _clear_review_label(bead, data, main, actor)  # review passed → drop stale review:pending
    otel.count_bead_transition("approved", {"bh.review.gate": "human"})
    typer.echo(f"✓ approved {bead}: resolved review gate(s) {', '.join(resolved_ids)} as {actor}")


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
    """Close the swarm orchestration bead created over `epic` at kickoff (bh-7tno): without
    this every landed molecule leaves one permanent open type:molecule bead behind, silting up
    `work list` until a manual groom sweep. Best-effort — a failure warns, never unwinds a
    completed land."""
    data = bd.json(["swarm", "list"], main)
    swarms = data.get("swarms") if isinstance(data, dict) else None
    for sw in swarms or []:
        if str(sw.get("epic_id")) != epic or str(sw.get("status", "")) == "closed":
            continue
        sid = str(sw.get("id") or "")
        if not sid:
            continue
        if bd.run(["close", sid, "--reason", f"molecule {epic} landed"], main).returncode != 0:
            typer.echo(
                f"⚠ landed but failed to close swarm bead {sid} — close it manually", err=True
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

    children = bd.json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        typer.echo(f"✗ cannot list children of {epic} — refusing to land", err=True)
        raise typer.Exit(1)
    # An adopted origin report is linked child-of the epic as PROVENANCE, not
    # molecule work — it carries no acceptance and never gets worked/closed on its own. Hold it OUT
    # of the completeness check (it must never gate the land) and auto-close it once the epic lands
    # (the intended jf5k/jey0 behavior — the report rides the epic to completion).
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

    if not worktree.is_clean(main):
        typer.echo(f"✗ main clone {main} not clean — cannot land molecule", err=True)
        raise typer.Exit(1)

    # Recursive land (xn3o.7): resolve the land target one tier up via the integration_base climb,
    # so `finish <container>` lands wt/bead/epic/<container> onto its nearest container ancestor —
    # a top-level epic onto main (byte-identical to the old hardcoded target), a nested epic
    # <ws>.<epic> onto its workstream container. A workstream itself (dotless, epic-typed, with epic
    # children) climbs to main. base feeds staleness / merge_no_ff / postland / safe_to_rewrite, so
    # the private-vs-shared rollback safety generalizes up the chain with no new safety code: a
    # nested container branch is local/unpushed → safe_to_rewrite → an intermediate red rolls back
    # losslessly; only the final workstream→main land touches the shared branch (fixed forward).
    integration = config.integration_branch(cfg, entry)
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
    mode = config.validation_mode(cfg, entry)
    # PR-only-main landing (work.landing: pr): a molecule landing onto the SHARED integration
    # branch publishes as a PR instead of local-merging. The assembled molecule is still
    # validated from a clean checkout first (a red molecule never reaches the PR either);
    # the postland/combined validation role passes to CI on the PR.
    if base == integration and config.work_landing(cfg, entry) == "pr":
        if mode != "loose":
            rc = worktree.clean_checkout(
                entry, mol_branch, config.validate_cmd(cfg, entry, "molecule")
            )
            otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
            if rc != 0:
                typer.echo(f"✗ molecule validation failed (exit {rc}) — no PR opened", err=True)
                raise typer.Exit(rc)
        _open_landing_pr(cfg, entry, main, epic, epic_data, mol_branch, base)
        return

    slot_attrs = {"bh.merge.kind": "molecule", "bh.hive": _hive(entry)}
    started = time.perf_counter()
    with work_group.merge_slot(main, slot_attrs):
        # Validate the ASSEMBLED molecule from a clean checkout — the land must not depend on
        # dirty local state, and a red molecule never reaches the integration line. `loose` trusts
        # the per-bead submits and skips even this.
        if mode != "loose":
            v_start = time.perf_counter()
            rc = worktree.clean_checkout(
                entry, mol_branch, config.validate_cmd(cfg, entry, "molecule")
            )
            otel.record_validation_duration(
                time.perf_counter() - v_start,
                {
                    "bh.work.phase": "molecule",
                    "bh.validation.result": _vres(rc),
                    "bh.hive": _hive(entry),
                },
            )
            otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
            if rc != 0:
                typer.echo(f"✗ molecule validation failed (exit {rc}) — nothing landed", err=True)
                raise typer.Exit(rc)

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
            typer.echo(f"✗ molecule merge failed — aborted, nothing landed:\n{out}", err=True)
            raise typer.Exit(mrc)

        # Post-land re-validation of the integration tip. Runs under `conservative` always, and as
        # a correctness backstop under `relaxed` when main moved (stale). Still holding the slot, so
        # a red tip is reset to its pre-land sha before release — no one ever sees a broken main.
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

        otel.count_merge_outcome({**slot_attrs, "bh.merge.how": "no_ff"})
        if bd.run(["close", epic, "--reason", "molecule landed"], main).returncode != 0:
            typer.echo("⚠ landed but failed to close the epic — close it manually", err=True)
        # Auto-close any adopted origin report now that its epic has landed: the
        # report is provenance that rides the epic to completion, so it closes WITH the molecule
        # rather than lingering open forever. Best-effort — a close failure only warns, never
        # unwinds a completed land.
        for report in origin_reports:
            rid = str(report.get("id"))
            if str(report.get("status", "")) == "closed":
                continue
            close = bd.run(["close", rid, "--reason", f"adopted epic {epic} landed"], main)
            if close.returncode != 0:
                typer.echo(
                    f"⚠ landed but failed to close origin report {rid} — close it manually",
                    err=True,
                )
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
    if bd.state(bead, "landing", main) != "pr-pending":
        typer.echo(
            f"✗ {bead} is not pr-pending — `land` completes a `work.landing: pr` landing "
            f"opened by merge/finish",
            err=True,
        )
        raise typer.Exit(1)
    pr = ghpr.merged_pr_for(entry, branch)
    if not pr:
        cur = ghpr.pr_for_branch(entry, branch)
        state = str((cur or {}).get("state") or "not found")
        typer.echo(f"✗ PR for {branch} is {state}, not MERGED — nothing landed", err=True)
        raise typer.Exit(1)
    ref = _pr_ref(pr)
    # Resolve any still-open pr-merge gate — bd's own gh:pr gate watcher may already have
    # (both orders are fine); a resolve failure only warns, the merge already happened on GitHub.
    for g in _pr_merge_gates(bead, main):
        gid = str(g.get("id") or "")
        if bd.run(["gate", "resolve", gid, "--reason", f"{ref} merged"], main).returncode != 0:
            typer.echo(f"⚠ failed to resolve gh:pr gate {gid} — resolve it manually", err=True)
    reason = "molecule landed" if _is_epic(data) else "merged"
    if bd.run(["close", bead, "--reason", reason], main).returncode != 0:
        typer.echo(f"✗ PR merged but failed to close {bead} — close it manually", err=True)
        raise typer.Exit(1)
    _clear_review_label(bead, data, main)  # landed → drop any stale review:pending label
    if _is_epic(data):
        # Epic parity with the local land: adopted origin reports ride the epic to completion,
        # and the coordinator seat comes down. Best-effort — never unwinds a completed land.
        children = bd.json(["list", "--parent", bead], main)
        for report in children if isinstance(children, list) else []:
            rid = str(report.get("id"))
            if not adopt.is_origin_report(report.get("labels")):
                continue
            if str(report.get("status", "")) == "closed":
                continue
            if bd.run(["close", rid, "--reason", f"adopted epic {bead} landed"], main).returncode:
                typer.echo(f"⚠ landed but failed to close origin report {rid}", err=True)
        _close_swarm_bead(bead, main)  # the kickoff swarm bead rides the epic down too (bh-7tno)
        _teardown_coordinator_seat(cfg, hive, bead)
    otel.count_bead_transition("pr_landed")
    typer.echo(
        f"✓ {ref} merged — closed {bead} (close_reason: {reason}); "
        f"`{config.BINARY_ALIAS} worktree prune` reaps the seat + branch"
    )


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
    group = work_logic.opt_str(group)
    if group:
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


def _merge_bead(cfg, bead, hive, rm):
    """Serialize the land of a single approved bead onto its integration base: guard open + review
    resolved + a small clean conventional history, hold the merge slot, rebase-retry merge
    `--no-ff`, re-validate the combined tip on a main-gate, close the bead. The single-bead
    sibling of `_merge_molecule` / `merge_group`; `merge` is the thin 3-way dispatch over them."""
    started = time.perf_counter()
    entry, main, target, branch = worktree.locate(cfg, hive, bead)
    bead_data = bd.show(bead, main)  # reused for the at-merge cycle/stage flow metrics below
    _guard_open(bead_data, bead)

    if bd.state(bead, "review", main) == "changes-requested":
        typer.echo(f"✗ {bead} has changes-requested — resume & resubmit, don't merge", err=True)
        raise typer.Exit(1)
    # ANY open gate blocks the merge (broad on purpose — the warden's security:* gate blocks in
    # parallel with review); the refusal enumerates each open gate by kind so the merger knows
    # who clears what (bh-c3il). Under `landing: pr` the ONE exception is the landing path's own
    # `pr-merge` gate — it must not block an idempotent re-run of that same path (which reuses
    # the open PR + gate rather than opening duplicates).
    landing_pr = config.work_landing(cfg, entry) == "pr"
    gate_lines = work_logic.open_gate_lines(
        bead, main, skip_marker="pr-merge" if landing_pr else ""
    )
    if gate_lines:
        typer.echo(f"✗ {bead}: open gate(s) block the merge:\n" + "\n".join(gate_lines), err=True)
        raise typer.Exit(1)

    integration = config.integration_branch(cfg, entry)
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
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)

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
    on_main = base == config.integration_branch(cfg, entry)
    revalidate = mode == "conservative" or (on_main and mode != "loose")
    pre = worktree._ref_sha(main, base) if revalidate else ""
    with work_group.merge_slot(main, slot_attrs):
        prof = config.work_identity(cfg, entry)
        agent = prof["mode"] == "agent"
        # rebase-then-retry: a replay-resolvable conflict (a coupled sibling's change already
        # landed on the base — e.g. both beads added the same boilerplate line) is recovered by
        # rebasing this bead onto the newer base; a genuinely divergent conflict still fails
        # cleanly with the bead branch restored, so the merger bounces it for rework.
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
            typer.echo(
                f"✗ real conflict merging {bead} — rebase retry failed, bead branch restored; "
                f"bounce it back for rework:\n{out}",
                err=True,
            )
            raise typer.Exit(rc)

        # Re-test the integration tip after this clean merge — the bead was green in isolation at
        # submit, but the COMBINATION with what's already on the tip may be red. Fires under
        # conservative (every merge) OR whenever the target is main (on_main — the ad-hoc→main gate,
        # which also covers a main that moved under the bead). Still holding the slot, so on red we
        # reset a safe-to-rewrite tip (the private mol/<epic>, or an unpushed main) to its pre-merge
        # sha and bounce the bead; a shared (pushed) main is left standing and fixed forward.
        if revalidate:
            vrc = worktree.clean_checkout(
                entry, base, config.validate_cmd(cfg, entry, "merge", main_gate=on_main)
            )
            otel.count_validation(vrc == 0, {"bh.work.phase": "merge"})
            if vrc != 0:
                # Roll back `base` where it lives — the coordinator seat for a container base,
                # else the main clone (a top-level land onto main).
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

        otel.count_merge_outcome({**slot_attrs, "bh.merge.how": how})
        if bd.run(["close", bead, "--reason", "merged"], main).returncode != 0:
            typer.echo("⚠ merged but failed to close the bead — close it manually", err=True)
        _clear_review_label(bead, bead_data, main)  # merged → drop the stale review:pending label

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
    typer.echo(f"✓ merged {bead} ({branch} --no-ff → {base}){note} and closed it")
    if rm:
        worktree.remove(hive, bead, force=True)


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
            ["gate", "resolve", str(gate.get("id") or ""), "--reason",
             "orphaned by bounce — cleared on resume"],
            main,
        )
    entry, target, _branch = worktree.ensure(cfg, hive, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _stamp(cfg, entry, target, actor)
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
    if sum([bool(plan), autosquash, bool(since)]) != 1:
        raise WorkError(["✗ pass exactly one of --plan / --autosquash / --since"])
    if not target.exists():
        raise WorkError([f"✗ no worktree for {bead} — claim it first"])
    base = worktree.base_of(
        entry, branch, worktree.integration_base(entry, bead, config.integration_branch(cfg, entry))
    )
    if not base:
        raise WorkError(["✗ cannot compute base (is the integration branch present locally?)"])

    # Build the plan + resolve groups (autosquash lets git build its own todo, so no plan).
    groups: list[dict] = []
    if not autosquash:
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
    else:
        rows = worktree.commit_rows(entry, base, branch)

    # --dry-run: simulate; make NO changes (no clean-tree requirement — read-only).
    if dry_run:
        subjects = (
            [r["subject"] for r in rows if not _MARKER.match(r["subject"])]
            if autosquash
            else _simulate(rows, groups)
        )
        return RefineResult(base=base, dry_run=True, subjects=subjects)

    # Real refine — now require a clean tree on the expected branch.
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

    return RefineResult(
        base=base,
        backup=backup,
        branch=branch,
        log=worktree.log_range(entry, base, branch),
        target=target,
    )


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
