"""`bh hive remote sync --store <tracker store>`: pull from the tracker, push to its replica.

Operator model (docs/design, bh-ga5sz): a TRACKER store (today the public store whose
``source`` is ``github-issues``) has exactly one authority, the tracker. One sync rule applies:
pull from the authority, then push to the replicas. For a tracker store that is

1. PULL — :func:`github_projection.project` (the former ``bh hive project-issues``);
   ``--pull-only`` stops after it;
2. PUSH — :func:`public_publish.publish_public`: fast-forward only to the store's Git Dolt
   replica (``git-origin`` ``refs/dolt/data``).

The one-time replacement of a non-fast-forward replica is the separate, explicit
``--force-with-lease <old-sha> --confirm`` direction (and is the only way past a rejected
fast-forward — there is no bare force route). Internal stores keep ``bh hive sync remotes``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import github_projection, jsonout, public_publish, store_target
from .store_target import SCOPE_PUBLIC

TRACKER_SYNC_SCHEMA = 1


class TrackerSyncError(RuntimeError):
    """The sync refused before touching anything, or a leg failed."""


@dataclass
class TrackerSyncReport:
    hive: str
    store: str
    dry_run: bool
    pull: github_projection.ProjectionReport | None = None
    publish: public_publish.PublishReport | None = None

    @property
    def ok(self) -> bool:
        return self.pull is None or self.pull.ok


def select_tracker_store(entry: Mapping[str, Any], store: str) -> store_target.StoreTarget:
    """The tracker store named ``store`` (its prefix, or the scope word ``public``), or a
    refusal. Only tracker stores are handled here; the internal store is never selected."""
    wanted = (store or "").strip()
    if not wanted:
        raise TrackerSyncError("--store is required: name the tracker store to sync")
    target = store_target.resolve_store(entry, SCOPE_PUBLIC)
    if wanted not in (target.prefix, SCOPE_PUBLIC):
        raise TrackerSyncError(
            f"{target.hive}: no tracker store {wanted!r} (this hive's tracker store is "
            f"{target.prefix!r}); the internal store syncs with `bh hive sync remotes`"
        )
    if target.source != github_projection.SOURCE_GITHUB_ISSUES:
        raise TrackerSyncError(
            f"{target.hive}: store {target.prefix!r} is not a tracker store (source "
            f"{target.source!r}); only tracker stores sync here"
        )
    return target


def sync(
    entry: Mapping[str, Any],
    store: str,
    *,
    pull_only: bool = False,
    dry_run: bool = False,
    force_with_lease: str = "",
    confirmed: bool = False,
    eng=None,
    origin_url: str | None = None,
) -> TrackerSyncReport:
    target = select_tracker_store(entry, store)
    report = TrackerSyncReport(hive=target.hive, store=target.prefix, dry_run=dry_run)
    if pull_only and (force_with_lease or confirmed):
        raise TrackerSyncError("--pull-only never pushes; drop --force-with-lease / --confirm")
    if force_with_lease:
        # The replacement moves only the replica and does not pull: it is a separate direction.
        report.publish = public_publish.publish_public(
            entry,
            dry_run=dry_run,
            eng=eng,
            origin_url=origin_url,
            lease=force_with_lease,
            confirmed=confirmed,
        )
        return report
    if confirmed:
        raise TrackerSyncError("--confirm only applies with --force-with-lease")
    report.pull = github_projection.project(entry, dry_run=dry_run)
    if not report.pull.ok or pull_only:
        return report
    report.publish = public_publish.publish_public(
        entry, dry_run=dry_run, eng=eng, origin_url=origin_url
    )
    return report


def payload(report: TrackerSyncReport) -> dict[str, Any]:
    body: dict[str, Any] = {
        "hive": report.hive,
        "store": report.store,
        "dry_run": report.dry_run,
        "ok": report.ok,
        "pull": report.pull.to_json() if report.pull else None,
        "push": None,
    }
    if report.publish is not None:
        body["push"] = public_publish.payload(report.publish)
    return jsonout.envelope("hive remote sync", TRACKER_SYNC_SCHEMA, body)


def render(report: TrackerSyncReport) -> list[str]:
    lines: list[str] = []
    if report.pull is not None:
        lines += github_projection.render(report.pull)
    if report.publish is not None:
        lines += public_publish.render_report(report.publish)
    return lines


__all__ = [
    "TrackerSyncError",
    "TrackerSyncReport",
    "payload",
    "render",
    "select_tracker_store",
    "sync",
]
