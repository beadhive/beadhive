"""Leaving and recording local-only (bh-sj2bf; docs/design/local-only-hives.md R2, R9).

``bh hive replicate <hive>`` publishes a local-only hive's bead database once its owner has push
access, then clears ``managed_repos[].local_only``: publish first, flip second, so any failure
leaves the hive local-only (the safe state). It also publishes an unpublished registration that
carries no marker (R9), with nothing to clear.

``bh hive repair --local-only --yes`` records an EXISTING unpublished registration as local-only
to this host (reason ``migrated``, or ``external`` for an external hive), on the host that holds
its store. It refuses when another host holds an unexpired lease for the prefix, or when the
entry already names a different owner.

Every external effect goes through a module-level seam (push probe, origin probe, remote restore,
push, lease read, clock) so the behaviour is testable without gh, bd or a remote.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import typer

from . import config, host, registry
from .storage_migrate import origin_has_dolt_data

#: clock seam (tests pin it; never a timing dependency)
_now = time.time

_ORIGIN = "origin"


class LocalOnlyError(Exception):
    """A refusal: nothing was changed by the step that raised it."""


def _push_access(entry) -> registry.PushAccess:
    ans = registry.has_push_access(
        str(entry.get("provider", "")), str(entry.get("org", "")), str(entry.get("repo", ""))
    )
    if isinstance(ans, registry.PushAccess):
        return ans
    return registry.PushAccess(registry.PUSH_CONFIRMED if ans else registry.PUSH_DENIED)


def _git_origin_url(hive_dir: Path) -> str:
    from .run import run

    res = run(["git", "-C", str(hive_dir), "remote", "get-url", _ORIGIN], check=False, capture=True)
    return (getattr(res, "stdout", "") or "").strip() if res.returncode == 0 else ""


def restore_beads_remote(cfg, hive_dir: Path) -> None:
    """Point the store's beads remote back at the hive's git origin — the remote onboard's push
    guard unset (``bd config unset sync.remote``) and that ``bd init`` would have derived.
    Idempotent: an existing ``sync.remote`` and an existing ``origin`` Dolt remote are kept."""
    from . import bd, engine
    from .public_publish import dolt_git_url

    url = dolt_git_url(_git_origin_url(hive_dir))
    if not url:
        raise LocalOnlyError(f"{hive_dir} has no git origin to publish the bead database to")
    if not bd.sync_remote(hive_dir):
        res = bd.run(["config", "set", "sync.remote", url], hive_dir, capture=True)
        if getattr(res, "returncode", 1) != 0:
            raise LocalOnlyError(f"could not restore sync.remote: {bd.err_line(res)}")
    eng = engine.get_engine(cfg)
    if eng.get_remote(hive_dir, _ORIGIN) is None:
        added = eng.add_remote(hive_dir, _ORIGIN, url)
        if getattr(added, "returncode", 1) != 0:
            raise LocalOnlyError(f"could not register the origin Dolt remote: {bd.err_line(added)}")


def _publish(cfg, hive_dir: Path) -> tuple[bool, str]:
    """The same push leg onboard's publish step uses (reserves the remote fence)."""
    from . import sync_remote

    return sync_remote._push_dolt_state(cfg, hive_dir)


def _key(entry) -> str:
    return f"{entry['provider']}/{entry['org']}/{entry['repo']}"


def _refuse_unreplicable(entry) -> None:
    if str(entry.get("kind", "")) == registry.HQ_KIND:
        raise LocalOnlyError(f"{_key(entry)} is the HQ store, not a hive")
    if entry.get("dolt_remote") or entry.get("stores"):
        raise LocalOnlyError(
            f"{_key(entry)} declares an HQ remote or stores — it is already replicated"
        )


# ---- bh hive replicate ------------------------------------------------------------------------


@dataclass
class ReplicateResult:
    hive: str
    prefix: str
    published: bool
    cleared: bool


def replicate(cfg, hive_id: str) -> ReplicateResult:
    """Publish then clear (R2). Raises :class:`LocalOnlyError` on any refusal or failure; the
    ``local_only`` marker is deleted only after origin is verified to carry ``refs/dolt/data``."""
    entry = registry.resolve_hive(cfg, hive_id)
    _refuse_unreplicable(entry)
    marker = registry.local_only_of(entry)
    key, prefix = _key(entry), str(entry.get("prefix", ""))
    if str(entry.get("kind", "")) == "external" or (
        marker is not None and marker["reason"] == registry.LOCAL_ONLY_EXTERNAL
    ):
        raise LocalOnlyError(
            f"{key} is an external hive — external hives are never replicated (publishing its "
            "beads onto the fork's refs/dolt/data is not allowed)"
        )
    if marker is not None and marker["owner_host"] != host.host_id_or_none():
        raise LocalOnlyError(
            f"{key} is local-only on host {registry.host_label(marker['owner_host'])} "
            f"({marker['owner_host']}) — only its owner can replicate it"
        )
    hive_dir = registry.hive_dir(entry)
    if not (hive_dir / ".beads").is_dir():
        raise LocalOnlyError(f"{hive_dir} has no bead store on this host — nothing to publish")
    # 1. a CONFIRMED push probe (bh-9eua2 tri-state); denied and unknown refuse
    access = _push_access(entry)
    if access.state != registry.PUSH_CONFIRMED:
        cause = f" ({access.cause})" if access.cause else ""
        raise LocalOnlyError(
            f"push access to {entry['org']}/{entry['repo']} is {access.state}{cause} — "
            "replicate needs confirmed push access; the hive stays local-only"
        )
    # 2. someone else already published a database: merging the two is out of scope
    if origin_has_dolt_data(hive_dir):
        raise LocalOnlyError(
            f"origin of {key} already carries refs/dolt/data — another database was published "
            "there; refusing to push over it (merging the two is out of scope)"
        )
    # 3. restore the beads remote the push guard unset, then 4. publish through onboard's leg
    restore_beads_remote(cfg, hive_dir)
    typer.echo(f"• beads: publishing the bead database of {key}")
    ok, reason = _publish(cfg, hive_dir)
    if not ok:
        detail = f": {reason.strip()}" if reason and reason.strip() else ""
        raise LocalOnlyError(f"publishing refs/dolt/data failed{detail}; the hive stays local-only")
    # 5. verify, then 6. flip
    if not origin_has_dolt_data(hive_dir):
        raise LocalOnlyError(
            "the push reported success but origin does not carry refs/dolt/data; the hive "
            "stays local-only"
        )
    typer.echo(f"✓ beads: published refs/dolt/data for {key}")
    if marker is None:
        return ReplicateResult(key, prefix, published=True, cleared=False)
    registry.set_local_only(entry["provider"], entry["org"], entry["repo"], None)
    return ReplicateResult(key, prefix, published=True, cleared=True)


def replicate_cli(hive_id: str) -> None:
    cfg = config.load()
    try:
        result = replicate(cfg, hive_id)
    except LocalOnlyError as exc:
        typer.echo(f"✗ replicate refused: {exc}", err=True)
        raise typer.Exit(1) from None
    if result.cleared:
        typer.echo(f"✓ {result.hive} (prefix {result.prefix}) is replicated: local_only cleared")
    else:
        typer.echo(f"✓ {result.hive} (prefix {result.prefix}) published (it was not local-only)")


# ---- bh hive repair --local-only --------------------------------------------------------------


def _lease(prefix: str):
    """The current host lease for ``prefix`` (a :class:`HostLease`), or None when none exists.

    A frame fleet (SQL, or a Git authority anchor) reads the authoritative lease; a legacy host
    reads ``refs/bh/lease/<prefix>`` on its HQ remote; a host with no HQ clone has no lease
    store, so nothing was ever adopted. A read failure raises (fail closed)."""
    from . import host_lease
    from .hq_control_plane import control_plane

    hq_dir = config.hq_dir()
    binding = config.load_host().get("hq", {}) or {}
    if (binding.get("sql") or {}).get("enabled") or binding.get("authority_anchor"):
        return control_plane(hq_dir).read_hive_lease(prefix)
    if not (hq_dir / ".git").exists():
        return None
    return host_lease.read(_ORIGIN, prefix, cwd=hq_dir)


@dataclass
class LocalOnlyRepairPlan:
    entry: dict
    owner: str
    reason: str
    in_sync: bool


def detect_local_only(cfg, entry) -> LocalOnlyRepairPlan:
    """What ``repair --local-only`` would record. Raises :class:`LocalOnlyError` on a refusal."""
    _refuse_unreplicable(entry)
    key, prefix = _key(entry), str(entry.get("prefix", ""))
    owner = host.host_id_or_none()
    if not owner:
        raise LocalOnlyError(
            f"host.yaml (this host's host_id) is missing or unreadable — run "
            f"`{config.BINARY_ALIAS} config init` first; an owner is never recorded empty"
        )
    reason = (
        registry.LOCAL_ONLY_EXTERNAL
        if str(entry.get("kind", "")) == "external"
        else registry.LOCAL_ONLY_MIGRATED
    )
    marker = registry.local_only_of(entry)
    if marker is not None:
        if marker["owner_host"] != owner:
            raise LocalOnlyError(
                f"{key} is already local-only on host {registry.host_label(marker['owner_host'])} "
                f"({marker['owner_host']}) — the entry names a different owner; choose the "
                "canonical copy (the other host exports what it keeps, then `bh hive reclaim`)"
            )
        return LocalOnlyRepairPlan(entry, owner, marker["reason"], in_sync=True)
    hive_dir = registry.hive_dir(entry)
    if not (hive_dir / ".beads").is_dir():
        raise LocalOnlyError(
            f"{hive_dir} has no bead store on this host — run this on the host that holds it"
        )
    if origin_has_dolt_data(hive_dir):
        raise LocalOnlyError(
            f"origin of {key} already carries refs/dolt/data — the hive is published, not "
            "local-only"
        )
    try:
        lease = _lease(prefix)
    except Exception as exc:  # noqa: BLE001 — an unreadable lease never counts as free
        raise LocalOnlyError(f"cannot read the host lease for {prefix}: {exc}") from None
    if lease is not None and lease.host_id and lease.host_id != owner:
        if not lease.is_expired(_now()):
            raise LocalOnlyError(
                f"host {lease.label or lease.host_id} ({lease.host_id}) holds an unexpired lease "
                f"for {prefix} — it may hold a store too; choose the canonical copy first"
            )
    return LocalOnlyRepairPlan(entry, owner, reason, in_sync=False)


def repair_local_only(cfg, entry, *, yes: bool, dry_run: bool, allow_unknown_readers: bool):
    """The ``repair --local-only`` mode: detect → preview → (``--yes``) rollout floor → write."""
    from . import local_only_rollout

    try:
        plan = detect_local_only(cfg, entry)
    except LocalOnlyError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None
    e = plan.entry
    typer.echo(f"Hive: {_key(e)} (prefix {e.get('prefix', '')}, kind={e.get('kind', '')})")
    typer.echo(f"local_only: -> {{owner_host: {plan.owner}, reason: {plan.reason}}}")
    if plan.in_sync:
        typer.echo("\n✓ already local-only to this host — nothing to repair")
        return
    if dry_run:
        typer.echo("\n(dry-run: no changes made — pass --yes to apply)")
        return
    if not yes:
        typer.echo(
            "\n✗ refusing to record local_only without --yes; pass --yes to confirm", err=True
        )
        raise typer.Exit(1)
    why = local_only_rollout.refusal(allow_unknown_readers=allow_unknown_readers)
    if why:
        typer.echo(f"✗ {why}", err=True)
        raise typer.Exit(1)
    registry.set_local_only(
        e["provider"], e["org"], e["repo"], {"owner_host": plan.owner, "reason": plan.reason}
    )
    typer.echo(
        f"\n✓ recorded {_key(e)} as local-only to this host (reason: {plan.reason}); other hosts "
        "skip it in hives-ready and may not lease or claim it"
    )


def repair_cli(
    hive: str,
    *,
    other_modes: list[bool],
    yes: bool,
    dry_run: bool,
    allow_unknown_readers: bool = False,
) -> None:
    """``bh hive repair --local-only``: the fifth repair mode, beside ``hive_repair``'s four."""
    from . import hive_repair

    hive_repair.require_one_mode([True, *other_modes])
    cfg = config.load()
    try:
        entry = hive_repair._resolve_entry(cfg, hive)
    except hive_repair.RepairError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(1) from None
    repair_local_only(
        cfg, entry, yes=yes, dry_run=dry_run, allow_unknown_readers=allow_unknown_readers
    )
