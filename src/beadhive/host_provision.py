"""``bh host provision`` — one idempotent, resumable verb for the whole new-host adoption path
(bh-twc8.1).

Adopting a new host today is a hand-assembled sequence with no verb, no preconditions checked,
and no way to resume a partial run::

    bh config init                                  # mints host.yaml / host_id
    bh config set hq.remote <owner>/beadhive-hq     # cannot be inherited: it is how a host FINDS HQ
    bh hq clone                                     # HQ config + aggregate + workspace.toml
    git workspace update                            # re-clone repos from HQ's provider list
    bh host init --role <executor|transient|viewer>  # register in the fleet roster
    bh bd sync                                      # per hive: pull bead state
    chmod 700 <hive>/.beads                         # or bh warns on every command
    bh doctor

This module mechanizes that path as :func:`provision`: the ordered steps in :data:`PLAN`, each of
which
PROBES its own precondition before acting, so a re-run against any partial state (a fresh
host, a half-finished prior run, or an already-fully-provisioned host being re-verified) is
always safe — every step reports ``done`` / ``skipped`` / ``would`` (``--dry-run``) / ``failed``,
never mutates blindly, and one step's failure never aborts the rest (:func:`provision` catches
per-step exceptions so the remaining steps — and the final verifying gate — still run and
report honestly).

Hard requirements this module holds itself to:

* **Never clobber an existing ``host.yaml``.** ``host_id`` is identity, minted once
  (:func:`beadhive.host.mint_if_needed`) and never regenerated — reused via
  :func:`beadhive.config.scaffold_home` here, the SAME no-clobber mechanics ``bh config init``
  drives (extracted there so this module and the CLI command share one code path).
* **Resolve ``hq.remote`` explicitly and confirm it interactively unless ``--auto``** — reuses
  :func:`beadhive.hq._confirm_remote`, the exact prompt ``bh hq init``/``bh hq clone`` already
  use (bh-mw97), rather than a parallel implementation.
* **``--dry-run`` prints the ordered plan with zero mutation** — every step probes first and,
  under ``dry_run=True``, reports what it WOULD do without writing/prompting/subprocess-ing.
* **Ends on a verifying gate** (:func:`status`) that fails the whole run (a non-zero CLI exit)
  when the host is not actually usable afterward — see :func:`_step_verify`.
* **Fixes the 0700 ``.beads`` permission nag as part of the flow** (:func:`_step_fix_permissions`)
  rather than leaving a warning for the operator to act on by hand.

One latent conflict this pipeline runs straight into (surfaced by chaining these steps end to
end for the first time): the config template ``config init`` scaffolds legitimately sets
FLEET-classified keys (``schema_version``, ``providers``, ``dimensions``, …) — the right
default before any ``fleet.yaml`` exists to disagree with. The moment ``hq clone`` lands a REAL
``fleet.yaml``, those same host-side keys become a hard ``ConfigError`` on every later
``config.load()``. :func:`_step_hq_clone` reconciles this itself right after a successful (or
already-done) clone — dropping the host's now-stale copies (never merging them anywhere; the
just-cloned ``fleet.yaml`` is authoritative) — via :func:`_reconcile_host_config_after_clone`.

Pairs with ``bh host retire`` (sibling bead, bh-twc8.2) as the other end of the host lifecycle.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

import typer

from . import (
    config,
    engine,
    git_identity,
    gitworkspace,
    hive_sync,
    host,
    host_cli,
    hosts,
    hq,
    registry,
    store_locator,
)
from .bd import err_line
from .identity import workspace_root
from .run import run

# `git workspace update` may clone/verify an entire fleet's worth of repos — far longer than a
# single git op (hq.py's own GIT_TIMEOUT=30 is sized for one remote round trip).
GIT_WORKSPACE_TIMEOUT = 900.0
GIT_TIMEOUT = 30.0  # a single quick op (reading HQ's own `origin` remote URL)

# Permission bits every `.beads/` dir must carry — the fix for the nag `bd` repeats on every
# command when a hive's store is group/world-readable (observed on factory-orca: 0750).
BEADS_MODE = 0o700

PLAN: tuple[str, ...] = (
    # Step 0 exists because provision IS the documented entry point (bh-twc8.1), and a plan
    # whose first listed step cannot run from a fresh host is not the whole plan (bh-1kzc).
    # `bh host` is gated behind a passing setup cache, so before this the operator had to know
    # to run `bh setup check` out of band — a step nothing documented, whose only named escape
    # was a bypass labelled debug-only. provision now performs it, and cli.py exempts this one
    # verb from the gate so it can (see _SETUP_GATE_ALLOW_VERBS).
    "setup check",
    "config init",
    # AFTER `hq clone`, DELIBERATELY (bh-28ha). It used to run here, third, inherited from the
    # hand-assembled sequence in the docstring above — which assumed the operator had already
    # placed a `workspace*.toml` themselves. Under the internally-managed shape the provider
    # list ARRIVES WITH HQ, so a host cannot clone the fleet's repos before it has the fleet's
    # list of them. Running it before `hq clone` is why the beadhive-factory run skipped this
    # step on the very run that then cloned the file it was asking for.
    "hq.remote",
    "hq clone",
    # AFTER `hq clone`, AND IT CANNOT MOVE EARLIER (bh-ijd4). A git identity has two halves that
    # become available at different steps: `config init` mints the PER-HOST half (which key this
    # machine signs with, in host.yaml), while the FLEET half — the operator's name/email in
    # fleet.yaml, and allowed_signers — does not exist on this host until HQ is cloned, one step
    # above. So `config init` provably cannot own this; this step is where the two are married.
    "git identity",
    "git workspace update",
    "host init",
    "bead sync",
    "fix permissions",
    "verify",
    # LAST, AND AFTER VERIFY, DELIBERATELY (bh-q160.2). Every other step is local and
    # reversible; adopt CASes the hive's epoch fence and then HQ's lease, which is
    # fleet-visible and races other hosts. Running it only once the host is VERIFIED usable is
    # what makes "a failure in any earlier step leaves zero leases adopted" true — a half-built
    # host that grabbed primary is strictly worse than one that failed cleanly.
    "adopt",
)

GLYPH: dict[str, str] = {"done": "✓", "skipped": "•", "would": "→", "failed": "✗"}


@dataclass(frozen=True)
class StepResult:
    """One provisioning step's outcome. ``status`` is one of ``done`` (mutated, succeeded),
    ``skipped`` (probe found nothing to do — already satisfied, or a prior step's precondition
    isn't met yet), ``would`` (``--dry-run``: this is what a live run would do), or ``failed``
    (attempted and did not succeed — never raised past :func:`provision`)."""

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Check:
    """One line of :func:`status`'s verifying-gate report — a fact about whether this host is
    actually usable right now, independent of whether :func:`provision` ran this session."""

    label: str
    ok: bool
    detail: str = ""


# ---- shared probes ------------------------------------------------------------


def _cfg_or_none() -> dict | None:
    """``config.load()``, or ``None`` when it can't be resolved yet — either a genuinely fresh
    host (neither a fleet base nor a local ``config.yaml`` exists: ``FileNotFoundError``) or a
    host whose config still conflicts with a real ``fleet.yaml`` (``config.ConfigError`` — see
    the module docstring's note on :func:`_reconcile_host_config_after_clone`; this is the
    ``--dry-run`` path, which never mutates to fix it). Every step below that needs config
    content probes through this instead of calling ``config.load()`` directly, so either case
    reports a clean ``skipped`` instead of an unhandled-exception ``failed``. :func:`status`
    surfaces the ``ConfigError`` case explicitly (its own ``config loads cleanly`` check) rather
    than swallowing the detail entirely."""
    try:
        return config.load()
    except (FileNotFoundError, config.ConfigError):
        return None


def _present_hive_entries(cfg: dict | None) -> list[dict]:
    """Registered hives (never HQ itself — it has no `.beads` federation peer, matching
    `hive_sync._targets`' own exclusion) whose clone actually exists on disk. `bead sync` and
    `fix permissions` both scope to this set: a hive this host hasn't cloned yet (git-workspace
    disabled, or simply not tracked here) has no `.beads` dir to sync or chmod. ``cfg=None``
    (no config.yaml at all yet — see :func:`_cfg_or_none`) is treated as "no hives registered"."""
    if cfg is None:
        return []
    return [
        e
        for e in cfg.get("managed_repos", []) or []
        if str(e.get("kind", "")) != registry.HQ_KIND and registry.hive_dir(e).is_dir()
    ]


def _beads_dirs(cfg: dict | None) -> list[Path]:
    """Every `.beads` dir this host actually has on disk right now — HQ's own store plus each
    present hive's. The set :func:`_step_fix_permissions` and the verifying gate's permission
    check both walk."""
    dirs: list[Path] = []
    hq_beads = config.hq_dir() / ".beads"
    if hq_beads.is_dir():
        dirs.append(hq_beads)
    dirs.extend(registry.hive_dir(e) / ".beads" for e in _present_hive_entries(cfg))
    return [d for d in dirs if d.is_dir()]


def _wrong_perms(cfg: dict | None) -> list[Path]:
    return [d for d in _beads_dirs(cfg) if stat.S_IMODE(d.stat().st_mode) != BEADS_MODE]


# ---- step 0: setup check (the gate provision used to require out of band) ------


def _step_setup_check(*, dry_run: bool) -> StepResult:
    """Probe post-install dependencies and write the setup cache, unless it already passes.

    An ALREADY-PASSING cache is the common case and is skipped without probing — this step
    exists for the fresh host, where every other `bh host` verb is refused until it runs.

    ``run_check`` exits non-zero when a dependency is missing, and that exit must not abort
    provision: the later steps still have to report honestly, and a missing dep is a *failed
    step*, not a crash. So the exit is swallowed and the cache is re-read for the real verdict.
    """
    from . import setup as setup_mod  # lazy: keeps provision's import graph cheap

    if setup_mod.is_setup_complete():
        return StepResult("setup check", "skipped", "setup cache already passing")
    if dry_run:
        return StepResult("setup check", "would", "would run `bh setup check` — no cache yet")

    try:
        setup_mod.run_check()
    except (SystemExit, typer.Exit):
        pass  # the re-read below is the verdict, not the exit code
    if setup_mod.is_setup_complete():
        return StepResult("setup check", "done", "dependencies probed; cache written")
    return StepResult("setup check", "failed", "missing dependencies — see the probe output")


# ---- step 1: config init (host.yaml + config.yaml) ----------------------------


def _step_config_init(*, dry_run: bool) -> StepResult:
    if dry_run:
        planned = config.scaffold_home(dry_run=True)
        missing = [str(p) for p, would in planned if would]
        if not missing:
            return StepResult("config init", "skipped", "already scaffolded")
        return StepResult("config init", "would", f"would write: {', '.join(missing)}")

    written = config.scaffold_home(force=False)
    wrote = [str(p) for p, did in written if did]
    if not wrote:
        return StepResult("config init", "skipped", "already scaffolded")
    return StepResult("config init", "done", f"wrote: {', '.join(wrote)}")


# ---- step 2: git workspace update (re-clone repos from providers) -------------


def _link_workspace_config(sources: list[Path]) -> Path | None:
    """Make the config bh RESOLVED reachable by the `git-workspace` BINARY, returning the link
    created (``None`` when nothing needed doing).

    The binary takes only ``--workspace <dir>`` and reads ``workspace*.toml`` from inside it —
    there is no config-path flag (`git-workspace --help`, 1.10.1). So a provider list that lives
    anywhere else, which after bh-9bkj is the normal internally-managed case (HQ's own copy), is
    invisible to the child no matter how well bh resolves it. On beadhive-factory that is exactly
    what happened: `bh hq clone` left the file on disk and step 3 still said "place one".

    A SYMLINK, not a copy (bh-28ha weighed both). HQ stays the single source of truth: editing
    HQ's providers changes what the host clones, with no second file to keep in step. A copy
    drifts the moment HQ's providers change. git-workspace writes only ``workspace-lock.toml``
    into the workspace root — a different filename — so its normal operation never overwrites the
    link, and the lockfile lands beside it where `gitworkspace.upstreams`/`repo_urls`/
    `tracked_repos` already read it.

    Does nothing when the resolved config is already under the workspace root (the
    externally-managed shape) or when a ``workspace*.toml`` is already there.
    """
    root = Path(workspace_root())
    target = sources[0]
    if not target.exists() or target.parent.resolve() == root.resolve():
        return None
    if gitworkspace.glob_configs(root):
        return None  # the child can already see a config of its own — never shadow it
    root.mkdir(parents=True, exist_ok=True)
    link = root / "workspace.toml"
    link.symlink_to(target)
    return link


def _step_git_workspace_update(*, dry_run: bool) -> StepResult:
    cfg = _cfg_or_none()
    if cfg is None:
        return StepResult(
            "git workspace update", "skipped", "no config.yaml yet — see config init above"
        )
    sources = gitworkspace.config_paths(cfg)
    if not sources:
        return StepResult(
            "git workspace update",
            "skipped",
            f"no workspace*.toml under {workspace_root()}, {config.hq_dir()} or "
            "git_workspace.path — place one, or `bh hq clone` a fleet that carries one",
        )
    if dry_run:
        return StepResult("git workspace update", "would", "would run `git workspace update`")

    linked = _link_workspace_config(sources)

    # `github_token=True` and no `env=`: the child environment is CONSTRUCTED by `run` itself
    # (bh-9qor). git-workspace resolves its root from $GIT_WORKSPACE and queries every provider's
    # GraphQL API with the token its `env_var` names — on beadhive-factory neither was set in the
    # invoking shell, and bh knew both. Nothing is written to disk: the token is derived fresh
    # from `gh auth token` into this one child's environment.
    res = run(
        ["git", "workspace", "update"],
        check=False,
        capture=True,
        timeout=GIT_WORKSPACE_TIMEOUT,
        github_token=True,
    )
    if res.returncode != 0:
        return StepResult("git workspace update", "failed", err_line(res))
    detail = "repos cloned/updated from providers"
    if linked is not None:
        detail += f"; linked {linked} -> {sources[0]}"
    return StepResult("git workspace update", "done", detail)


# ---- step 3: hq.remote (resolve + confirm + persist) --------------------------


def _step_hq_remote(*, auto: bool, dry_run: bool) -> StepResult:
    cfg = _cfg_or_none()
    if cfg is None:
        return StepResult("hq.remote", "skipped", "no config.yaml yet — see config init above")
    explicit = str(config.hq_cfg(cfg).get("remote", "") or "")
    if explicit:
        return StepResult("hq.remote", "skipped", f"already set: {explicit}")

    if dry_run:
        derived = config.hq_remote(cfg)
        detail = (
            f"would resolve + confirm (derives to {derived!r})"
            if derived
            else "would prompt — no derivable default (no `gh` login); needs --auto or a TTY"
        )
        return StepResult("hq.remote", "would", detail)

    remote = hq._confirm_remote(cfg, auto=auto)
    if not remote:
        return StepResult(
            "hq.remote",
            "failed",
            "unresolvable — set explicitly with `bh config set hq.remote <owner>/beadhive-hq`",
        )
    res = config.set_value("hq.remote", remote)
    if not res["ok"]:
        return StepResult("hq.remote", "failed", "; ".join(p["message"] for p in res["problems"]))
    return StepResult("hq.remote", "done", f"hq.remote = {remote}")


# ---- step 4: hq clone (never clobbers an existing hq_dir) ---------------------


def _reconcile_host_config_after_clone() -> list[str]:
    """Drop FLEET-classified leaves the host config still carries once a real ``fleet.yaml``
    exists — see :func:`beadhive.config.reconcile_host_after_fleet`, which owns the logic.

    Kept as a named seam because this module's step table and its tests refer to it, and
    because ``bh hq clone`` performs the SAME reconciliation itself now (bh-w2u9): provisioning
    calls clone, so by the time this runs there is usually nothing left to do — it stays as the
    belt-and-braces pass for a host whose clone predates that fix."""
    return config.reconcile_host_after_fleet()


def _step_hq_clone(*, dry_run: bool) -> StepResult:
    hq_dir = config.hq_dir()
    if hq_dir.exists():
        dropped = [] if dry_run else _reconcile_host_config_after_clone()
        detail = f"HQ already present at {hq_dir}"
        if dropped:
            detail += f"; reconciled stale fleet-shaped host key(s): {', '.join(dropped)}"
        return StepResult("hq clone", "skipped", detail)

    cfg = _cfg_or_none()
    remote = config.hq_remote(cfg) if cfg is not None else ""
    if not remote:
        return StepResult("hq clone", "skipped", "no hq.remote resolved yet — see the step above")
    if dry_run:
        return StepResult("hq clone", "would", f"would clone HQ from {remote} → {hq_dir}")

    try:
        hq.clone(auto=True)  # remote is already resolved+persisted above — never re-prompt
    except typer.Exit:
        return StepResult("hq clone", "failed", f"clone from {remote} failed — see output above")

    dropped = _reconcile_host_config_after_clone()
    detail = f"cloned HQ from {remote} → {hq_dir}"
    if dropped:
        detail += f"; reconciled stale fleet-shaped host key(s): {', '.join(dropped)}"
    return StepResult("hq clone", "done", detail)


# ---- step 5: git identity (marry the per-host + fleet halves) -----------------


def _step_git_identity(*, dry_run: bool) -> StepResult:
    """Fill this host's GLOBAL git identity gaps from bh's own two halves (bh-ijd4).

    GAP-FILL ONLY — :func:`beadhive.git_identity.establish` never overwrites a value git
    already carries, so the origin Mac (a full, working, human-owned identity) comes out of
    this untouched and reports every key as ``kept``. Reported as ``done`` even when
    everything was kept: the step ran and the host's identity is now known-good, which is the
    fact the operator needs; the detail line says which keys bh actually wrote."""
    fills = git_identity.establish(dry_run=dry_run)
    wrote = [f.key for f in fills if f.action == git_identity.SET]
    would = [f.key for f in fills if f.action == git_identity.WOULD]
    unresolved = [f"{f.key} ({f.detail})" for f in fills if f.action == git_identity.UNRESOLVED]
    if dry_run:
        detail = f"would set {', '.join(would)}" if would else "nothing to fill — already complete"
        if unresolved:
            detail += f"; unresolved: {', '.join(unresolved)}"
        return StepResult("git identity", "would", detail)
    ok, summary = git_identity.summary()
    detail = (f"set {', '.join(wrote)}; " if wrote else "no gaps to fill; ") + summary
    if unresolved:
        detail += f"; unresolved: {', '.join(unresolved)}"
    # A host that still cannot name an author is a FAILED step, not a quiet skip: every later
    # commit made there would be refused by git or land unattributed (bh-1atj — a host that
    # reports itself usable must actually be usable).
    return StepResult("git identity", "done" if ok else "failed", detail)


# ---- step 6: host init (register in the fleet roster) -------------------------


def _step_host_init(*, role: str, force: bool, dry_run: bool) -> StepResult:
    try:
        host_id = host.host_id()
    except FileNotFoundError:
        return StepResult("host init", "skipped", "no host identity yet — see config init above")

    hq_dir = config.hq_dir()
    if not hq_dir.exists():
        return StepResult("host init", "skipped", "no local HQ yet — see the hq clone step above")

    target = hosts.manifest_path(hq_dir, host_id)
    exists = target.exists()
    if exists and not force:
        return StepResult("host init", "skipped", f"already registered at {target}")
    if dry_run:
        action = "overwrite" if exists else "write"
        return StepResult("host init", "would", f"would {action} {target} (role={role})")

    written, wrote = host_cli.ensure_manifest(role=role, force=force)
    if not wrote:
        return StepResult("host init", "skipped", f"already registered at {written}")
    return StepResult("host init", "done", f"wrote {written} (role={role})")


# ---- step 6: bead sync (per-hive bootstrap-then-pull) ---------------------------

#: The three states a cloned hive's bead store can be in on a host, and the only three this
#: step distinguishes (bh-fxw6). They need different verbs, and conflating the last two is the
#: bug: `bd federation sync` moves state BETWEEN TWO EXISTING databases and is the wrong verb
#: for a host that has none.
STORE_READY = "ready"  # a local database exists — sync it
STORE_UNBOOTSTRAPPED = "unbootstrapped"  # published config, no database yet — bootstrap first
STORE_UNPUBLISHED = "unpublished"  # nothing to bootstrap FROM — the origin never committed one


#: The ref a hive's bead store is published under on its origin. The store travels as this
#: REF, not as tracked files under `.beads/` — which is exactly why the absence of a local
#: `.beads` says nothing about whether the origin has a store to bootstrap from (bh-22z70).
STORE_DATA_REF = "refs/dolt/data"


def _origin_publishes_store(hive_dir: Path) -> bool:
    """Whether `hive_dir`'s origin carries a bead store at :data:`STORE_DATA_REF`.

    ONE ``git ls-remote``, paid only on the branch that would otherwise answer "unpublished"
    wrongly — never on the path where a local database already settles the question.

    An unreachable or erroring origin returns ``False``, which preserves the pre-bh-22z70
    behaviour for that case: skip, rather than attempt a bootstrap we cannot know will work."""
    res = run(
        ["git", "ls-remote", "origin", STORE_DATA_REF],
        cwd=str(hive_dir),
        check=False,
        capture=True,
    )
    return res.returncode == 0 and bool((res.stdout or "").strip())


def _store_state(hive_dir: Path) -> str:
    """Which of the three states above ``hive_dir``'s bead store is in.

    Local filesystem facts settle it whenever a `.beads` directory exists, via
    :mod:`beadhive.store_locator` (mode-aware: an embedded hive keeps its database inside the
    clone, a server-mode one under bd's shared-server root).

    ``STORE_UNPUBLISHED`` is a fact about the ORIGIN repo, and it CANNOT be read off this
    host's filesystem — conflating the two was bh-22z70. `.beads/` is gitignored by default
    (bd ships that `.gitignore` itself), so a fresh clone of a perfectly well-published hive
    has no `.beads` at all. This function used to call that "unpublished", so provisioning
    skipped the bootstrap and left the new host with no store and `bd` reporting "no beads
    database found" — with no signal that a working verb had simply never been attempted.

    MEASURED on github/briancripe/nvidia-hackathon — the hive the previous docstring cited as
    proof it could NOT be bootstrapped: `git ls-files .beads` is indeed empty, AND the origin
    carries `refs/dolt/data` (a push from another host moved it adbf205 -> c1238c1). `bd
    bootstrap` then hydrated it in one command, 27,905 chunks. "Are the files tracked" and "is
    a store published" are different questions; only the second decides bootstrappability.

    So with no local `.beads`, ask the ORIGIN (:func:`_origin_publishes_store`) rather than
    inferring from its absence."""
    beads = Path(hive_dir) / ".beads"
    if not beads.is_dir() or not any(beads.iterdir()):
        # No local store — the ORIGIN decides whether there is anything to bootstrap FROM.
        return STORE_UNBOOTSTRAPPED if _origin_publishes_store(hive_dir) else STORE_UNPUBLISHED
    has_db = store_locator.database_dir(beads.parent).is_dir()
    return STORE_READY if has_db else STORE_UNBOOTSTRAPPED


def _bootstrap_hive(cfg, entry: dict) -> str:
    """Hydrate one hive's bead database from its committed remote — ``""`` on success, else the
    error line.

    ``Engine.bootstrap`` (``bd bootstrap``) is the SANCTIONED seam and the only one used here:
    ``bh hq clone`` hydrates HQ through it, and ``hub._fetch_cache`` hydrates an uncloned hive
    through it (hq.py:22 says so outright). A hand-rolled ``refs/dolt/data`` fetch is explicitly
    not the path. Proven by hand on the failing host: 164,050 chunks, first try, after which
    ``bd list`` returned real beads."""
    res = engine.get_engine(cfg).bootstrap(registry.hive_dir(entry))
    return "" if res.returncode == 0 else (err_line(res) or f"exit {res.returncode}")


def _step_bead_sync(*, dry_run: bool, hives: list[str] | None = None) -> StepResult:
    """BOOTSTRAP the hives that have no local database, then sync the hives this host carries.

    A NEW HOST HAS NO DATABASE TO SYNC (bh-fxw6). This step used to call ``hive_sync`` straight
    away, which drives ``bd federation sync`` — bidirectional movement between two EXISTING
    databases. Measured on beadhive-factory: the clone carried `.beads/` with its remote and its
    ``metadata.json``, so the host knew both the remote AND the database name, and step 7 still
    reported ``2/2 hive(s) failed`` while ``bd list`` said "no beads database found". bh
    bootstraps HQ and bootstraps a hive for the hub cache; provisioning a HOST did not bootstrap
    that host's hives. It does now, and only when the database is absent — a host that already
    has one is untouched, so the step stays the no-op on re-run that it already promises.

    BOOTSTRAP IS THE ONLY UPSTREAM MOVEMENT THIS STEP MAKES (bh-libi, settled deliberately —
    federation is hive-to-hive and moves nothing upstream, so the question had to be answered
    on its own terms). ``bd bootstrap`` hydrates the full remote state, so a hive it just ran
    on IS current; the sync behind it correctly does nothing in a fleet with no peer towns.

    A ``bd dolt pull`` for the already-bootstrapped re-run was weighed and REJECTED, and NOT
    for cost — ``Engine.pull_state`` already exists and the call would be one line. It is
    rejected on blast radius and redundancy: a pull is a MERGE into a live store that may hold
    primary and carry unpushed local commits, with outcomes this step cannot resolve, and it
    would fire across every hive on the host on every run. Freshness is already handled where
    it matters — ``work._pull_state`` pulls the ONE hive being acted on immediately before
    `claim`/`resume` read its bead state — and the push direction is an explicit operator verb
    (`bh hive sync-remote`). If a fleet-wide pull is ever wanted it belongs beside that verb,
    symmetrical and operator-driven, not fired implicitly by provisioning.

    ``hives`` from the answers file NARROWS what is present on disk; it never widens it. A
    subset is the whole point of a second host with less disk or a narrower scope (bh-q160.2),
    and an EMPTY list is a legitimate answer meaning "carry none" — which is why the filter
    distinguishes None (all) from [] (none) rather than treating both as falsey.
    """
    if not config.hq_dir().exists():
        return StepResult("bead sync", "skipped", "no local HQ yet — see the hq clone step above")
    present = _present_hive_entries(_cfg_or_none())
    if not present:
        return StepResult("bead sync", "skipped", "no hive clones present on disk yet")

    cfg = _cfg_or_none()
    selected = present
    if hives is not None:
        wanted = set(hives)
        unselected = sorted(str(e["prefix"]) for e in present if str(e["prefix"]) not in wanted)
        selected = [e for e in present if str(e["prefix"]) in wanted]
        if not selected:
            note = f" (present but not selected: {', '.join(unselected)})" if unselected else ""
            return StepResult("bead sync", "skipped", f"no hives selected by `hives:`{note}")

    by_state: dict[str, list[dict]] = {
        STORE_READY: [],
        STORE_UNBOOTSTRAPPED: [],
        STORE_UNPUBLISHED: [],
    }
    for entry in selected:
        by_state[_store_state(registry.hive_dir(entry))].append(entry)
    unpublished = [str(e["prefix"]) for e in by_state[STORE_UNPUBLISHED]]
    # REPORTED, never bootstrapped or synced: there is nothing on the origin to hydrate from,
    # and calling that a sync failure would send the operator looking at this host.
    note = (
        f"; {len(unpublished)} hive(s) have no committed `.beads` to bootstrap from "
        f"(never published upstream): {', '.join(unpublished)}"
        if unpublished
        else ""
    )
    syncable = by_state[STORE_READY] + by_state[STORE_UNBOOTSTRAPPED]
    if not syncable:
        return StepResult("bead sync", "skipped", (note or "; nothing to sync").lstrip("; "))

    prefixes = [str(e["prefix"]) for e in syncable]
    to_bootstrap = [str(e["prefix"]) for e in by_state[STORE_UNBOOTSTRAPPED]]
    if dry_run:
        plan = f"would sync {len(prefixes)} hive(s): {', '.join(prefixes)}"
        if to_bootstrap:
            plan += f"; would bootstrap first (no local database): {', '.join(to_bootstrap)}"
        return StepResult("bead sync", "would", plan + note)

    offending: list[str] = []
    bootstrapped: list[str] = []
    for entry in by_state[STORE_UNBOOTSTRAPPED]:
        prefix = str(entry["prefix"])
        if problem := _bootstrap_hive(cfg, entry):
            offending.append(f"{prefix} (bootstrap: {problem})")
        else:
            bootstrapped.append(prefix)
    failed_bootstrap = {o.split(" (bootstrap:", 1)[0] for o in offending}
    for prefix in (p for p in prefixes if p not in failed_bootstrap):
        offending.extend(hive_sync.hive_sync(hive_id=prefix))

    did = f"bootstrapped {len(bootstrapped)}: {', '.join(bootstrapped)}; " if bootstrapped else ""
    if offending:
        return StepResult(
            "bead sync",
            "failed",
            f"{did}{len(offending)}/{len(prefixes)} hive(s) failed or paused: "
            f"{', '.join(offending)}{note}",
        )
    # "up to date", not "synced" (bh-s0wj). `hive_sync` returns only an OFFENDING list, so this
    # step knows that nothing FAILED and cannot know that anything MOVED. It used to say
    # "synced 1 hive(s): bh" one line under a per-hive line reading "no federation peers —
    # nothing to sync (upstream moves via `bh hive sync-remote`)" — which after bh-libi is the
    # correct and expected state for every hive in this fleet. Zero hives were synced.
    #
    # Deliberately NOT fixed by widening hive_sync's return to a synced/skipped/offending triple:
    # no other caller wants it (`bh hive sync` renders its own table), and this line only ever
    # needed to stop claiming more than it knows. Same shape as bh-1atj — a summary overstating
    # what happened — but on the SUCCESS path, which is where nobody looks twice.
    return StepResult(
        "bead sync",
        "done",
        f"{did}{len(prefixes)} hive(s) up to date: {', '.join(prefixes)}{note}",
    )


# ---- step 7: fix permissions (chmod 700 .beads) --------------------------------


def _step_fix_permissions(*, dry_run: bool) -> StepResult:
    cfg = _cfg_or_none()
    dirs = _beads_dirs(cfg)
    if not dirs:
        return StepResult("fix permissions", "skipped", "no `.beads` directories present yet")

    wrong = _wrong_perms(cfg)
    if not wrong:
        return StepResult("fix permissions", "skipped", f"{len(dirs)} `.beads` dir(s) already 0700")
    if dry_run:
        return StepResult(
            "fix permissions",
            "would",
            f"would chmod 700 on {len(wrong)} dir(s): {', '.join(str(d) for d in wrong)}",
        )
    for d in wrong:
        d.chmod(BEADS_MODE)
    return StepResult("fix permissions", "done", f"chmod 700 on {len(wrong)} `.beads` dir(s)")


# ---- step 8: verify (the gate) -------------------------------------------------


def status(cfg=None) -> list[Check]:
    """Read-only: is THIS host fully provisioned, and what's missing. Every check here is
    REQUIRED — unlike :mod:`beadhive.hive_ready`'s required/optional split, there is no
    "optional integration" concept at host-provisioning scope: every listed condition is
    precisely what "usable" means. :func:`_step_verify` is a thin wrapper over this that also
    renders it as a :class:`StepResult`; a future ``bh host status`` CLI verb would call this
    directly."""
    load_error = ""
    if cfg is None:
        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = None
        except config.ConfigError as exc:
            cfg = None
            load_error = str(exc)

    checks: list[Check] = []

    checks.append(Check("host identity", host.path().exists(), str(host.path())))
    checks.append(Check("config.yaml", config.config_path().exists(), str(config.config_path())))
    # Distinct from mere file-existence above: a host config that CONFLICTS with a real
    # fleet.yaml (config.ConfigError) is exactly the "partial, undiagnosed state" bh-twc8.1's
    # motivating incident describes — surface it by name instead of folding it into the
    # `_cfg_or_none()`-style silent-skip every other step uses.
    checks.append(
        Check("config loads cleanly", not load_error, load_error or "no fleet/host key conflicts")
    )

    hq_dir = config.hq_dir()
    hq_present = (hq_dir / ".beads").is_dir()
    checks.append(Check("HQ local store", hq_present, str(hq_dir)))

    remote = ""
    if hq_present:
        got = run(
            ["git", "-C", str(hq_dir), "remote", "get-url", "origin"],
            check=False,
            capture=True,
            timeout=GIT_TIMEOUT,
        )
        remote = (got.stdout or "").strip() if got.returncode == 0 else ""
    checks.append(Check("HQ remote wired", bool(remote), remote or "no `origin` remote"))

    manifest_ok = False
    manifest_detail = "no host identity — cannot resolve host_id"
    if hq_present:
        try:
            hid = host.host_id()
        except FileNotFoundError:
            pass
        else:
            manifest_ok = hosts.manifest_path(hq_dir, hid).exists()
            manifest_detail = (
                f"hosts/{hid}.yaml"
                if manifest_ok
                else f"missing hosts/{hid}.yaml — run `bh host init --role <role>`"
            )
    checks.append(Check("registered in HQ roster", manifest_ok, manifest_detail))

    # VALIDATE IDENTITY BEFORE ANY COMMIT, not at merge (bh-ijd4). An unsigned or misattributed
    # commit discovered at merge is discovered after an agent has already done the work;
    # discovered here it costs nothing. Required, like every other check in this list: a host
    # that cannot name an author cannot commit at all — git refuses outright.
    id_ok, id_detail = git_identity.summary(cfg)
    checks.append(Check("git identity", id_ok, id_detail))
    # Only asserted when the fleet has actually turned the gate on — a host whose commits are
    # never signature-gated is not broken for lacking an enrolled key, and failing every
    # unprepared host's provisioning over an off-by-default policy would be the same
    # block-everything mistake the flag's default guards against.
    if cfg is not None and config.enforce_signing(cfg, None):
        sign_ok, sign_detail = git_identity.signing_summary()
        checks.append(Check("signature trust (work.enforce_signing on)", sign_ok, sign_detail))

    wrong = _wrong_perms(cfg)
    checks.append(
        Check(
            ".beads permissions",
            not wrong,
            "all 0700"
            if not wrong
            else f"{len(wrong)} dir(s) not 0700: {', '.join(str(d) for d in wrong)}",
        )
    )
    return checks


def _step_verify() -> StepResult:
    """Always actually runs — `status()` is read-only, so there is nothing to preview
    differently under ``--dry-run``; it honestly reports the CURRENT state either way.

    THE VERDICT IS SCOPED TO WHAT WAS ACTUALLY CHECKED (bh-1atj). Every check in `status()` is
    about this host's own wiring — identity, config, HQ store, HQ remote, roster registration,
    permissions. None of them asks whether the host carries a single hive, and on
    beadhive-factory all of them passed on a host with zero hive clones: "host is fully
    provisioned and usable" printed four lines above `adopt` failing because the host carried
    no repos at all. A general claim is only made when the host has something to serve; with
    zero clones the step still succeeds — a host that carries no hives yet is not BROKEN — but
    it says what it verified instead of claiming what it did not."""
    checks = status()
    failed = [c for c in checks if not c.ok]
    if failed:
        return StepResult("verify", "failed", "; ".join(f"{c.label}: {c.detail}" for c in failed))
    hives = _present_hive_entries(_cfg_or_none())
    if not hives:
        return StepResult(
            "verify",
            "done",
            f"host identity, config, HQ wiring and roster registration verified — but this host "
            f"carries NO hive clones ({workspace_root()} is empty of them), so it can serve none "
            f"yet; `usable` is not claimed",
        )
    return StepResult(
        "verify", "done", f"host is fully provisioned and usable ({len(hives)} hive(s) present)"
    )


# ---- orchestration --------------------------------------------------------------


# ---- step 8: adopt (the operator's actual goal, and the only fleet-visible step) -----------


def _step_adopt(*, adopt: list[str], dry_run: bool, prior: list[StepResult]) -> StepResult:
    """Take primary for each hive named in the answers file — last, and only on a clean run.

    FAIL-CLOSED ON PRIOR FAILURE. If any earlier step failed, this adopts NOTHING and says so.
    A half-provisioned host that has already grabbed the fence and lease for a hive is worse
    than one that failed cleanly: the lease is fleet-visible, other hosts now defer to a host
    that does not work, and recovering means a forced takeover somebody has to notice is needed.

    Per-hive failures do NOT roll back the hives already adopted. `host_adopt.adopt` is itself
    two-phase and fail-closed per hive, so each adoption either happened completely or not at
    all; unwinding a completed one would mean a second fence CAS purely to tidy up, which is
    more fleet churn than the partial state it would be hiding.
    """
    if not adopt:
        return StepResult("adopt", "skipped", "no `adopt:` in the answers file")

    if failed := [r.name for r in prior if r.status == "failed"]:
        return StepResult(
            "adopt",
            "skipped",
            f"adopting NOTHING — earlier step(s) failed: {', '.join(failed)}",
        )

    if dry_run:
        return StepResult("adopt", "would", f"would take primary for: {', '.join(adopt)}")

    adopted: list[str] = []
    for prefix in adopt:
        try:
            host_cli.adopt_one(prefix)
        except Exception as exc:  # noqa: BLE001 - one hive's refusal must not hide the rest
            done = f" (adopted first: {', '.join(adopted)})" if adopted else ""
            return StepResult("adopt", "failed", f"{prefix}: {exc}{done}")
        adopted.append(prefix)
    return StepResult("adopt", "done", f"primary for: {', '.join(adopted)}")


def provision(
    *,
    role: str,
    auto: bool = False,
    dry_run: bool = False,
    force_manifest: bool = False,
    adopt: list[str] | None = None,
    hives: list[str] | None = None,
) -> list[StepResult]:
    """Run every step of the new-host adoption path, in :data:`PLAN` order, probing before each
    so a partial prior run (or an already-fully-provisioned host) resumes/re-verifies cleanly.

    Returns the ordered :class:`StepResult`\\ s (always ``len(PLAN)`` of them — one per name in
    :data:`PLAN`, in the same order). One step raising is caught here and turned into a
    ``failed`` result rather than aborting the run: a single misbehaving step must never stop
    the LATER steps (especially the verifying gate) from reporting honestly. Callers decide the
    process exit code from the results (the CLI command: non-zero if any is ``failed``)."""
    steps = (
        lambda: _step_setup_check(dry_run=dry_run),
        lambda: _step_config_init(dry_run=dry_run),
        lambda: _step_hq_remote(auto=auto, dry_run=dry_run),
        lambda: _step_hq_clone(dry_run=dry_run),
        lambda: _step_git_identity(dry_run=dry_run),
        lambda: _step_git_workspace_update(dry_run=dry_run),
        lambda: _step_host_init(role=role, force=force_manifest, dry_run=dry_run),
        lambda: _step_bead_sync(dry_run=dry_run, hives=hives),
        lambda: _step_fix_permissions(dry_run=dry_run),
        lambda: _step_verify(),
        lambda: _step_adopt(adopt=adopt, dry_run=dry_run, prior=results),
    )
    adopt = list(adopt or [])
    results: list[StepResult] = []
    for name, step in zip(PLAN, steps, strict=True):
        try:
            results.append(step())
        except Exception as exc:  # noqa: BLE001 - one step's crash must not abort the whole run
            results.append(StepResult(name, "failed", f"unexpected error: {exc}"))
    return results
