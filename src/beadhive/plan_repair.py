"""`ws plan repair <epic>` — idempotent backfill for a hand-assembled molecule.

An epic assembled by hand (`bd create --type=epic` + `bd dep add <child> <epic> -t parent-child`
over pre-existing beads) or by re-parenting children out of another molecule misses the
planning-plane plumbing `bh plan file` establishes by construction — the bd swarm, the kickoff
gate on each root, the kickoff state, the identity-triplet labels on children — so `bh work
start` correctly refuses the seat. Before this verb, the only fix was reverse-engineering
`plan.file_molecule` to learn the gate contract and hand-running the gated `bd` passthrough.

`repair` converges such an epic: it backfills everything `plan.verify_epic` checks, through the
SAME shared helpers `file_molecule` uses (`plan._create_swarm` / `plan._create_kickoff_gate` /
`plan._set_kickoff_pending` — the single authoritative code path for the kickoff-gate
description contract, so the format cannot drift), then re-runs verify and prints what it fixed.
Idempotent: re-running on a convention-clean molecule is a clean no-op.

Root selection reuses the verify-side filters exactly (`plan._ungated_roots`): origin-report
children (adopt.is_origin_report) are held out of the sibling set by `plan._epic_molecule` —
they are neither gated nor counted as roots — and a satisfied root (all blocking predecessors
merged) needs no fresh gate.

Lives in its own module mounted onto `plan.app` (bottom of plan.py) so the verb doesn't push
plan.py further past its size budget (bh-62rm tracks the wider extraction). Imports of `plan`
are function-local to keep the plan → plan_repair mount cycle-safe in either import order
(mirrors work.py's plan seam).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import typer

from . import bd, config, otel, registry, validate
from .identity import resolve_actor, workspace_identity

_HIVE = typer.Option("", "--hive", "-r", help="target hive (default: cwd's hive)")

# The per-child identity-triplet label fields verify demands (plan._check_child_labels) and
# repair backfills. `bd label add` takes ONE label per call, so backfill loops per missing field
# (a space-separated label list silently degrades to per-issue-id parsing errors).
_IDENTITY_FIELDS = ("provider", "org", "repo")


@dataclass
class RepairResult:
    """What repair changed (`fixes`; empty ⇒ clean no-op) and what still fails verify
    (`problems`; empty ⇒ molecule conventions satisfied)."""

    fixes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _repair_swarm(epic_id: str, cwd, actor: str, fixes: list[str]) -> None:
    """Backfill the bd swarm over the epic when missing."""
    from . import plan  # function-local: cycle-safe with plan's bottom mount

    missing = plan._swarm_missing(epic_id, cwd)
    if missing is None:
        raise plan.PlanError(f"could not retrieve swarm list for {epic_id} — inspect the hive")
    if missing:
        if not plan._create_swarm(epic_id, cwd, actor):
            raise plan.PlanError(f"`bd swarm create {epic_id}` failed — inspect the hive")
        fixes.append(f"created bd swarm for {epic_id}")


def _repair_kickoff_gates(epic_id: str, issues: list[dict], cwd, actor: str, fixes: list[str]):
    """Open a kickoff gate for each GENUINE ungated root, via the shared gate contract."""
    from . import plan

    ungated = plan._ungated_roots(epic_id, issues, cwd)
    if ungated is None:
        raise plan.PlanError(f"could not retrieve gate list for {epic_id} — inspect the hive")
    for root_id in ungated:
        plan._create_kickoff_gate(root_id, epic_id, cwd, actor)
        fixes.append(f"created kickoff gate for root {root_id}")


def _repair_kickoff_state(epic_id: str, cwd, actor: str, fixes: list[str]) -> None:
    """Set kickoff=pending when the state dimension is unset (pending/approved are left alone)."""
    from . import plan

    if not bd.state(epic_id, "kickoff", cwd):
        plan._set_kickoff_pending(epic_id, cwd, actor)
        fixes.append(f"set kickoff=pending on {epic_id}")


def _repair_identity_labels(issues: list[dict], cwd, actor: str, fixes: list[str]) -> None:
    """Backfill each child's missing provider/org/repo identity labels — one `bd label add` per
    missing field per child (bd rejects multi-label calls)."""
    from . import plan

    ident = workspace_identity(cwd)
    if ident is None:
        return  # outside a managed workspace path — verify will surface the missing labels
    for issue in issues:
        child_id = issue["handle"]
        labels = issue.get("labels") or []
        for fld, value in zip(_IDENTITY_FIELDS, ident, strict=True):
            if validate._label_val(labels, f"{fld}:"):
                continue
            label = f"{fld}:{value}"
            if bd.run(["label", "add", child_id, label], cwd, actor=actor).returncode != 0:
                raise plan.PlanError(f"`bd label add {child_id} {label}` failed — inspect the hive")
            fixes.append(f"added label {label} to {child_id}")


def repair_epic(epic_id: str, cfg, cwd, actor: str) -> RepairResult:
    """Backfill everything `plan.verify_epic` checks on a filed-but-malformed epic, then re-run
    verify. Typer-free; raises plan.PlanError when a read fails or a backfill write is refused.
    Idempotent — a convention-clean molecule yields RepairResult([], [])."""
    from . import plan

    loaded = plan._epic_molecule(epic_id, cwd)
    if loaded is None:
        raise plan.PlanError(
            f"could not retrieve epic {epic_id} or its children — does it exist in this hive?"
        )
    epic_data, issues, _origin_reports = loaded
    type_problems = plan._check_epic_type(epic_data, epic_id)
    if type_problems:
        raise plan.PlanError(f"{type_problems[0]} — repair backfills molecule plumbing, not types")

    fixes: list[str] = []
    _repair_swarm(epic_id, cwd, actor, fixes)
    _repair_kickoff_gates(epic_id, issues, cwd, actor, fixes)
    _repair_kickoff_state(epic_id, cwd, actor, fixes)
    _repair_identity_labels(issues, cwd, actor, fixes)
    return RepairResult(fixes=fixes, problems=plan.verify_epic(epic_id, cfg, cwd))


@otel.trace_verb("plan.repair")
def repair(
    epic: str = typer.Argument(..., metavar="<epic>", help="filed epic id to repair"),
    hive: str = _HIVE,
):
    """Idempotently backfill a hand-assembled epic to the molecule conventions `verify` checks:
    create the bd swarm if missing, open kickoff gates for GENUINE ungated roots (through the
    same shared code path as `file`, so the gate description cannot drift), set kickoff=pending
    when unset, and backfill missing provider/org/repo identity labels on children. Re-runs
    verify and prints what it fixed; re-running on a clean molecule is a no-op. This is the fix
    for the `work start` / `plan approve` convention refusal on an epic assembled without
    `plan file`."""
    from . import plan

    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    actor = resolve_actor("", "", cwd=cwd)
    try:
        result = repair_epic(epic, cfg, cwd, actor)
    except plan.PlanError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None
    for fix in result.fixes:
        typer.echo(f"  + {fix}")
    if result.problems:
        for problem in result.problems:
            typer.echo(f"  - {problem}", err=True)
        typer.echo(
            f"✗ {epic}: applied {len(result.fixes)} fix(es) but {len(result.problems)} "
            f"problem(s) remain (above) — these need a planner, not a backfill",
            err=True,
        )
        raise typer.Exit(1)
    if result.fixes:
        typer.echo(
            f"✓ repaired {epic}: {len(result.fixes)} fix(es) — molecule conventions satisfied"
        )
    else:
        typer.echo(f"✓ {epic} already convention-clean — nothing to repair")
