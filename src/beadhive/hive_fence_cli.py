"""``bh hive fence cutover|status|rollback|orphans|orphan-merge`` — the HIDDEN, TEMPORARY per-hive
cutover verb.

bh-oarxp (P-M4), ADR ``docs/design/hive-writer-partitioning-adr.md`` Decision 3. Documented only
in ``docs/design/hive-writer-cutover-runbook.md``; outside ``bh --help``, ``bh hive --help`` and
the CLI reference. Removed by E1 once every hive is cut over. Operator-invoked on the current
holder, one hive at a time: nothing calls it automatically and no config key turns it on.

The procedure itself is :mod:`beadhive.fence_cutover`; this module only resolves the hive, this
host and HQ, renders, and maps refusals to exit 1. ``orphans`` / ``orphan-merge`` (bh-4z3oz) list
and merge the ``frame/<id>/orphan-<epoch>-<n>`` branches a superseded frame's managed push
diverted to (:mod:`beadhive.fence_orphan`).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import fence_cutover, fence_data, fence_orphan, fence_schema

__all__ = ["impl_cutover", "impl_orphan_merge", "impl_orphans", "impl_rollback", "impl_status"]


def _context(prefix: str, hive_dir: Path) -> fence_data.FenceNode:
    node = fence_data.node_for(hive_dir)
    if node is None:
        typer.echo(
            f"✗ {prefix}: no local Dolt store this verb can open at {hive_dir} (bd's mode is "
            "unknown or the store is not on this host) — run it on a host that carries the hive",
            err=True,
        )
        raise typer.Exit(1)
    return node


def _host_and_hq() -> tuple[str, Path]:
    from . import host_cli

    return host_cli._require_host_id(), host_cli._require_hq_dir()


def _ports(prefix: str, hive_dir: Path, hq_dir: Path):
    ref = fence_cutover.GitLegacyRef(remote="origin", cwd=hive_dir)
    placement = fence_cutover.LeasePlacementReader(remote="origin", prefix=prefix, cwd=hq_dir)
    return ref, placement


def _fail(exc: Exception) -> None:
    typer.echo(f"✗ {exc}", err=True)
    raise typer.Exit(1) from None


def _emit(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def impl_cutover(prefix: str, hive_dir: Path, *, others_published: bool, as_json: bool) -> None:
    """C1–C6 on this host (it must be the current holder)."""
    node = _context(prefix, hive_dir)
    host_id, hq_dir = _host_and_hq()
    ref, placement = _ports(prefix, hive_dir, hq_dir)
    try:
        out = fence_cutover.cutover(
            node,
            placement,
            ref,
            prefix=prefix,
            host_id=host_id,
            others_published=others_published,
        )
    except (RuntimeError, ValueError) as exc:  # CutoverError, HQ / git remote failures
        _fail(exc)
        return
    fence_data.reset_probe_cache()  # this process now sees the hive's data switch
    if as_json:
        _emit(
            {
                **out.record.as_dict(),
                "already": out.already,
                "provisioned": out.provisioned,
                "replica": out.replica,
                "trigger_count": out.guard.count,
                "fence_audit": out.audit.as_dict(),
            }
        )
        return
    r = out.record
    if out.replica:
        done = "provisioned" if out.provisioned else "already had"
        typer.echo(
            f"✓ {prefix}: cut over (holder {r.holder}, epoch {r.epoch}); this replica {done} "
            f"its {fence_schema.LOCAL_IDENT_TABLE} and stays read-only on main until an adopt "
            "names it"
        )
        return
    verb = "already cut over" if out.already else "cut over"
    typer.echo(
        f"✓ {prefix}: {verb} to the in-data epoch fence at epoch {r.epoch} (holder {r.holder})"
    )
    typer.echo(f"  cutover commit  {r.commit or '?'}")
    typer.echo(f"  refs/bh/epoch   {r.ref_sha or '?'}")
    typer.echo(f"  triggers        {out.guard.count} of {fence_data.TRIGGER_COUNT}")
    typer.echo("  fence_audit     clean")
    if out.provisioned:
        typer.echo(f"  provisioned this host's {fence_schema.LOCAL_IDENT_TABLE}")
    typer.echo(
        "  next: every other replica provisions its identity on its next pull (until then it "
        "is read-only on main) — see the runbook"
    )


def impl_status(prefix: str, hive_dir: Path, *, as_json: bool) -> None:
    """Read-only: {hive, E, cutover commit, ref sha, fence_audit, trigger count}."""
    node = _context(prefix, hive_dir)
    placement = None
    ref = fence_cutover.GitLegacyRef(remote="origin", cwd=hive_dir)
    errors: list[str] = []
    try:
        _host_id, hq_dir = _host_and_hq()
        placement = fence_cutover.LeasePlacementReader(
            remote="origin", prefix=prefix, cwd=hq_dir
        ).read()
    except typer.Exit:
        errors.append("HQ placement not read (no HQ on this host)")
    except Exception as exc:  # noqa: BLE001 — status degrades, it never crashes on HQ
        errors.append(f"HQ placement unreadable: {exc}")
    st = fence_cutover.status(node, prefix=prefix, placement=placement, ref=ref)
    payload = st.as_dict()
    payload["findings"] = errors + payload["findings"]
    if as_json:
        _emit(payload)
    else:
        for line in fence_cutover.render_status(payload):
            typer.echo(line)
    if payload["findings"]:
        raise typer.Exit(1)


def impl_rollback(prefix: str, hive_dir: Path, *, as_json: bool) -> None:
    """R1–R5 on the current bh_writer holder; on a rolled-back hive, R5 only."""
    node = _context(prefix, hive_dir)
    host_id, hq_dir = _host_and_hq()
    ref, placement = _ports(prefix, hive_dir, hq_dir)
    try:
        out = fence_cutover.rollback(node, placement, ref, prefix=prefix, host_id=host_id)
    except (RuntimeError, ValueError) as exc:  # CutoverError, HQ / git remote failures
        _fail(exc)
        return
    fence_data.reset_probe_cache()
    if as_json:
        _emit(out.as_dict())
        return
    if out.already:
        dropped = "dropped" if out.dropped_ident else "had no"
        typer.echo(f"✓ {prefix}: not cut over on the remote head; {dropped} local identity (R5)")
        return
    typer.echo(f"✓ {prefix}: rolled back to the legacy fence (floor {out.floor})")
    typer.echo(f"  rollback commit {out.commit}")
    typer.echo(f"  refs/bh/epoch   {out.ref_sha}")
    typer.echo(
        "  next: run `bh hive fence rollback` on every other replica after it pulls (R5); "
        "the next adopt mints an epoch above the floor"
    )


def impl_orphans(prefix: str, hive_dir: Path, *, as_json: bool) -> None:
    """Read-only: the orphan branches on the hive's remote not yet merged into its head."""
    node = _context(prefix, hive_dir)
    try:
        orphans = fence_orphan.list_orphans(node)
    except (RuntimeError, ValueError) as exc:
        _fail(exc)
        return
    if as_json:
        _emit({"hive": prefix, "orphans": [o.as_dict() for o in orphans]})
        return
    if not orphans:
        typer.echo(f"✓ {prefix}: no unmerged orphan branches")
        return
    typer.echo(f"{prefix}: {len(orphans)} unmerged orphan branch(es)")
    for o in orphans:
        typer.echo(f"  {o.branch}  {o.commit[:12]}  (frame {o.frame}, epoch {o.epoch})")
    typer.echo(f"  merge on the writer: bh hive fence orphan-merge {prefix} --branch <branch>")


def impl_orphan_merge(prefix: str, hive_dir: Path, *, branch: str, as_json: bool) -> None:
    """On the writer: merge one orphan in one SQL session (marks re-stamped), then publish it
    through the managed push."""
    from . import engine

    node = _context(prefix, hive_dir)
    host_id = _host_and_hq()[0]
    try:
        merged = fence_orphan.merge_orphan(node, branch, frame=host_id)
    except (RuntimeError, ValueError) as exc:
        _fail(exc)
        return
    published = True
    detail = ""
    if not merged.already:
        res = engine.get_engine().push_state(hive_dir, message=f"bh: publish merge of {branch}")
        published = res.returncode == 0
        detail = (f"{res.stdout or ''}{res.stderr or ''}").strip()
    if as_json:
        _emit({**merged.as_dict(), "hive": prefix, "published": published, "detail": detail})
    elif merged.already:
        typer.echo(f"✓ {prefix}: {branch} is already merged into main ({merged.orphan_commit})")
    else:
        typer.echo(
            f"✓ {prefix}: merged {branch} at epoch {merged.epoch} — {merged.restamped} mark(s) "
            f"re-stamped, commit {merged.commit}"
        )
        if published:
            typer.echo("  published through the managed push")
    if not published:
        typer.echo(
            f"✗ {prefix}: the merge is committed locally but its publish failed; retry with "
            f"`bh hive sync remotes --push`.\n  {detail[:600]}",
            err=True,
        )
        raise typer.Exit(1)
