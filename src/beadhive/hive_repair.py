"""`bh hive repair --prefix <p> | --node-id | --role` — reconcile one piece of hive-level or
host-level config drift through the same idempotent detect/preview/confirm/apply/verify shape.
Exactly one of the three modes runs per invocation.

The problem (bh-6h1m): the registry prefix (`managed_repos[*].prefix` in config.yaml) and the
beads-DB prefix (`bd config get issue_prefix`) are tracked separately — nothing keeps them in
sync, and reconciling them by hand meant `bd rename-prefix` (whose argument needs a trailing
hyphen the registry's stored form never carries) followed by an unregister/re-register dance.
`--prefix` folds all of that into one call: it reads both prefixes, previews the change against
an explicit target, requires `--yes` to mutate (mirrors `hive init`'s prefix-change-needs-yes
gate — no stdin-blocking prompt, so it stays agent-drivable), migrates the DB via
`bd rename-prefix` when it disagrees with the target, upserts the registry entry via
`registry.register` (in place — same triplet key, no unregister/re-register), then re-reads both
sources to verify convergence. Re-running once converged is a clean no-op.

`--node-id` (bh-y85rj) and `--role` (bh-f3blt) are the same shape applied to two more drift
cases `bh doctor` detects: a missing per-host `node_id` (`~/.config/bd/config.yaml`, never
project-tracked) and a missing/mismatched `beads.role` (git config, derived from the hive's
registry `kind`). Both reuse this module's detect → preview → confirm (`--yes`) → apply →
verify skeleton rather than growing a parallel repair path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import typer

from . import bd, config, host, registry, store_locator
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
            e["provider"],
            e["org"],
            e["repo"],
            plan.target,
            str(e.get("kind", "")),
            upstream=str(e.get("upstream", "")),
            furnish=str(e.get("furnish", "")),
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


def _repair_prefix(cfg, hive: str, prefix: str, yes: bool, dry_run: bool) -> None:
    """`--prefix` mode: detect -> preview -> confirm (--yes) -> migrate -> update -> verify."""
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


# ---- node_id repair (bh-y85rj) -----------------------------------------------
# node_id is a per-HOST value (never per-hive: this hive runs `dolt.shared-server = true`, and
# bd's own reclaim docs are explicit that every client of the same shared sql-server is ONE
# replica and must share one value). It lives in the per-machine
# `~/.config/bd/config.yaml` (`bd config set node_id <name>`/`BEADS_NODE_ID`), never in a
# hive's git-tracked config. `--hive` here only picks which local checkout's `.beads/` to run
# `bd` against — the write itself lands on the host, not the hive.


@dataclass
class NodeIdPlan:
    """`current`/`target` are both this HOST's node_id — `--hive` only supplies a `.beads/`
    checkout to invoke `bd` from. `in_sync` is the idempotent no-op signal."""

    entry: dict
    cwd: Path
    current: str
    target: str

    @property
    def in_sync(self) -> bool:
        return self.current == self.target


def detect_node_id(cfg, hive: str) -> NodeIdPlan:
    """Read this host's persisted `node_id` (never the `BEADS_NODE_ID` env override — that
    already wins at read time everywhere bd itself consults it, and repair only ever fixes the
    persisted file) and target `host.host_id()` — the stable per-host identity `bh` already
    mints once via `bh config init` and never regenerates, reused here rather than inventing a
    second per-host name."""
    entry = _resolve_entry(cfg, hive)
    cwd = registry.hive_dir(entry)
    if not (cwd / ".beads").is_dir():
        raise RepairError(f"{cwd} has no .beads/ — clone/init the hive before repairing node_id")
    current = bd.json(["config", "get", "node_id"], cwd)
    if not isinstance(current, dict):
        raise RepairError(f"could not read node_id via bd at {cwd}")
    return NodeIdPlan(
        entry=entry,
        cwd=cwd,
        current=str(current.get("value") or "").strip(),
        target=host.host_id(),
    )


def apply_node_id(plan: NodeIdPlan, actor: str) -> list[str]:
    """`bd config set node_id <target>` — writes `~/.config/bd/config.yaml`, never a
    hive-tracked file. Empty list means the plan was already in sync."""
    if plan.in_sync:
        return []
    res = bd.run(["config", "set", "node_id", plan.target], plan.cwd, actor=actor)
    if res.returncode != 0:
        raise RepairError(f"`bd config set node_id` failed: {bd.err_line(res)}")
    return [f"node_id: '{plan.current}' -> '{plan.target}'"]


def verify_node_id(plan: NodeIdPlan) -> list[str]:
    current = bd.json(["config", "get", "node_id"], plan.cwd)
    value = str((current or {}).get("value") or "").strip()
    return [] if value == plan.target else [f"node_id did not converge to '{plan.target}'"]


def _repair_node_id(cfg, hive: str, yes: bool, dry_run: bool) -> None:
    try:
        plan = detect_node_id(cfg, hive)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    typer.echo(f"Host node_id: '{plan.current}' -> '{plan.target}'")
    if plan.in_sync:
        typer.echo("\n✓ node_id already set — nothing to repair")
        return
    if dry_run:
        typer.echo("\n(dry-run: no changes made — pass --yes to apply)")
        return
    if not yes:
        typer.echo(
            "\n✗ refusing to set this host's node_id without --yes; pass --yes to confirm",
            err=True,
        )
        raise typer.Exit(1)

    actor = resolve_actor("", "", cwd=plan.cwd)
    try:
        apply_node_id(plan, actor)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    problems = verify_node_id(plan)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        typer.echo("✗ repair applied changes but node_id did not converge (above)", err=True)
        raise typer.Exit(1)
    typer.echo("✓ node_id set")


# ---- beads.role repair (bh-f3blt) --------------------------------------------
# `beads.role` (git config) drives bd's own routing; bh maps it explicitly off the hive's
# registry `kind` rather than leaving bd to guess from remote shape. `org-native`/`hq` (we
# administer it) -> maintainer; everything else (`fork`, `external`, `personal`, `prototype` —
# hives we don't own the same way) -> contributor.

_MAINTAINER_KINDS = frozenset({"org-native", "hq"})


def expected_role(kind: str) -> str:
    """The `beads.role` a hive's registry `kind` maps to — the ONE mapping (onboard, doctor,
    and this repair all call this, never re-derive it separately)."""
    return "maintainer" if kind in _MAINTAINER_KINDS else "contributor"


@dataclass
class RolePlan:
    entry: dict
    cwd: Path
    current: str
    target: str

    @property
    def in_sync(self) -> bool:
        return self.current == self.target

    @property
    def mismatched(self) -> bool:
        """A NON-EMPTY current value that disagrees with the target — the case onboard must
        report rather than silently overwrite."""
        return bool(self.current) and not self.in_sync


def detect_role(cfg, hive: str) -> RolePlan:
    entry = _resolve_entry(cfg, hive)
    cwd = registry.hive_dir(entry)
    if not (cwd / ".beads").is_dir():
        raise RepairError(f"{cwd} has no .beads/ — clone/init the hive before repairing role")
    current = bd.json(["config", "get", "beads.role"], cwd, pin_process_cwd=True)
    if not isinstance(current, dict):
        raise RepairError(f"could not read beads.role via bd at {cwd}")
    return RolePlan(
        entry=entry,
        cwd=cwd,
        current=str(current.get("value") or "").strip(),
        target=expected_role(str(entry.get("kind", ""))),
    )


def apply_role(plan: RolePlan, actor: str) -> list[str]:
    """`bd config set beads.role <target>` — an explicit, `--yes`-gated repair MAY overwrite a
    mismatch (unlike onboard's auto-set, which never does); an empty current value is always
    safe to set."""
    if plan.in_sync:
        return []
    res = bd.run(
        ["config", "set", "beads.role", plan.target], plan.cwd, actor=actor, pin_process_cwd=True
    )
    if res.returncode != 0:
        raise RepairError(f"`bd config set beads.role` failed: {bd.err_line(res)}")
    return [f"beads.role: '{plan.current}' -> '{plan.target}'"]


def verify_role(plan: RolePlan) -> list[str]:
    current = bd.json(["config", "get", "beads.role"], plan.cwd, pin_process_cwd=True)
    value = str((current or {}).get("value") or "").strip()
    return [] if value == plan.target else [f"beads.role did not converge to '{plan.target}'"]


def _repair_role(cfg, hive: str, yes: bool, dry_run: bool) -> None:
    try:
        plan = detect_role(cfg, hive)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    e = plan.entry
    typer.echo(f"Hive: {e['provider']}/{e['org']}/{e['repo']} (kind={e.get('kind', '')})")
    typer.echo(f"beads.role: '{plan.current}' -> '{plan.target}'")
    if plan.in_sync:
        typer.echo("\n✓ beads.role already correct — nothing to repair")
        return
    if dry_run:
        typer.echo("\n(dry-run: no changes made — pass --yes to apply)")
        return
    if not yes:
        typer.echo(
            "\n✗ refusing to set beads.role without --yes; pass --yes to confirm",
            err=True,
        )
        raise typer.Exit(1)

    actor = resolve_actor("", "", cwd=plan.cwd)
    try:
        apply_role(plan, actor)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    problems = verify_role(plan)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        typer.echo("✗ repair applied changes but beads.role did not converge (above)", err=True)
        raise typer.Exit(1)
    typer.echo("✓ beads.role set")


# ---- server-database mode (bh-td8t9) ----------------------------------------
# A server-mode hive with no `dolt_server_database` key still RESOLVES a name — `store_locator
# .server_database`'s order-2 grandfather clause falls back to `dolt_database`. That derivation
# is what bh-g5ujg's rule exists to stop: re-derivation is how an already-migrated hive gets
# "corrected" onto a name its store isn't under (bh-4o07n). This mode RECORDS the name the hive
# already resolves today — it never picks a new one, so applying it can't move a working hive.


@dataclass
class ServerDatabasePlan:
    entry: dict
    cwd: Path
    embedded: bool
    current: str
    target: str

    @property
    def in_sync(self) -> bool:
        """Nothing to do — either already recorded, or embedded (which must NOT get the key)."""
        return self.embedded or self.current == self.target


def detect_server_database(cfg, hive: str) -> ServerDatabasePlan:
    entry = _resolve_entry(cfg, hive)
    cwd = registry.hive_dir(entry)
    if not (cwd / ".beads").is_dir():
        raise RepairError(f"{cwd} has no .beads/ — clone/init the hive before repairing it")
    if store_locator.dolt_mode(cwd) is None:
        raise RepairError(
            f"{cwd}/.beads/metadata.json records no dolt_mode — unknown is not 'embedded', and "
            "recording a server database for a store whose mode is unreadable would be a guess"
        )
    embedded = store_locator.is_embedded_mode(cwd)
    return ServerDatabasePlan(
        entry=entry,
        cwd=cwd,
        embedded=embedded,
        current=store_locator.recorded_server_database(cwd),
        # The name the hive resolves TODAY, not a fresh derivation — see the module note above.
        target="" if embedded else store_locator.server_database(cwd),
    )


def apply_server_database(plan: ServerDatabasePlan) -> list[str]:
    if plan.in_sync:
        return []
    store_locator.ensure_server_database_persisted(plan.cwd, plan.target)
    return [f"dolt_server_database: '{plan.current}' -> '{plan.target}'"]


def verify_server_database(plan: ServerDatabasePlan) -> list[str]:
    recorded = store_locator.recorded_server_database(plan.cwd)
    if plan.embedded:
        if not recorded:
            return []
        return [f"embedded hive carries a spurious dolt_server_database '{recorded}'"]
    if recorded == plan.target:
        return []
    return [f"dolt_server_database did not converge to '{plan.target}'"]


def _repair_server_database(cfg, hive: str, yes: bool, dry_run: bool) -> None:
    try:
        plan = detect_server_database(cfg, hive)
    except RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None

    e = plan.entry
    typer.echo(f"Hive: {e['provider']}/{e['org']}/{e['repo']}")
    if plan.embedded:
        typer.echo("\n✓ embedded-mode hive — dolt_server_database does not apply, nothing to do")
        return
    typer.echo(f"dolt_server_database: '{plan.current}' -> '{plan.target}'")
    if plan.in_sync:
        typer.echo("\n✓ dolt_server_database already recorded — nothing to repair")
        return
    if dry_run:
        typer.echo("\n(dry-run: no changes made — pass --yes to apply)")
        return
    if not yes:
        typer.echo(
            "\n✗ refusing to write dolt_server_database without --yes; pass --yes to confirm",
            err=True,
        )
        raise typer.Exit(1)

    apply_server_database(plan)
    problems = verify_server_database(plan)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        typer.echo("✗ repair wrote metadata but dolt_server_database did not converge", err=True)
        raise typer.Exit(1)
    typer.echo("✓ dolt_server_database recorded")


def repair(
    hive: str,
    prefix: str = "",
    node_id: bool = False,
    role: bool = False,
    server_database: bool = False,
    *,
    yes: bool,
    dry_run: bool,
) -> None:
    """CLI core: dispatch to exactly one of the four repair modes."""
    modes = [bool(prefix), node_id, role, server_database]
    if sum(modes) != 1:
        typer.echo(
            "✗ pass exactly one of --prefix <p>, --node-id, --role, --server-database", err=True
        )
        raise typer.Exit(1)

    cfg = config.load()
    if prefix:
        _repair_prefix(cfg, hive, prefix, yes, dry_run)
    elif node_id:
        _repair_node_id(cfg, hive, yes, dry_run)
    elif role:
        _repair_role(cfg, hive, yes, dry_run)
    else:
        _repair_server_database(cfg, hive, yes, dry_run)
