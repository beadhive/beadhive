"""`bh hive repair --prefix <p>` — reconcile a hive's registry prefix against its beads-DB
issue prefix through one idempotent detect/preview/confirm/migrate/update/verify flow.

The problem (bh-6h1m): the registry prefix (`managed_repos[*].prefix` in config.yaml) and the
beads-DB prefix (`bd config get issue_prefix`) are tracked separately — nothing keeps them in
sync, and reconciling them by hand meant `bd rename-prefix` (whose argument needs a trailing
hyphen the registry's stored form never carries) followed by an unregister/re-register dance.
`repair` folds all of that into one call: it reads both prefixes, previews the change against
an explicit `--prefix` target, requires `--yes` to mutate (mirrors `hive init`'s
prefix-change-needs-yes gate — no stdin-blocking prompt, so it stays agent-drivable), migrates
the DB via `bd rename-prefix` when it disagrees with the target, upserts the registry entry via
`registry.register` (in place — same triplet key, no unregister/re-register), then re-reads both
sources to verify convergence. Re-running once converged is a clean no-op.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import typer

from . import bd, config, registry
from .identity import resolve_actor

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")

# bd rename-prefix's own hard rule (its --help): "Max length: 8 characters" — counted on the
# trailing-hyphen CLI-argument form ("kw-"), so the canonical (no-hyphen) form we store caps at
# one less. Kept local (not registry.PREFIX_SOFT_MAX, which is an unenforced *derivation*
# warning) — this is bd's real, enforced limit.
_BD_PREFIX_MAX_WITH_HYPHEN = 8
_PREFIX_RE = re.compile(r"[a-z][a-z0-9-]*")


class RepairError(Exception):
    """A blocking problem repair cannot proceed past — as opposed to an unrelated warning."""


def normalize_prefix(raw: str) -> str:
    """The ONE canonical prefix form: lowercase, no trailing hyphen(s) — what the registry
    stores and what `bd config get issue_prefix` reports. Strips a caller's trailing hyphen(s)
    so a value copied from a `bd rename-prefix` invocation still normalizes cleanly, then
    validates against bd's own rules (starts with a letter, lowercase letters/digits/hyphens
    only, non-empty, fits bd's hard length cap). Raises `RepairError` on anything invalid —
    this is the single seam that resolves the trailing-dash guessing (bh-6h1m #5)."""
    p = (raw or "").strip().rstrip("-")
    if not p:
        raise RepairError(f"prefix cannot be empty (got {raw!r})")
    if not _PREFIX_RE.fullmatch(p):
        raise RepairError(
            f"invalid prefix '{p}' — must start with a lowercase letter and contain only "
            "lowercase letters, digits, and hyphens"
        )
    if len(p) + 1 > _BD_PREFIX_MAX_WITH_HYPHEN:  # +1: bd's cap counts the trailing hyphen
        raise RepairError(
            f"prefix '{p}' is {len(p) + 1} chars with its trailing hyphen (bd's max is "
            f"{_BD_PREFIX_MAX_WITH_HYPHEN}) — choose a shorter prefix"
        )
    return p


def rename_prefix_arg(prefix: str) -> str:
    """The `bd rename-prefix` CLI-argument form: the canonical prefix plus its trailing
    hyphen — computed here so a caller never has to guess whether to append one."""
    return f"{prefix}-"


@dataclass
class RepairPlan:
    """What `detect` found: the hive entry + cwd, both current prefixes (already normalized),
    and the normalized target. `in_sync` is the idempotent no-op signal."""

    entry: dict
    cwd: Path
    registry_prefix: str
    db_prefix: str
    target: str

    @property
    def in_sync(self) -> bool:
        return self.registry_prefix == self.target and self.db_prefix == self.target


def _resolve_entry(cfg, hive: str) -> dict:
    """The registered managed_repos entry to repair — `--hive` when given, else cwd's hive.
    Refuses an unregistered/synthesized entry (`current_hive` can synthesize a minimal one for
    an unregistered checkout): there is no registry prefix to reconcile against yet."""
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    if entry is None:
        raise RepairError(
            "not in a registered hive — pass --hive <provider/org/repo> or run from a hive checkout"
        )
    key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
    registered = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in cfg.get("managed_repos", [])}
    if key not in registered:
        raise RepairError(f"{key} is not a registered hive — nothing to repair")
    return entry


def detect(cfg, hive: str, target_prefix: str) -> RepairPlan:
    """Read the registry prefix + the beads-DB issue_prefix for the target hive, and normalize
    both plus the requested target. Raises `RepairError` when the hive isn't registered, has no
    local checkout/`.beads`, the target collides with ANOTHER hive's prefix (repair must not just
    move the mismatch — bh-6h1m #4), or a prefix fails normalization."""
    entry = _resolve_entry(cfg, hive)
    cwd = registry.hive_dir(entry)
    if not (cwd / ".beads").is_dir():
        raise RepairError(f"{cwd} has no .beads/ — clone/init the hive before repairing prefixes")
    target = normalize_prefix(target_prefix)
    key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
    if registry.prefix_taken(cfg, target, skip=key):
        raise RepairError(f"prefix '{target}' is already used by another registered hive")
    db = bd.json(["config", "get", "issue_prefix"], cwd)
    if not isinstance(db, dict) or "value" not in db:
        raise RepairError(f"could not read issue_prefix from {cwd}'s beads DB")
    return RepairPlan(
        entry=entry,
        cwd=cwd,
        registry_prefix=normalize_prefix(str(entry["prefix"])),
        db_prefix=normalize_prefix(str(db["value"])),
        target=target,
    )


def _print_preview(plan: RepairPlan) -> None:
    e = plan.entry
    typer.echo(f"Hive: {e['provider']}/{e['org']}/{e['repo']}")
    typer.echo(f"Registry prefix: {plan.registry_prefix} -> {plan.target}")
    typer.echo(
        f"Database prefix: {rename_prefix_arg(plan.db_prefix)} -> {rename_prefix_arg(plan.target)}"
    )


def apply(plan: RepairPlan, actor: str) -> list[str]:
    """Migrate the DB (`bd rename-prefix`, skipped when it already matches the target) then
    update the registry in place via `registry.register` — an upsert by the same triplet key, so
    there is no separate unregister/re-register step. Returns the fixes applied; an empty list
    means the plan was already in sync (the idempotent no-op)."""
    fixes: list[str] = []
    if plan.db_prefix != plan.target:
        res = bd.run(["rename-prefix", rename_prefix_arg(plan.target)], plan.cwd, actor=actor)
        if res.returncode != 0:
            raise RepairError(f"`bd rename-prefix` failed: {bd.err_line(res)}")
        fixes.append(f"database migrated: {plan.db_prefix} -> {plan.target}")
        typer.echo("✓ Database migrated")
    if plan.registry_prefix != plan.target:
        e = plan.entry
        registry.register(
            e["provider"], e["org"], e["repo"], plan.target, str(e.get("kind", "")),
            upstream=str(e.get("upstream", "")), furnish=str(e.get("furnish", "")),
        )
        fixes.append(f"registry updated: {plan.registry_prefix} -> {plan.target}")
        typer.echo("✓ Registry updated")
    return fixes


def verify(plan: RepairPlan) -> list[str]:
    """Re-read both sources after `apply` and report anything that failed to converge — a
    blocking problem (bd refused, a racing writer, an unpersisted config save), never swallowed."""
    problems: list[str] = []
    entry = registry.find_entry(
        config.load(), plan.entry["provider"], plan.entry["org"], plan.entry["repo"]
    )
    if entry is None or normalize_prefix(str(entry["prefix"])) != plan.target:
        problems.append(f"registry prefix did not converge to '{plan.target}'")
    db = bd.json(["config", "get", "issue_prefix"], plan.cwd)
    if not isinstance(db, dict) or normalize_prefix(str(db.get("value", ""))) != plan.target:
        problems.append(f"database issue_prefix did not converge to '{plan.target}'")
    return problems


def repair(hive: str, prefix: str, yes: bool, dry_run: bool) -> None:
    """CLI core: detect -> preview -> confirm (--yes) -> migrate -> update -> verify."""
    cfg = config.load()
    try:
        plan = detect(cfg, hive, prefix)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    _print_preview(plan)
    if plan.in_sync:
        typer.echo("\n✓ Prefixes consistent — nothing to repair")
        return
    if dry_run:
        typer.echo("\n(dry-run: no changes made — pass --yes to apply)")
        return
    if not yes:
        typer.echo(
            "\n✗ refusing to change a hive's prefix without --yes — changing it orphans "
            "the prefix half of every existing bead ID reference; pass --yes to confirm",
            err=True,
        )
        raise typer.Exit(1)

    actor = resolve_actor("", "", cwd=plan.cwd)
    typer.echo()
    try:
        apply(plan, actor)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    problems = verify(plan)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        typer.echo("✗ repair applied changes but prefixes did not converge (above)", err=True)
        raise typer.Exit(1)
    typer.echo("✓ Prefixes consistent")
