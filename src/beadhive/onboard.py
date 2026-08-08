"""onboard.py — onboarding step/check framework + two-phase preflight gate.

Models ``ws hive onboard``/``init`` as a small DAG of **steps**, each declaring **preflight
checks** tied to its layer. ``run_onboard`` evaluates every statically-evaluable check up
front and fails as a **batch** — so onboarding never starts mutating and then has to roll
back — then executes the enabled steps in topological order.

This module holds the tiny reusable core (this bead):
  - ``Check``       — a read-only ``(ok, detail)`` predicate with an id + overridable flag.
  - ``CheckResult`` — the recorded outcome of evaluating a ``Check``.
  - ``Step``        — a DAG node: id, requires (edges), mutates, enabled, checks, action.
  - ``OnboardPlan`` — the structured outcome; tests assert on it, not on stdout (retire pattern).
  - ``Ctx``         — the context threaded through every ``check.fn`` / ``step.action``.
  - ``run_onboard(ctx, *, dry_run, skip_checks)`` — the two-phase executor.

Modelled on ``retire.py``'s phased pattern and ``hive_ready``'s ``Check`` NamedTuple. It is
onboarding-specific by design — NOT a generic workflow engine (retire keeps its own flow).

Two-phase execution
-------------------
- **Phase A — preflight (read-only, batch fast-fail).** Evaluate each step's applicable
  checks. The DAG's single *preflight* step (the clone/acquire) is the carve-out: its own
  checks gate as a batch, its action runs, and it opens a second batch for the now-present
  repo's checks. Any non-overridable — or non-skipped overridable — failure prints EVERY
  failure in the batch and raises ``typer.Exit`` before any further mutation.
- **Phase B — execute.** Run the remaining enabled steps' actions in topological order.
  ``--dry-run`` skips mutating actions (read-only assessment actions still run) but returns a
  fully-populated ``OnboardPlan``. An overridable failure whose id is in ``skip_checks`` is
  downgraded to a ``⚠`` warning and recorded in ``OnboardPlan.skipped_checks``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import typer

from . import plugins as _plugins
from . import registry, safety, store_locator
from .storage_migrate import SHARED_SERVER_CONFIG_KEY, SHARED_SERVER_FLAG

# typer glyphs (house style, cf. hive_ready._GLYPH): pass / fail / downgraded / info.
_GLYPH_OK = "✓"
_GLYPH_FAIL = "✗"
_GLYPH_WARN = "⚠"
_GLYPH_INFO = "•"


class Check(NamedTuple):
    """A read-only preflight predicate.

    ``fn(ctx) -> (ok, detail)`` must be pure/read-only. ``overridable=False`` marks an
    invariant that ``--skip-check`` can never bypass (e.g. excluded, prefix-policy).
    ``applies(ctx)`` gates evaluation (e.g. dirty-tree only for an existing folder).
    """

    id: str
    label: str
    overridable: bool
    fn: Callable[[Any], tuple[bool, str]]
    applies: Callable[[Any], bool] = lambda c: True


@dataclass
class CheckResult:
    """Recorded outcome of evaluating one ``Check`` (mirrors the printed line)."""

    id: str
    label: str
    ok: bool
    detail: str
    overridable: bool
    skipped: bool = False  # overridable failure downgraded to a warning via skip_checks

    @property
    def glyph(self) -> str:
        if self.ok:
            return _GLYPH_OK
        return _GLYPH_WARN if self.skipped else _GLYPH_FAIL


@dataclass
class Step:
    """A DAG node. ``requires`` are predecessor step ids (the edges); ``enabled(ctx)``
    flag-gates the step (e.g. ``--claude``); ``action(ctx)`` performs the work.

    ``preflight=True`` marks the single acquire step (clone) whose action runs *during*
    Phase A — it creates rather than modifies, so there is nothing to roll back — splitting
    the preflight batch into pre-acquire and repo-level halves.
    """

    id: str
    label: str
    action: Callable[[Any], None]
    requires: list[str] = field(default_factory=list)
    mutates: bool = False
    checks: list[Check] = field(default_factory=list)
    enabled: Callable[[Any], bool] = lambda c: True
    preflight: bool = False


@dataclass
class OnboardPlan:
    """Structured outcome of ``run_onboard`` — mirrors the printed summary so callers/tests
    assert on the object, never on stdout (the ``retire.RetirePlan`` pattern)."""

    hive: str
    target: str
    dry_run: bool
    cloned: bool = False
    checks: list[CheckResult] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)
    registered: bool = False
    installers_run: list[str] = field(default_factory=list)
    hub_synced: bool = False
    warnings: list[str] = field(default_factory=list)  # fenced step failures (non-fatal)


@dataclass
class Ctx:
    """Context threaded through every ``check.fn`` and ``step.action``.

    The engine needs only ``hive``/``target``/``steps`` plus the mutable ``cloned``/``plan``
    slots it maintains. The concrete onboard DAG (``build_steps``) additionally reads the
    identity triplet, the installer flags, and the config — and memoizes the derived
    kind/prefix/upstream (``_ensure_derived``) so checks and actions agree on one derivation.
    """

    hive: str
    target: str
    steps: list[Step] = field(default_factory=list)
    cloned: bool = False
    plan: OnboardPlan | None = None

    # ---- concrete onboard inputs (built by hive.onboard/hive.init) ----
    provider: str = ""
    org: str = ""
    repo: str = ""
    clone_url: str = ""
    cwd: str | None = None  # target hive dir threaded to installers (None = process cwd)
    cfg: Any = None
    # Declared footprint: None = not passed (resolve from installer flags → registry entry →
    # default zero-footprint); _ensure_derived collapses this to a concrete bool.
    furnish: bool | None = None
    claude: bool = False
    skills: bool = False
    observaloop: bool = False
    agents: bool = False
    opencode: bool = False
    plugins: list[str] = field(default_factory=list)  # plugin names forced on via --plugin
    force: bool = False
    yes: bool = False
    kind: str = ""
    prefix: str = ""
    # tri-state (bh-d5jhc.1): None = default — sync THIS hive synchronously, background the
    # fleet-wide aggregation walk; True = wait for the full `hub.sync()` synchronously (explicit
    # `--hub-sync`, the pre-bh-d5jhc.1 behavior); False = skip the hub step entirely (`hive
    # init`'s default, or explicit `--no-hub-sync`).
    hub_sync: bool | None = False

    # ---- derived once by _ensure_derived, read by checks + actions ----
    existing: Any = None
    upstream: str = ""
    classification: str = ""
    prefix_override: bool = False
    kind_override: bool = False
    reconfigure: bool = False
    furnish_explicit: bool = False  # declared this invocation (flag or installer) vs sticky
    _derived: bool = False

    @property
    def base(self) -> Path:
        """The hive dir installers/checks operate on (``cwd`` when threaded, else process cwd)."""
        return Path(self.cwd) if self.cwd else Path(".")

    @property
    def target_exists(self) -> bool:
        return Path(self.target).exists()


def _topo_order(steps: Sequence[Step]) -> list[Step]:
    """Kahn topological sort over ``Step.requires`` (same shape as ``molecule._topo_order``).

    Only counts ``requires`` edges to steps that are *present* in this set, so filtering out
    disabled steps never deadlocks the sort. Raises ``ValueError`` on a dependency cycle.
    """
    by_id = {s.id: s for s in steps}
    indegree = {s.id: sum(1 for r in s.requires if r in by_id) for s in steps}
    ready = [s for s in steps if indegree[s.id] == 0]
    out: list[Step] = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for s in steps:  # any step requiring cur loses an in-edge
            if cur.id in s.requires:
                indegree[s.id] -= 1
                if indegree[s.id] == 0:
                    ready.append(by_id[s.id])
    if len(out) != len(steps):
        raise ValueError("onboard steps contain a dependency cycle")
    return out


def _gate(batch: list[CheckResult], plan: OnboardPlan) -> None:
    """Record a preflight batch onto the plan and fast-fail as a group.

    Every result in ``batch`` is appended to ``plan.checks``; downgraded (skipped) overridable
    failures are recorded in ``plan.skipped_checks``. If any HARD failure remains (a failure
    that was not downgraded — always the case for non-overridable checks), print EVERY failure
    and raise ``typer.Exit(1)`` before the caller runs any further mutation.
    """
    failures = _record_batch(batch, plan)
    if not failures:
        return
    _print_failures(failures)
    raise typer.Exit(1)


def _record_batch(batch: list[CheckResult], plan: OnboardPlan) -> list[CheckResult]:
    """Append every result onto the plan, track skipped checks, and return HARD failures."""
    for res in batch:
        plan.checks.append(res)
        if res.skipped:
            plan.skipped_checks.append(res.id)
    return [r for r in batch if not r.ok and not r.skipped]


def _print_failures(failures: list[CheckResult]) -> None:
    """Print every failure in the batch plus an ``--skip-check`` hint for overridable ones."""
    typer.echo("✗ onboarding preflight failed:", err=True)
    for r in failures:
        typer.echo(f"  {_GLYPH_FAIL} {r.id}: {r.detail}", err=True)
    overridable = [r.id for r in failures if r.overridable]
    if overridable:
        typer.echo(f"  override with --skip-check {','.join(overridable)}", err=True)


def _evaluate(step: Step, ctx: Ctx, skip: set[str], batch: list[CheckResult]) -> None:
    """Append this step's applicable check results to the current preflight batch."""
    for chk in step.checks:
        if not chk.applies(ctx):
            continue
        ok, detail = chk.fn(ctx)
        skipped = (not ok) and chk.overridable and chk.id in skip
        batch.append(CheckResult(chk.id, chk.label, ok, detail, chk.overridable, skipped))


def _run_action(step: Step, ctx: Ctx, dry_run: bool) -> bool:
    """Execute a step's action. Returns True iff the action actually ran.

    A *mutating* step's action is skipped under ``dry_run``; read-only assessment actions
    still run. ``plan.steps_run`` is the topological plan (recorded up front), so a skipped
    mutating step still appears there — the retire ``removed``-in-dry-run idiom.
    """
    if step.mutates and dry_run:
        return False
    step.action(ctx)
    return True


def run_onboard(ctx: Ctx, *, dry_run: bool = False, skip_checks: Iterable[str] = ()) -> OnboardPlan:
    """Two-phase onboarding: batch preflight (fast-fail), then topological execute.

    Phase A evaluates every applicable check as a batch and refuses (printing ALL failures)
    before any mutation; the sole ``preflight`` step (clone) is the carve-out that runs
    mid-preflight so the repo-level checks can inspect the now-present repo. Phase B runs the
    remaining enabled steps' actions in topological order. ``dry_run`` skips mutating actions
    but still returns a fully-populated ``OnboardPlan``. An overridable failure whose id is in
    ``skip_checks`` is downgraded to a warning; non-overridable failures never bypass.

    Raises ``typer.Exit`` on a refused preflight gate.
    """
    skip = set(skip_checks)
    plan = OnboardPlan(hive=ctx.hive, target=ctx.target, dry_run=dry_run)
    ctx.plan = plan
    ctx.cloned = False

    ordered = _topo_order([s for s in ctx.steps if s.enabled(ctx)])
    plan.steps_run = [s.id for s in ordered]  # the topological plan (retire dry-run idiom)

    # ---- Phase A: preflight (batched, with the clone/acquire carve-out) ----
    batch: list[CheckResult] = []
    phase_b: list[Step] = []
    for step in ordered:
        _evaluate(step, ctx, skip, batch)
        if step.preflight:
            _gate(batch, plan)  # gate the pre-acquire batch before the acquire mutation
            batch = []
            if _run_action(step, ctx, dry_run):
                ctx.cloned = True
        else:
            phase_b.append(step)
    _gate(batch, plan)  # gate the repo-level batch before Phase B

    plan.cloned = ctx.cloned

    # ---- Phase B: execute the remaining enabled steps in topological order ----
    for step in phase_b:
        _run_action(step, ctx, dry_run)

    _render(plan)
    return plan


def _render(plan: OnboardPlan) -> None:
    """Print the preflight results + executed steps (tests assert on the plan, not this)."""
    tag = "DRY-RUN " if plan.dry_run else ""
    typer.echo(f"{tag}onboard {plan.target}")
    for res in plan.checks:
        # Render the check id (targetable by --skip-check) + human label + detail.
        detail = f"  {res.detail}" if res.detail else ""
        typer.echo(f"  {res.glyph} {res.id} ({res.label}){detail}")
    for sid in plan.steps_run:
        verb = "would run" if plan.dry_run else "ran"
        typer.echo(f"  {_GLYPH_INFO} {verb} {sid}")
    for warning in plan.warnings:
        # Fenced step failures: summarized here so they survive the step-by-step scroll,
        # but never fail the onboard (exit stays 0 for this class of failure).
        typer.echo(f"  ⚠ {warning}")


# ===========================================================================
# The concrete onboard DAG — steps + per-step preflight checks (bead .3)
# ===========================================================================
#
# Steps reuse the existing hive.py helpers + registry/safety/hub; no logic is
# reimplemented here. Derivation (existing lookup → classify → kind/prefix/upstream →
# reconfigure, with the reinit diagnostics) is done ONCE in _ensure_derived, matching
# hive.init's assessment block verbatim, so checks and actions agree on one outcome.


def _ensure_derived(ctx: Ctx) -> None:
    """Resolve kind/prefix/upstream/reconfigure once (idempotent), mirroring hive.init's
    assessment (existing-vs-fresh/force branching + the /a12 diagnostics).

    Read-only w.r.t. the repo/registry — it only classifies, derives, and prints the same
    notes hive.init did; the actual register()/bd-init happen later in Phase B. Excluded/fork
    are NOT raised here (the not-excluded / fork-needs-yes checks own the gate)."""
    if ctx._derived:
        return
    ctx._derived = True

    cfg = ctx.cfg
    provider, org, repo = ctx.provider, ctx.org, ctx.repo
    existing = registry.find_entry(cfg, provider, org, repo)
    ctx.existing = existing
    ctx.prefix_override = bool(ctx.prefix)
    ctx.kind_override = bool(ctx.kind)

    _resolve_kind_prefix_upstream(ctx, cfg, provider, org, repo, existing)
    _note_prefix_drift(ctx, cfg, provider, org, repo, existing)

    ctx.reconfigure = existing is None or ctx.force or ctx.prefix_override or ctx.kind_override

    _resolve_furnish(ctx, existing)


def _resolve_kind_prefix_upstream(
    ctx: Ctx, cfg: Any, provider: str, org: str, repo: str, existing: Any
) -> None:
    """Resolve ``ctx.kind``/``ctx.prefix``/``ctx.upstream`` once.

    Preserve path (registered + not ``--force``): start from the recorded entry, apply only
    explicit overrides. Otherwise classify + derive from scratch, mirroring hive.init's
    assessment branching.
    """
    if existing is not None and not ctx.force:
        # Preserve path: start from the recorded entry, apply only explicit overrides.
        ctx.prefix = ctx.prefix or str(existing["prefix"])
        ctx.kind = ctx.kind or str(existing["kind"])
        ctx.upstream = str(existing.get("upstream", "") or "")
    else:
        # Fresh hive, or --force: classify + derive from scratch.
        if ctx.kind == "external":
            # Explicit --kind external: the passed triplet IS the fork target, so upstream is
            # deterministic — no classify()/gh probe needed to know it (and none should run
            # before a --dry-run's plan is final).
            ctx.classification = ""
            ctx.upstream = ctx.upstream or f"{org}/{repo}"
        else:
            cls = registry.classify(provider, org, repo, cfg)
            ctx.classification = cls
            if cls == "org-native":
                ctx.kind = ctx.kind or "org-native"
            elif cls.startswith("fork upstream="):
                ctx.upstream = cls[len("fork upstream=") :]
                ctx.kind = ctx.kind or "fork"
            elif cls != "excluded":
                # A repo outside $GIT_WORKSPACE (or missing a lock upstream) still classifies as
                # a fork when its local `upstream` remote differs from `origin` (bh-rax6).
                local_up = _distinct_upstream(ctx.base) if ctx.target_exists else ""
                if local_up:
                    ctx.upstream = ctx.upstream or local_up
                    ctx.kind = ctx.kind or "fork"
                else:
                    ctx.kind = ctx.kind or "prototype"
        if existing is not None:
            # --force on a registered hive keeps the registered prefix:
            # re-registering under a re-derived prefix would orphan every existing bead ID.
            ctx.prefix = ctx.prefix or str(existing["prefix"])
        if not ctx.prefix:
            ctx.prefix, warns = registry.derive_prefix(provider, org, repo, ctx.kind, cfg)
            for w in warns:
                typer.echo(w, err=True)


def _note_prefix_drift(
    ctx: Ctx, cfg: Any, provider: str, org: str, repo: str, existing: Any
) -> None:
    """Warn (non-fatal) when the registered prefix no longer matches what re-deriving now
    would produce — the registered prefix always wins unless ``--prefix`` overrides it."""
    if existing is not None and not ctx.prefix_override:
        derived, _ = registry.derive_prefix(provider, org, repo, ctx.kind, cfg)
        if derived != ctx.prefix:
            typer.echo(
                f"note: derived prefix '{derived}' differs from the registered prefix "
                f"'{ctx.prefix}' — keeping the registered one (use --prefix <p> --yes "
                "to change it)",
                err=True,
            )


def _resolve_furnish(ctx: Ctx, existing: Any) -> None:
    """Resolve ``ctx.furnish`` (declared footprint) once.

    Furnishing (tracked scaffolding + a scaffold commit) is a conscious opt-in: explicit
    --furnish/--no-furnish wins; a tracked-furniture installer flag IS the declaration;
    otherwise the registry entry's declared state is sticky; default zero-footprint.
    """
    ctx.furnish_explicit = (
        ctx.furnish is not None or ctx.claude or ctx.agents or ctx.skills or ctx.opencode
    )
    if ctx.furnish is None:
        if ctx.claude or ctx.agents or ctx.skills or ctx.opencode:
            ctx.furnish = True
        else:
            ctx.furnish = existing is not None and registry.furnish_of(existing) == "full"
    if ctx.furnish and not ctx.furnish_explicit:
        # Sticky furnish from the registry: the ownership decision was made when it was
        # declared — only re-verify EXTERNALITY (cheap, local) and downgrade with a note
        # instead of bricking the re-onboard. Explicit/implied declarations are hard-gated
        # by the furnish-allowed preflight check instead.
        reason = _external_reason(ctx)
        if reason:
            typer.echo(f"note: furnish downgraded to zero-footprint — {reason}", err=True)
            ctx.furnish = False
    if (
        existing is not None
        and ctx.furnish_explicit
        and ((registry.furnish_of(existing) == "full") != bool(ctx.furnish))
    ):
        ctx.reconfigure = True  # persist the changed footprint declaration


def _external_reason(ctx: Ctx) -> str:
    """Why this repo counts as EXTERNAL (never furnished): fork kind, or a distinct
    `upstream` remote — tracked scaffolding would pollute a repo with an external upstream.
    '' when the repo is not external."""
    upstream = ctx.upstream or (_distinct_upstream(ctx.base) if ctx.target_exists else "")
    if ctx.kind in ("fork", "external") or upstream:
        suffix = f" of {upstream}" if upstream else ""
        return f"{ctx.hive} is external{suffix} — external hives are never furnished"
    return ""


# ---- checks (pure, read-only (ok, detail) predicates) ----------------------


def _chk_valid_triplet(ctx: Ctx) -> tuple[bool, str]:
    parts = ctx.hive.split("/")
    ok = len(parts) == 3 and all(parts)
    return ok, ctx.hive if ok else f"expected a provider/org/repo triplet, got '{ctx.hive}'"


def _chk_clone_url_present(ctx: Ctx) -> tuple[bool, str]:
    ok = bool(ctx.clone_url)
    return ok, ctx.clone_url if ok else f"{ctx.target} absent — pass --clone-url to clone it"


def _chk_clone_url_reachable(ctx: Ctx) -> tuple[bool, str]:
    # ponytail: a live `git ls-remote` reachability probe is a tracked follow-up (optional,
    # network). The id is surfaced in --dry-run + targetable by --skip-check; today it never
    # blocks onboarding on a transient network condition.
    return True, "not probed (reachability deferred)"


def _chk_parent_writable(ctx: Ctx) -> tuple[bool, str]:
    parent = Path(ctx.target).parent
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    ok = os.access(probe, os.W_OK)
    return ok, str(parent) if ok else f"{probe} is not writable"


def _chk_under_git_workspace(ctx: Ctx) -> tuple[bool, str]:
    from . import hive  # via hive so it honors the same workspace_identity binding hive uses

    ident = hive.workspace_identity(cwd=ctx.cwd)
    ok = ident is not None
    return ok, "under $GIT_WORKSPACE" if ok else "not in a git repo under $GIT_WORKSPACE"


def _chk_not_excluded(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    ok = ctx.classification != "excluded"
    return ok, "not excluded" if ok else f"{ctx.hive} is excluded by the registry"


def _distinct_upstream(base: Path) -> str:
    """The `upstream` git remote's `owner/repo` slug when it differs from `origin` — an INDEPENDENT
    fork signal (a fork always carries an upstream remote) that never depends on classify resolving
    kind=fork (bh-4k3w/bh-djx2/bh-rax6). '' when there is no distinct upstream, or git is
    unreadable."""
    from . import gitworkspace, hive  # via hive.run so it honors the same patched run seam

    def _get(remote: str) -> str:
        res = hive.run(
            ["git", "-C", str(base), "remote", "get-url", remote], check=False, capture=True
        )
        if getattr(res, "returncode", 1) != 0:
            return ""
        return (getattr(res, "stdout", "") or "").strip()

    up_slug = gitworkspace.url_slug(_get("upstream"))
    if not up_slug or up_slug == gitworkspace.url_slug(_get("origin")):
        return ""
    return up_slug


def _chk_fork_needs_yes(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    # A fork is a fork whether or not classify resolved kind=fork: an `upstream` remote distinct
    # from `origin` is a first-class, gh-free signal (bh-4k3w).
    upstream = ctx.upstream or _distinct_upstream(ctx.base)
    is_fork = ctx.kind in ("fork", "external") or bool(upstream)
    blocked = is_fork and not ctx.yes
    if not blocked:
        return True, "ok"
    suffix = f" of {upstream}" if upstream else ""
    return False, f"{ctx.hive} is a fork{suffix} — pass --yes to track it (beads is OFF by default)"


def _chk_external_no_furnish(ctx: Ctx) -> tuple[bool, str]:
    """A furnish declared THIS invocation (explicit --furnish or an installer flag) is refused
    outright on an external hive — never overridable, unlike the sticky-registry downgrade."""
    _ensure_derived(ctx)
    if not (ctx.furnish and ctx.furnish_explicit):
        return True, "ok"
    reason = _external_reason(ctx)
    return (not reason), reason or "ok"


def _chk_furnish_needs_ownership(ctx: Ctx) -> tuple[bool, str]:
    """Furnishing puts resources into the repo's history — only its OWNER opts in. Requires
    CONFIRMED push access (viewerPermission ADMIN/WRITE/MAINTAIN); fail-closed like
    `_guard_beads_remote` (bh-dhl6): gh absent / probe error refuse the furnish."""
    _ensure_derived(ctx)
    if not (ctx.furnish and ctx.furnish_explicit):
        return True, "ok"
    if registry.has_push_access(ctx.provider, ctx.org, ctx.repo):
        return True, f"push access to {ctx.org}/{ctx.repo} confirmed"
    return False, (
        f"no confirmed push access to {ctx.org}/{ctx.repo} — only the hive's owner may "
        "furnish it (drop --furnish/--claude/--agents/--skills for a zero-footprint onboard)"
    )


def _chk_prefix_policy(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    if registry.org_policy(ctx.cfg, ctx.org) == "required":
        code = registry.org_code(ctx.cfg, ctx.org)
        ok = registry.required_prefix_ok(code, ctx.org, ctx.repo, ctx.prefix)
        detail = (
            ctx.prefix
            if ok
            else (f"prefix '{ctx.prefix}' violates required-org policy (expected {code}-*)")
        )
        return ok, detail
    return True, ctx.prefix


def _chk_prefix_change_needs_yes(ctx: Ctx) -> tuple[bool, str]:
    #: changing a registered hive's prefix orphans every existing bead ID,
    # so an explicit --prefix that differs from the registered one needs --yes (the same
    # confirmation mechanism the fork gate uses). Never bypassable via --skip-check.
    _ensure_derived(ctx)
    if ctx.existing is None or not ctx.prefix_override or ctx.prefix == str(ctx.existing["prefix"]):
        return True, ctx.prefix
    registered = ctx.existing["prefix"]
    if ctx.yes:
        return True, f"prefix change '{registered}' → '{ctx.prefix}' confirmed (--yes)"
    return False, (
        f"--prefix '{ctx.prefix}' differs from the registered prefix '{registered}' — "
        "changing it orphans every existing bead ID; pass --yes to confirm"
    )


def _chk_dirty_tree(ctx: Ctx) -> tuple[bool, str]:
    # Hive-state residue (.beads/, .claude/, CLAUDE.md — exactly what a prior onboard leaves
    # behind) is discounted, mirroring safety.difficulty(): the footprint step is about to
    # commit those paths anyway, so only genuine dirt should block a (re-)onboard.
    dirt = safety._non_hive_dirty_paths(str(ctx.base))
    if dirt is None:  # git status failed — fall back to the scan-based signal
        record = safety.scan(ctx.base)
        dirty = any(b.dirty for b in record.branches)
    else:
        dirty = bool(dirt)
    return (not dirty), "clean" if not dirty else "working tree has uncommitted changes"


def _chk_on_default_branch(ctx: Ctx) -> tuple[bool, str]:
    return safety.on_default_branch(str(ctx.base))


# ---- actions (mutations reuse hive.py helpers) ------------------------------


def _noop(ctx: Ctx) -> None:
    """Assessment steps carry checks only; their derivation is memoized in _ensure_derived."""


def _act_clone(ctx: Ctx) -> None:
    from . import hive  # lazy: hive imports onboard

    Path(ctx.target).parent.mkdir(parents=True, exist_ok=True)
    if ctx.kind == "external":
        # `gh repo fork <target> --clone --remote` forks the triplet, clones the FORK to
        # ctx.target (extra `git clone` args after `--`), sets origin=our fork, and adds
        # `upstream` = the target repo we forked from — the whole dual-remote wiring in one
        # call (bh-uxam.1). Never consumed as a push target here — pull-only (worktree.push_branch).
        target_repo = f"{ctx.org}/{ctx.repo}"
        typer.echo(f"• forking {target_repo} → {ctx.target} (origin=fork, upstream={target_repo})")
        hive.run(["gh", "repo", "fork", target_repo, "--clone", "--remote", "--", str(ctx.target)])
    else:
        typer.echo(f"• cloning {ctx.clone_url} → {ctx.target}")
        hive.run(["git", "clone", ctx.clone_url, str(ctx.target)])


def _run_bd_mint(cmd: list[str], ctx: Ctx, *, env: dict) -> None:
    """Run a `bd init`/`bd bootstrap` MINT step (never on an already-initialized hive — every
    caller is inside a branch the `.beads`-exists skip has already ruled out) with bd's own
    stdout/stderr streaming straight through (never captured — bd's own progress and error
    output is already good, per this bead's own review: keep it verbatim, don't paraphrase
    it). A failure is caught HERE (`check=False`) rather than left to raise
    `subprocess.CalledProcessError` into bh's generic top-level handler, which would otherwise
    dump a raw traceback plus a structlog JSON blob a first-time user has no way to parse —
    the exact busy-port failure this bead's review reproduced.

    A failed mint is cleaned up before exiting (`hive.cleanup_failed_bd_init`): `.beads/` and
    any stray tracked `.gitignore` bd wrote are removed, so a retry is never blocked by
    wreckage the idempotent `.beads`-exists skip would otherwise mistake for a real store —
    the review's second, worse finding: a user following bh's own `--skip-check dirty-tree`
    hint got a hive reported `✓ ready` whose store never existed."""
    from . import hive

    res = hive.run(cmd, env=env, cwd=ctx.cwd, check=False)
    if getattr(res, "returncode", 1) == 0:
        return
    hive.cleanup_failed_bd_init(ctx.base)
    typer.echo(
        "✗ beads: onboarding did not complete — bd's error is above. The working tree has "
        "been cleaned up (nothing left behind to trip a retry); resolve the underlying issue "
        "(e.g. free the port, or set BEADS_DOLT_SERVER_HOST/_PORT to point at the right "
        "server) and re-run.",
        err=True,
    )
    raise typer.Exit(1)


def _act_bd_init(ctx: Ctx) -> None:
    """Materialize the local beads store, landing a BRAND-NEW hive on bd's shared server —
    the fleet's target mode and the default for newly-onboarded hives, per
    `docs/design/dolt-server-mode-adr.md` / `bh-ukit.4` ("Not per-hive opt-in" — no flag, no
    config key, always on for a mint/bootstrap). Furnished hives run today's tracked-convention
    `bd init --shared-server`; zero-footprint hives bootstrap from origin's `refs/dolt/data`
    when it exists (second-host case, `BEADS_DOLT_SHARED_SERVER=1`) or run
    `bd init --setup-exclude --shared-server` — zero commits, zero tracked changes (bd's stray
    .gitignore append is relocated into .git/info/exclude).

    EXISTING hives are never touched here: the `.beads`-exists idempotent skip below returns
    before any of this runs, so an operator on an embedded hive stays embedded across an
    upgrade — moving them is `bh hive migrate-storage`'s job, a deliberate operator action,
    never a side effect of re-running onboard (bh-areg.7's own constraint).

    That skip trusts `.beads/` existing to mean "already initialized" (unchanged from before
    this bead, and depended on by a wide, pre-existing hermetic-test convention across this
    suite: `.beads/` pre-created so onboarding skips a real `bd` call entirely). This bead
    keeps that trust HONEST from the other end instead of hardening the check itself: a
    FAILED mint (`_run_bd_mint` below) cleans up whatever it left behind before returning, so
    `.beads/` genuinely does not exist after a failure and the skip is never fooled by
    wreckage from THIS run (bh-areg.7's own review finding — a busy dolt-server port left a
    `.beads/` with no store behind, and the skip reported the hive ready anyway)."""
    from . import hive  # via hive.run so it honors the same run binding hive.init used

    _ensure_derived(ctx)
    if (ctx.base / ".beads").exists():
        # ponytail: idempotent — skip bd init so re-runs (e.g. to add --skills) never abort.
        # Never touches dolt_mode: an already-initialized hive (embedded or otherwise) is left
        # exactly as it is, so this can never silently convert an existing hive. A FAILED
        # mint never leaves `.beads/` behind to be misread here — see `_run_bd_mint`.
        typer.echo("ℹ beads already initialized — skipping bd init.")
        _configure_auto_export(ctx)
        return
    env = dict(os.environ, BD_NON_INTERACTIVE="1")
    if ctx.furnish:
        bd_init = [
            "bd",
            "init",
            "--prefix",
            ctx.prefix,
            SHARED_SERVER_FLAG,
            "--skip-agents",
            "--skip-hooks",
            "--init-if-missing",
        ]
        _run_bd_mint(bd_init + ["--non-interactive"], ctx, env=env)
        # A bare `bd init` (no --remote) always MINTS a fresh store — the shape bh-u562.1
        # Finding 7 measured as reproducing the GH#2455 dirty-config bug under any server mode.
        _bypass_gh2455_dirty_config(ctx)
    elif _origin_has_dolt_data(ctx):
        typer.echo("• beads: bootstrapping from origin refs/dolt/data (zero-footprint)")
        # `bd bootstrap` has no --shared-server flag of its own (unlike `bd init`) — activate
        # the same target mode via bd's own env var (measured, this bead: a FRESH bootstrap
        # persists `dolt_mode: "server"` into metadata.json from this alone, same as a fresh
        # `bd init --shared-server` does — the metadata-drift bh-areg.4 found is specific to
        # `--reinit-local` re-initializing an EXISTING local store, not this clone-from-origin
        # path; `_ensure_server_mode_persisted` below still re-asserts it defensively).
        _run_bd_mint(
            ["bd", "bootstrap", "--non-interactive"],
            ctx,
            env=dict(env, BEADS_DOLT_SHARED_SERVER="1"),
        )
        # No GH#2455 bypass here, deliberately: `bd bootstrap`'s sync-from-origin action calls
        # the SAME clone primitive as `bd init --remote` (bd source: cmd/bd/bootstrap.go's
        # executeSyncAction -> cloneFromRemote, shared verbatim with cmd/bd/init.go's --remote
        # handling) — the exact recipe bh-u562.1 Finding 7 measured as producing a clean
        # `dolt_status` on every server mode, because a clone pulls an already-committed dolt
        # history rather than minting a fresh one. Re-verified for this bead against a real
        # git-backed origin in shared-server mode: `dolt_status` was `[]` immediately after
        # bootstrap. A bare, non-clone `bd init` is what reproduces the bug; bootstrap never is.
    else:
        bd_init = [
            "bd",
            "init",
            "--prefix",
            ctx.prefix,
            "--setup-exclude",
            SHARED_SERVER_FLAG,
            "--skip-agents",
            "--skip-hooks",
            "--init-if-missing",
        ]
        _run_bd_mint(bd_init + ["--non-interactive"], ctx, env=env)
        if hive._relocate_bd_gitignore(ctx.base):
            typer.echo(
                "• beads: relocated bd's .gitignore block into .git/info/exclude (zero-footprint)"
            )
        # Same reasoning as the furnished branch above: a bare `bd init` mints a fresh store.
        _bypass_gh2455_dirty_config(ctx)
    # One call site for all three fresh-mint paths above (never reached by the existing-hive
    # skip branch): constraint 1 (persist dolt_mode for real) + constraint 4 (backup.enabled
    # must not regress vs. what embedded would have defaulted to).
    _ensure_server_mode_persisted(ctx)
    _configure_auto_export(ctx)
    _guard_beads_remote(ctx)
    _enable_backup_if_remote(ctx)


def _ensure_server_mode_persisted(ctx: Ctx) -> None:
    """Constraint 1 — bh-areg.4's hardest-won lesson, restated for onboarding: `dolt_mode` MUST
    be persisted into `.beads/metadata.json` ITSELF, never left to a per-invocation activation
    (`--shared-server` / `BEADS_DOLT_SHARED_SERVER=1`) that a future bd invocation might not
    repeat — exactly the drift bd's own `main.go:warnSharedServerEmbeddedMismatch` documents,
    and the one `store_locator.is_embedded_mode()` (bh-areg.1) depends on never happening.

    Measured for this bead against a real bd binary: a FRESH (non-`--reinit-local`) `bd init
    --shared-server` and a FRESH `bd bootstrap` (with the env var active) both already persist
    `dolt_mode: "server"` correctly on their own — unlike `--reinit-local`, which is the one bd
    verifiably leaves stale (bh-areg.4's finding, `storage_migrate.py`'s module docstring). This
    still re-asserts it defensively rather than trusting that measurement to hold across bd
    versions forever: silently leaving a newly-minted hive on the wrong mode is exactly the
    outcome that resurrects bh-areg.1's silent-no-op-restore bug. Also (re-)persists
    `dolt.shared-server: true` in `.beads/config.yaml` — belt-and-suspenders durability so a
    later invocation never depends on the activating flag/env var being supplied again
    (mirrors `storage_migrate._persist_shared_server_config`)."""
    from . import hive

    if store_locator.ensure_server_mode_persisted(ctx.base):
        # Defensive path only — not the measured common case (see docstring). Already written
        # by the shared helper; fix it visibly, never silently, matching
        # `_bypass_gh2455_dirty_config`'s own "state the mutation out loud" discipline.
        typer.echo(
            "⚠ beads: dolt_mode was not persisted by bd init/bootstrap — wrote "
            'dolt_mode="server" directly to .beads/metadata.json so a restore never trusts '
            "a stale mode.",
            err=True,
        )
    hive.run(["bd", "config", "set", SHARED_SERVER_CONFIG_KEY, "true"], cwd=ctx.cwd, check=False)


def _repo_has_git_remote(base: Path) -> bool:
    """Whether `base`'s git repo has at least one remote configured — the SAME condition bd's
    own auto-backup default checks for embedded mode (see `_enable_backup_if_remote`)."""
    from . import hive

    res = hive.run(["git", "remote"], cwd=str(base), check=False, capture=True)
    return bool((getattr(res, "stdout", "") or "").strip())


def _enable_backup_if_remote(ctx: Ctx) -> None:
    """Constraint 4 (`docs/design/dolt-server-mode-adr.md` Consequence 1, `bd backup --help`):
    auto-backup defaults ON in embedded mode when a git remote exists, and OFF in sql-server /
    shared-server mode, always — upstream's own anti-storm reasoning, not a bug to route
    around. A hive minted straight onto server mode must not be born LESS durable than an
    equivalent embedded one would have been: set `backup.enabled=true` under exactly the same
    condition embedded's own default uses (a git remote present) rather than unconditionally —
    a remote-less prototype would have defaulted OFF in embedded too, so leave bd's own default
    alone there instead of manufacturing a difference that was never real."""
    from . import hive

    if not _repo_has_git_remote(ctx.base):
        return
    hive.run(["bd", "config", "set", "backup.enabled", "true"], cwd=ctx.cwd, check=False)


# GH#2455 dirty-config bypass (bh-areg.2) — ONE named unit, removable without archaeology.
#
# "GH#2455" is bd's own INTERNAL numbering; it resolves to no public issue. The real, open,
# unpatched upstream reports are gastownhall/beads#4934 and #5111 — cite those, not GH#2455,
# in anything user-facing.
_GH2455_UPSTREAM = "gastownhall/beads#4934, #5111 (both open, unpatched)"


def _bypass_gh2455_dirty_config(ctx: Ctx) -> None:
    """Clear bd's own dirty-`config`-table bug after a FRESH, non-clone server-mode `bd init`.

    bd's own storage layer can leave a freshly-minted server-mode store's `config` table
    reported as ``modified`` by `dolt_status` immediately after init, with no bd-native
    command able to clear it — `bd dolt commit` prints "Committed." while the row stays dirty
    (verified empirically, bh-u562.1 Finding 7). The next `bd dolt pull` then refuses with a
    dirty-config guard. Verified NOT to occur via a clone-based path (`bd init --remote`, or
    `bd bootstrap`'s equivalent origin-sync) — see the callers' own comments for why only the
    two bare-``bd init`` branches of ``_act_bd_init`` call this.

    No-ops instantly and SILENTLY (never prints) when there is nothing to report: embedded
    mode (`bd sql` is unsupported there — bd's own error is the discriminator, not a fragile
    parsed "mode" string, which bh-u562.1 Findings 8/9 found inconsistent across bd's four
    engine modes) or a store whose `dolt_status` is already clean. Written before either
    bare-`bd init` branch of `_act_bd_init` defaulted to server mode (bh-areg.7), so this
    fires on the overwhelmingly common case now rather than staying dormant ahead of it —
    still correctly a no-op for an unmigrated EXISTING embedded hive, which never reaches
    this call at all (the idempotent `.beads`-exists skip returns first).

    NOT sanctioned bd behavior: `bd sql --help` itself warns that direct SQL access "bypasses
    the storage layer." When it DOES have something to clear, it says so out loud (stdout on
    success, stderr if the bypass itself fails to clear it) — never silent about the mutation
    itself, only silent when there is truly nothing to do. See
    docs/design/gh2455-dirty-config-bypass-adr.md for the full decision record.

    REMOVAL CONDITION, stated so this never needs archaeology: delete this function and both
    of its call sites in `_act_bd_init` the moment bh's required bd floor version ships a fix
    for gastownhall/beads#4934 or #5111 — i.e. once a bd-native `bd dolt commit`/`bd dolt add`
    clears the dirty `config` row on a fresh server-mode init without this SQL bypass.
    """
    from . import hive  # via hive.run so it honors the same run binding hive.init used

    probe = hive.run(
        ["bd", "sql", "--json", "SELECT * FROM dolt_status"],
        cwd=ctx.cwd,
        check=False,
        capture=True,
        timeout=30,
    )
    if getattr(probe, "returncode", 1) != 0:
        return  # embedded mode (`bd sql` unsupported), or nothing to probe yet — nothing to do
    if not _dolt_status_has_dirty_config(getattr(probe, "stdout", "")):
        return  # clean — nothing to report

    typer.echo(
        "⚠ beads: bd's own dirty-config bug left the fresh server-mode store's `config` table "
        f"showing modified (bd-internal 'GH#2455'; tracked upstream at {_GH2455_UPSTREAM}). "
        "Clearing it via bd's own documented — but NOT bd-sanctioned — SQL bypass so "
        "`bd dolt pull` doesn't refuse.",
        err=True,
    )
    hive.run(
        ["bd", "sql", "CALL DOLT_ADD('-A')"], cwd=ctx.cwd, check=False, capture=True, timeout=30
    )
    hive.run(
        ["bd", "sql", "CALL DOLT_COMMIT('-m', 'chore: clear bd dirty-config state (bh-areg.2)')"],
        cwd=ctx.cwd,
        check=False,
        capture=True,
        timeout=30,
    )
    verify = hive.run(
        ["bd", "sql", "--json", "SELECT * FROM dolt_status"],
        cwd=ctx.cwd,
        check=False,
        capture=True,
        timeout=30,
    )
    still_dirty = getattr(verify, "returncode", 1) != 0 or _dolt_status_has_dirty_config(
        getattr(verify, "stdout", "")
    )
    if still_dirty:
        typer.echo(
            "✗ beads: the GH#2455 bypass did not clear the dirty config — `bd dolt pull` may "
            'still refuse. Inspect with `bd sql "SELECT * FROM dolt_status"`; see '
            "docs/design/gh2455-dirty-config-bypass-adr.md.",
            err=True,
        )
    else:
        typer.echo("✓ beads: cleared bd's dirty-config state (GH#2455) — dolt_status is clean.")


def _dolt_status_has_dirty_config(raw_stdout: str) -> bool:
    """True iff *raw_stdout* (a `bd sql --json "SELECT * FROM dolt_status"` result) reports the
    internal `config` table as modified. Never raises on an unexpected/empty shape — treats it
    as clean rather than guessing."""
    try:
        rows = json.loads(raw_stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(rows, list):
        return False
    return any(isinstance(r, dict) and r.get("table_name") == "config" for r in rows)


# bd's own defaults, all off: export.auto=false, export.git-add=false, export.interval=60s.
# Only `auto` differs from what we want — `git-add` is pinned explicitly anyway because "never
# tracked" is a hard requirement and an unpinned default is one upstream change away from
# staging the snapshot on every write. interval stays at bd's 60s: a full export of a
# 1.5k-issue hive measures ~2.6s, so a shorter window spends a large fraction of wall-clock
# re-dumping and blocks the write that triggered it (bh-ug5u).
_EXPORT_CONFIG = (("export.auto", "true"), ("export.git-add", "false"))


def _configure_auto_export(ctx: Ctx) -> None:
    """Turn on bd's throttled JSONL auto-export so external consumers get a fresh
    ``.beads/issues.jsonl`` without spawning bd, and keep that file out of git.

    Runs on the already-initialized path too, which is what migrates the existing fleet — no new
    verb needed, `bh hive onboard` is already safe to re-run.

    NOTE ON FRESHNESS, because the name oversells it: bd exports *after write commands*,
    throttled by ``export.interval``. It is not a timer — an idle hive emits nothing, and the
    snapshot is at most one interval stale relative to the last WRITE, not relative to now. For
    push-based state see bh-jksq."""
    from . import hive

    for key, value in _EXPORT_CONFIG:
        res = hive.run(["bd", "config", "set", key, value], cwd=ctx.cwd, check=False)
        if getattr(res, "returncode", 1) != 0:
            # Advisory: never fail an onboard over an optional interop nicety. A bd that
            # predates these keys still produces a working hive.
            typer.echo(f"• beads: could not set {key} (bd too old?) — auto-export left as-is")
            return
    typer.echo("✓ beads: auto-export on (issues.jsonl, throttled 60s, never git-added)")
    if ctx.furnish and hive._ensure_export_exclude(ctx.base):
        typer.echo("✓ beads: excluded .beads/issues.jsonl from git (furnished hive)")


def _origin_has_dolt_data(ctx: Ctx) -> bool:
    """True when origin already carries beads state under refs/dolt/data — the fresh-clone /
    second-host case where `bd bootstrap` re-materializes the DB instead of a fresh init."""
    from . import hive

    res = hive.run(
        ["git", "ls-remote", "origin", "refs/dolt/data"],
        cwd=ctx.cwd,
        check=False,
        capture=True,
    )
    return getattr(res, "returncode", 1) == 0 and bool((getattr(res, "stdout", "") or "").strip())


def _guard_beads_remote(ctx: Ctx) -> None:
    """`bd init` derives the Dolt `sync.remote` from git origin, so onboarding a repo we do NOT own
    would point our beads remote at THEIR upstream. Beads must live on a repo we own or nowhere
    (bh-dhl6): unless push access is confirmed (viewerPermission ADMIN/WRITE/MAINTAIN), unset the
    remote. Fail-closed — gh absent / non-github / probe error all leave the remote unset."""
    from . import hive  # via hive.run so it honors the same run binding

    if registry.has_push_access(ctx.provider, ctx.org, ctx.repo):
        return
    res = hive.run(["bd", "config", "unset", "sync.remote"], cwd=ctx.cwd, check=False, capture=True)
    if getattr(res, "returncode", 0) == 0:
        typer.echo(
            "• beads remote: unset sync.remote — no confirmed push access to "
            f"{ctx.org}/{ctx.repo}; beads stay local (bh-dhl6)."
        )


def _act_register(ctx: Ctx) -> None:
    if ctx.reconfigure:
        registry.register(
            ctx.provider,
            ctx.org,
            ctx.repo,
            ctx.prefix,
            ctx.kind,
            ctx.upstream,
            furnish="full" if ctx.furnish else "none",
            # Contribution-plane marker (bh-uxam.1): today always "pull" — upstream is a read
            # rail only (worktree.push_branch refuses it); nothing yet consumes it for a PR.
            contribution="pull" if ctx.kind == "external" else "",
        )
        if ctx.plan is not None:
            ctx.plan.registered = True
    else:
        typer.echo(
            f"ℹ hive already configured: prefix '{ctx.prefix}' (kind={ctx.kind})"
            + (f", upstream {ctx.upstream}" if ctx.upstream else "")
            + " — settings preserved (use --force to re-register, or --prefix <p> --yes "
            "to change just the prefix).",
            err=True,
        )


def _installer(name: str, run_it):
    """Wrap an installer body so it records itself in plan.installers_run when it runs."""

    def action(ctx: Ctx) -> None:
        run_it(ctx)
        if ctx.plan is not None:
            ctx.plan.installers_run.append(name)

    return action


def _do_claude(ctx: Ctx) -> None:
    from . import config, hive

    # Local, idempotent steps first — they must land even when the plugin install
    # below aborts mid-run, so an interrupted --claude phase
    # leaves nothing unreachable and a re-run only has the fallible step left.
    hive._install_claude_settings(ctx.base)
    hive._install_sandbox_grant(ctx.cfg, ctx.provider, ctx.org, ctx.repo, ctx.base)
    hive._ensure_agf_hint(ctx.base / "CLAUDE.md", ctx.force, "--claude")
    source = config.claude_source(ctx.cfg)
    if source == "plugin":
        # Fallible last: shells out to the external `claude` CLI. Fenced warn-and-continue
        # (same guard as _do_observaloop / _plugin_step): harness setup never blocks repo
        # commissioning — on failure print the manual recovery, keep onboarding, exit 0.
        try:
            hive._install_plugin_claude(ctx.cfg)
        except Exception as exc:  # noqa: BLE001 - defensive fence: never aborts onboard
            warning = (
                f"--claude: plugin install failed ({exc}); recover manually with "
                f"`claude plugin marketplace add {config.REMOTE_MARKETPLACE} && "
                f"claude plugin install bh@beadhive --scope user`"
            )
            typer.echo(f"• {warning} — onboarding continues.", err=True)
            plan = getattr(ctx, "plan", None)
            if plan is not None:
                plan.warnings.append(warning)
    else:
        # legacy copy mode — copy agent files into .claude/agents/
        hive._install_agents_claude(ctx.force, ctx.base)


def _do_agents(ctx: Ctx) -> None:
    from . import hive

    hive._ensure_agf_hint(ctx.base / "AGENTS.md", ctx.force, "--agents")


def _do_opencode(ctx: Ctx) -> None:
    from . import hive

    hive._install_opencode_config(ctx.base)
    hive._install_agents_opencode(ctx.force, ctx.base)
    hive._install_skills_opencode(ctx.force)
    hive._install_bd_steer_opencode(ctx.force, ctx.base)
    # OpenCode reads AGENTS.md natively (Codex/others too) — write the AGF hint stanza
    # regardless of --agents, mirroring --claude's own CLAUDE.md hint.
    hive._ensure_agf_hint(ctx.base / "AGENTS.md", ctx.force, "--opencode")


def _do_skills(ctx: Ctx) -> None:
    from . import config, hive

    # In plugin mode with --claude, skills come from the agf plugin — never write a local copy.
    # This guard is belt-and-suspenders: the CLI already rejects --claude --skills in plugin mode.
    if ctx.claude and config.claude_source(ctx.cfg) == "plugin":
        typer.echo(
            "• --skills: skipped — plugin mode vends skills via the agf plugin (no local copy)",
            err=True,
        )
        return
    hive._install_skills(ctx.force, ctx.base)
    if ctx.claude:
        hive._link_skills_claude(ctx.force, ctx.base)


def _do_observaloop(ctx: Ctx) -> None:
    from . import hive

    # Best-effort, fully isolated: an unexpected failure anywhere in the observaloop wiring must
    # never abort onboarding (matches hive.init's fence).
    try:
        hive._install_observaloop(ctx.cfg, {"prefix": ctx.prefix})
    except Exception as exc:  # pragma: no cover - defensive: wrappers never raise
        typer.echo(f"• --observaloop: skipped ({exc}) — onboarding continues.", err=True)


def _plugin_step(p) -> Step:
    """A GENERIC onboard step for a plugin's ``on_onboard`` hook — fenced warn-and-continue,
    recording ``plan.installers_run`` on success (mirrors ``_do_observaloop``'s fence).

    Enabled when the plugin was forced on via ``--plugin <name>`` (``ctx.plugins``) OR the
    plugin's own ``enabled(cfg, entry)`` predicate is true."""

    def action(ctx: Ctx) -> None:
        try:
            p.on_onboard(ctx)
        except Exception as exc:  # noqa: BLE001 - defensive fence: a plugin never aborts onboard
            typer.echo(f"• plugin {p.name}: skipped ({exc}) — onboarding continues.", err=True)
            return
        if ctx.plan is not None:
            ctx.plan.installers_run.append(f"plugin-{p.name}")

    return Step(
        f"plugin-{p.name}",
        f"plugin {p.name}",
        action,
        requires=["register"],
        mutates=True,
        enabled=lambda c, _p=p: _p.name in c.plugins or _p.enabled(c.cfg, c.existing),
    )


def _act_footprint(ctx: Ctx) -> None:
    """Settle the hive's declared footprint (see hive.py's convention note).

    Furnished hives (declared, ownership-gated): un-stealth .beads/ and commit the scaffolding
    so a green onboard ends with a clean survey row. Runs last (after hub-sync) so the
    exported .beads/issues.jsonl lands in the commit too; re-runs amend an unpushed scaffold
    commit or use the distinct repair subject — never duplicate identically-titled commits.
    Zero-footprint hives (the default; every external hive): ensure .beads/ stays
    stealth-excluded and commit NOTHING — onboarding leaves no trace in the repo."""
    from . import hive

    _ensure_derived(ctx)
    # A distinct `upstream` remote makes this external regardless of the classified kind —
    # committing .beads/ + agent config onto it would pollute a repo with an external
    # upstream (bh-djx2); _ensure_derived already downgraded/refused furnish for it.
    if not ctx.furnish:
        if hive._ensure_stealth_exclude(ctx.base):
            typer.echo("✓ footprint: .beads/ stealth-excluded (zero-footprint)")
        typer.echo("• footprint: zero — nothing tracked, nothing committed")
        return
    if hive._remove_stealth_exclude(ctx.base):
        typer.echo("✓ footprint: removed .beads/ stealth exclusion (furnished hive)")
    if hive._commit_scaffolding(ctx.base):
        typer.echo("✓ footprint: committed hive scaffolding")
    else:
        typer.echo("• footprint: nothing to commit — hive already clean")


def _act_hq_parent(ctx: Ctx) -> None:
    """Surface a missing escalation parent (kind=hq) — fenced, warn-only (bh-ufne).

    Every onboarded hive should have an HQ to point escalations at, but onboard never
    auto-creates one: it only warns (exit stays 0), pointing at ``bh hq init``. The same
    signal is a REQUIRED ``hive ready`` check, so a properly onboarded host closes the gap."""
    from . import config

    if registry.hive_of_kind(ctx.cfg, registry.HQ_KIND) is not None:
        return
    warning = (
        "no HQ (kind=hq) hive exists — escalations have no parent; "
        f"run `{config.BINARY_ALIAS} hq init` to stand one up"
    )
    typer.echo(f"{_GLYPH_WARN} {warning} — onboarding continues.", err=True)
    if ctx.plan is not None:
        ctx.plan.warnings.append(warning)


def _act_hub_sync(ctx: Ctx) -> None:
    """Split per bh-d5jhc.1: the TRIGGERING hive's own export/`bd repo add` stays synchronous —
    `footprint` (below) depends on it landing before a furnished hive's scaffold commit — but
    the fleet-wide `bd repo sync` aggregation walk (every OTHER registered hive) moves off the
    interactive path by default. ``ctx.hub_sync``: ``True`` (explicit ``--hub-sync``) waits for
    the full `hub.sync()` synchronously, matching the pre-bh-d5jhc.1 behavior; ``None`` (default,
    unset) backgrounds the fleet walk (`hub.sync_background`, a best-effort daemon thread —
    mirrors `metadata._spawn_reload`); ``False`` (`hive init`, or explicit ``--no-hub-sync``)
    never reaches here — the step is disabled entirely (see ``enabled=`` below)."""
    from . import hub

    if ctx.hub_sync is True:
        hub.sync()
        if ctx.plan is not None:
            ctx.plan.hub_synced = True
        return
    ok = hub.sync_one(ctx.prefix, ctx.target)
    if ctx.plan is not None:
        ctx.plan.hub_synced = ok
    hub.sync_background(ctx.cfg)


def build_steps(ctx: Ctx) -> list[Step]:
    """The concrete onboarding DAG.

    Edges: resolve→clone→identity→{classify,worktree-clean}; classify→prefix;
    {prefix,worktree-clean}→bd-init→register; register→{installers}；
    {register,installers}→hub-sync. Clone is the preflight/acquire step; hub-sync runs last
    and only when ``ctx.hub_sync is not False`` (onboard defaults to deferred; plain init passes
    ``False`` and skips it). dirty-tree/on-default-branch apply only to an existing folder we
    did NOT just clone."""
    repo_present = lambda c: c.target_exists  # noqa: E731
    # dirty/branch only make sense for an existing git repo we did NOT just clone.
    unclean_applies = lambda c: (  # noqa: E731
        c.target_exists and not c.cloned and (c.base / ".git").exists()
    )

    resolve = Step(
        "resolve",
        "resolve triplet",
        _noop,
        checks=[Check("valid-triplet", "valid triplet", False, _chk_valid_triplet)],
    )
    clone = Step(
        "clone",
        "clone if absent",
        _act_clone,
        requires=["resolve"],
        mutates=True,
        preflight=True,
        enabled=lambda c: not c.target_exists,
        checks=[
            # --kind external derives what to clone from the triplet itself (`gh repo fork`) —
            # no --clone-url needed.
            Check(
                "clone-url-present",
                "clone url present",
                False,
                _chk_clone_url_present,
                applies=lambda c: c.kind != "external",
            ),
            Check("clone-url-reachable", "clone url reachable", True, _chk_clone_url_reachable),
            Check("parent-writable", "parent writable", False, _chk_parent_writable),
        ],
    )
    identity = Step(
        "identity",
        "workspace identity",
        _noop,
        requires=["clone"],
        checks=[
            Check(
                "under-git-workspace",
                "under $GIT_WORKSPACE",
                False,
                _chk_under_git_workspace,
                applies=repo_present,
            )
        ],
    )
    classify = Step(
        "classify",
        "classify hive",
        _noop,
        requires=["identity"],
        # fresh/--force only — evaluated at plan time, so gate on a direct registry lookup
        # rather than the derived ctx.existing (which _ensure_derived sets later, during checks).
        enabled=lambda c: registry.find_entry(c.cfg, c.provider, c.org, c.repo) is None or c.force,
        checks=[
            Check("not-excluded", "not excluded", False, _chk_not_excluded),
            Check("fork-needs-yes", "fork needs --yes", False, _chk_fork_needs_yes),
        ],
    )
    prefix = Step(
        "prefix",
        "derive prefix",
        _noop,
        requires=["classify"],
        checks=[
            Check("prefix-policy", "prefix policy", False, _chk_prefix_policy),
            Check(
                "prefix-change-needs-yes",
                "prefix change needs --yes",
                False,
                _chk_prefix_change_needs_yes,
            ),
        ],
    )
    worktree_clean = Step(
        "worktree-clean",
        "working tree clean",
        _noop,
        requires=["identity"],
        checks=[
            Check("dirty-tree", "dirty tree", True, _chk_dirty_tree, applies=unclean_applies),
            Check(
                "on-default-branch",
                "on default branch",
                True,
                _chk_on_default_branch,
                applies=unclean_applies,
            ),
        ],
    )
    bd_init = Step(
        "bd-init",
        "bd init",
        _act_bd_init,
        requires=["prefix", "worktree-clean"],
        mutates=True,
        checks=[
            Check(
                "external-no-furnish",
                "external hives are never furnished",
                False,
                _chk_external_no_furnish,
            ),
            Check(
                "furnish-needs-ownership",
                "furnish needs push access",
                False,
                _chk_furnish_needs_ownership,
            ),
        ],
    )
    register = Step("register", "register hive", _act_register, requires=["bd-init"], mutates=True)

    # NO prepush-hook step (bh-smcj). Onboard used to furnish the pre-push fence hook here,
    # which made bh install a hook file as a side effect of onboarding — the thing
    # docs/design/hooks-as-functionality-adr.md forbids. The fence is a FAST-FAIL convenience
    # in front of the real --force-with-lease epoch fence (host_fence.py), never the
    # enforcement, so defaulting it off costs an early refusal and nothing else. Operators who
    # want it run `bh hive hook install` explicitly.

    installers = [
        Step(
            "claude",
            "install .claude",
            _installer("claude", _do_claude),
            requires=["register"],
            mutates=True,
            enabled=lambda c: c.claude,
        ),
        Step(
            "agents",
            "install AGENTS hint",
            _installer("agents", _do_agents),
            requires=["register"],
            mutates=True,
            enabled=lambda c: c.agents,
        ),
        Step(
            "skills",
            "install skills",
            _installer("skills", _do_skills),
            requires=["register"],
            mutates=True,
            enabled=lambda c: c.skills,
        ),
        Step(
            "opencode",
            "install opencode furnishing",
            _installer("opencode", _do_opencode),
            requires=["register"],
            mutates=True,
            enabled=lambda c: c.opencode,
        ),
        Step(
            "observaloop",
            "install observaloop",
            _installer("observaloop", _do_observaloop),
            requires=["register"],
            mutates=True,
            enabled=lambda c: c.observaloop,
        ),
    ]
    hub_sync = Step(
        "hub-sync",
        "sync hub",
        _act_hub_sync,
        requires=["register", *[s.id for s in installers]],
        mutates=True,
        enabled=lambda c: c.hub_sync is not False,
    )
    # Last on purpose: hub-sync exports .beads/issues.jsonl into the hive (synchronously, even
    # under the default deferred mode — bh-d5jhc.1), and a furnished hive's scaffold commit
    # should capture it. When hub-sync is disabled (plain init, or explicit --no-hub-sync) the
    # edge is ignored by the topo sort, so footprint still runs after register + installers.
    footprint = Step(
        "footprint",
        "settle declared footprint",
        _act_footprint,
        requires=["register", *[s.id for s in installers], "hub-sync"],
        mutates=True,
    )

    # Escalation-parent surfacing (bh-ufne): read-only, fenced warn-only — runs even under
    # --dry-run (assessment action), never fails the onboard, never auto-creates the HQ.
    hq_parent = Step("hq-parent", "escalation parent (HQ)", _act_hq_parent, requires=["register"])

    # Generic plugin steps: one per registered plugin that declares an on_onboard hook. When
    # the registry is empty, no plugin step is built (integrations are not hardcoded here).
    plugin_steps = [_plugin_step(p) for p in _plugins.registry() if p.on_onboard is not None]

    return [
        resolve,
        clone,
        identity,
        classify,
        prefix,
        worktree_clean,
        bd_init,
        register,
        *installers,
        *plugin_steps,
        hq_parent,
        hub_sync,
        footprint,
    ]
