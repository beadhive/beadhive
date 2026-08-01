"""Factory HQ — the one durable central store.

HQ is the aggregation primary (the cross-hive view that supersedes the disposable ``~/.ws/hub``)
that ALSO holds canonical hq-prefixed control-plane beads. A SINGLETON (kind=hq), registered
ONLY in the ws registry under the RESERVED SYNTHETIC IDENTITY ``local/factory/hq`` — LOCAL infra
like the hub/cache (no remote, never a git-workspace provider). It lives at ``config.hq_dir()``.

`ws hq init` stands it up: bd-init the store (prefix ``hq``), register the synthetic identity,
then move the aggregation role onto it (``bd repo add`` every registered hive + sync). The old
``~/.ws/hub`` is subsumed — rebuildable, no data migration (re-add + sync at the new location).

bh-e0y8.2 extends the SAME verb to turn the local-only store into a *distributable* repo: once
the store exists, ``init`` also (idempotently) scaffolds the documented layout (``fleet.yaml`` +
``workspace.toml`` + ``hosts/``), wires the configured ``hq.remote``, and — after a VERIFIED
three-level backup — pushes ``main`` and ``refs/dolt/data`` for the first time. A re-run once the
remote is already configured is a clean no-op (superseding the old "refuse a second `hq init`"
posture, which predates the remote/distribution story): the singleton guard now only prevents
create-time double-registration, not a harmless idempotent re-run. See ``_wire_remote``.

bh-e0y8.4 adds ``hq clone`` — the mirror image, for a SECOND host that has no local HQ at all:
``git clone`` the remote's ``main`` + hydrate bead state from ``refs/dolt/data`` via ``bd
bootstrap`` (the same seam ``hub._fetch_cache`` already uses to hydrate an uncloned hive), then
register the synthetic identity so ``bh hq bd ready`` resolves to it. See ``clone``.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer

from . import config, engine, gitworkspace, hub, registry
from .bd import err_line
from .run import run

GIT_TIMEOUT = 30.0  # seconds — bounds a git ls-remote/fetch/push so a wedged remote can't hang
BD_TIMEOUT = 120.0  # seconds — matches engine.STATE_TIMEOUT for the bd dolt/status calls below

# fleet.yaml carries only the shared/fleet-truth keys (docs/design/multi-host-model-adr.md):
# host-local keys (worktrees.path, otel.endpoint, identity, dispatch budgets, …) stay out.
_FLEET_KEYS = (
    "schema_version", "delimiter", "orgs", "dimensions", "exclude",
    "managed_repos", "work", "passthrough",
)
# regenerable — pure waste in a backup (bh-e0y8.2 acceptance amendment).
_DOLT_EXCLUDE_DIR = "git-remote-cache"


def init_store() -> list:
    """Core of ``bh hq init`` (no singleton gate, no ``typer.Exit``): stand up the store.

    Reuses ``hub.ensure_store`` to bd-init a durable git+bd store at ``config.hq_dir()`` with
    prefix ``hq``, registers the reserved synthetic identity, and reuses ``hub.sync`` to
    ``bd repo add`` every registered hive + sync (the aggregation role moves off the disposable
    hub to HQ). Returns ``hub.sync``'s failed list. Callers own the singleton check — this is
    the seam ``escalate``'s consent-prompted auto-init calls directly (bh-ufne), never a
    subprocess."""
    # Create the durable store FIRST (prefix hq) — so a bd-init failure never leaves a dangling
    # registration — then register the synthetic identity in the ws registry.
    hq = hub.ensure_store(config.hq_dir(), registry.HQ_PREFIX)
    registry.register(
        registry.HQ_PROVIDER, registry.HQ_ORG, registry.HQ_REPO,
        registry.HQ_PREFIX, registry.HQ_KIND,
    )
    typer.echo(f"✓ Factory HQ store initialized at {hq} (prefix '{registry.HQ_PREFIX}', kind=hq)")

    # Aggregation moves onto HQ: hub.sync now resolves the target to HQ (it is registered), so
    # this bd repo add's every registered hive into HQ and syncs. Reuse over a parallel mechanism.
    return hub.sync()


def init(*, dry_run: bool = False, auto: bool = False, create: bool = False) -> None:
    """Stand up the Factory HQ store (first call only) and — idempotently — scaffold its
    distributable layout, wire the configured remote, and push (bh-e0y8.2).

    First call ever (no HQ registered): creates the store (``init_store``), then wires the
    remote. Every later call: the store already exists — skip straight to (idempotent) remote
    wiring, which itself no-ops once the remote is configured. ``--dry-run`` previews the
    pre-push backup plan with zero mutation; against a not-yet-created store it can only report
    that the store itself would be created (there is nothing local yet to back up)."""
    cfg = config.load()
    existing = registry.hive_of_kind(cfg, registry.HQ_KIND)
    if existing is None:
        if dry_run:
            typer.echo(
                f"DRY-RUN would stand up Factory HQ store at {config.hq_dir()} "
                f"(prefix '{registry.HQ_PREFIX}')"
            )
            return
        failed = init_store()
        if failed:
            raise typer.Exit(1)
        cfg = config.load()  # init_store just registered HQ — reload before remote wiring
    else:
        triplet = f"{existing['provider']}/{existing['org']}/{existing['repo']}"
        typer.echo(f"✓ Factory HQ already initialized: {triplet} → {config.hq_dir()} (no-op)")

    _wire_remote(cfg, dry_run=dry_run, auto=auto, create=create)


# ---- bh hq clone: bootstrap a host with no local HQ (bh-e0y8.4) -------------


def clone(*, auto: bool = False) -> None:
    """Bootstrap a fresh host that has no local HQ: clone ``main`` (fleet.yaml/workspace.toml/
    hosts/) from the configured ``hq.remote`` and hydrate bead state from ``refs/dolt/data`` via
    ``bd bootstrap`` — the mirror image of ``init``'s remote-wiring path. Refuses (never
    clobbers) when ``config.hq_dir()`` already exists.

    Reuses ``_remote_urls``/``config.hq_remote`` for the URL and the SAME ``bd bootstrap`` seam
    ``hub._fetch_cache`` already relies on to hydrate an uncloned hive's beads from a fresh git
    clone (``engine.Engine.bootstrap``) — no hand-rolled ``refs/dolt/data`` fetch. Registers the
    reserved synthetic HQ identity on success (mirroring ``init_store``'s create-then-register
    order), so ``bh hq bd ready`` resolves to this store afterward."""
    hq_dir = config.hq_dir()
    if hq_dir.exists():
        typer.echo(
            f"✗ {hq_dir} already exists — refusing to clobber it; `{config.BINARY_ALIAS} hq "
            "clone` is only for a host with no local HQ",
            err=True,
        )
        raise typer.Exit(1)

    cfg = config.load()
    remote = _confirm_remote(cfg, auto=auto)
    if not remote:
        typer.echo(
            "✗ hq.remote is unset and unresolvable — nothing to clone from; set one with "
            f"`{config.BINARY_ALIAS} config set hq.remote <owner>/beadhive-hq`",
            err=True,
        )
        raise typer.Exit(1)

    git_url, _dolt_url = _remote_urls(remote)
    typer.echo(f"hq clone: cloning {git_url} → {hq_dir}")
    cloned = run(
        ["git", "clone", git_url, str(hq_dir)], check=False, capture=True, timeout=GIT_TIMEOUT
    )
    if cloned.returncode:
        typer.echo(f"✗ git clone {git_url} failed: {err_line(cloned)}", err=True)
        raise typer.Exit(1)

    bootstrapped = engine.get_engine(cfg).bootstrap(hq_dir)
    if bootstrapped.returncode:
        typer.echo(f"✗ bd bootstrap failed: {err_line(bootstrapped)}", err=True)
        raise typer.Exit(1)

    registry.register(
        registry.HQ_PROVIDER, registry.HQ_ORG, registry.HQ_REPO,
        registry.HQ_PREFIX, registry.HQ_KIND,
    )
    typer.echo(f"✓ Factory HQ cloned from {git_url} → {hq_dir}")


# ---- remote wiring: scaffold + backup + push (bh-e0y8.2) --------------------


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)


def _bd(args: list[str], cwd: Path):
    return run(["bd", "-C", str(cwd), *args], check=False, capture=True, timeout=BD_TIMEOUT)


def _remote_urls(remote: str) -> tuple[str, str]:
    """(git clone URL, bd Dolt remote URL) for ``<owner>/<repo>`` — GitHub, the only provider
    ``config.hq_remote`` can derive today (mirrors ``hub._hive_url``'s github fallback). bd's
    Dolt-on-git-ref transport needs its own ``git+ssh://`` scheme (verified against a real bd
    binary), distinct from git's scp-like clone form."""
    return f"git@github.com:{remote}.git", f"git+ssh://git@github.com/{remote}.git"


def _confirm_remote(cfg: dict, *, auto: bool) -> str:
    """Resolve HQ's remote, CONFIRMING it with the operator rather than acting on a guess.

    Wiring HQ's remote is a one-way fleet decision — it pushes ``main`` + ``refs/dolt/data``
    and fixes which HQ this fleet answers to — so the derived owner is a SUGGESTION offered as
    the prompt default, never a value taken silently (bh-mw97). ``--auto`` (and any non-TTY,
    where there is nobody to ask) takes the derived value as-is, which is the CI/headless
    path. Returns "" only when derivation missed AND we could not ask."""
    derived = config.hq_remote(cfg)
    if auto or not sys.stdin.isatty():
        return derived
    typer.echo(
        "HQ remote — the fleet-wide Factory HQ repo this host pushes to. It must already "
        "exist and be EMPTY."
    )
    answer = typer.prompt("  <owner>/<repo>", default=derived or None)
    return str(answer or "").strip()


# A 404 is the ONLY probe failure `--create` may answer by creating the repo. Auth failures
# ("Permission denied (publickey)") and network failures ("Could not resolve hostname") must
# keep failing loudly — creating a repo because the network blinked would be the wrong repair.
_MISSING_REPO_MARKERS = ("repository not found", "does not exist", "not found")


def _remote_missing(probe) -> bool:
    """True when the ls-remote failure specifically means "no such repo"."""
    blob = f"{probe.stdout or ''}{probe.stderr or ''}".lower()
    return any(marker in blob for marker in _MISSING_REPO_MARKERS)


def _should_create(remote: str, *, auto: bool, create: bool) -> bool:
    """Whether to create the missing repo: explicit ``--create``, else an interactive offer.

    ``--auto`` never creates on its own (bh-aee3) — a headless run that invents repositories
    because a name was typo'd is strictly worse than one that fails. Non-TTY likewise falls
    through to the existing hard failure: there is nobody to ask."""
    if create:
        return True
    if auto or not sys.stdin.isatty():
        return False
    return typer.confirm(f"  {remote} does not exist — create it private and empty?", default=False)


def _create_repo(remote: str, *, dry_run: bool) -> bool:
    """Create ``remote`` as a PRIVATE, EMPTY GitHub repo. Returns False on failure.

    Deliberately no ``--source``/``--push`` (bh-aee3). Empty is the requirement, not a
    shortcut: ``_wire_remote`` below owns the first push — backup, scaffold, ``remote add``,
    ``push main``, ``bd dolt push`` — and it refuses any remote that already carries refs. A
    seeded repo would abort that path, skip ``_take_backup``, and (because ``--source`` adds
    ``origin`` itself) make the whole wiring step a silent no-op."""
    if dry_run:
        typer.echo(f"  DRY-RUN: would create {remote} as a private, empty repo")
        return False
    typer.echo(f"  creating {remote} (private, empty)…")
    done = run(["gh", "repo", "create", remote, "--private"], check=False, capture=True,
               timeout=GIT_TIMEOUT)
    if done.returncode != 0:
        typer.echo(f"✗ gh repo create {remote} failed: {err_line(done)}", err=True)
        return False
    typer.echo(f"  ✓ created {remote}")
    return True


def _wire_remote(
    cfg: dict, *, dry_run: bool = False, auto: bool = False, create: bool = False
) -> None:
    """Idempotently scaffold HQ's distributable layout, add its git remote, and push ``main`` +
    ``refs/dolt/data`` — the FIRST time only. A no-op once the remote is already configured;
    refuses (never force-pushes) when the remote is unreachable or already carries content.
    Before the very first push, takes and verifies a three-level backup (``_take_backup``) — the
    highest-value backup point in HQ's lifecycle, since giving a database a remote is what makes
    a schema migration a one-way fleet decision (bh-e0y8.2's acceptance amendment)."""
    hq_dir = config.hq_dir()
    if not (hq_dir / ".beads").is_dir():
        return  # no local store yet (fresh --dry-run) — nothing to wire

    already = _git(["remote", "get-url", "origin"], hq_dir)
    if already.returncode == 0:
        typer.echo(f"✓ HQ remote already configured ({(already.stdout or '').strip()}) — no-op")
        return

    remote = _confirm_remote(cfg, auto=auto)
    if not remote:
        typer.echo(
            "  (hq.remote is unset and unresolvable — skipping remote wiring; set one with "
            f"`{config.BINARY_ALIAS} config set hq.remote <owner>/beadhive-hq`)"
        )
        return

    git_url, dolt_url = _remote_urls(remote)
    tag = "DRY-RUN " if dry_run else ""
    typer.echo(f"{tag}hq init: wiring remote {git_url}")

    # Reachability + diverging-content refusal — BEFORE any backup/push; never force-pushes.
    # Scoped to refs/heads/* (the code branch our non-force `git push origin main` could
    # actually conflict with) — NOT refs/dolt/data, which lives outside refs/heads/* entirely
    # and is exactly what backup level 3 below exists to protect when it's already present on
    # an otherwise-fresh remote (bh-e0y8.2 amendment).
    probe = _git(["ls-remote", "--heads", git_url], hq_dir)
    if probe.returncode != 0 and _remote_missing(probe):
        if _should_create(remote, auto=auto, create=create):
            created = _create_repo(remote, dry_run=dry_run)
            if dry_run:
                # Nothing was created, so there is no reachable remote to go on previewing
                # against — stop at 0 (a preview that reports failure is a lie).
                typer.echo("  DRY-RUN: stopping here — the remote does not exist yet")
                return
            if not created:
                raise typer.Exit(1)
            probe = _git(["ls-remote", "--heads", git_url], hq_dir)
    if probe.returncode != 0:
        hint = ""
        if _remote_missing(probe):
            hint = " — pass --create to create it private and empty"
        typer.echo(
            f"✗ HQ remote {git_url} is unreachable — refusing to push: {err_line(probe)}{hint}",
            err=True,
        )
        raise typer.Exit(1)
    if (probe.stdout or "").strip():
        typer.echo(
            f"✗ HQ remote {git_url} already has content — refusing to push over it (never "
            "force-pushes); point hq.remote at an empty repo, or reconcile manually.",
            err=True,
        )
        raise typer.Exit(1)

    plan = _take_backup(hq_dir, git_url, cfg, dry_run=dry_run)
    _print_backup_plan(plan)
    if dry_run:
        typer.echo(
            "  DRY-RUN: would write fleet.yaml/workspace.toml/hosts/, add remote origin, "
            "push main + refs/dolt/data"
        )
        return
    if not plan.ok:
        typer.echo("✗ pre-push backup could not be verified — refusing to push", err=True)
        raise typer.Exit(1)

    for path in scaffold_layout(hq_dir, cfg):
        typer.echo(f"  ✓ wrote {path.relative_to(hq_dir)}")
    _commit_if_dirty(hq_dir, "chore(hq): scaffold fleet.yaml/workspace.toml/hosts/")

    add_origin = _git(["remote", "add", "origin", git_url], hq_dir)
    if add_origin.returncode:
        typer.echo(f"✗ git remote add origin failed: {err_line(add_origin)}", err=True)
        raise typer.Exit(1)
    push_main = _git(["push", "origin", "main"], hq_dir)
    if push_main.returncode:
        typer.echo(f"✗ git push origin main failed: {err_line(push_main)}", err=True)
        raise typer.Exit(1)

    remote_add = _bd(["dolt", "remote", "add", "origin", dolt_url], hq_dir)
    if remote_add.returncode:
        typer.echo(f"✗ bd dolt remote add failed: {err_line(remote_add)}", err=True)
        raise typer.Exit(1)
    pushed = engine.get_engine(cfg).push_state(hq_dir, message="hq init: first push")
    if pushed.returncode:
        typer.echo(f"✗ bd dolt push failed: {err_line(pushed)}", err=True)
        raise typer.Exit(1)

    typer.echo(f"✓ HQ remote wired ({git_url}) — pushed main + refs/dolt/data")


def _commit_if_dirty(hq_dir: Path, message: str) -> None:
    status = _git(["status", "--porcelain"], hq_dir)
    if not (status.stdout or "").strip():
        return
    _git(["add", "-A"], hq_dir)
    _git(["commit", "-m", message], hq_dir)


# ---- layout scaffold: fleet.yaml + workspace.toml + hosts/ ------------------


def scaffold_layout(hq_dir: Path, cfg: dict) -> list[Path]:
    """Write fleet.yaml + workspace.toml + hosts/ into ``hq_dir`` — idempotent, only writes
    what's missing. Returns the paths actually written (empty ⇒ layout already complete)."""
    written: list[Path] = []
    fleet = hq_dir / "fleet.yaml"
    if not fleet.exists():
        fleet.write_text(_fleet_yaml(cfg))
        written.append(fleet)
    workspace = hq_dir / "workspace.toml"
    if not workspace.exists():
        workspace.write_text(_workspace_toml(cfg))
        written.append(workspace)
    hosts = hq_dir / "hosts"
    hosts.mkdir(exist_ok=True)
    readme = hosts / "README.md"
    if not readme.exists():
        readme.write_text(
            "# hosts/\n\nPer-host manifests land here as `<host_id>.yaml` — the fleet/host "
            "config split (sub-molecule II).\n"
        )
        written.append(readme)
    return written


def _fleet_yaml(cfg: dict) -> str:
    import io

    from ruamel.yaml import YAML

    subset = {k: cfg[k] for k in _FLEET_KEYS if k in cfg}
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    yaml.dump(subset, buf)
    return buf.getvalue()


def _workspace_toml(cfg: dict) -> str:
    """git-workspace providers are fleet truth (the clone PATH stays host-local) — copy the
    operator's own workspace*.toml content when git-workspace is enabled and resolvable, else a
    placeholder a later host can fill in."""
    if gitworkspace.enabled(cfg):
        for path in gitworkspace.config_paths(cfg):
            try:
                return path.read_text()
            except OSError:
                continue
    return "# git-workspace providers — none configured on this host yet.\n"


# ---- three-level pre-push backup (bh-e0y8.2 acceptance amendment) -----------


@dataclass
class BackupTarget:
    """One backup level's outcome (or plan preview, on ``--dry-run``)."""

    name: str
    path: str = ""
    size_bytes: int = 0
    verified: bool = False
    detail: str = ""


@dataclass
class BackupPlan:
    dry_run: bool
    targets: list[BackupTarget] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.targets) and all(t.verified for t in self.targets)


def _backup_root(cfg: dict) -> Path:
    return config.home() / "hq-backups"


def _take_backup(hq_dir: Path, git_url: str, cfg: dict, *, dry_run: bool) -> BackupPlan:
    """Three-level pre-push backup: (1) a portable JSONL interchange export, (2) a tarball of
    the local Dolt store (excluding the regenerable ``git-remote-cache``), and (3) — only when
    the remote already carries one — a copy of its existing ``refs/dolt/data`` kept outside
    ``refs/dolt/`` so dolt's own ref-globbing never sees it. Each level is VERIFIED, not merely
    written. ``dry_run`` previews targets/sizes with zero writes."""
    backup_dir = _backup_root(cfg) / datetime.now(UTC).strftime("%Y-%m-%d")
    plan = BackupPlan(dry_run=dry_run)
    plan.targets.append(_backup_jsonl(hq_dir, backup_dir, cfg, dry_run=dry_run))
    plan.targets.append(_backup_tar(hq_dir, backup_dir, dry_run=dry_run))
    plan.targets.append(_backup_remote_ref(hq_dir, git_url, dry_run=dry_run))
    return plan


def _print_backup_plan(plan: BackupPlan) -> None:
    tag = "DRY-RUN " if plan.dry_run else ""
    typer.echo(f"  {tag}pre-push backup:")
    for t in plan.targets:
        mark = "○" if plan.dry_run else ("✓" if t.verified else "✗")
        size = f" ({t.size_bytes:,}B)" if t.size_bytes else ""
        where = f" {t.path}" if t.path else ""
        typer.echo(f"    {mark} {t.name}{where}{size} — {t.detail}")
    # A backup nobody knows how to consume is a false sense of safety: three green checkmarks
    # at the most dangerous moment in HQ's lifecycle, with recovery left to a future operator
    # under duress. Name the restore path here, while the artifacts are on screen (bh-cmqp.1).
    if not plan.dry_run:
        typer.echo(
            f"    restore with `{config.BINARY_ALIAS} hq restore --list`, then "
            f"`{config.BINARY_ALIAS} hq restore --confirm`"
        )


def _issue_count(hq_dir: Path) -> int:
    """HQ's own reported issue count (``bd status``), independent of the JSONL export itself —
    the cross-check the amendment requires. -1 when it can't be determined."""
    res = _bd(["status", "--json", "--no-activity"], hq_dir)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return -1
    return int((data.get("summary") or {}).get("total_issues", -1))


def _backup_jsonl(hq_dir: Path, backup_dir: Path, cfg: dict, *, dry_run: bool) -> BackupTarget:
    out = backup_dir / "hq-issues.jsonl"
    if dry_run:
        return BackupTarget(
            name="jsonl-export", path=str(out),
            detail="would `bd export` + verify line count == reported issue count",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    res = engine.get_engine(cfg).export_jsonl(hq_dir, out)
    if res.returncode or not out.exists():
        return BackupTarget(
            name="jsonl-export", path=str(out), detail=f"bd export failed: {err_line(res)}"
        )
    lines = sum(1 for _ in out.open())
    count = _issue_count(hq_dir)
    verified = count >= 0 and lines == count
    detail = (
        f"{lines} lines (bd reports {count} issues)"
        if count >= 0
        else f"{lines} lines (issue count unverifiable)"
    )
    return BackupTarget(
        name="jsonl-export", path=str(out), size_bytes=out.stat().st_size,
        verified=verified, detail=detail,
    )


def _backup_tar(hq_dir: Path, backup_dir: Path, *, dry_run: bool) -> BackupTarget:
    src = hq_dir / ".beads" / "embeddeddolt"
    out = backup_dir / "hq-embeddeddolt.tar.gz"
    if dry_run:
        size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file()) if src.is_dir() else 0
        return BackupTarget(
            name="embeddeddolt-tar", path=str(out), size_bytes=size,
            detail=f"would tar {src} (excluding {_DOLT_EXCLUDE_DIR}/) + verify listing",
        )
    if not src.is_dir():
        return BackupTarget(
            name="embeddeddolt-tar", path=str(out), verified=True,
            detail="no .beads/embeddeddolt store — nothing to tar",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        tf.add(
            src, arcname="embeddeddolt",
            filter=lambda ti: None if _DOLT_EXCLUDE_DIR in Path(ti.name).parts else ti,
        )
    try:
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        verified = bool(names)
    except tarfile.TarError:
        names, verified = [], False
    return BackupTarget(
        name="embeddeddolt-tar", path=str(out), size_bytes=out.stat().st_size,
        verified=verified, detail=f"{len(names)} entries",
    )


def _ref_safe(text: str) -> str:
    """Sanitize a string into a valid single git ref *component* (no `~^: ?*[\\`, no leading
    dot, collapsed runs of disallowed chars)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "unknown"


def _bd_version() -> str:
    res = run(["bd", "--version"], check=False, capture=True, timeout=GIT_TIMEOUT)
    tokens = (res.stdout or res.stderr or "").split()
    # "bd version HEAD-af076b6 (Homebrew)" -> "HEAD-af076b6"; tolerate any other shape.
    raw = tokens[2] if len(tokens) >= 3 else (tokens[-1] if tokens else "unknown")
    return _ref_safe(raw)


def _schema_version(hq_dir: Path) -> str:
    """Best-effort schema marker for the backup ref name: bd exposes no dedicated read-only
    "Dolt migration counter" in embedded mode, so this reads the ``schema_version`` field ``bd
    doctor --json`` reports even from its embedded-mode "unsupported" response (verified against
    a real bd binary) — stable and machine-readable, if not the exact migration ordinal."""
    res = _bd(["doctor", "--json"], hq_dir)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    return _ref_safe(str(data.get("schema_version", "unknown")))


def _backup_ref_name(hq_dir: Path) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"refs/backup/dolt-data-schema-{_schema_version(hq_dir)}-bd-{_bd_version()}-{today}"


def _backup_remote_ref(hq_dir: Path, git_url: str, *, dry_run: bool) -> BackupTarget:
    """Copy any PRE-EXISTING ``refs/dolt/data`` on the remote to a backup ref kept outside
    ``refs/dolt/`` before it could ever be overwritten. A freshly-created remote (the common
    case for HQ's first push) has none — that's trivially "verified", nothing to do."""
    probe = _git(["ls-remote", git_url, "refs/dolt/data"], hq_dir)
    sha = (
        (probe.stdout or "").split()[0]
        if probe.returncode == 0 and (probe.stdout or "").strip()
        else ""
    )
    if not sha:
        return BackupTarget(
            name="remote-dolt-data-ref", verified=True,
            detail="no pre-existing refs/dolt/data on the remote — nothing to back up",
        )
    backup_ref = _backup_ref_name(hq_dir)
    if dry_run:
        return BackupTarget(
            name="remote-dolt-data-ref", path=backup_ref,
            detail=f"would copy refs/dolt/data ({sha[:12]}) → {backup_ref}",
        )
    tmp_ref = "refs/hq-backup-tmp"
    fetched = _git(["fetch", git_url, f"refs/dolt/data:{tmp_ref}"], hq_dir)
    if fetched.returncode:
        return BackupTarget(
            name="remote-dolt-data-ref", path=backup_ref,
            detail=f"fetch of existing refs/dolt/data failed: {err_line(fetched)}",
        )
    pushed = _git(["push", git_url, f"{tmp_ref}:{backup_ref}"], hq_dir)
    _git(["update-ref", "-d", tmp_ref], hq_dir)  # local temp ref is scratch — always clean up
    if pushed.returncode:
        return BackupTarget(
            name="remote-dolt-data-ref", path=backup_ref,
            detail=f"push of backup ref failed: {err_line(pushed)}",
        )
    verify = _git(["ls-remote", git_url, backup_ref], hq_dir)
    verified = verify.returncode == 0 and sha in (verify.stdout or "")
    return BackupTarget(
        name="remote-dolt-data-ref", path=backup_ref, verified=verified,
        detail=f"copied {sha[:12]} → {backup_ref}",
    )
