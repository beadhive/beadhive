"""One-time migration splitting an existing flat config.yaml into fleet.yaml + a reduced
host config.yaml (bh-e0y8.7), per the leaf-level fleet/host partition
:mod:`beadhive.config_partition` defines.

Every install that predates the fleet/host split (bh-e0y8.5) has ONE flat ``config.yaml``
mixing fleet-wide truth (``orgs``, ``dimensions``, ``work.validate_cmd``, …) with host-local
truth (``worktrees.path``, ``otel.*``, ``work.identity``, ``hq.remote``, …). Splitting that by
hand is error-prone — the failure mode is a host that LOOKS configured but silently disagrees
with the fleet the moment ``fleet.yaml`` changes underneath it (exactly what
:func:`beadhive.config.fleet_override_violations` exists to catch on every later ``load()``).
This module does the split mechanically instead, leaf by leaf, via
:func:`beadhive.config_partition.partition_of` — the SAME classification
:func:`beadhive.config.load` merges by, so a config this migration produces reads back
identical to the one it replaced.

Follows the structural conventions :mod:`beadhive.home_migration` sets for a one-time,
idempotent config transform: a cheap "already done" check up front (:func:`needs_split`,
mirroring ``_home_migrated``), the original left recoverable (a ``.bak`` copy taken before
anything is overwritten), and pure/testable helpers (:func:`split_leaves`) kept separate from
the I/O-performing orchestrator (:func:`split_flat_config`). Unlike the automatic home-dir
move, this is a deliberate, operator-invoked action (``bh config split``) with a ``--dry-run``
preview — restructuring one file into two is a bigger, more visible change than a silent path
rename, so it stays opt-in rather than firing on every ``bh`` invocation (never wired into
``cli._root``'s best-effort migration hooks).
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Mapping

import typer

from . import config, config_partition

#: Suffix appended to `config.config_path()` for the pre-split backup (bh-e0y8.7's
#: reversibility requirement) — taken once, right before the host file is overwritten.
BACKUP_SUFFIX = ".bak"


def _get_leaf(node: Mapping, dotted: str):
    """Walk `dotted` through `node`, the read-side counterpart to :func:`_set_leaf`."""
    value = node
    for part in dotted.split("."):
        value = value[part]
    return value


def _set_leaf(node: dict, dotted: str, value) -> None:
    """Set `value` at dotted leaf path `dotted` inside nested plain-dict `node`, auto-vivifying
    intermediate levels — the write-side counterpart to :func:`beadhive.config._leaf_paths`'
    read walk."""
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def needs_split(host) -> bool:
    """True when `host` (a flat/partially-flat config mapping) still has at least one
    FLEET-classified leaf to move out — the idempotency check. A host already reduced to
    HOST-only (plus any unclassified) leaves has nothing left to split; re-running is a no-op.
    """
    return any(
        config_partition.partition_of(path) == config_partition.FLEET
        for path in config._leaf_paths(host)
    )


def split_leaves(host: Mapping) -> tuple[dict, dict]:
    """Partition every leaf of `host` into `(fleet_portion, host_portion)` per
    :func:`beadhive.config_partition.partition_of`.

    A FLEET-classified leaf moves to `fleet_portion`; a HOST leaf, or one neither side claims
    (`partition_of` -> ``None`` — e.g. the un-schema'd `beads` section) stays in
    `host_portion`. That mirrors :func:`beadhive.config.fleet_override_violations`'s existing
    "unclassified is not a licence to reject" rule: an unrecognized key is preserved on the
    host side rather than dropped or guessed at, so the split never loses a value and the
    round-trip (`_deep_merge(fleet_portion, host_portion) == host`) always holds regardless of
    schema drift.
    """
    fleet_portion: dict = {}
    host_portion: dict = {}
    for path in config._leaf_paths(host):
        value = _get_leaf(host, path)
        target = (
            fleet_portion
            if config_partition.partition_of(path) == config_partition.FLEET
            else host_portion
        )
        _set_leaf(target, path, value)
    return fleet_portion, host_portion


def _backup_path():
    p = config.config_path()
    return p.with_name(p.name + BACKUP_SUFFIX)


def _render_yaml(data) -> str:
    """Render `data` the same way `config.save`/`config.save_fleet` would write it, for the
    `--dry-run` preview — a real, exact rendering rather than a Python repr."""
    buf = io.StringIO()
    config._yaml.dump(data, buf)
    return buf.getvalue()


def split_flat_config(*, dry_run: bool = False) -> None:
    """Split the host's flat `config.yaml` into `fleet.yaml` + a reduced `config.yaml`, per
    the fleet/host partition.

    Idempotent (:func:`needs_split` is a cheap no-op check), reversible (the original file is
    copied to `config.yaml.bak` before anything is overwritten), and `--dry-run`-able (prints
    the exact prospective content of both files, writes nothing). Absence degrades like its
    sibling migrations: no `config.yaml` at all is nothing to split, not an error.

    The `fleet.yaml` write deep-merges the extracted FLEET portion ONTO whatever fleet base
    already exists (:func:`beadhive.config.load_fleet`) rather than replacing it outright, so
    running this on a second host after a first has already migrated folds this host's fleet
    keys in without discarding the first host's. On any key both sides set, the value from
    THIS host's own flat config wins — the file actually being split is the freshest source of
    truth for what this host has been running with.
    """
    try:
        host = config.load_host()
    except FileNotFoundError:
        typer.echo(f"no config found at {config.config_path()} — nothing to split.")
        return

    if not needs_split(host):
        typer.echo(f"✓ {config.config_path()} is already split — nothing to do (no-op).")
        return

    fleet_portion, host_portion = split_leaves(host)
    merged_fleet = config._deep_merge(config.load_fleet(), fleet_portion)

    if dry_run:
        typer.echo(f"DRY-RUN would split {config.config_path()}:\n")
        typer.echo(f"  {config.fleet_path()} (fleet, after merge):")
        typer.echo(_render_yaml(merged_fleet))
        typer.echo(f"  {config.config_path()} (host, reduced):")
        typer.echo(_render_yaml(host_portion))
        typer.echo(f"  backup: {_backup_path()} (original preserved, untouched)")
        return

    shutil.copy2(config.config_path(), _backup_path())
    config.save_fleet(merged_fleet)
    config.save(host_portion)
    typer.echo(f"✓ backed up original to {_backup_path()}")
    typer.echo(f"✓ wrote fleet keys to {config.fleet_path()}")
    typer.echo(f"✓ reduced {config.config_path()} to host-only keys")
