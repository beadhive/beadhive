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

Composition map: reads/rendering live in ``work_reads`` and ``work_show``; intake in
``work_intake``; assignment and scheduling in ``work_assignment`` / ``work_dispatch``;
check/submit/review gates in ``work_submission``; molecule, bead, and PR landing in
``work_merge``; safe history rewriting in ``work_refine``. This module keeps the Typer command
registry, public types, and injected compatibility seams used by callers and tests.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - injected lifecycle collaborator on the stable facade
import datetime  # noqa: F401 - injected refine collaborator on the stable facade
import json
import os  # noqa: F401 - injected merge collaborator on the stable facade
import shlex  # noqa: F401 - injected submission collaborator on the stable facade
import sys
import time  # noqa: F401 - injected merge collaborator on the stable facade
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from . import (
    adopt,  # noqa: F401 - injected merge collaborator
    bd,
    claim_authority,  # noqa: F401 - injected submission collaborator
    config,
    converge,  # noqa: F401 - injected submission collaborator
    ghpr,  # noqa: F401 - injected merge collaborator
    git_linkage,  # noqa: F401 - injected merge collaborator
    guard,  # noqa: F401 - injected merge collaborator
    host,  # noqa: F401 - injected lifecycle collaborator
    identity,
    jsonout,  # noqa: F401 - injected lifecycle collaborator
    model_routing,  # noqa: F401 - injected lifecycle collaborator
    otel,
    registry,  # noqa: F401 - injected lifecycle collaborator
    release_order,  # noqa: F401 - injected lifecycle collaborator
    test_report,  # noqa: F401 - injected submission collaborator
    triage_store,  # noqa: F401 - injected submission collaborator
    validation_ledger,  # noqa: F401 - injected submission collaborator
    validation_records,  # noqa: F401 - injected submission collaborator
    work_assignment,
    work_dispatch,
    work_group,  # noqa: F401 - injected merge collaborator
    work_guards,
    work_intake,
    work_logic,
    work_merge,
    work_metrics,
    work_next,  # noqa: F401 - injected lifecycle collaborator
    work_reads,
    work_refine,
    work_show,
    work_submission,
    worktree,
)
from . import log as dispatch_log
from . import schedule as schedule_mod  # noqa: F401 - injected lifecycle collaborator
from .run import missing_binary, run  # noqa: F401 - injected submission collaborator
from .work_logic import (
    _MARKER,  # noqa: F401 - injected refine collaborator
    _guard_holds_claim,  # noqa: F401 - injected submission collaborator
    _guard_not_other,
    _guard_open,
    _history_ok,  # noqa: F401 - injected merge collaborator
    _simulate,  # noqa: F401 - injected refine collaborator
    _stamp,
    build_todo,  # noqa: F401 - injected refine collaborator
    plan_from_since,  # noqa: F401 - injected refine collaborator
    validate_plan,  # noqa: F401 - injected refine collaborator
)

# Preserve the historical ``work.log`` patch/import seam without a static binding that collides
# with the existing local ``log`` variables used by validation and merge helpers.
globals()["log"] = dispatch_log

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


_first = work_guards.first

# Compatibility exports: implementations live in work_metrics.
RETRYABLE_VALIDATION_EXIT = 75
_hive = work_metrics.hive
_vres = work_metrics.validation_result
_parse_ts = work_metrics.parse_ts
_emit_delta = work_metrics.emit_delta
_flow_events = work_metrics.flow_events
_event_text = work_metrics.event_text
_is_review_pending = work_metrics.is_review_pending
_is_changes_requested = work_metrics.is_changes_requested
_is_dispatch_cause = work_metrics.is_dispatch_cause
dispatch_cause_count = work_metrics.dispatch_cause_count
record_dispatch_failure = work_metrics.record_dispatch_failure
_review_pending_at = work_metrics.review_pending_at
_clear_review_label = work_metrics.clear_review_label
_strip_review_pending = work_metrics.strip_review_pending
backfill_stale_review_labels = work_metrics.backfill_stale_review_labels
_open_gates = work_metrics.open_gates
_match_gate = work_metrics.match_gate
_security_gate = work_metrics.security_gate
_release_hold_gate = work_metrics.release_hold_gate
_stage_recorder = work_metrics.stage_recorder
_emit_cycle = work_metrics.emit_cycle
_emit_bead_flow = work_metrics.emit_bead_flow

# ---- guards & shared steps ---------------------------------------------------


# Identity namespaces: dispatchers drive molecules (container beads), developers implement leaves.
# Prefixes + returned seat literals follow the roles/RBAC matrix (docs/design/roles-rbac-matrix.md):
# dispatcher (disp/) coordinates a set of beads on a long-lived branch; developer (dev/) implements
# ONE bead on an ephemeral bead branch.
_DISP_PREFIX = work_guards.DISP_PREFIX
_DEV_PREFIX = work_guards.DEV_PREFIX
_LEGACY_SEAT_PREFIXES = work_guards.LEGACY_SEAT_PREFIXES
_DIRECTOR_PREFIX = work_guards.DIRECTOR_PREFIX
_KNOWN_SEAT_PREFIXES = work_guards.KNOWN_SEAT_PREFIXES
_is_epic = work_guards.is_epic
_kind_of = work_guards.kind_of


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


_seat_of = work_guards.seat_of
_guard_seat = work_guards.guard_seat
_is_orchestrator = work_guards.is_orchestrator
_names_a_seat = work_guards.names_a_seat
_guard_orchestrator = work_guards.guard_orchestrator
_epic_of = work_guards.epic_of
_guard_conventions = work_guards.guard_conventions
_print_brief = work_guards.print_brief

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
_NextEpic = Annotated[
    str,
    typer.Option("--epic", help="restrict candidates to this molecule (the epic and its children)"),
]


_READ_CTX = work_reads.READ_CTX
_READY_LIMIT_FLAGS = work_reads.READY_LIMIT_FLAGS
_READY_NARROWING_FLAGS = work_reads.READY_NARROWING_FLAGS
_READY_SHOWING_RE = work_reads.READY_SHOWING_RE
READY_TRUNCATED_EXIT = work_reads.READY_TRUNCATED_EXIT
MoleculeReadinessError = work_reads.MoleculeReadinessError
_forward_read = work_reads.forward_read
_reorder_ready_lines = work_reads.reorder_ready_lines
_count_avoided_conflicts = work_reads.count_avoided_conflicts
_forward_ready_ordered = work_reads.forward_ready_ordered
_readiness_json = work_reads.readiness_json
molecule_readiness_payload = work_reads.molecule_readiness_payload
_render_molecule_readiness = work_reads.render_molecule_readiness
_ready_arg_name = work_reads.ready_arg_name
_ready_has_flag = work_reads.ready_has_flag
_widen_narrowed_ready_args = work_reads.widen_narrowed_ready_args
_ready_truncated_exit = work_reads.ready_truncated_exit
_forward_ready_plain = work_reads.forward_ready_plain
_emit_start_gated_ready = work_reads.emit_start_gated_ready


@app.command("brief")
@otel.trace_verb("work.brief")
def brief(bead: str = _BEAD, hive: str = _HIVE):
    """Print the bead's requirements/goals and validation command. Read-only."""
    return work_reads.brief(bead, hive)


@app.command("readiness")
@otel.trace_verb("work.readiness")
def readiness(
    molecule: str = typer.Argument(
        ..., metavar="<molecule-id>", help="persistent or wisp molecule"
    ),
    hive: str = _HIVE,
    as_json: bool = typer.Option(False, "--json", help="emit the machine-readable per-step report"),
):
    """Report blocker-correct readiness for every step in a persistent or wisp molecule."""
    return work_reads.readiness(molecule, hive, as_json)


@app.command("ready", context_settings=_READ_CTX)
@otel.trace_verb("work.ready")
def ready(ctx: typer.Context, hive: str = _HIVE):
    """List ready work, preserving bd streams, ordering, and truncation signals."""
    return work_reads.ready(ctx, hive)


@app.command("issue", context_settings=_READ_CTX)
@otel.trace_verb("work.issue")
def issue(ctx: typer.Context, bead: str = _BEAD, hive: str = _HIVE):
    """Show a single issue's fields through the stable first-class read."""
    return work_reads.issue(ctx, bead, hive)


@app.command("list", context_settings=_READ_CTX)
@otel.trace_verb("work.list")
def list_(ctx: typer.Context, hive: str = _HIVE):
    """List or filter issues through the stable first-class read."""
    return work_reads.list_(ctx, hive)


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
_render_disposition = work_intake.render_disposition


@app.command("intake")
@otel.trace_verb("work.intake")
def intake_cmd(
    hive: str = _HIVE,
    source: str = _SOURCE,
    as_json: bool = _INTAKE_JSON,
    no_dupes: bool = _NO_DUPES,
):
    """List this hive's untriaged intake queue and surface likely duplicates."""
    return work_intake.intake(hive, source, as_json, no_dupes)


@app.command("accept")
@otel.trace_verb("work.accept")
def accept_cmd(
    bead: str = _BEAD,
    issue_type: str = typer.Option("", "--type", "-t", help="set the accepted type (type-aware)"),
    priority: str = typer.Option("", "--priority", "-p", help="set priority (0-4 / P0-P4)"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Accept an intake report into backlog and clear its intake state."""
    return work_intake.accept(bead, issue_type, priority, as_, hive)


@app.command("reject")
@otel.trace_verb("work.reject")
def reject_cmd(
    bead: str = _BEAD,
    reason: str = typer.Option(..., "--reason", help="reporter-visible reason (recorded on close)"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Reject an intake report with a reporter-visible reason."""
    return work_intake.reject(bead, reason, as_, hive)


@app.command("reroute")
@otel.trace_verb("work.reroute")
def reroute_cmd(
    bead: str = _BEAD,
    to: str = typer.Option("", "--to", help="re-file the report into this hive"),
    super_: str = typer.Option("", "--super", help="bounce to this superintendent seat"),
    as_: str = _AS,
    hive: str = _HIVE,
):
    """Reroute an intake report to a hive or superintendent."""
    return work_intake.reroute(bead, to, super_, as_, hive)


@app.command("promote")
@otel.trace_verb("work.promote")
def promote_cmd(bead: str = _BEAD, as_: str = _AS, hive: str = _HIVE):
    """Promote an intake report to the planner."""
    return work_intake.promote(bead, as_, hive)


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
    return work_assignment.impl_assign(sys.modules[__name__], bead, to, as_, hive, preview, as_json)


def _claim_fence(cfg, hive) -> tuple[str, int]:
    """This host's `(host_id, epoch)` — the fencing token stamped into a fresh `ClaimRecord`
    (bh-ytbb.10). Both resolve LOCALLY: `host.host_id()` reads `~/.beadhive/host.yaml`, and
    `guard.live_epoch` reads the cached host lease (no network — see its docstring). Claiming
    must stay cheap; a worker that had to poll a remote to take a bead would be exactly the
    design this molecule rejects.

    Degrades to the unfenced `("", 0)` rather than failing the claim: a host that never ran
    `bh config init`, or a factory that never adopted anything, has no token to mint and must
    keep working exactly as before."""
    return work_assignment.impl__claim_fence(sys.modules[__name__], cfg, hive)


def _issue_claim(cfg, entry, bead, actor, target, hive="") -> None:
    """Mint + persist a `ClaimRecord` naming `actor` as this worktree's claim holder (bh-ejlq),
    through the configured `ClaimAuthority` (default Tier 0 `local`). `submit` reads this back to
    default its actor when `--as` is omitted, instead of re-deriving identity from ambient env/git
    and risking a mismatch against what `claim`/`resume` actually recorded.

    The record is also stamped with this host's fencing token (bh-ytbb.10) so `submit` can catch
    a claim that outlived the host lease it was taken under — see `_guard_claim_fence`."""
    return work_assignment.impl__issue_claim(
        sys.modules[__name__], cfg, entry, bead, actor, target, hive
    )


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
    return work_assignment.impl_claim(
        sys.modules[__name__], bead, as_, group, collapse, hive, preview, as_json
    )


def _claim_single_bead(cfg, hive, bead, as_) -> None:
    """The single-bead claim: re-attach/provision the worktree with `actor`'s identity, refuse
    if it's someone else's or the wrong seat, then `bd update --claim` (→ in_progress)."""
    return work_assignment.impl__claim_single_bead(sys.modules[__name__], cfg, hive, bead, as_)


def _batch_member_procedure_msg(bead, grp) -> str:
    """The error a per-bead `submit`/`check` on a BATCH member gets instead of the misleading
    "claim it first": a batch member has no per-bead worktree — the whole batch lives in the ONE
    shared `wt/batch/<grp>` worktree and completes as a UNIT (bh-n5z3.7)."""
    return work_assignment.impl__batch_member_procedure_msg(sys.modules[__name__], bead, grp)


def _batch_worktree(cfg, hive, bead, main):
    """`(group, shared worktree)` for `bead`'s `batch:<group>` label — the ONE seam per-bead verbs
    use to refuse to act on a batch member's own dir (bh-c3nf).

    `("", None)` when it carries no batch label. `(grp, None)` when it does but `wt/batch/<grp>`
    is absent. A batch member's artifact is ALWAYS the shared worktree: any `wt/bead/<type>/<id>`
    dir is a stray from a per-bead verb and holds none of the group's work, so callers must key on
    the returned group and never on that dir's existence."""
    return work_assignment.impl__batch_worktree(sys.modules[__name__], cfg, hive, bead, main)


# ---- next: the optimistic pick → claim → re-verify loop ----------------------
#
# `bd update --claim` is NOT a hard compare-and-swap: two drivers racing for the same bead can
# both see exit 0, and the last write wins. Every external driver that picks a bead off
# `bd ready` and then claims it therefore reimplements the same race — badly, and separately.
# This verb is the ONE safe entry point: pick optimistically, claim, then RE-READ the bead and
# verify we are the holder, moving to the next candidate when we lost. Losing a race is the
# normal case under an unattended dispatcher, not an error, so it is never surfaced as a failure.
#
# bh-qczj.2: once a claim is won, the worktree is provisioned through the SAME `worktree.ensure`
# path `claim`/`assign`/`start` already use — no second provisioning path — and the envelope
# reports the resolved path + stamped identity alongside the bead/seat/actor bh-qczj.1 established.

#: Version of the `bh work next --json` contract (`_next_payload`).
NEXT_SCHEMA = 1

#: Exit code for a clean decline — nothing eligible, or every candidate lost its race. DISTINCT
#: from 1 on purpose: "nothing to do" is a normal poll result an unattended driver must be able to
#: tell apart from "the call failed", without parsing stderr.
NEXT_DECLINE_EXIT = 3

#: Exit code for a REFUSAL — the caller declared a seat (`disp/<name>` / `dev/<name>`) that
#: mismatches every candidate it could otherwise have taken. Distinct from 0 (claimed) and from
#: NEXT_DECLINE_EXIT (nothing eligible right now, a normal poll result): a refusal means the
#: caller asked for something it is never allowed to have, not "try again later". Also distinct
#: from BOTH the generic 1 other verbs use for a hard error AND from 2, which typer/click already
#: owns for its own usage errors (verified empirically: a bad flag on this CLI exits 2) — colliding
#: with that would leave an external scheduler unable to tell "seat mismatch, don't retry the same
#: way" from "you passed a malformed flag". 4 is unclaimed by the framework, so it's ours.
NEXT_REFUSE_EXIT = 4


def _next_seat_actor(actor: str, data) -> str | None:
    """Resolve or validate the seat to claim `data` (a `bd ready`/`bd show` row) under, per AGF's
    recursive dispatch rule: an epic resolves to a dispatcher (`disp/<name>`), any other bead to a
    developer (`dev/<name>`) — the SAME rule `_guard_seat` enforces for `assign`/`claim`, reused
    (not reimplemented) here at the point of atomic pick so an external scheduler can't hand
    itself the wrong seat:

    - `actor` declares NO seat (no `disp/`/`dev/` prefix): resolved server-side — returns `actor`
      re-qualified with the seat this candidate's shape requires.
    - `actor` declares a seat that MATCHES the candidate's shape: returns `actor` unchanged.
    - `actor` declares a seat that MISMATCHES: returns `None` — the caller is refused this
      candidate rather than trusted, matching `_guard_seat`'s epic/leaf split exactly."""
    return work_dispatch.impl__next_seat_actor(sys.modules[__name__], actor, data)


def _molecule_members(epic: str, main) -> set[str]:
    """The ids `--epic <id>` admits: the epic itself plus what `bd list --parent <epic>` returns.

    ONE LEVEL IS THE ANSWER, not a limitation to fix later (bh-sh6yt's third open question). Two
    reasons it has to be one level:

    * It is byte-for-byte the membership `localloop.LoopDriver.load_molecule` feeds the decision
      table. The scope filter and the decision that sized the budget must admit the SAME set, or
      the loop decides against one molecule and claims against another — which is the bug this
      flag exists to close, reintroduced one tier down.
    * A nested epic is dispatched AS A BEAD by this loop and then driven by its own nested loop
      scoped to itself (the workstream tier). Recursing here would let the outer loop claim a
      grandchild out from under the inner loop that owns it.

    A member that `bd ready` did not return is still not claimable: this filter only ever REMOVES
    rows from the ready set. `bd` stays the authority on blocking.
    """
    return work_dispatch.impl__molecule_members(sys.modules[__name__], epic, main)


def _next_payload(
    hive,
    actor,
    claimed,
    claim_actor,
    rows,
    tried,
    refused,
    status,
    reason,
    worktree_path="",
    ident=None,
) -> dict:
    """The `bh work next` envelope — one stable key set for every outcome (`claimed` / `declined`
    / `refused`), so a consumer branches on `status` rather than on which keys happen to be
    present. `actor` is always the identity a claim was ATTEMPTED/HELD under (server-resolved when
    the caller declared no seat); `refused` lists candidates skipped for a declared-seat mismatch,
    independent of whether the call ultimately claimed something else. `worktree`/`identity` are
    `None` on a decline or refusal (nothing was provisioned) and populated once a claim is won and
    provisioned (bh-qczj.2)."""
    return work_dispatch.impl__next_payload(
        sys.modules[__name__],
        hive,
        actor,
        claimed,
        claim_actor,
        rows,
        tried,
        refused,
        status,
        reason,
        worktree_path,
        ident,
    )


def _try_claim(bead, actor, main) -> bool:
    """Claim `bead` for `actor`, then RE-VERIFY by re-reading it. True only when we hold it.

    The re-read is the whole point (see the block comment above): a non-zero claim means we lost
    outright, but a ZERO claim proves nothing — `bd` will happily hand the same bead to a second
    caller. `work_next.claim_won` decides from the re-read row, so the verdict is a pure function
    of what the store actually says rather than of an exit code."""
    return work_assignment.impl__try_claim(sys.modules[__name__], bead, actor, main)


def _release_claim(main, bead, actor, detail: str = "") -> None:
    """Undo `_try_claim`'s in_progress transition when provisioning fails afterward, so a bead
    never sits claimed with no worktree behind it (bh-qczj.2's acceptance criterion) — the same
    reopen/unassign write `abandon` uses for its recovery path.

    Filed under the `dispatch` closed dimension (`dispatch=provisioning_failed`), NOT `review` —
    a worktree provisioning failure is an infrastructure failure on the dispatch path, not a
    review outcome. Filing it under `review` would let `attempt_count`/`dispatch_cause_count`'s
    text-matching conflate "the reviewer bounced this" with "the disk was full" (state.py's
    module docstring, "Dispatcher failure dimensions"; bh-qczj.2's NOTES field). `detail`
    (typically the provisioning exception's message) rides as `--reason` for the operator."""
    return work_assignment.impl__release_claim(sys.modules[__name__], main, bead, actor, detail)


def _provision_claim(cfg, hive, main, bead, actor):
    """Provision/attach the just-won claim's worktree via the existing `worktree.ensure` path —
    the SAME op `assign`/`claim`/`start` already use, not a second provisioning path — then stamp
    identity and record the claim holder exactly as `_claim_single_bead` does. Returns
    `(worktree path, identity dict)`.

    Any failure here releases the claim (`_release_claim`) before re-raising, so a caller of
    `bh work next` never observes `status: claimed` for a bead with no worktree behind it."""
    return work_assignment.impl__provision_claim(
        sys.modules[__name__], cfg, hive, main, bead, actor
    )


@app.command("next")
@otel.trace_verb("work.next")
def next_(as_: str = _AS, hive: str = _HIVE, as_json: _NextJson = False, epic: _NextEpic = ""):
    """Atomically take the next ready bead: pick, claim, re-verify — retrying the next candidate
    when another worker won the race. The safe entry point for an unattended driver.

    `--epic <id>` SCOPES the candidate set to one molecule (bh-sh6yt). Unset — the human default —
    the candidate set is the whole hive, exactly as before. It exists because an unattended driver
    is required to claim through this verb (re-deriving the pick-then-claim race in the loop is
    what a loop must not do), and a per-epic driver that can only say "give me anything" will
    happily take work from a molecule nobody pointed it at.

    Walks `bh work ready` order (dependency-ordered, and release-scored when the hive configured a
    strategy) and claims the first candidate it can PROVE it holds. Seat-typed per AGF's recursive
    dispatch rule (`_next_seat_actor`, reusing `_guard_seat`'s epic/leaf split): a caller who
    declares no seat (`--as` names no `disp/`/`dev/` prefix) is resolved server-side — `disp/` for
    an epic candidate, `dev/` for anything else; a caller who DOES declare a seat is validated
    against each candidate, not trusted.

    Three distinct outcomes, so a driver never has to parse stderr to tell them apart:
      - `status: claimed`, exit 0 — held a bead; the worktree is provisioned/attached through the
        same `worktree.ensure` path `claim`/`assign`/`start` already use, identity is stamped
        worktree-scoped, and the `--json` envelope reports the resolved `worktree` path +
        `identity` alongside `bead`/`seat`/`actor`. A provisioning failure releases the claim
        (bead reopened, unassigned) rather than leaving it orphaned, and propagates as a normal
        CLI error (exit 1).
      - `status: declined`, exit 3 (`NEXT_DECLINE_EXIT`), reasoned `empty_queue` /
        `none_eligible` / `all_lost` — nothing takeable RIGHT NOW; a normal poll result, back off
        and retry.
      - `status: refused`, exit 4 (`NEXT_REFUSE_EXIT`), reason `seat_mismatch` — the declared seat
        mismatched every candidate it could otherwise have taken; the caller asked for something
        it is never allowed to have, not "try again later". (Not exit 2 — typer/click already
        owns that for its own usage errors.)"""
    return work_dispatch.impl_next_(sys.modules[__name__], as_, hive, as_json, epic)


_LoopPasses = Annotated[
    int, typer.Option("--passes", help="stop after N passes (0 = run until the molecule lands)")
]
_LoopJson = Annotated[
    bool, typer.Option("--json", help="emit one JSON pass report per line, AS EACH PASS ENDS")
]
_LoopDryRun = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help=(
            "decide-only: resolve gates, check (never renew) the host lease, read the ready "
            "set and consult the caps, emit the SAME dispatch_pass stream stamped "
            "dry_run:true — but never claim, provision, spawn, or write a bead. Forces exactly "
            "one pass regardless of --passes."
        ),
    ),
]
_LoopSeatBinary = Annotated[
    str,
    typer.Option(
        "--seat-binary",
        help=(
            "dispatch for real, but spawn THIS binary instead of the configured bh-<role> "
            "role binary for every seat — e.g. tests/fixtures/stub_seat.py, a no-op harness "
            "that drives the full claim/provision/spawn/cause path without an agent spending "
            "tokens. A path, not a boolean: point it at your own harness."
        ),
    ),
]
_LoopHarness = Annotated[
    str,
    typer.Option(
        "--harness",
        help="provider for a BAML-required loop (claude|codex); defaults to hive config.",
    ),
]
_LoopBamlRequired = Annotated[
    bool,
    typer.Option(
        "--baml-required",
        help=(
            "require exact provider-qualified packed seats and named-hive sources; validate "
            "every loop role before the first claim and never fall back"
        ),
    ),
]


@app.command("loop")
@otel.trace_verb("work.loop")
def loop(
    epic: str = typer.Argument(..., help="the epic whose molecule this loop drives"),
    as_: str = _AS,
    hive: str = _HIVE,
    passes: _LoopPasses = 0,
    as_json: _LoopJson = False,
    dry_run: _LoopDryRun = False,
    seat_binary: _LoopSeatBinary = "",
    harness: _LoopHarness = "",
    baml_required: _LoopBamlRequired = False,
):
    """Drive one molecule to completion with **no human present and no server running** — the
    `local` work-runtime tier (bh-c6dk.5).

    Each pass resolves gates, reclaims dead workers, renews the host lease while workers are
    active, heartbeats its own claims, enforces the in-process caps, harvests finished seats
    stdout-first, then asks `work_next.decide`'s 12-row table what to do and spawns the seat that
    does it. Every seat runs in its OWN PROCESS GROUP and is cancelled through the three-rung
    ladder with the group reaped behind it, so a cancelled run can never leave a live, spending
    agent orphaned to init (bh-a7so.2 §3).

    RESTART IS THE DURABILITY EVENT. Nothing is persisted outside beads — the in-flight map is
    deliberately volatile — so killing this process and starting it again is a no-op by
    construction: the next loop re-derives everything from `bd ready` + open gates + `bd reclaim`.
    That is why it is safe to run this under a supervisor that restarts it.

    A ready bead sitting behind an open `type:human` gate is never spawned: `bd gate check` only
    resolves timer/gh:run/gh:pr/bead gates, so the bead simply is not ready until a person
    resolves it (`bh work approve`), which needs no runtime running at all.

    TWO WAYS TO SEE WHAT THIS LOOP WOULD DO WITHOUT LETTING IT DO IT (bh-3xl60), deliberately
    different questions:

    `--dry-run` — DECIDE-ONLY. Runs the pass for real (gates, lease check, ready set, caps) and
    emits the SAME `dispatch_pass` stream, stamped `dry_run: true`, but never claims, provisions,
    spawns, or writes a bead. Forces exactly one pass. Answers "what would this loop do to my
    hive right now?".

    `--seat-binary <path>` — NO-OP HARNESS. Dispatches for real (claims, provisions, spawns) but
    points every seat at *path* instead of the configured `bh-<role>` role binary —
    `tests/fixtures/stub_seat.py` is the reference contract-implementing stub. Answers "does the
    full mechanical path work against MY corpus, without an agent spending tokens?".
    """
    return work_dispatch.impl_loop(
        sys.modules[__name__],
        epic,
        as_,
        hive,
        passes,
        as_json,
        dry_run,
        seat_binary,
        harness,
        baml_required,
    )


@app.command("check")
@otel.trace_verb("work.check")
def check(bead: str = _BEAD, hive: str = _HIVE):
    """Run the hive's validation command against the worktree; propagate its exit code.

    The hive's `verify: true` init rules run first, so the environment the command validates is
    derived from THIS TREE rather than from whenever the seat happened to be provisioned
    (bh-ku9n9.14) — the same establishment `clean_checkout` does in its verify dir.

    A green run against a CLEAN tree also seeds the verdict ledger `submit` reuses from
    (bh-i0p1.4), so the ordinary check-then-submit sequence pays for validation once, not
    twice — see `_record_check_verdict`."""
    return work_submission.impl_check(sys.modules[__name__], bead, hive)


@app.command("artifacts-uploaded")
@otel.trace_verb("work.artifacts-uploaded")
def artifacts_uploaded(
    run_id: str = typer.Argument(..., metavar="<run-id>", help="uploaded validation run id"),
    hive: str = _HIVE,
):
    """Acknowledge an external CI upload, then apply safe raw-artifact retention.

    Invoke this only after the complete run directory (``reports/`` plus
    ``gate.log``) has been uploaded. It is deliberately separate from `check`:
    bh cannot infer whether an external artifact service accepted the upload.
    """
    cfg = config.load()
    entry = worktree._resolve_entry(cfg, hive)
    main = registry.hive_dir(entry)
    if validation_records.mark_artifacts_uploaded(main, run_id) is None:
        typer.echo(f"✗ validation run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"✓ acknowledged upload for {run_id}; applied artifact retention")


def _mark_self_check(cfg, entry, target, rc) -> None:
    """Stamp this SELF-CHECK attempt onto the `work.check` verb span (bh-trgcd.2) — seat, tree
    content key, and green/red — so "how many self-checks did this bead take before one came back
    green" is answerable from the span stream alone.

    Emphatically NOT a bead write: per-attempt developer iteration is exactly the signal
    CLAUDE.md keeps OUT of bead history (an archive that must never be squashed), and exactly what
    an event stream is allowed to age out of. It goes to OTel or nowhere.

    Everything here is gated on `otel.is_active()` because the reads are not free — the tree
    resolve is a `git rev-parse`, and the seat is a claim-record read — and the off-path must stay
    zero-cost. The seat comes from the claim record `claim`/`resume` already wrote in this
    worktree, the same source `_resolve_submit_actor` trusts, so no `bd` read is added either.
    A dirty tree names no tree that ran (see `_checked_sha`), so HEAD's tree rides with
    `dirty=True` rather than posing as the content key."""
    return work_submission.impl__mark_self_check(sys.modules[__name__], cfg, entry, target, rc)


def _record_check_verdict(
    entry,
    target,
    cmd,
    rc,
    report=None,
    drop=None,
    log=None,
    cfg=None,
    run_id=None,
    bead=None,
    branch=None,
) -> None:
    """Feed a green `check` into the same verdict ledger `submit` reuses from (bh-i0p1.4): a
    clean-checkout validation and a `check` against a CLEAN worktree prove the exact same thing
    for the exact same tree — `check` runs the same `verify: true` init rules first
    (bh-ku9n9.14), so both establish their environment from that tree — and there is no reason
    the second (submit's) has to re-pay the ~6 minute run the first (check's, run moments
    earlier in the ordinary check-then-submit flow — see the `work` skill) already proved green.
    Recording is against `target`'s own HEAD — which the ledger keys by its TREE (bh-ku9n9.3),
    the commit itself kept only as metadata — so it
    is only trustworthy, and only attempted, when the tree is clean (no uncommitted delta):
    a dirty tree's HEAD would misrepresent what `cmd` actually ran against. Best-effort, silent,
    and skipped outright on a red run — `validation_ledger.record` never reuses a non-green
    verdict anyway, so there's nothing to gain recording one from here.

    It also files this run's triage detail under the same tree (bh-ku9n9.6), which is why the
    clean-tree gate now comes FIRST: the ledger's rc gate is unchanged and still short-circuits a
    red run before any ledger write, but the durable per-tree store wants a red run precisely
    *because* it is red, and both need the same honest answer to "which tree actually ran". A
    dirty tree names no tree to file under either. `drop`/`log` are the run's drop zone and tee'd
    gate log, both live only inside the caller's `with`; `triage_store` owns whether to keep
    anything at all (red or retried only) and swallows its own failures. `cfg` — `check`'s own,
    already resolved — is forwarded to the ledger's TTL lookup instead of a fresh `config.load()`
    (bh-ku9n9.19, item 2)."""
    return work_submission.impl__record_check_verdict(
        sys.modules[__name__],
        entry,
        target,
        cmd,
        rc,
        report,
        drop,
        log,
        cfg,
        run_id,
        bead,
        branch,
    )


def _checked_sha(target) -> str:
    """`target`'s HEAD when the worktree is CLEAN, else `""` — the one honest answer to "which
    tree actually ran". A dirty tree's HEAD names content the command never saw, so neither the
    ledger nor the triage store may be keyed by it."""
    return work_submission.impl__checked_sha(sys.modules[__name__], target)


def _merged_batch_groups(cfg, entry, main, beads) -> set[str]:
    """The `batch:<group>` names among `beads` whose group branch `wt/batch/<group>` already merged
    into integration — dead labels a re-parent/split can leave behind (bh-bfoy). Scheduling must not
    resurrect these as a batch. A group with no branch yet (never claimed) is live, not merged."""
    return work_dispatch.impl__merged_batch_groups(sys.modules[__name__], cfg, entry, main, beads)


def schedule_payload(epic: str, cfg, entry, main) -> dict:
    """Core payload for ``ws work schedule --json`` and ``beadhive://work/schedule/{epic}``.

    Returns ``{groups, singletons, coordinators, max_depth}`` — the cost-model dispatch plan with
    a complete late-bound model decision on every launch unit. Wraps
    ``schedule_mod.plan_schedule`` + shared model resolution;
    raises ``ValueError`` when ``epic`` is not found in this hive so callers can map the
    error to the appropriate surface (``typer.Exit`` or MCP ``ResourceError``).
    """
    return work_dispatch.impl_schedule_payload(sys.modules[__name__], epic, cfg, entry, main)


def _apply_start_gating(payload: dict, beads: list, cfg, entry) -> None:
    """Opt-in release-strategy start-gating (bh-k2j8.6). When the hive has set `release.strategy`,
    surface the scorer's merge order and mark any bead the start-gate would DEFER — one that would
    finish only to wait behind higher-priority work it's likely to conflict with — under a `release`
    key. When `release.strategy` is UNSET (the default / every hive today) this is a no-op, so the
    payload stays byte-identical to the pre-release FCFS/dep-order plan. Mutates `payload` in place.
    """
    return work_dispatch.impl__apply_start_gating(sys.modules[__name__], payload, beads, cfg, entry)


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
    return work_dispatch.impl_schedule(sys.modules[__name__], epic, hive, as_json)


def _guard_fork_remote(entry, remote) -> None:
    """Defense in depth alongside `worktree.push_branch`'s own pull-only rail (bh-uxam.1): an
    external hive's push target must never resolve to `upstream`, whatever produced `remote` —
    catch a misconfiguration here, at the caller, before ever reaching the git-shelling seam."""
    return work_submission.impl__guard_fork_remote(sys.modules[__name__], entry, remote)


@app.command("submit")
@otel.trace_verb("work.submit")
def submit(bead: str = _BEAD_OPT, as_: str = _AS, hive: str = _HIVE, group: str = _GROUP):
    """Hand off to async review: verify the branch is clean conventional digests, validate the
    proposed hash from a clean checkout, (publish for out-of-process review,) then open a gate.
    Not 'done' — leaves the worktree intact and returns immediately.

    With `--group <ids>`, submits a whole work-group from the shared `wt/batch/<group>` worktree:
    validate it once and open exactly ONE review gate whose reason names every member, so a single
    `approve` on any member clears it before `merge --group`."""
    return work_submission.impl_submit(sys.modules[__name__], bead, as_, hive, group)


def _record_submit_commits(bead, main, entry, branch, base) -> None:
    """Append this submit's own branch commits (`base..branch`, oldest-first) onto the bead's
    `git.commits` linkage (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md) — every
    commit on the branch, not just the tip, because the eventual `--no-ff` merge preserves each
    one verbatim into history. Non-fatal by construction: a metadata write must never fail (or
    strand) a submit whose code already landed on the branch — a failure is surfaced as a
    warning, never swallowed silently and never raised."""
    return work_submission.impl__record_submit_commits(
        sys.modules[__name__], bead, main, entry, branch, base
    )


def _guard_submit_worktree(bead, main, target) -> None:
    """Refuse when there's no worktree for `bead` — routes a batch member to `submit --group`
    (bh-n5z3.7) instead of a bare 'claim it first'."""
    return work_submission.impl__guard_submit_worktree(sys.modules[__name__], bead, main, target)


def _resolve_submit_actor(cfg, entry, target, bead, main, as_) -> str:
    """Resolve the submitting actor and guard the claim: no explicit `--as` defaults to the seat
    `claim`/`resume` actually recorded (bh-ejlq) — NOT a fresh env/git re-derivation, which is
    exactly what used to diverge from the held claim across separate shells/tool-calls. An
    explicit `--as` still wins outright; `_guard_holds_claim` refuses a mismatch or an unclaimed
    bead either way. Also warns (non-fatal) when cwd isn't the bead worktree."""
    return work_submission.impl__resolve_submit_actor(
        sys.modules[__name__], cfg, entry, target, bead, main, as_
    )


def _guard_claim_fence(cfg, entry, target, hive) -> None:
    """Verify the claim's FENCING TOKEN at the write boundary (bh-ytbb.10): refuse the submit
    when the host lease was lost and re-adopted while this work was in flight, so the recorded
    epoch is behind the generation now in force.

    Deliberately separate from `_resolve_submit_actor` above, and run AFTER it: that function
    owns seat verification and is unchanged by this bead, so an unclaimed bead or a seat
    mismatch still produces exactly the error it always did. This adds a second, orthogonal
    check on a different axis (generation, not identity) — see `guard.guard_claim_epoch`."""
    return work_submission.impl__guard_claim_fence(sys.modules[__name__], cfg, entry, target, hive)


def _guard_submit_ready(entry, target, branch, bead, cfg) -> str:
    """Guard the worktree is clean, on the expected branch, and a small clean conventional
    history — returns the resolved integration base."""
    return work_submission.impl__guard_submit_ready(
        sys.modules[__name__], entry, target, branch, bead, cfg
    )


def _warn_submit_release_hint(bead, main, entry, branch, base) -> None:
    """Release-hint reconcile (bh-k2j8.5): a NON-BLOCKING cross-check of the planner's `release:`
    hint against what the branch actually landed — a `release:feature`/`fix` bead that ships a
    breaking commit gets a warning so the label (or the commit) is fixed before release-order
    scoring reads a stale hint. Advisory only; never aborts the submit."""
    return work_submission.impl__warn_submit_release_hint(
        sys.modules[__name__], bead, main, entry, branch, base
    )


def _validate_submit_checkout(entry, branch, cfg, bead=None) -> None:
    """Clean-checkout validation — the result must not depend on dirty local state. Submit is
    the trusted-local opt-in to the verdict ledger (bh-dfx0): a fresh green verdict for this
    exact (TREE, cmd) skips the redundant checkout, so a re-submit of an unchanged sha is a
    true end-to-end no-op. Since bh-ku9n9.17 the landing boundaries (merge / postland / finish /
    batch land) reuse on the same key — an exact tree match, ADR Decision 4 — which is what makes
    THIS verdict the one a `--no-ff` land onto an unmoved base gets to ride."""
    return work_submission.impl__validate_submit_checkout(
        sys.modules[__name__], entry, branch, cfg, bead
    )


def _open_submit_gate(cfg, entry, bead, branch, main, sha) -> tuple[str, bool]:
    """Publish + open (or reuse) the review gate: push BEFORE set-state so a failed push blocks
    the gate too (no half-submitted bead) — out-of-process reviewers (GitHub CI) can't see a
    branch we don't push, and a `kind=external` (contribution) hive always pushes to its fork
    whatever the gate (bh-uxam.6). Opens the gate FIRST, then flips state, so we never leave a
    bead review=pending with nothing blocking it. Returns (gate type, reused an open gate)."""
    return work_submission.impl__open_submit_gate(
        sys.modules[__name__], cfg, entry, bead, branch, main, sha
    )


def _person_of(name: str) -> str:
    """The person part of a seat identity ('dev/alice' -> 'alice'); a bare name maps to itself. Used
    to spot a cross-seat self-review — the SAME person wearing both an author and a reviewer hat."""
    return work_submission.impl__person_of(sys.modules[__name__], name)


def _guard_self_review(cfg, entry, data, actor, bead) -> None:
    """Reviewer cross-seat policy (roles/RBAC matrix §3, bead .39; default flipped by bh-e5kv):
    approving a `type:human` review gate on a bead you authored is a rubber-stamp risk — the same
    leak whether the approver is a human wearing two hats or an agent self-approving its own
    dispatched work. Under `hard` (the default) this BLOCKS deterministically, so the human
    sign-off a `type:human` gate exists for can't be skipped by self-approval; under `advise`
    (explicit opt-out) it only WARNS and lets the approval through. Self-review is judged by
    PERSON, not seat — dev/alice authoring and rev/alice (or dev/alice) approving both count.
    No-op when the approver differs from the author, or either is unknown."""
    return work_submission.impl__guard_self_review(
        sys.modules[__name__], cfg, entry, data, actor, bead
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
    return work_submission.impl_approve(sys.modules[__name__], bead, as_, hive)


def _approve_security_gate(gates, bead, main, actor, open_review) -> bool:
    """Assurance (bead .33): a security:* gate is warden-only to resolve and runs in PARALLEL with
    review. Resolved here when a warden is clearing it, or when it's the only open gate (so a
    non-warden targeting it hits the warden-only refusal, not a misleading "no review gate").
    Returns True iff it handled (and reported) the approve — the caller returns immediately."""
    return work_submission.impl__approve_security_gate(
        sys.modules[__name__], gates, bead, main, actor, open_review
    )


def _approve_release_hold_gate(gates, bead, main, actor, open_review) -> bool:
    """Release (bh-k2j8): a release-hold: gate is releaser-only to resolve and blocks the merge
    like any open gate. Resolved here when a releaser is clearing it, or when it's the only open
    gate (so a non-releaser targeting it hits the releaser-only refusal, not a misleading "no
    review gate"). Mirrors `_approve_security_gate`. Returns True iff it handled the approve."""
    return work_submission.impl__approve_release_hold_gate(
        sys.modules[__name__], gates, bead, main, actor, open_review
    )


def _guard_human_review_gate(open_review, bead) -> None:
    """Refuse when `bead`'s open review gate is out-of-process (`gh:*`/`timer`) — resolve those
    through their own channel (CI / PR merge), not `bh work approve`."""
    return work_submission.impl__guard_human_review_gate(sys.modules[__name__], open_review, bead)


def _resolve_review_gates(open_review, bead, main, actor) -> list[str]:
    """Resolve EVERY open review gate — never first-match a possibly-stale one (bh-c3il): a
    duplicate left by an older submit would otherwise deadlock approve against merge. `bd gate
    resolve` only ever takes ONE gate id, so this stays a per-gate spawn (not batchable). Returns
    the resolved gate ids."""
    return work_submission.impl__resolve_review_gates(
        sys.modules[__name__], open_review, bead, main, actor
    )


def _clear_stale_review_state(bead, data, main, actor) -> None:
    """Clear a stale review=changes-requested left by a raw `bd set-state` bounce (bh-n5z3.6): once
    the gate is resolved, an approval must also flip the review state out of changes-requested,
    else `_merge_bead` refuses forever. review=approved is a new value nothing reads (merge only
    refuses changes-requested), so this is a pure unblock. Otherwise drop the stale
    review:pending label — review passed."""
    return work_submission.impl__clear_stale_review_state(
        sys.modules[__name__], bead, data, main, actor
    )


@app.command("bounce")
@otel.trace_verb("work.bounce")
def bounce(bead: str = _BEAD, message: str = _BOUNCE_MSG, as_: str = _AS, hive: str = _HIVE):
    """Reviewer: bounce a submitted bead back for changes. Resolves every OPEN review gate (so no
    orphan is left blocking a later merge while `approve` says "no open review gate"), then sets
    review=changes-requested. With no open gate it warns and still records the bounce. Points the
    developer at `bh work resume`. Batch behavior falls out free — the one batch gate names every
    member, so bouncing any member resolves it and blocks `merge --group` (bh-n5z3.6)."""
    return work_submission.impl_bounce(sys.modules[__name__], bead, message, as_, hive)


def _delete_branch(main, branch) -> None:
    """Best-effort delete of a landed molecule branch. The molecule already landed, so a failure
    here only warns (leaving a stale ref the coordinator can drop). GIT_* dir-pointing env is
    scrubbed so our explicit `-C <main>` always wins."""
    return work_merge.impl__delete_branch(sys.modules[__name__], main, branch)


def _teardown_coordinator_seat(cfg, hive, epic) -> None:
    """Best-effort removal of a coordinator seat worktree after its molecule lands (mirrors
    `merge --rm`). Runs BEFORE `_delete_branch` so the container branch isn't checked out (a
    `git branch -d` on a still-attached branch fails). No-op when the seat was never provisioned
    (a Phase-A / separate-merger land drove from the main clone) — a removal failure only warns,
    never blocks the completed land."""
    return work_merge.impl__teardown_coordinator_seat(sys.modules[__name__], cfg, hive, epic)


def _rollback_or_keep(entry, main, base, pre, slot_attrs) -> bool:
    """Handle a RED post-merge re-validation while still holding the slot: roll `base` back to its
    pre-merge sha `pre` IFF the branch is safe to rewrite (local/unpushed), else leave the merge
    bubble standing (a shared/pushed branch is fixed FORWARD, never reset). Emits the
    rolled_back/red_kept merge-outcome metric. Returns True iff the tip was rolled back — the caller
    renders the (site-specific) message and any bead bounce."""
    return work_merge.impl__rollback_or_keep(
        sys.modules[__name__], entry, main, base, pre, slot_attrs
    )


def _pr_ref(pr) -> str:
    """The human/bd-facing 'PR #<n> <url>' handle for a gh PR row."""
    return work_merge.impl__pr_ref(sys.modules[__name__], pr)


def _close_swarm_bead(epic, main) -> None:
    """Close the swarm orchestration bead(s) created over `epic` at kickoff (bh-7tno): without
    this every landed molecule leaves one permanent open type:molecule bead behind, silting up
    `work list` until a manual groom sweep. Best-effort — a failure warns, never unwinds a
    completed land. Batched into ONE `bd close` for every still-open match (`bd close` accepts
    multiple ids) instead of a subprocess-per-swarm loop."""
    return work_merge.impl__close_swarm_bead(sys.modules[__name__], epic, main)


def _pr_merge_gates(bead, main) -> list[dict]:
    """The OPEN `pr-merge` gates blocking `bead` — the landing-PR analog of `review_gates`
    (same description-marker selector convention, bh-c3il)."""
    return work_merge.impl__pr_merge_gates(sys.modules[__name__], bead, main)


def _ensure_pr_gate(main, bead, ref) -> None:
    """Idempotently open the bd `gh:pr` gate that blocks `bead` until its landing PR merges —
    bd's own gate check/discover watcher machinery can resolve it, and `work land` resolves any
    survivor at close. Reuses an already-open pr-merge gate on re-runs (submit's reuse rule)."""
    return work_merge.impl__ensure_pr_gate(sys.modules[__name__], main, bead, ref)


def _open_landing_pr(cfg, entry, main, bead, data, branch, base):
    """The `work.landing: pr` boundary — landing onto the SHARED integration branch of a
    PR-only-main repo. Instead of a local --no-ff merge: push the branch (work.push_remote) and
    open a GitHub PR against `base` (title from the bead digest, body carries id + acceptance),
    record the PR on the bead, and leave the bead/epic OPEN in a `landing=pr-pending` condition
    behind a `gh:pr` gate. CI on the PR takes over the postland-validation role; the close (with
    the squash-proof close_reason) fires from `work land` once GitHub reports the PR merged.
    Idempotent: a re-run reuses the open PR and its gate."""
    return work_merge.impl__open_landing_pr(
        sys.modules[__name__], cfg, entry, main, bead, data, branch, base
    )


def _guard_molecule_children(epic, main) -> list[dict]:
    """Guard the molecule is complete — every child closed, except an adopted origin report,
    linked child-of the epic as PROVENANCE, not molecule work — it carries no acceptance and never
    gets worked/closed on its own, so it must never gate the land. Returns the origin-report
    children (the intended jf5k/jey0 behavior: the report rides the epic to completion) for the
    caller to auto-close once the epic lands. Children come from `bd.children`, which trusts the
    parent EDGE — bd's own `--parent` matches by dotted-id PREFIX, so a bead detached from this
    epic used to gate the land forever on the strength of its id alone (bh-89mrf)."""
    return work_merge.impl__guard_molecule_children(sys.modules[__name__], epic, main)


def _guard_molecule_land_base(entry, epic, integration) -> str:
    """Recursive land (xn3o.7): resolve the land target one tier up via the integration_base climb,
    so `finish <container>` lands wt/bead/epic/<container> onto its nearest container ancestor —
    a top-level epic onto main (byte-identical to the old hardcoded target), a nested epic
    <ws>.<epic> onto its workstream container. Guards a container parent-link ambiguity and a
    closed-epic land target before resolving."""
    return work_merge.impl__guard_molecule_land_base(
        sys.modules[__name__], entry, epic, integration
    )


def _open_molecule_pr(cfg, entry, main, epic, epic_data, mol_branch, base, mode) -> None:
    """PR-only-main landing (work.landing: pr): a molecule landing onto the SHARED integration
    branch publishes as a PR instead of local-merging. The assembled molecule is still validated
    from a clean checkout first (a red molecule never reaches the PR either); the
    postland/combined validation role passes to CI on the PR. Reuses an exact-tree verdict on the
    same terms as the local-land path (`_validate_molecule_checkout`)."""
    return work_merge.impl__open_molecule_pr(
        sys.modules[__name__], cfg, entry, main, epic, epic_data, mol_branch, base, mode
    )


def _validate_molecule_checkout(entry, mol_branch, cfg, mode) -> None:
    """Validate the ASSEMBLED molecule from a clean checkout before landing — the land must not
    depend on dirty local state, and a red molecule never reaches the integration line. `loose`
    trusts the per-bead submits and skips even this. Raises on a red result.

    `reuse=True` — LANDING-BOUNDARY REUSE, ADR Decision 4 (bh-ku9n9.17). The ledger is keyed on
    (TREE, cmd_hash) since bh-ku9n9.3, so a hit IS the exact-tree-match test and nothing else can
    hit: same patch on a different base, a subtree, a moved base, a changed command, a stale entry
    and a red verdict all miss and run the gate for real. That is the whole condition Decision 4
    relaxes to, so there is deliberately no second tree comparison layered on top of the key —
    one source of truth for "same bytes", and it is the lookup. The last bead to land onto
    mol/<epic> already validated this exact tree; re-running it here proves nothing new."""
    return work_merge.impl__validate_molecule_checkout(
        sys.modules[__name__], entry, mol_branch, cfg, mode
    )


def _postland_revalidate_molecule(
    cfg, entry, main, base, pre, mode, stale, epic, mol_branch, slot_attrs
) -> None:
    """Post-land re-validation of the integration tip. Runs under `conservative` always, and as a
    correctness backstop under `relaxed` when main moved (stale). Still holding the slot, so a red
    tip is reset to its pre-land sha before release — no one ever sees a broken main. Raises on an
    unrecoverable red result. Reuses an exact-tree verdict (Decision 4, see
    `_validate_molecule_checkout`): a land onto an UNMOVED base produces a merge commit whose tree
    is byte-identical to the molecule's, which the pre-land run just proved — and the `stale` arm
    above is exactly the case where the base MOVED, so that tree is new and the lookup misses."""
    return work_merge.impl__postland_revalidate_molecule(
        sys.modules[__name__],
        cfg,
        entry,
        main,
        base,
        pre,
        mode,
        stale,
        epic,
        mol_branch,
        slot_attrs,
    )


def _close_molecule_origin_reports(origin_reports, epic, main) -> None:
    """Auto-close any adopted origin report now that its epic has landed: the report is
    provenance that rides the epic to completion, so it closes WITH the molecule rather than
    lingering open forever. Best-effort — a close failure only warns, never unwinds a completed
    land. Batched into ONE `bd close` for every still-open report (`bd close` accepts multiple
    ids) instead of a subprocess-per-report loop."""
    return work_merge.impl__close_molecule_origin_reports(
        sys.modules[__name__], origin_reports, epic, main
    )


def _reconcile_landed_molecule(cfg, entry, main, epic, epic_data, mol_branch, base, hive) -> None:
    """Finish the bookkeeping half of a molecule land whose CODE already landed (bh-lvqs).

    The molecule twin of `_reconcile_landed_bead`, and the one with the least forgiving failure
    mode: `merge_no_ff` over an already-merged container succeeds with "Already up to date", so
    the old path could re-run forever, reporting nothing wrong while the epic stayed open and its
    seat worktree and container branch stayed alive. Reconcile does the tail the first run missed —
    close the epic, ride the origin reports and swarm bead down with it, tear the seat down, delete
    the container — and exits 0."""
    return work_merge.impl__reconcile_landed_molecule(
        sys.modules[__name__], cfg, entry, main, epic, epic_data, mol_branch, base, hive
    )


def _merge_molecule(cfg, epic, hive):
    """The molecule wrap-up / land: collapse a whole assembled `mol/<epic>` onto the hive
    integration branch as ONE `--no-ff` bubble (the bead merges live inside it). Guards the
    molecule is complete (every child closed) + clean, holds the hive merge slot, validates the
    assembled branch from a clean checkout, lands it, closes the epic, and deletes the branch.
    On conflict / validation failure it aborts and releases the slot — never drops work."""
    return work_merge.impl__merge_molecule(sys.modules[__name__], cfg, epic, hive)


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
    return work_merge.impl_finish(sys.modules[__name__], epic, hive)


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
    return work_merge.impl_land(sys.modules[__name__], bead, hive)


def _prune_landed_hive(entry) -> None:
    """Best-effort cleanup after a PR-confirmed land.

    ``worktree.prune`` applies the SAFE classifier, so this can only reclaim work that is
    already closed, landed, and clean.  The close has already completed, however, so a metadata
    or filesystem failure here must never turn a successful land into a failed command.
    """
    return work_merge.impl__prune_landed_hive(sys.modules[__name__], entry)


def _guard_land_pr_pending(bead, main) -> None:
    """Refuse `land` on a bead that isn't `pr-pending` — it only completes a `work.landing: pr`
    landing opened by `merge`/`finish`."""
    return work_merge.impl__guard_land_pr_pending(sys.modules[__name__], bead, main)


def _resolve_merged_land_pr(entry, branch) -> dict:
    """The MERGED PR for `branch`, or refuse — completion is driven by PR STATE, never asserted
    (the operator escape hatch for an out-of-band landing is `worktree mark-landed`)."""
    return work_merge.impl__resolve_merged_land_pr(sys.modules[__name__], entry, branch)


def _resolve_land_pr_merge_gates(bead, main, ref) -> None:
    """Resolve any still-open pr-merge gate — bd's own gh:pr gate watcher may already have (both
    orders are fine); a resolve failure only warns, the merge already happened on GitHub. `bd
    gate resolve` only ever takes ONE gate id, so this stays a per-gate spawn (not batchable)."""
    return work_merge.impl__resolve_land_pr_merge_gates(sys.modules[__name__], bead, main, ref)


def _close_land_origin_reports(bead, main) -> None:
    """Epic parity with the local land: adopted origin reports ride the epic to completion.
    Best-effort — never unwinds a completed land. Batched into ONE `bd close` for every
    still-open report (`bd close` accepts multiple ids) instead of a subprocess-per-report loop.

    Uses `bd.children` (the parent EDGE), matching `_guard_molecule_children`. These are the READ
    and WRITE halves of one feature, and a first pass at bh-89mrf fixed only the read — leaving a
    detached bead invisible to the guard yet still CLOSED by the land, which is worse than fixing
    neither."""
    return work_merge.impl__close_land_origin_reports(sys.modules[__name__], bead, main)


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
    return work_merge.impl_merge(sys.modules[__name__], bead, hive, rm, molecule, group)


def _guard_bead_merge_gates(bead, main, landing_pr) -> None:
    """Guard `bead` is mergeable: not changes-requested, and no open gate blocks it (broad on
    purpose — the warden's security:* gate blocks in parallel with review); the refusal
    enumerates each open gate by kind so the merger knows who clears what (bh-c3il). Under
    `landing: pr` the ONE exception is the landing path's own `pr-merge` gate — it must not block
    an idempotent re-run of that same path (which reuses the open PR + gate rather than opening
    duplicates)."""
    return work_merge.impl__guard_bead_merge_gates(sys.modules[__name__], bead, main, landing_pr)


def _guard_bead_land_base(entry, bead, integration) -> str:
    """Recursive land (xn3o.7): guard a container parent-link ambiguity and a closed-epic land
    target, then resolve `bead`'s land base one tier up via the integration_base climb."""
    return work_merge.impl__guard_bead_land_base(sys.modules[__name__], entry, bead, integration)


def already_landed(entry, branch: str, base: str) -> bool:
    """Merge-verb alias for :func:`worktree.landed_via_merge` — the branch's commits are on `base`
    because they were merged there, not because the branch was never implemented (bh-lvqs)."""
    return work_merge.impl_already_landed(sys.modules[__name__], entry, branch, base)


def _guard_bead_clean_history(entry, branch, base, cfg) -> bool:
    """Guard the branch is a small clean conventional history before it's allowed to merge —
    reuses submit's `_history_ok` check as a merge-time backstop.

    Returns True when the branch is ALREADY LANDED, so the caller reconciles bookkeeping instead
    of merging (bh-lvqs); False on the ordinary path. A genuinely empty branch — no commits over
    base and NOT an ancestor of it — still takes the self-refine bounce unchanged."""
    return work_merge.impl__guard_bead_clean_history(
        sys.modules[__name__], entry, branch, base, cfg
    )


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
    return work_merge.impl__reconcile_landed_bead(
        sys.modules[__name__], cfg, entry, main, bead, bead_data, branch, base, hive, rm
    )


def _guard_signed_history(entry, branch, base, cfg) -> None:
    """The enforce-signing gate (bh-ijd4), beside the clean-history check because that is
    already the pre-merge guard: with `work.enforce_signing` on, EVERY commit in the merge
    range must verify as trusted, not just the tip. A no-op when the flag is off, so default
    behaviour is byte-identical to before."""
    return work_merge.impl__guard_signed_history(sys.modules[__name__], entry, branch, base, cfg)


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
    return work_merge.impl__merge_bead_no_ff(
        sys.modules[__name__], entry, branch, base, target, cfg, bead, main, slot_attrs
    )


def _postland_revalidate_bead(cfg, entry, main, base, pre, bead, slot_attrs, on_main) -> None:
    """Re-test the integration tip after a clean bead merge — green in isolation at submit, but
    the COMBINATION with what's already on the tip may be red. Still holding the slot, so on red
    we reset a safe-to-rewrite tip (the private mol/<epic>, or an unpushed main) to its pre-merge
    sha and bounce the bead to changes-requested; a shared (pushed) tip is left standing and
    fixed forward. Raises on an unrecoverable red result.

    "The COMBINATION may be red" is precisely a statement about the TREE: a merge onto a base that
    moved since the branch forked produces a tree neither parent has, the (tree, cmd_hash) lookup
    misses, and this runs in full. When the base did NOT move the merge tree is byte-identical to
    the branch tip submit already validated, so there is no combination to test — that is ADR
    Decision 4 (bh-ku9n9.17), and the ledger key is the entire test for it (see
    `_validate_molecule_checkout` for why no second tree comparison exists)."""
    return work_merge.impl__postland_revalidate_bead(
        sys.modules[__name__], cfg, entry, main, base, pre, bead, slot_attrs, on_main
    )


def _record_merge_commit(bead, main, base) -> None:
    """Append the just-landed merge commit's own sha onto `bead`'s `git.commits` linkage
    (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md). Read `base`'s tip AFTER the merge
    lands and (when this run re-validates) AFTER `_postland_revalidate_bead` has returned —
    that call either returns clean or raises `typer.Exit` on a red re-validation that ROLLS THE
    MERGE BACK, so calling this only once control reaches here means a rolled-back sha is never
    recorded. Non-fatal by construction: a metadata write must never fail a merge that already
    landed — a failure is surfaced as a warning, never swallowed silently and never raised."""
    return work_merge.impl__record_merge_commit(sys.modules[__name__], bead, main, base)


def _merge_bead(cfg, bead, hive, rm):
    """Serialize the land of a single approved bead onto its integration base: guard open + review
    resolved + a small clean conventional history, hold the merge slot, rebase-retry merge
    `--no-ff`, re-validate the combined tip on a main-gate, close the bead. The single-bead
    sibling of `_merge_molecule` / `merge_group`; `merge` is the thin 3-way dispatch over them."""
    return work_merge.impl__merge_bead(sys.modules[__name__], cfg, bead, hive, rm)


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


def _claim_residue(data) -> str:
    """What of the claim SURVIVED the release write — "" when the bead is genuinely free.

    The re-verify half of abandon (bh-0mckw), and the deliberate mirror of
    `work_next.claim_won`: taking a claim is not believed on an exit code, so giving one back
    must not be either. Same store, same non-CAS write, same reason.
    """
    if not isinstance(data, dict):
        # A bead we cannot re-read is a bead we cannot vouch for. Reporting success here would
        # be the exact unqualified ✓ this function exists to stop.
        return "the bead could not be re-read, so its claim state is unknown"
    residue = []
    status = str(data.get("status") or "")
    if status not in ("", "open"):
        residue.append(f"status is still {status}")
    holder = str(data.get("assignee") or "")
    if holder:
        residue.append(f"still assigned to {holder}")
    return "; ".join(residue)


@app.command("abandon")
@otel.trace_verb("work.abandon")
def abandon(
    bead: str = _BEAD,
    hive: str = _HIVE,
    rm: bool = typer.Option(False, "--rm", help="also remove the worktree (default: keep it)"),
):
    """Release the claim and record the abandon, then RE-READ to prove it. Recovery path for
    stalls.

    The re-read is the point (bh-0mckw). This verb reported an unqualified ✓ off two exit codes
    and nothing else, and an operator cleaning up after a runaway loop measured eight beads that
    came back `in_progress` and still assigned — so the only net effect was an `abandoned` review
    marker on work that was still held, which is strictly worse than having left them alone.
    Whatever made the release not take, a verb whose whole job is releasing a claim must not be
    the last thing to find out it failed. `bd update --claim` is not a compare-and-swap in either
    direction; `work_next.claim_won` re-reads for the same reason on the way in.
    """
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
    residue = _claim_residue(bd.show(bead, main))
    if residue:
        # NAME THE REMAINING STEP. The bead's acceptance criterion is that abandon either leaves
        # the bead open and unassigned or says what is still needed — a bare ✓ over a still-held
        # bead points at no follow-up at all, which is how eight of them went unnoticed.
        typer.echo(
            f"⚠ {bead}: review=abandoned was recorded, but the claim was NOT released "
            f"({residue}).\n"
            f"  The bead is still held, so nothing else can take it. Release it with:\n"
            f"    bh bd reclaim            # reverts claims whose lease has expired\n"
            f"    bh bd update {bead} --status open --assignee ''   # or force it directly",
            err=True,
        )
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
    return work_refine.impl__load_plan(sys.modules[__name__], plan_arg)


def _restore(target, backup) -> None:
    """Abort any in-progress rebase and hard-reset the branch back to its pre-refine tip."""
    return work_refine.impl__restore(sys.modules[__name__], target, backup)


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
    return work_refine.impl_refine_branch(
        sys.modules[__name__],
        cfg,
        hive=hive,
        bead=bead,
        plan=plan,
        autosquash=autosquash,
        since=since,
        dry_run=dry_run,
    )


def _guard_refine_mode(target, bead, plan, autosquash, since) -> None:
    """Guard exactly one input mode (--plan | --autosquash | --since) is given and the worktree
    exists."""
    return work_refine.impl__guard_refine_mode(
        sys.modules[__name__], target, bead, plan, autosquash, since
    )


def _resolve_refine_base(cfg, entry, bead, branch) -> str:
    """Resolve the refine base (the integration base climbed onto the branch's actual fork
    point), or raise when it can't be computed."""
    return work_refine.impl__resolve_refine_base(sys.modules[__name__], cfg, entry, bead, branch)


def _build_refine_plan(entry, base, branch, plan, autosquash, since) -> tuple[str, list, list]:
    """Build the squash plan + resolve commit rows/groups (autosquash lets git build its own
    todo, so no plan). Returns (base — possibly overridden by an explicit plan `base`, commit
    rows, groups)."""
    return work_refine.impl__build_refine_plan(
        sys.modules[__name__], entry, base, branch, plan, autosquash, since
    )


def _apply_refine_rebase(entry, target, branch, base, autosquash, rows, groups) -> str:
    """Real refine: require a clean tree on the expected branch, snapshot a backup branch,
    rebase (autosquash or an explicit squash-plan todo), and gate on a byte-identical net tree —
    restoring from the backup on any rebase failure or tree drift. Returns the backup branch."""
    return work_refine.impl__apply_refine_rebase(
        sys.modules[__name__], entry, target, branch, base, autosquash, rows, groups
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
    return work_refine.impl_refine(
        sys.modules[__name__], bead, plan, autosquash, since, dry_run, hive
    )
