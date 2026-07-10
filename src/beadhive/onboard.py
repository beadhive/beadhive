"""onboard.py — onboarding step/check framework + two-phase preflight gate.

Models ``ws rig onboard``/``init`` as a small DAG of **steps**, each declaring **preflight
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

Modelled on ``retire.py``'s phased pattern and ``rig_ready``'s ``Check`` NamedTuple. It is
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

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import typer

from . import plugins as _plugins
from . import registry, safety

# typer glyphs (house style, cf. rig_ready._GLYPH): pass / fail / downgraded / info.
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
    flag-gates the step (e.g. ``--prime``); ``action(ctx)`` performs the work.

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

    rig: str
    target: str
    dry_run: bool
    cloned: bool = False
    checks: list[CheckResult] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)
    registered: bool = False
    installers_run: list[str] = field(default_factory=list)
    hub_synced: bool = False


@dataclass
class Ctx:
    """Context threaded through every ``check.fn`` and ``step.action``.

    The engine needs only ``rig``/``target``/``steps`` plus the mutable ``cloned``/``plan``
    slots it maintains. The concrete onboard DAG (``build_steps``) additionally reads the
    identity triplet, the installer flags, and the config — and memoizes the derived
    kind/prefix/upstream (``_ensure_derived``) so checks and actions agree on one derivation.
    """

    rig: str
    target: str
    steps: list[Step] = field(default_factory=list)
    cloned: bool = False
    plan: OnboardPlan | None = None

    # ---- concrete onboard inputs (built by rig.onboard/rig.init) ----
    provider: str = ""
    org: str = ""
    repo: str = ""
    clone_url: str = ""
    cwd: str | None = None  # target rig dir threaded to installers (None = process cwd)
    cfg: Any = None
    prime: bool = False
    claude: bool = False
    skills: bool = False
    observaloop: bool = False
    agents: bool = False
    plugins: list[str] = field(default_factory=list)  # plugin names forced on via --plugin
    force: bool = False
    yes: bool = False
    kind: str = ""
    prefix: str = ""
    do_hub_sync: bool = False  # onboard syncs the hub last; plain init does not

    # ---- derived once by _ensure_derived, read by checks + actions ----
    existing: Any = None
    upstream: str = ""
    classification: str = ""
    prefix_override: bool = False
    kind_override: bool = False
    reconfigure: bool = False
    _derived: bool = False

    @property
    def base(self) -> Path:
        """The rig dir installers/checks operate on (``cwd`` when threaded, else process cwd)."""
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
    for res in batch:
        plan.checks.append(res)
        if res.skipped:
            plan.skipped_checks.append(res.id)

    failures = [r for r in batch if not r.ok and not r.skipped]
    if not failures:
        return

    typer.echo("✗ onboarding preflight failed:", err=True)
    for r in failures:
        typer.echo(f"  {_GLYPH_FAIL} {r.id}: {r.detail}", err=True)
    overridable = [r.id for r in failures if r.overridable]
    if overridable:
        typer.echo(
            f"  override with --skip-check {','.join(overridable)}", err=True
        )
    raise typer.Exit(1)


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


def run_onboard(
    ctx: Ctx, *, dry_run: bool = False, skip_checks: Iterable[str] = ()
) -> OnboardPlan:
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
    plan = OnboardPlan(rig=ctx.rig, target=ctx.target, dry_run=dry_run)
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


# ===========================================================================
# The concrete onboard DAG — steps + per-step preflight checks (bead .3)
# ===========================================================================
#
# Steps reuse the existing rig.py helpers + registry/safety/hub; no logic is
# reimplemented here. Derivation (existing lookup → classify → kind/prefix/upstream →
# reconfigure, with the reinit diagnostics) is done ONCE in _ensure_derived, matching
# rig.init's assessment block verbatim, so checks and actions agree on one outcome.


def _ensure_derived(ctx: Ctx) -> None:
    """Resolve kind/prefix/upstream/reconfigure once (idempotent), mirroring rig.init's
    assessment (existing-vs-fresh/force branching + the /a12 diagnostics).

    Read-only w.r.t. the repo/registry — it only classifies, derives, and prints the same
    notes rig.init did; the actual register()/bd-init happen later in Phase B. Excluded/fork
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

    if existing is not None and not ctx.force:
        # Preserve path: start from the recorded entry, apply only explicit overrides.
        ctx.prefix = ctx.prefix or str(existing["prefix"])
        ctx.kind = ctx.kind or str(existing["kind"])
        ctx.upstream = str(existing.get("upstream", "") or "")
    else:
        # Fresh rig, or --force: classify + derive from scratch.
        cls = registry.classify(provider, org, repo, cfg)
        ctx.classification = cls
        if cls == "org-native":
            ctx.kind = ctx.kind or "org-native"
        elif cls.startswith("fork upstream="):
            ctx.upstream = cls[len("fork upstream=") :]
            ctx.kind = ctx.kind or "fork"
        elif cls != "excluded":
            ctx.kind = ctx.kind or "prototype"
        if existing is not None:
            # --force on a registered rig keeps the registered prefix:
            # re-registering under a re-derived prefix would orphan every existing bead ID.
            ctx.prefix = ctx.prefix or str(existing["prefix"])
        if not ctx.prefix:
            ctx.prefix, warns = registry.derive_prefix(provider, org, repo, ctx.kind, cfg)
            for w in warns:
                typer.echo(w, err=True)

    if existing is not None and not ctx.prefix_override:
        derived, _ = registry.derive_prefix(provider, org, repo, ctx.kind, cfg)
        if derived != ctx.prefix:
            typer.echo(
                f"note: derived prefix '{derived}' differs from the registered prefix "
                f"'{ctx.prefix}' — keeping the registered one (use --prefix <p> --yes "
                "to change it)",
                err=True,
            )

    ctx.reconfigure = (
        existing is None or ctx.force or ctx.prefix_override or ctx.kind_override
    )


# ---- checks (pure, read-only (ok, detail) predicates) ----------------------


def _chk_valid_triplet(ctx: Ctx) -> tuple[bool, str]:
    parts = ctx.rig.split("/")
    ok = len(parts) == 3 and all(parts)
    return ok, ctx.rig if ok else f"expected a provider/org/repo triplet, got '{ctx.rig}'"


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
    from . import rig  # via rig so it honors the same workspace_identity binding rig uses

    ident = rig.workspace_identity(cwd=ctx.cwd)
    ok = ident is not None
    return ok, "under $GIT_WORKSPACE" if ok else "not in a git repo under $GIT_WORKSPACE"


def _chk_not_excluded(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    ok = ctx.classification != "excluded"
    return ok, "not excluded" if ok else f"{ctx.rig} is excluded by the registry"


def _chk_fork_needs_yes(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    blocked = ctx.kind == "fork" and not ctx.yes
    if not blocked:
        return True, "ok"
    suffix = f" of {ctx.upstream}" if ctx.upstream else ""
    return False, f"{ctx.rig} is a fork{suffix} — pass --yes to track it (beads is OFF by default)"


def _chk_prefix_policy(ctx: Ctx) -> tuple[bool, str]:
    _ensure_derived(ctx)
    if registry.org_policy(ctx.cfg, ctx.org) == "required":
        code = registry.org_code(ctx.cfg, ctx.org)
        ok = ctx.prefix.startswith(f"{code}-")
        detail = ctx.prefix if ok else (
            f"prefix '{ctx.prefix}' violates required-org policy (expected {code}-*)"
        )
        return ok, detail
    return True, ctx.prefix


def _chk_prefix_change_needs_yes(ctx: Ctx) -> tuple[bool, str]:
    #: changing a registered rig's prefix orphans every existing bead ID,
    # so an explicit --prefix that differs from the registered one needs --yes (the same
    # confirmation mechanism the fork gate uses). Never bypassable via --skip-check.
    _ensure_derived(ctx)
    if (
        ctx.existing is None
        or not ctx.prefix_override
        or ctx.prefix == str(ctx.existing["prefix"])
    ):
        return True, ctx.prefix
    registered = ctx.existing["prefix"]
    if ctx.yes:
        return True, f"prefix change '{registered}' → '{ctx.prefix}' confirmed (--yes)"
    return False, (
        f"--prefix '{ctx.prefix}' differs from the registered prefix '{registered}' — "
        "changing it orphans every existing bead ID; pass --yes to confirm"
    )


def _chk_dirty_tree(ctx: Ctx) -> tuple[bool, str]:
    # Rig-state residue (.beads/, .claude/, CLAUDE.md — exactly what a prior onboard leaves
    # behind) is discounted, mirroring safety.difficulty(): the scaffold step is about to
    # commit those paths anyway, so only genuine dirt should block a (re-)onboard.
    dirt = safety._non_rig_dirty_paths(str(ctx.base))
    if dirt is None:  # git status failed — fall back to the scan-based signal
        record = safety.scan(ctx.base)
        dirty = any(b.dirty for b in record.branches)
    else:
        dirty = bool(dirt)
    return (not dirty), "clean" if not dirty else "working tree has uncommitted changes"


def _chk_on_default_branch(ctx: Ctx) -> tuple[bool, str]:
    return safety.on_default_branch(str(ctx.base))


# ---- actions (mutations reuse rig.py helpers) ------------------------------


def _noop(ctx: Ctx) -> None:
    """Assessment steps carry checks only; their derivation is memoized in _ensure_derived."""


def _act_clone(ctx: Ctx) -> None:
    from . import rig  # lazy: rig imports onboard

    Path(ctx.target).parent.mkdir(parents=True, exist_ok=True)
    typer.echo(f"• cloning {ctx.clone_url} → {ctx.target}")
    rig.run(["git", "clone", ctx.clone_url, str(ctx.target)])


def _act_bd_init(ctx: Ctx) -> None:
    from . import rig  # via rig.run so it honors the same run binding rig.init used

    if (ctx.base / ".beads").exists():
        # ponytail: idempotent — skip bd init so re-runs (e.g. to add --skills) never abort.
        typer.echo("ℹ beads already initialized — skipping bd init.")
        return
    env = dict(os.environ, BD_NON_INTERACTIVE="1")
    bd_init = ["bd", "init", "--prefix", ctx.prefix, "--skip-agents", "--skip-hooks"]
    rig.run(bd_init + ["--non-interactive"], env=env, cwd=ctx.cwd)


def _act_register(ctx: Ctx) -> None:
    if ctx.reconfigure:
        registry.register(ctx.provider, ctx.org, ctx.repo, ctx.prefix, ctx.kind, ctx.upstream)
        if ctx.plan is not None:
            ctx.plan.registered = True
    else:
        typer.echo(
            f"ℹ rig already configured: prefix '{ctx.prefix}' (kind={ctx.kind})"
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


def _do_prime(ctx: Ctx) -> None:
    from . import rig

    rig._install_prime_md(ctx.force, ctx.base)


def _do_claude(ctx: Ctx) -> None:
    from . import config, rig

    # Local, idempotent steps first — they must land even when the plugin install
    # below aborts mid-run, so an interrupted --claude phase
    # leaves nothing unreachable and a re-run only has the fallible step left.
    rig._install_claude_settings(ctx.base)
    rig._install_sandbox_grant(ctx.cfg, ctx.provider, ctx.org, ctx.repo, ctx.base)
    rig._ensure_agf_hint(ctx.base / "CLAUDE.md", ctx.force, "--claude")
    source = config.claude_source(ctx.cfg)
    if source == "plugin":
        # Fallible last: shells out to the external `claude` CLI.
        rig._install_plugin_claude(ctx.cfg)
    else:
        # legacy copy mode — copy agent files into .claude/agents/
        rig._install_agents_claude(ctx.force, ctx.base)


def _do_agents(ctx: Ctx) -> None:
    from . import rig

    rig._ensure_agf_hint(ctx.base / "AGENTS.md", ctx.force, "--agents")


def _do_skills(ctx: Ctx) -> None:
    from . import config, rig

    # In plugin mode with --claude, skills come from the agf plugin — never write a local copy.
    # This guard is belt-and-suspenders: the CLI already rejects --claude --skills in plugin mode.
    if ctx.claude and config.claude_source(ctx.cfg) == "plugin":
        typer.echo(
            "• --skills: skipped — plugin mode vends skills via the agf plugin (no local copy)",
            err=True,
        )
        return
    rig._install_skills(ctx.force, ctx.base)
    if ctx.claude:
        rig._link_skills_claude(ctx.force, ctx.base)


def _do_observaloop(ctx: Ctx) -> None:
    from . import rig

    # Best-effort, fully isolated: an unexpected failure anywhere in the observaloop wiring must
    # never abort onboarding (matches rig.init's fence).
    try:
        rig._install_observaloop(ctx.cfg, {"prefix": ctx.prefix})
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
        f"plugin-{p.name}", f"plugin {p.name}", action, requires=["register"], mutates=True,
        enabled=lambda c, _p=p: _p.name in c.plugins or _p.enabled(c.cfg, c.existing),
    )


def _act_scaffold_commit(ctx: Ctx) -> None:
    """Restore the tracked-rig convention: un-stealth .beads/ and commit the scaffolding.

    Established rigs track their beads/agent scaffolding (see rig.py's convention note); this
    step makes a green onboard end with a clean survey row instead of stealth-excluded .beads/
    plus untracked .claude/settings.json / CLAUDE.md. Forks are the deliberate exception —
    bd auto-configures the stealth exclude there so beads never pollutes an upstream PR.
    Runs last (after hub-sync) so the exported .beads/issues.jsonl lands in the commit too.
    Idempotent: re-running onboard/init on an already-diverged rig repairs it in place."""
    from . import rig

    _ensure_derived(ctx)
    if ctx.kind == "fork":
        typer.echo("• scaffold: fork rig — .beads/ stays stealth-excluded; nothing committed.")
        return
    if rig._remove_stealth_exclude(ctx.base):
        typer.echo("✓ scaffold: removed .beads/ stealth exclusion (tracked-rig convention)")
    if rig._commit_scaffolding(ctx.base):
        typer.echo(f"✓ scaffold: committed rig scaffolding ({rig._SCAFFOLD_COMMIT_MSG!r})")
    else:
        typer.echo("• scaffold: nothing to commit — rig already clean")


def _act_hub_sync(ctx: Ctx) -> None:
    from . import hub

    hub.sync()
    if ctx.plan is not None:
        ctx.plan.hub_synced = True


def build_steps(ctx: Ctx) -> list[Step]:
    """The concrete onboarding DAG.

    Edges: resolve→clone→identity→{classify,worktree-clean}; classify→prefix;
    {prefix,worktree-clean}→bd-init→register; register→{installers}；
    {register,installers}→hub-sync. Clone is the preflight/acquire step; hub-sync runs last
    and only when ``do_hub_sync`` (onboard, not plain init). dirty-tree/on-default-branch
    apply only to an existing folder we did NOT just clone."""
    repo_present = lambda c: c.target_exists  # noqa: E731
    # dirty/branch only make sense for an existing git repo we did NOT just clone.
    unclean_applies = lambda c: (  # noqa: E731
        c.target_exists and not c.cloned and (c.base / ".git").exists()
    )

    resolve = Step(
        "resolve", "resolve triplet", _noop,
        checks=[Check("valid-triplet", "valid triplet", False, _chk_valid_triplet)],
    )
    clone = Step(
        "clone", "clone if absent", _act_clone, requires=["resolve"],
        mutates=True, preflight=True, enabled=lambda c: not c.target_exists,
        checks=[
            Check("clone-url-present", "clone url present", False, _chk_clone_url_present),
            Check("clone-url-reachable", "clone url reachable", True, _chk_clone_url_reachable),
            Check("parent-writable", "parent writable", False, _chk_parent_writable),
        ],
    )
    identity = Step(
        "identity", "workspace identity", _noop, requires=["clone"],
        checks=[Check("under-git-workspace", "under $GIT_WORKSPACE", False,
                      _chk_under_git_workspace, applies=repo_present)],
    )
    classify = Step(
        "classify", "classify rig", _noop, requires=["identity"],
        # fresh/--force only — evaluated at plan time, so gate on a direct registry lookup
        # rather than the derived ctx.existing (which _ensure_derived sets later, during checks).
        enabled=lambda c: registry.find_entry(c.cfg, c.provider, c.org, c.repo) is None or c.force,
        checks=[
            Check("not-excluded", "not excluded", False, _chk_not_excluded),
            Check("fork-needs-yes", "fork needs --yes", False, _chk_fork_needs_yes),
        ],
    )
    prefix = Step(
        "prefix", "derive prefix", _noop, requires=["classify"],
        checks=[
            Check("prefix-policy", "prefix policy", False, _chk_prefix_policy),
            Check("prefix-change-needs-yes", "prefix change needs --yes", False,
                  _chk_prefix_change_needs_yes),
        ],
    )
    worktree_clean = Step(
        "worktree-clean", "working tree clean", _noop, requires=["identity"],
        checks=[
            Check("dirty-tree", "dirty tree", True, _chk_dirty_tree, applies=unclean_applies),
            Check("on-default-branch", "on default branch", True, _chk_on_default_branch,
                  applies=unclean_applies),
        ],
    )
    bd_init = Step(
        "bd-init", "bd init", _act_bd_init, requires=["prefix", "worktree-clean"], mutates=True,
    )
    register = Step("register", "register rig", _act_register, requires=["bd-init"], mutates=True)

    installers = [
        Step("prime", "install PRIME.md", _installer("prime", _do_prime), requires=["register"],
             mutates=True, enabled=lambda c: c.prime),
        Step("claude", "install .claude", _installer("claude", _do_claude), requires=["register"],
             mutates=True, enabled=lambda c: c.claude),
        Step("agents", "install AGENTS hint", _installer("agents", _do_agents),
             requires=["register"], mutates=True, enabled=lambda c: c.agents),
        Step("skills", "install skills", _installer("skills", _do_skills), requires=["register"],
             mutates=True, enabled=lambda c: c.skills),
        Step("observaloop", "install observaloop", _installer("observaloop", _do_observaloop),
             requires=["register"], mutates=True, enabled=lambda c: c.observaloop),
    ]
    hub_sync = Step(
        "hub-sync", "sync hub", _act_hub_sync,
        requires=["register", *[s.id for s in installers]], mutates=True,
        enabled=lambda c: c.do_hub_sync,
    )
    # Last on purpose: hub-sync exports .beads/issues.jsonl into the rig, and the scaffold
    # commit should capture it. When hub-sync is disabled (plain init) the edge is ignored
    # by the topo sort, so scaffold still runs after register + the installers.
    scaffold = Step(
        "scaffold", "commit rig scaffolding", _act_scaffold_commit,
        requires=["register", *[s.id for s in installers], "hub-sync"], mutates=True,
    )

    # Generic plugin steps: one per registered plugin that declares an on_onboard hook. When
    # the registry is empty, no plugin step is built (integrations are not hardcoded here).
    plugin_steps = [_plugin_step(p) for p in _plugins.registry() if p.on_onboard is not None]

    return [resolve, clone, identity, classify, prefix, worktree_clean, bd_init, register,
            *installers, *plugin_steps, hub_sync, scaffold]
