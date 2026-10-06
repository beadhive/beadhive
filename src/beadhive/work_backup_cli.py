"""``bh work backup`` — the explicit checkpoint, recoverability query and retention sweep (M14).

The verb surface over :mod:`beadhive.work_backup`:

* ``bh work backup <id>`` — push the bead worktree's committed tip to this frame's backup ref now
  (D2a's explicit checkpoint). Exit 1 when the push does not land.
* ``bh work backup <id> --status [--json]`` — the D3 query M3's reclaim policy reads: is the
  claimed bead's work recoverable from the backup remote? ``recoverable | unbacked | unknown |
  suspect`` for the claim-frame, plus every frame's ref. Read-only; works with pairing off.
* ``bh work backup --from-hook`` — the ``post-commit`` entrypoint: resolves the bead from the
  current branch, returns at once, and pushes in a detached child. A no-op unless
  ``bh.pairing.enabled`` and ``bh.pairing.checkpoint.on_commit`` are both on. Never fails a commit.
* ``bh work backup --reap [--dry-run]`` — the D7 retention sweep over every backup ref of the hive.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import work_backup, work_pairing_policy


def _bead_from_branch(target: Path) -> str:
    res = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch = (res.stdout or "").strip()
    for prefix in ("wt/bead/issue/", "wt/bead/epic/"):
        if branch.startswith(prefix):
            return branch[len(prefix) :]
    return ""


def _from_hook(api, cfg, hive: str) -> None:
    """post-commit: never raise, never block the commit, never print unless pushing."""
    try:
        bead = _bead_from_branch(Path.cwd())
        if not bead:
            return
        _entry, main, _target, _branch = api.worktree.locate(cfg, hive, bead)
        policy = work_pairing_policy.read(main)
        if not policy.checkpoint_on_commit:
            return
        argv = [sys.argv[0], "work", "backup", bead]
        if hive:
            argv += ["--hive", hive]
        subprocess.Popen(  # noqa: S603 - our own CLI, fixed argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return


def _status(api, cfg, bead: str, hive: str, as_json: bool) -> None:
    entry, main, target, _branch = api.worktree.locate(cfg, hive, bead)
    policy = work_pairing_policy.read(main)
    claim_frame = api.bd.state(bead, work_backup.CLAIM_FRAME_DIMENSION, main)
    remote = work_backup.resolve_remote(policy, cfg, entry)
    result = work_backup.recoverability(
        target if Path(target).exists() else main,
        remote,
        bead,
        claim_frame,
        integration=api.config.integration_branch(cfg, entry),
        signature_policy=policy.signature_policy,
    )
    if as_json:
        payload = {"pairing_enabled": policy.enabled, **result.to_json()}
        api.typer.echo(json.dumps(payload, indent=2))
        return
    frame = claim_frame or "(none recorded)"
    api.typer.echo(f"{bead}: {result.status} — claim-frame {frame} on {remote}")
    if result.detail:
        api.typer.echo(f"  {result.detail}")
    for ref in result.refs:
        api.typer.echo(
            f"  {ref.ref} @ {ref.sha[:12]}: {ref.status}{f' ({ref.detail})' if ref.detail else ''}"
        )
    if not policy.enabled:
        api.typer.echo("  (pairing is off for this hive: bh.pairing.enabled=false)")


def _reap(api, cfg, hive: str, dry_run: bool) -> None:
    entry = api.worktree._resolve_entry(cfg, hive)
    main = api.registry.hive_dir(entry)
    policy = work_pairing_policy.read(main)
    if not policy.enabled:
        api.typer.echo("pairing is off for this hive (bh.pairing.enabled=false) — nothing to reap")
        return
    report = work_backup.reap(
        main,
        work_backup.resolve_remote(policy, cfg, entry),
        policy=policy,
        integration=api.config.integration_branch(cfg, entry),
        show=lambda b: api.bd.show(b, main),
        dry_run=dry_run,
    )
    if report.error:
        api.typer.echo(f"✗ backup remote unreachable: {report.error}", err=True)
        raise api.typer.Exit(1)
    verb = "would delete" if dry_run else "deleted"
    for ref in report.deleted:
        api.typer.echo(f"  {verb} {ref}")
    for ref, why in report.kept:
        api.typer.echo(f"  kept {ref}: {why}")
    for ref in report.failed:
        api.typer.echo(f"⚠ delete refused (CAS lost): {ref}", err=True)
    api.typer.echo(
        f"✓ retention sweep: {len(report.deleted)} {verb}, {len(report.kept)} kept, "
        f"{len(report.failed)} refused"
    )
    if report.failed:
        raise api.typer.Exit(1)


def impl_backup(api, bead, status, as_json, from_hook, reap, dry_run, hive):
    cfg = api.config.load()
    if from_hook:
        _from_hook(api, cfg, hive)
        return
    if reap:
        _reap(api, cfg, hive, dry_run)
        return
    if not bead:
        api.typer.echo("✗ pass a bead <id> (or --reap / --from-hook)", err=True)
        raise api.typer.Exit(1)
    if status:
        _status(api, cfg, bead, hive, as_json)
        return
    entry, main, target, _branch = api.worktree.locate(cfg, hive, bead)
    policy = work_pairing_policy.read(main)
    if not policy.enabled:
        api.typer.echo(
            "pairing is off for this hive (bh.pairing.enabled=false) — nothing backed up"
        )
        return
    if not Path(target).exists():
        api.typer.echo(f"✗ no worktree for {bead} — nothing to back up", err=True)
        raise api.typer.Exit(1)
    out = work_backup.checkpoint_worktree(
        cfg=cfg,
        entry=entry,
        main=main,
        bead=bead,
        target=target,
        say=api.typer.echo,
        warn=lambda line: api.typer.echo(line, err=True),
        policy=policy,
    )
    if out is None or not out.ok:
        raise api.typer.Exit(1)
