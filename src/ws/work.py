"""`ws work` — the integration-plane driver.

Takes a single bead assigned → merged through the Agentic Git Flow lifecycle
(brief → claim → check → submit → resume → abandon, plus orchestrator-only assign),
so an agent drives the lifecycle through `ws` instead of improvising raw git. It is a
thin facade: each verb composes `bd` (Beads), `ws` managed worktrees, and per-agent
identity primitives that already exist. Raw git is for the change *inside* the worktree
only — never the lifecycle around it.

Test seam: this module shells out to **`bd` only** (via `_bd`); every git / worktree
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

import typer

from . import config, identity, otel, work_group, work_logic, work_show, worktree
from . import schedule as schedule_mod
from .run import run
from .work_logic import (
    _CONVENTIONAL,
    _MARKER,
    _simulate,
    build_todo,
    plan_from_since,
    validate_plan,
)

# Re-exported for the public/test surface (used by callers, not within this module).
auto_message = work_logic.auto_message
flag_rows = work_logic.flag_rows

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


# ---- bd plumbing (the only subprocess surface here) -------------------------


def _bd(args, cwd, actor="", capture=False):
    """Run a `bd` subcommand scoped to the rig via `-C <cwd>` (so the right Beads DB is hit
    regardless of the process cwd / `--rig`). Prepends `--actor <name>` for the audit trail."""
    cmd = ["bd", "-C", str(cwd)]
    if actor:
        cmd += ["--actor", actor]
    cmd += list(args)
    return run(cmd, check=False, capture=capture)


def _bd_json(args, cwd):
    """Parse `bd <args> --json`, or None on failure."""
    res = _bd([*args, "--json"], cwd, capture=True)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None


def _show(bead, cwd):
    """The bead's JSON object (bd show may return a single object or a 1-list)."""
    data = _bd_json(["show", bead], cwd)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _state(bead, dim, cwd):
    """Current value of a state dimension via `bd state` ('' if unset)."""
    res = _bd(["state", bead, dim], cwd, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _first(data, *keys):
    """First present, truthy value among keys (bd JSON field-name drift insurance)."""
    return next((data[k] for k in keys if data.get(k)), None)


def _open_gate(bead, cwd) -> bool:
    """True iff an open review gate still blocks `bead` — i.e. it isn't approved yet. The gate
    names the bead in its description (matches `bd gate create --blocks <bead>` at submit)."""
    gates = _bd_json(["gate", "list"], cwd)
    if not isinstance(gates, list):
        return False
    return any(g.get("status") == "open" and bead in str(g.get("description") or "") for g in gates)


# ---- at-merge flow metrics (hqfy.2): best-effort, skew-guarded bd reads ------
#
# Everything below feeds the commit-flow metrics emitted at the merge seam. EVERY bd read here is
# best-effort: the caller wraps the emission in try/except so a slow/failing read NEVER blocks a
# merge, and each individual metric is skipped when its inputs are missing or its delta is negative
# (clock skew / out-of-order data). Attributes are bounded — no bead/epic ids on the metric points.


def _rig(entry) -> str:
    """The low-cardinality rig name for a metric attribute (the managed-repo prefix)."""
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
    rows = _bd_json(["list", "--parent", bead, "--include-infra"], cwd)
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


def _review_gate(bead, cwd):
    """The review gate for `bead` (reason 'review <sha>' in its description), or None — chosen by
    matching both the bead id and the review reason so the kickoff/other gates don't match."""
    gates = _bd_json(["gate", "list", "--all"], cwd)
    if not isinstance(gates, list):
        return None
    for g in gates:
        desc = str(g.get("description") or "").lower()
        if bead.lower() in desc and "reason: review" in desc:
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
    review_pending_at = None
    if events is not None:
        review_pending_at = _review_pending_at(events)
        otel.record_rework(sum(1 for e in events if _is_changes_requested(e)), attrs)

    gate = _review_gate(bead, main)
    gate_closed_at = _parse_ts(_first(gate or {}, "closed_at", "resolved_at")) if gate else None

    _emit_delta(_stage_recorder("coding"), review_pending_at, started, attrs)
    _emit_delta(_stage_recorder("review_wait"), gate_closed_at, review_pending_at, attrs)
    _emit_delta(_stage_recorder("merge_latency"), now, gate_closed_at, attrs)


# ---- guards & shared steps ---------------------------------------------------


def _guard_open(data, bead):
    if data is None:
        typer.echo(f"✗ no such bead: {bead}", err=True)
        raise typer.Exit(1)
    if str(data.get("status", "")) == "closed":
        typer.echo(f"✗ bead {bead} is closed", err=True)
        raise typer.Exit(1)


def _guard_not_other(data, actor, bead):
    """Refuse if assigned to a *different* actor — `bd --claim` would otherwise steal it."""
    cur = str(data.get("assignee") or "")
    if cur and cur != actor:
        typer.echo(f"✗ bead {bead} assigned to {cur} (not {actor}) — refusing to steal", err=True)
        raise typer.Exit(1)


def _stamp(cfg, entry, target, actor):
    """Stamp agent identity + signing into the worktree, unless supervised (inherit human)."""
    prof = config.work_identity(cfg, entry, actor)
    if prof["mode"] == "supervised":
        return
    identity.stamp(
        target,
        name=actor or prof["name"] or "",
        email=prof["email"] or "",
        signing_key=prof["signing_key"] or "",
        sign=prof["sign"],
    )


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


def _history_ok(count, subjects, limit):
    """(ok, message) for submit's 'small set of conventional digests' guard."""
    if count < 0:
        return False, "cannot compare against the integration branch (is it present locally?)"
    if count == 0:
        return False, "no commits over the integration branch — nothing to submit"
    if count > limit:
        return False, (
            f"{count} commits over base (> {limit}) — self-refine into a few conventional "
            "digests before submitting"
        )
    bad = [s for s in subjects if not _CONVENTIONAL.match(s)]
    if bad:
        return False, "non-conventional commit subjects:\n  " + "\n  ".join(bad)
    return True, ""


# ---- verbs ------------------------------------------------------------------

_RIG = typer.Option("", "--rig", "-r", help="target rig (default: cwd's rig)")
_BEAD = typer.Argument(..., metavar="<id>", help="bead id")
_BEAD_OPT = typer.Argument("", metavar="<id>", help="bead id (omit when using --group)")
_AS = typer.Option("", "--as", help="crew/<name> identity (default: config/$WS_CREW/git)")
_GROUP = typer.Option(
    "", "--group", help="batch mode: comma-separated member ids sharing a batch:<group> label"
)


@app.command("brief")
@otel.trace_verb("work.brief")
def brief(bead: str = _BEAD, rig: str = _RIG):
    """Print the bead's requirements/goals and the repo's validation command. Read-only."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    _print_brief(cfg, entry, bead, _show(bead, main))


@app.command("assign")
@otel.trace_verb("work.assign")
def assign(
    bead: str = _BEAD,
    to: str = typer.Option(..., "--to", help="crew/<name> to assign + provision for"),
    rig: str = _RIG,
):
    """Orchestrator-only: stamp the assignee and provision the worktree with that identity.
    Leaves status `open` — the worker's `claim` is the ack that flips it to in_progress."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    _entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    data = _show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, to, bead)
    # EXPERIMENTAL (cit.5): the coordinator->developer dispatch seam. The coordinator agent loop
    # hands this bead to a developer crew — emit it as a GenAI `invoke_agent` span, with the brief
    # carried as a droppable span EVENT (gated no-op when otel is off; see ws.otel).
    brief_text = _first(data, "description")
    with otel.record_agent_dispatch(
        agent=to,
        model=config.otel_genai_model(cfg),
        system=config.otel_genai_system(cfg),
        brief=brief_text,
        attributes={"ws.bead": bead},
    ):
        res = _bd(["assign", bead, to], main)
        if res.returncode != 0:
            raise typer.Exit(res.returncode)
        entry, target, _branch = worktree.ensure(cfg, rig, bead)
        _stamp(cfg, entry, target, to)
    otel.count_bead_transition("assigned")  # bead id rides the span (set_bead), not the metric
    typer.echo(f"✓ assigned {bead} → {to}; worktree {target}")


@app.command("claim")
@otel.trace_verb("work.claim")
def claim(
    bead: str = _BEAD_OPT,
    as_: str = _AS,
    group: str = _GROUP,
    rig: str = _RIG,
):
    """Ack that you're starting: re-attach/provision the worktree with your identity, refuse
    if it's someone else's, then `bd update --claim` as your actor (→ in_progress).

    With `--group <ids>` this is the work-group ack: provision the ONE shared `wt/batch/<group>`
    worktree (members read from their `batch:<group>` labels), stamp it with your identity once,
    and claim every member — one agent owns the whole batch."""
    cfg = config.load()
    group = work_logic.opt_str(group)
    if group:
        if bead:
            typer.echo("✗ pass either <id> or --group, not both", err=True)
            raise typer.Exit(1)
        work_group.claim_group(cfg, rig, group, as_)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> for a batch)", err=True)
        raise typer.Exit(1)
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    data = _show(bead, main)
    _guard_open(data, bead)
    _guard_not_other(data, actor, bead)
    entry, target, _branch = worktree.ensure(cfg, rig, bead)
    _stamp(cfg, entry, target, actor)
    res = _bd(["update", bead, "--claim"], main, actor=actor)
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


@app.command("check")
@otel.trace_verb("work.check")
def check(bead: str = _BEAD, rig: str = _RIG):
    """Run the rig's validation command against the worktree; propagate its exit code."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, _main, target, _branch = worktree.locate(cfg, rig, bead)
    if not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
    if not worktree.in_bead_worktree(target):
        typer.echo(
            f"WARNING: cwd is not the bead worktree — uncommitted edits here are invisible.\n"
            f'  → cd "{target}"  # work happens in the worktree, NOT the main clone',
            err=True,
        )
    # Telemetry-neutral env so `check` agrees with `submit`'s clean-checkout validation regardless
    # of the rig's otel config (the worktree overlay seeds OTEL_* into os.environ otherwise).
    v_start = time.perf_counter()
    rc = run(
        shlex.split(config.validate_cmd(cfg, entry)),
        cwd=str(target),
        check=False,
        env=otel.telemetry_neutral_env(),
    ).returncode
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"ws.work.phase": "check", "ws.validation.result": _vres(rc), "ws.rig": _rig(entry)},
    )
    otel.count_validation(rc == 0, {"ws.work.phase": "check"})
    if rc != 0:
        raise typer.Exit(rc)


@app.command("schedule")
@otel.trace_verb("work.schedule")
def schedule(
    epic: str = typer.Argument(..., metavar="<epic>", help="molecule epic id"),
    rig: str = _RIG,
    as_json: bool = typer.Option(False, "--json", help="emit the plan as JSON"),
):
    """Cost-model dispatch plan for a molecule: which open children to run as ONE grouped agent
    (a planner `batch:<group>` or an auto-detected linear chain) vs as singletons (parallel
    wall-time, the default one-per-worktree). Read-only — surfaces the decision; you still
    `ws work claim --group` / `assign` to act on it. See the coordinator skill for the model."""
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, epic)
    children = _bd_json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        typer.echo(f"✗ cannot list children of {epic} — is it an epic in this rig?", err=True)
        raise typer.Exit(1)
    beads = [c for c in children if str(c.get("status", "")) != "closed"]
    plan = schedule_mod.plan_schedule(beads, max_size=config.batch_max_size(cfg, entry))
    if as_json:
        groups = [{"kind": g.kind, "ids": list(g.ids), "reason": g.reason} for g in plan.groups]
        payload = {"groups": groups, "singletons": plan.singletons}
        typer.echo(json.dumps(payload, indent=2))
        return
    if not plan.groups and not plan.singletons:
        typer.echo("(no open children to schedule)")
        return
    for g in plan.groups:
        typer.echo(f"▸ group [{g.kind}] {', '.join(g.ids)}  — {g.reason}")
    for s in plan.singletons:
        typer.echo(f"· single {s}")


@app.command("submit")
@otel.trace_verb("work.submit")
def submit(bead: str = _BEAD, rig: str = _RIG):
    """Hand off to async review: verify the branch is clean conventional digests, validate the
    proposed hash from a clean checkout, (publish for out-of-process review,) then open a gate.
    Not 'done' — leaves the worktree intact and returns immediately."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, target, branch = worktree.locate(cfg, rig, bead)
    if not target.exists():
        typer.echo(f"✗ no worktree for {bead} — claim it first", err=True)
        raise typer.Exit(1)
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
    base = worktree.molecule_base(entry, bead, config.integration_branch(cfg, entry))
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg}", err=True)
        raise typer.Exit(1)

    # Clean-checkout validation — the result must not depend on dirty local state.
    v_start = time.perf_counter()
    rc = worktree.clean_checkout(entry, branch, config.validate_cmd(cfg, entry))
    otel.record_validation_duration(
        time.perf_counter() - v_start,
        {"ws.work.phase": "submit", "ws.validation.result": _vres(rc), "ws.rig": _rig(entry)},
    )
    otel.count_validation(rc == 0, {"ws.work.phase": "submit"})
    if rc != 0:
        typer.echo(f"✗ clean-checkout validation failed (exit {rc}) — nothing submitted", err=True)
        raise typer.Exit(1)

    sha = worktree.head_sha(target)
    gate = config.review_gate(cfg, entry)
    # Out-of-process reviewers (GitHub CI) can't see a branch we don't push. Push BEFORE
    # set-state so a failed push blocks the gate too (no half-submitted bead).
    if gate.startswith("gh:") and worktree.push_branch(entry, branch) != 0:
        typer.echo("✗ failed to push branch for review — nothing submitted", err=True)
        raise typer.Exit(1)

    # Open the gate FIRST, then flip state — so we never leave a bead review=pending with
    # nothing blocking it (which would let the scheduler re-pick it).
    g = _bd(["gate", "create", "--blocks", bead, "--type", gate, "--reason", f"review {sha}"], main)
    if g.returncode != 0:
        typer.echo("✗ failed to open review gate — nothing submitted", err=True)
        raise typer.Exit(1)
    sres = _bd(["set-state", bead, "review=pending", "--reason", f"submitted {sha}"], main)
    if sres.returncode != 0:
        typer.echo("✗ failed to set review state — nothing submitted", err=True)
        raise typer.Exit(1)
    otel.count_bead_transition("review_pending", {"ws.review.gate": gate})
    typer.echo(f"✓ submitted {bead} @ {sha} — opened {gate} review gate (worktree left intact)")


def _delete_branch(main, branch) -> None:
    """Best-effort delete of a landed molecule branch. The molecule already landed, so a failure
    here only warns (leaving a stale ref the coordinator can drop). GIT_* dir-pointing env is
    scrubbed so our explicit `-C <main>` always wins."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    res = run(["git", "-C", str(main), "branch", "-d", branch], check=False, capture=True, env=env)
    if res.returncode != 0:
        typer.echo(f"⚠ landed but failed to delete {branch} — delete it manually", err=True)


def _merge_molecule(cfg, epic, rig):
    """The molecule wrap-up / land: collapse a whole assembled `mol/<epic>` onto the rig
    integration branch as ONE `--no-ff` bubble (the bead merges live inside it). Guards the
    molecule is complete (every child closed) + clean, holds the rig merge slot, validates the
    assembled branch from a clean checkout, lands it, closes the epic, and deletes the branch.
    On conflict / validation failure it aborts and releases the slot — never drops work."""
    entry, main, _target, _branch = worktree.locate(cfg, rig, epic)
    epic_data = _show(epic, main)
    _guard_open(epic_data, epic)

    mol_branch = f"{worktree.MOL_PREFIX}{epic}"
    if not worktree._branch_exists(main, mol_branch):
        typer.echo(f"✗ no molecule branch {mol_branch} — was {epic} kicked off?", err=True)
        raise typer.Exit(1)

    children = _bd_json(["list", "--parent", epic], main)
    if not isinstance(children, list):
        typer.echo(f"✗ cannot list children of {epic} — refusing to land", err=True)
        raise typer.Exit(1)
    open_kids = [str(c.get("id")) for c in children if str(c.get("status", "")) != "closed"]
    if open_kids:
        typer.echo(
            f"✗ molecule {epic} incomplete — open child issue(s): {', '.join(open_kids)}", err=True
        )
        raise typer.Exit(1)

    if not worktree.is_clean(main):
        typer.echo(f"✗ main clone {main} not clean — cannot land molecule", err=True)
        raise typer.Exit(1)

    base = config.integration_branch(cfg, entry)
    slot_attrs = {"ws.merge.kind": "molecule", "ws.rig": _rig(entry)}
    started = time.perf_counter()
    _bd(["merge-slot", "create"], main)  # idempotent: no-op once the rig's slot bead exists
    slot_mark = time.perf_counter()
    if _bd(["merge-slot", "acquire"], main).returncode != 0:
        typer.echo("✗ could not acquire merge slot — another merge holds it", err=True)
        raise typer.Exit(1)
    slot_acquired = time.perf_counter()
    otel.record_merge_slot_wait(slot_acquired - slot_mark, slot_attrs)
    try:
        # Validate the ASSEMBLED molecule from a clean checkout — the land must not depend on
        # dirty local state, and a red molecule never reaches the integration line.
        v_start = time.perf_counter()
        rc = worktree.clean_checkout(entry, mol_branch, config.validate_cmd(cfg, entry))
        otel.record_validation_duration(
            time.perf_counter() - v_start,
            {"ws.work.phase": "molecule", "ws.validation.result": _vres(rc), "ws.rig": _rig(entry)},
        )
        otel.count_validation(rc == 0, {"ws.work.phase": "molecule"})
        if rc != 0:
            typer.echo(f"✗ molecule validation failed (exit {rc}) — nothing landed", err=True)
            raise typer.Exit(rc)

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
            message=f"merge molecule {epic}",
        )
        if mrc != 0:
            otel.count_merge_outcome({**slot_attrs, "ws.merge.how": "conflict"})
            typer.echo(f"✗ molecule merge failed — aborted, nothing landed:\n{out}", err=True)
            raise typer.Exit(mrc)
        otel.count_merge_outcome({**slot_attrs, "ws.merge.how": "no_ff"})
        if _bd(["close", epic, "--reason", "molecule landed"], main).returncode != 0:
            typer.echo("⚠ landed but failed to close the epic — close it manually", err=True)
        _delete_branch(main, mol_branch)
    finally:
        otel.record_merge_slot_hold(time.perf_counter() - slot_acquired, slot_attrs)
        _bd(["merge-slot", "release"], main)

    otel.record_merge_duration(time.perf_counter() - started, {"ws.merge.kind": "molecule"})
    # Molecule asymmetry: emit cycle_time (+ slot, above) ONLY — never coding/review_wait/rework,
    # which are per-bead concepts. Best-effort, never blocks the land (it already succeeded).
    try:
        _emit_cycle(epic_data, {"ws.merge.kind": "molecule", "ws.rig": _rig(entry)})
    except Exception:  # best-effort: a metric read/parse must never fail a completed land
        pass
    otel.count_bead_transition("molecule_landed")
    typer.echo(f"✓ landed molecule {epic} ({mol_branch} --no-ff → {base}); closed {epic}")


@app.command("merge")
@otel.trace_verb("work.merge")
def merge(
    bead: str = _BEAD_OPT,
    rig: str = _RIG,
    rm: bool = typer.Option(False, "--rm", help="remove the worktree after a clean merge"),
    molecule: bool = typer.Option(
        False, "--molecule", help="land the whole molecule mol/<epic> (arg is the epic id)"
    ),
    group: str = _GROUP,
):
    """Merger-only: serialize integration of an *approved* bead onto the integration branch.
    Holds the rig merge slot, re-verifies a small clean conventional history, merges `--no-ff`
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
        work_group.merge_group(cfg, group, rig, rm)
        return
    if not bead:
        typer.echo("✗ pass a bead <id> (or --group <ids> / --molecule <epic>)", err=True)
        raise typer.Exit(1)
    otel.set_bead(bead)  # ws.bead/ws.epic on this verb span (bead is the epic when --molecule)
    if molecule:
        _merge_molecule(cfg, bead, rig)
        return
    started = time.perf_counter()
    entry, main, target, branch = worktree.locate(cfg, rig, bead)
    bead_data = _show(bead, main)  # reused for the at-merge cycle/stage flow metrics below
    _guard_open(bead_data, bead)

    if _state(bead, "review", main) == "changes-requested":
        typer.echo(f"✗ {bead} has changes-requested — resume & resubmit, don't merge", err=True)
        raise typer.Exit(1)
    if _open_gate(bead, main):
        typer.echo(f"✗ {bead} review gate still open — not approved yet", err=True)
        raise typer.Exit(1)

    base = worktree.molecule_base(entry, bead, config.integration_branch(cfg, entry))
    count, subjects = worktree.history(entry, branch, base)
    ok, msg = _history_ok(count, subjects, config.max_commits(cfg, entry))
    if not ok:
        typer.echo(f"✗ {msg} — bounce back for self-refine", err=True)
        raise typer.Exit(1)

    slot_attrs = {"ws.merge.kind": "bead", "ws.rig": _rig(entry)}
    _bd(["merge-slot", "create"], main)  # idempotent: no-op once the rig's slot bead exists
    slot_mark = time.perf_counter()
    if _bd(["merge-slot", "acquire"], main).returncode != 0:
        typer.echo("✗ could not acquire merge slot — another merge holds it", err=True)
        raise typer.Exit(1)
    slot_acquired = time.perf_counter()
    otel.record_merge_slot_wait(slot_acquired - slot_mark, slot_attrs)
    try:
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
            message=f"merge {bead}",
            union_globs=tuple(config.union_globs(cfg, entry)),
            validate_cmd=config.validate_cmd(cfg, entry),
        )
        if rc != 0:
            otel.count_merge_outcome({**slot_attrs, "ws.merge.how": "conflict"})
            typer.echo(
                f"✗ real conflict merging {bead} — rebase retry failed, bead branch restored; "
                f"bounce it back for rework:\n{out}",
                err=True,
            )
            raise typer.Exit(rc)
        otel.count_merge_outcome({**slot_attrs, "ws.merge.how": how})
        if _bd(["close", bead, "--reason", "merged"], main).returncode != 0:
            typer.echo("⚠ merged but failed to close the bead — close it manually", err=True)
    finally:
        otel.record_merge_slot_hold(time.perf_counter() - slot_acquired, slot_attrs)
        _bd(["merge-slot", "release"], main)

    otel.record_merge_duration(
        time.perf_counter() - started, {"ws.merge.kind": "bead", "ws.merge.how": how}
    )
    # At-merge cycle/stage/rework from bd — best-effort + skew-guarded; the bead already merged, so
    # a slow/failing read or a negative delta must never turn a successful land into a failure.
    try:
        _emit_bead_flow(bead, bead_data, main, {"ws.merge.kind": "bead", "ws.rig": _rig(entry)})
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
        worktree.remove(rig, bead, force=True)


@app.command("resume")
@otel.trace_verb("work.resume")
def resume(
    bead: str = _BEAD,
    as_: str = _AS,
    rig: str = _RIG,
):
    """After review returns changes-requested: re-attach a fresh worktree on the bead branch,
    print the feedback, and re-assert the claim. Address the feedback and `submit` again."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, _target, _branch = worktree.locate(cfg, rig, bead)
    state = _state(bead, "review", main)
    if state != "changes-requested":
        typer.echo(f"✗ {bead} not in review:changes-requested (now: {state or 'none'})", err=True)
        raise typer.Exit(1)
    entry, target, _branch = worktree.ensure(cfg, rig, bead)
    actor = identity.resolve_actor(as_, config.work_identity(cfg, entry)["name"] or "")
    _stamp(cfg, entry, target, actor)
    typer.echo("── review feedback ──")
    _bd(["comments", bead], main)
    _bd(["update", bead, "--claim"], main, actor=actor)
    typer.echo(f"✓ resumed {bead} as {actor}; worktree {target}")


@app.command("abandon")
@otel.trace_verb("work.abandon")
def abandon(
    bead: str = _BEAD,
    rig: str = _RIG,
    rm: bool = typer.Option(False, "--rm", help="also remove the worktree (default: keep it)"),
):
    """Release the claim and record the abandon. Recovery path for stalls."""
    otel.set_bead(bead)  # stamp ws.bead/ws.epic on this verb span
    cfg = config.load()
    entry, main, target, _branch = worktree.locate(cfg, rig, bead)
    actor = identity.resolve_actor("", config.work_identity(cfg, entry)["name"] or "")
    # Recovery path: deliberately no refuse-if-other guard (the point is to release a bead a
    # stalled/dead agent left claimed). Surface bd failures instead of always reporting success.
    r1 = _bd(["set-state", bead, "review=abandoned", "--reason", "abandoned"], main, actor=actor)
    r2 = _bd(["update", bead, "--status", "open", "--assignee", ""], main, actor=actor)
    if rm and target.exists():
        worktree.remove(rig, bead, force=True)
    if r1.returncode or r2.returncode:
        typer.echo(f"⚠ abandoned {bead} with bd errors (see above)", err=True)
        raise typer.Exit(1)
    otel.count_bead_transition("abandoned")  # bead id rides the span (set_bead), not the metric
    typer.echo(f"✓ abandoned {bead}" + ("; worktree removed" if rm else "; worktree kept"))


# ---- show / review (read-only render verbs; bodies live in work_show) -------
# Registered onto this app from work_show so the rendering surface sits in one file while the
# command names stay `ws work show` / `ws work review`. Re-bound here (show = …) so existing
# callers/tests that invoke `work.show(...)` / `work.review(...)` keep working.

show = app.command("show")(work_show.show)
review = app.command("review")(work_show.review)


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
    rig: str,
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
    entry, _main, target, branch = worktree.locate(cfg, rig, bead)
    if sum([bool(plan), autosquash, bool(since)]) != 1:
        raise WorkError(["✗ pass exactly one of --plan / --autosquash / --since"])
    if not target.exists():
        raise WorkError([f"✗ no worktree for {bead} — claim it first"])
    base = worktree.base_of(
        entry, branch, worktree.molecule_base(entry, bead, config.integration_branch(cfg, entry))
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
def refine(
    bead: str = _BEAD,
    plan: str = typer.Option("", "--plan", help="squash-plan JSON file or '-' for stdin"),
    autosquash: bool = typer.Option(False, "--autosquash", help="fold fixup!/squash! into targets"),
    since: str = typer.Option("", "--since", help="fold <ref>..tip into a single digest"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the would-be log; change nothing"),
    rig: str = _RIG,
):
    """Squash local checkpoint noise into conventional digests behind a backup branch and a
    byte-identical gate (the net tree never changes). Retains per-digest author dates. Exactly
    one input mode: --plan | --autosquash | --since."""
    cfg = config.load()
    try:
        result = refine_branch(
            cfg, rig=rig, bead=bead, plan=plan, autosquash=autosquash, since=since, dry_run=dry_run
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
