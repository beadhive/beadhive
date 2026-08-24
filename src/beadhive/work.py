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

import asyncio  # noqa: F401 - injected lifecycle collaborator on the stable facade
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
    claim_authority,
    config,
    converge,
    ghpr,
    git_linkage,
    guard,
    host,  # noqa: F401 - injected lifecycle collaborator
    identity,
    jsonout,  # noqa: F401 - injected lifecycle collaborator
    model_routing,  # noqa: F401 - injected lifecycle collaborator
    otel,
    registry,  # noqa: F401 - injected lifecycle collaborator
    release_order,  # noqa: F401 - injected lifecycle collaborator
    test_report,
    triage_store,
    validation_ledger,
    work_assignment,
    work_dispatch,
    work_group,
    work_guards,
    work_intake,
    work_logic,
    work_metrics,
    work_next,  # noqa: F401 - injected lifecycle collaborator
    work_reads,
    work_show,
    worktree,
)
from . import log as dispatch_log
from . import schedule as schedule_mod  # noqa: F401 - injected lifecycle collaborator
from .run import missing_binary, run
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
        sys.modules[__name__], epic, as_, hive, passes, as_json, dry_run, seat_binary
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
    # Establish the environment FROM THE TREE before validating (bh-ku9n9.14), exactly as
    # `clean_checkout` does in its verify dir. Without this, `check`'s verdict — which seeds the
    # ledger `submit` reuses — was a property of WHEN THE SEAT WAS PROVISIONED, not of the tree:
    # a seat whose venv predates a dependency change validates a different environment than the
    # clean checkout would, and the two verdicts are indistinguishable in the ledger (same key,
    # same rc). Warm `run_init` medians 0.119s here (bh-ku9n9.19) against THIS command — `check`
    # runs the FAST subset (~140-167s), not the ~371.4s full check-all pipeline that figure
    # belongs to elsewhere in this epic. The rules stay opaque {run, if_exists?, verify?} entries
    # in the operator's declared order — bh learns nothing about any ecosystem — so a hive
    # declaring none runs zero commands and is unaffected.
    worktree.run_init(cfg, entry, target, verify_only=True)
    v_start = time.perf_counter()
    # BH_TEST_REPORT_DIR (bh-ku9n9.20): same fresh, empty drop zone `submit`'s clean checkout
    # exports, so `check` and `submit` observe the run identically. bh never invokes a runner.
    # …and the gate log is teed to the durable per-tree triage store (bh-ku9n9.6) on the same
    # rule `submit`'s clean checkout uses, so a red `check` is readable afterwards.
    with test_report.drop_zone() as drop, triage_store.gate_log() as log:
        res = run(
            shlex.split(cmd),
            cwd=str(target),
            check=False,
            env=test_report.export(otel.telemetry_neutral_env(), drop),
            tee=log,
        )
        rc = res.returncode
        v_elapsed = time.perf_counter() - v_start  # the command itself, not bh's bookkeeping
        report = test_report.ingest(drop, rc)
        # Inside the `with`: the drop zone and the tee'd gate log are both gone the moment it
        # closes, so anything durable has to be copied out before then.
        _record_check_verdict(entry, target, cmd, rc, report, drop, log, cfg=cfg)
    otel.record_validation_duration(
        v_elapsed,
        {"bh.work.phase": "check", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "check"})
    _mark_self_check(cfg, entry, target, rc)
    missing = missing_binary(res)
    if rc != 0 and not missing:
        # THE CONVERGE LOOP (bh-ku9n9.8), and the ONLY place it is wired: a developer loop that
        # re-runs just the failures via `work.validate_subset` to get from red to knowing-why in
        # seconds instead of ~6 minutes. It can only ever produce a CANDIDATE — the verdict above
        # is already recorded (and, being red, was never written), and `converge` seals the ledger
        # shut before it spawns, so nothing downstream can turn a converged result into an
        # attestation. A hive with no `work.validate_subset`, or a run that named no failing
        # tests, converges nothing and gets today's behaviour. NEVER wire this into the gate.
        # The sha is the one `_record_check_verdict` would file under — empty on a dirty tree,
        # whose HEAD names a tree that is not what ran, so the retries are simply not stored.
        converge.converge(entry, cfg, target, _checked_sha(target), report)
    if missing:
        # No `capture` here, so a missing validate binary would exit 127 having printed NOTHING
        # — the operator sees `bh work check` fail silently and reads it as a test failure
        # (bh-7m2h9). Name the binary, and say this is not a verdict on the code.
        typer.echo(
            f"✗ validation could not RUN: `{missing}` is not on PATH (validate_cmd is {cmd!r}). "
            f"This is not a test failure — install it or fix PATH, then re-run.",
            err=True,
        )
    elif rc == RETRYABLE_VALIDATION_EXIT:
        # Same distinction `_validate_submit_checkout` makes for `submit` (bh-u9ip): a network
        # dependency was unreachable, not a verdict on the code.
        typer.echo(
            f"⚠ validation could not complete (exit {rc}) — a network dependency was "
            "unreachable. This is NOT a test failure — retry once connectivity recovers.",
            err=True,
        )
    if rc != 0:
        raise typer.Exit(rc)


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
    if not otel.is_active():
        return
    sha = _checked_sha(target)
    record = claim_authority.get_authority(config.claim_authority(cfg, entry)).read(target)
    otel.set_self_check(
        rc == 0,
        seat=record.seat if record else "",
        tree=validation_ledger.tree_of(entry, sha or worktree.head_full_sha(target)),
        dirty=not sha,
    )


def _record_check_verdict(
    entry, target, cmd, rc, report=None, drop=None, log=None, cfg=None
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
    sha = _checked_sha(target)
    if not sha:
        return
    triage_store.store(entry, sha, cmd, rc, report, drop, log)
    if rc == 0:
        validation_ledger.record(entry, sha, cmd, rc, report=report, cfg=cfg)
        # This IS the confirming run — a full, clean, un-retried gate over this tree, which is the
        # only thing that may attest (bh-ku9n9.8). Say so if some of it needed a retry to get here
        # at this exact content: green is the honest verdict, and a flake is still a flake.
        converge.warn_flakes(entry, sha, rc)


def _checked_sha(target) -> str:
    """`target`'s HEAD when the worktree is CLEAN, else `""` — the one honest answer to "which
    tree actually ran". A dirty tree's HEAD names content the command never saw, so neither the
    ledger nor the triage store may be keyed by it."""
    return worktree.head_full_sha(target) if worktree.is_clean(target) else ""


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
    exact (TREE, cmd) skips the redundant checkout, so a re-submit of an unchanged sha is a
    true end-to-end no-op. Since bh-ku9n9.17 the landing boundaries (merge / postland / finish /
    batch land) reuse on the same key — an exact tree match, ADR Decision 4 — which is what makes
    THIS verdict the one a `--no-ff` land onto an unmoved base gets to ride."""
    v_start = time.perf_counter()
    rc = worktree.clean_checkout(
        entry, branch, config.validate_cmd(cfg, entry, "submit"), reuse=True
    )
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"bh.work.phase": "submit", "bh.validation.result": _vres(rc), "bh.hive": _hive(entry)},
    )
    otel.count_validation(rc == 0, {"bh.work.phase": "submit"})
    if rc == RETRYABLE_VALIDATION_EXIT:
        # NOT a verdict (bh-u9ip): validate_cmd itself is saying it couldn't reach a network
        # dependency (deps.dev/osv.dev for the license gate) and never got to judge anything —
        # neither pass nor fail. Nothing was recorded green (the ledger only ever records a
        # green verdict as reusable), so a plain re-submit re-validates fresh rather than
        # replaying a stale answer.
        typer.echo(
            f"⚠ validation could not complete (exit {rc}) — a network dependency was "
            "unreachable; nothing submitted. This is NOT a policy verdict — retry "
            "`bh work submit` once connectivity recovers.",
            err=True,
        )
        raise typer.Exit(rc)
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
    caller to auto-close once the epic lands. Children come from `bd.children`, which trusts the
    parent EDGE — bd's own `--parent` matches by dotted-id PREFIX, so a bead detached from this
    epic used to gate the land forever on the strength of its id alone (bh-89mrf)."""
    children = bd.children(epic, main)
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
    postland/combined validation role passes to CI on the PR. Reuses an exact-tree verdict on the
    same terms as the local-land path (`_validate_molecule_checkout`)."""
    if mode != "loose":
        rc = worktree.clean_checkout(
            entry, mol_branch, config.validate_cmd(cfg, entry, "molecule"), reuse=True
        )
        otel.count_validation(rc == 0, {"bh.work.phase": "molecule"})
        if rc != 0:
            typer.echo(f"✗ molecule validation failed (exit {rc}) — no PR opened", err=True)
            raise typer.Exit(rc)
    _open_landing_pr(cfg, entry, main, epic, epic_data, mol_branch, base)


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
    if mode == "loose":
        return
    v_start = time.perf_counter()
    rc = worktree.clean_checkout(
        entry, mol_branch, config.validate_cmd(cfg, entry, "molecule"), reuse=True
    )
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
    unrecoverable red result. Reuses an exact-tree verdict (Decision 4, see
    `_validate_molecule_checkout`): a land onto an UNMOVED base produces a merge commit whose tree
    is byte-identical to the molecule's, which the pre-land run just proved — and the `stale` arm
    above is exactly the case where the base MOVED, so that tree is new and the lookup misses."""
    if mode == "conservative" or (mode != "loose" and stale):
        vrc = worktree.clean_checkout(
            entry, base, config.validate_cmd(cfg, entry, "postland"), reuse=True
        )
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
    _prune_landed_hive(entry)
    otel.count_bead_transition("pr_landed")
    typer.echo(
        f"✓ {ref} merged — closed {bead} (close_reason: {reason}); reaped any SAFE worktree(s)"
    )


def _prune_landed_hive(entry) -> None:
    """Best-effort cleanup after a PR-confirmed land.

    ``worktree.prune`` applies the SAFE classifier, so this can only reclaim work that is
    already closed, landed, and clean.  The close has already completed, however, so a metadata
    or filesystem failure here must never turn a successful land into a failed command.
    """
    hive = _hive(entry)
    try:
        worktree.prune(hive=hive)
    except Exception as exc:  # best-effort post-land cleanup; never unwind a completed land
        typer.echo(f"⚠ landed but automatic worktree prune for {hive} failed: {exc}", err=True)


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
    still-open report (`bd close` accepts multiple ids) instead of a subprocess-per-report loop.

    Uses `bd.children` (the parent EDGE), matching `_guard_molecule_children`. These are the READ
    and WRITE halves of one feature, and a first pass at bh-89mrf fixed only the read — leaving a
    detached bead invisible to the guard yet still CLOSED by the land, which is worse than fixing
    neither."""
    children = bd.children(bead, main)
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
    fixed forward. Raises on an unrecoverable red result.

    "The COMBINATION may be red" is precisely a statement about the TREE: a merge onto a base that
    moved since the branch forked produces a tree neither parent has, the (tree, cmd_hash) lookup
    misses, and this runs in full. When the base did NOT move the merge tree is byte-identical to
    the branch tip submit already validated, so there is no combination to test — that is ADR
    Decision 4 (bh-ku9n9.17), and the ledger key is the entire test for it (see
    `_validate_molecule_checkout` for why no second tree comparison exists)."""
    vrc = worktree.clean_checkout(
        entry,
        base,
        config.validate_cmd(cfg, entry, "merge", main_gate=on_main),
        reuse=True,
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
