"""``bh host provision`` — one idempotent, resumable verb for the whole new-host adoption path
(bh-twc8.1).

Adopting a new host today is a hand-assembled sequence with no verb, no preconditions checked,
and no way to resume a partial run::

    bh config init                                  # mints host.yaml / host_id
    git workspace update                            # re-clone repos from providers
    bh config set hq.remote <owner>/beadhive-hq     # cannot be inherited: it is how a host FINDS HQ
    bh hq clone                                     # HQ config + aggregate
    bh host init --role <primary-default|worker>    # register in the fleet roster
    bh bd sync                                      # per hive: pull bead state
    chmod 700 <hive>/.beads                         # or bh warns on every command
    bh doctor

This module mechanizes that path as :func:`provision`: eight ordered steps, each of which
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

from . import config, gitworkspace, hive_sync, host, host_cli, hosts, hq, registry
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
    "config init",
    "git workspace update",
    "hq.remote",
    "hq clone",
    "host init",
    "bead sync",
    "fix permissions",
    "verify",
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


def _step_git_workspace_update(*, dry_run: bool) -> StepResult:
    cfg = _cfg_or_none()
    if cfg is None:
        return StepResult(
            "git workspace update", "skipped", "no config.yaml yet — see config init above"
        )
    if not gitworkspace.enabled(cfg):
        return StepResult(
            "git workspace update",
            "skipped",
            "git_workspace.enabled is false — nothing to update",
        )
    sources = gitworkspace.config_paths(cfg)
    if not sources:
        return StepResult(
            "git workspace update",
            "skipped",
            f"no workspace*.toml under {workspace_root()} (or git_workspace.path) — place one",
        )
    if dry_run:
        return StepResult("git workspace update", "would", "would run `git workspace update`")

    res = run(
        ["git", "workspace", "update"], check=False, capture=True, timeout=GIT_WORKSPACE_TIMEOUT
    )
    if res.returncode != 0:
        return StepResult("git workspace update", "failed", err_line(res))
    return StepResult("git workspace update", "done", "repos cloned/updated from providers")


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


# ---- step 5: host init (register in the fleet roster) -------------------------


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


# ---- step 6: bead sync (per-hive pull) -----------------------------------------


def _step_bead_sync(*, dry_run: bool) -> StepResult:
    if not config.hq_dir().exists():
        return StepResult("bead sync", "skipped", "no local HQ yet — see the hq clone step above")
    present = _present_hive_entries(_cfg_or_none())
    if not present:
        return StepResult("bead sync", "skipped", "no hive clones present on disk yet")

    prefixes = [str(e["prefix"]) for e in present]
    if dry_run:
        return StepResult(
            "bead sync", "would", f"would sync {len(prefixes)} hive(s): {', '.join(prefixes)}"
        )

    offending: list[str] = []
    for prefix in prefixes:
        offending.extend(hive_sync.hive_sync(hive_id=prefix))
    if offending:
        return StepResult(
            "bead sync",
            "failed",
            f"{len(offending)}/{len(prefixes)} hive(s) failed or paused: {', '.join(offending)}",
        )
    return StepResult("bead sync", "done", f"synced {len(prefixes)} hive(s): {', '.join(prefixes)}")


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
    differently under ``--dry-run``; it honestly reports the CURRENT state either way."""
    checks = status()
    failed = [c for c in checks if not c.ok]
    if failed:
        return StepResult("verify", "failed", "; ".join(f"{c.label}: {c.detail}" for c in failed))
    return StepResult("verify", "done", "host is fully provisioned and usable")


# ---- orchestration --------------------------------------------------------------


def provision(
    *, role: str, auto: bool = False, dry_run: bool = False, force_manifest: bool = False
) -> list[StepResult]:
    """Run every step of the new-host adoption path, in :data:`PLAN` order, probing before each
    so a partial prior run (or an already-fully-provisioned host) resumes/re-verifies cleanly.

    Returns the ordered :class:`StepResult`\\ s (always ``len(PLAN)`` of them — one per name in
    :data:`PLAN`, in the same order). One step raising is caught here and turned into a
    ``failed`` result rather than aborting the run: a single misbehaving step must never stop
    the LATER steps (especially the verifying gate) from reporting honestly. Callers decide the
    process exit code from the results (the CLI command: non-zero if any is ``failed``)."""
    steps = (
        lambda: _step_config_init(dry_run=dry_run),
        lambda: _step_git_workspace_update(dry_run=dry_run),
        lambda: _step_hq_remote(auto=auto, dry_run=dry_run),
        lambda: _step_hq_clone(dry_run=dry_run),
        lambda: _step_host_init(role=role, force=force_manifest, dry_run=dry_run),
        lambda: _step_bead_sync(dry_run=dry_run),
        lambda: _step_fix_permissions(dry_run=dry_run),
        lambda: _step_verify(),
    )
    results: list[StepResult] = []
    for name, step in zip(PLAN, steps, strict=True):
        try:
            results.append(step())
        except Exception as exc:  # noqa: BLE001 - one step's crash must not abort the whole run
            results.append(StepResult(name, "failed", f"unexpected error: {exc}"))
    return results
