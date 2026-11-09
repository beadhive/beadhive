"""Opt-in overlap re-pull for the GitHub projection: no edit skipped by bd's checkpoint (bh-clqa5).

bd 1.3.0 records ``github.last_sync`` as the wall clock at the END of a sync, rounded up to the
next whole second, and passes it as the next incremental pull's ``since``. An issue edited on
GitHub after the fetch began and before that write carries ``updated_at < last_sync``: every later
incremental pull skips it, until it is edited again. bh leaves bd's mark alone: bd keeps it in the
dolt-ignored ``local_metadata`` table, which no bd 1.3 command can rewrite on an embedded store,
and moving it earlier would trip bd's own pull guard, which skips any row whose local
``updated_at`` (the write time for an update) is later than the mark.

With ``tracker_sync.overlap_repull`` on (host-owned, default off), bh keeps its OWN mark: the
tracker port's opaque, inclusive :class:`Checkpoint` (tracker-adapter-port-adr.md), stored per
store on this host and advanced only after a projection succeeded. After bd's incremental pull it
lists the issues the tracker reports at or after that mark and re-pulls exactly those numbers
through ``bd github sync --pull-only --prefer-github --issues=<numbers>``, so any edit bd skipped
lands on the next projection at the latest. The first run has no mark yet: it lists every issue
and re-pulls only the ones updated since shortly before bd's pull started.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: The checkpoint file's format version.
CHECKPOINT_SCHEMA = 1
#: First run only: re-pull issues updated from this long before bd's pull started (clock skew).
FIRST_RUN_MARGIN = timedelta(minutes=10)
#: Issue numbers per ``bd github sync --issues=`` call (bd fetches each one).
REPULL_BATCH = 100
_STATE_DIRS = ("state", "tracker-sync")

Clock = Callable[[], datetime]


class RepullUnavailable(RuntimeError):
    """``tracker_sync.overlap_repull`` is on but the tracker port cannot be bound (the message
    names the key to set); nothing was pulled."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OverlapRepull:
    """What :func:`beadhive.github_projection.project` needs to run the overlap re-pull.

    ``reader`` is the tracker port (``TrackerReader``) bound to the projected repository;
    ``checkpoint_path`` is this store's host-local mark; ``clock`` is read once, before bd's pull.
    """

    reader: Any
    checkpoint_path: Path
    clock: Clock = _utc_now


@dataclass
class RepullReport:
    #: True when a saved checkpoint bounded the listing; False on the first run.
    from_checkpoint: bool = False
    listed: int = 0
    repulled: list[int] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    #: True when the new checkpoint was saved (only after the whole projection succeeded).
    checkpoint_saved: bool = False


def enabled(cfg: Mapping[str, Any] | None) -> bool:
    """``tracker_sync.overlap_repull`` of a loaded config (strict ``true``; absent = off)."""
    section = (cfg or {}).get("tracker_sync") or {}
    return isinstance(section, Mapping) and section.get("overlap_repull") is True


def checkpoint_path(entry: Mapping[str, Any], home: Path) -> Path:
    """``<home>/state/tracker-sync/<hive key>/public.json``: one mark per hive public store."""
    from . import registry

    key = registry.sanitize(registry.hive_key(entry))
    return Path(home).joinpath(*_STATE_DIRS, key, "public.json")


def read_checkpoint(path: Path, repository: str) -> str | None:
    """The saved checkpoint token for ``repository``, or None (absent, unreadable, or another
    repository's — never trusted across a rebinding)."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != CHECKPOINT_SCHEMA:
        return None
    if str(doc.get("repository", "")).lower() != repository.lower():
        return None
    token = doc.get("checkpoint")
    return token if isinstance(token, str) and token else None


def write_checkpoint(path: Path, repository: str, token: str) -> None:
    """Atomically replace the saved mark (write a sibling, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    doc = {"schema": CHECKPOINT_SCHEMA, "repository": repository, "checkpoint": token}
    tmp.write_text(json.dumps(doc, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _pages(reader: Any, since: Any) -> Iterator[Any]:
    from .modules.tracker.contracts import IssueQuery

    cursor = None
    while True:
        page = reader.list_issues(IssueQuery(since=since, cursor=cursor))
        yield page
        cursor = page.next_cursor
        if cursor is None:
            return


def list_changed(
    reader: Any, token: str | None, issue_ref: re.Pattern[str], floor: datetime | None
) -> tuple[list[int], str | None, list[str]]:
    """``(numbers, new checkpoint token, warnings)`` for every issue the tracker reports at or
    after ``token`` (all issues when None, then only those updated at or after ``floor``).

    Numbers come from each issue's canonical URL matched by ``issue_ref`` (this repository's
    issue URL); an issue of any other shape is skipped with a warning. The new token is the last
    page's checkpoint, or the old one when the tracker returned none."""
    from .modules.tracker.contracts import Checkpoint

    since = Checkpoint(token) if token else None
    numbers: set[int] = set()
    warnings: list[str] = []
    mark = token
    for page in _pages(reader, since):
        for issue in page.items:
            if floor is not None and issue.revision.updated_at < floor:
                continue
            match = issue_ref.match(str(issue.key.url))
            if not match:
                warnings.append(f"overlap re-pull skipped {issue.key.url}: not this repository")
                continue
            numbers.add(int(match.group(1)))
        if page.checkpoint is not None:
            mark = page.checkpoint.token
    return sorted(numbers), mark, warnings


def first_run_floor(started: datetime) -> datetime:
    return started - FIRST_RUN_MARGIN


def batches(numbers: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(numbers), REPULL_BATCH):
        yield numbers[start : start + REPULL_BATCH]


def bound_reader(cfg: Mapping[str, Any], entry: Mapping[str, Any]) -> Any:
    """The tracker port bound to ``entry``'s public store for ``issues.read``.

    Raises :class:`RepullUnavailable` (naming the key to set) when the tracker plugin is off, the
    store binds no adapter, or ``issues.read`` is not enabled. Builds no request."""
    from . import run
    from .bootstrap.tracker import TrackerAdapters, TrackerBindingError, plugin_settings
    from .modules.tracker.contracts import Operation

    values = plugin_settings("tracker-github", cfg.get("plugins")).values
    credential = values.get("credential") or {}
    names = tuple(credential.get("sources") or ())
    adapters = TrackerAdapters(
        plugins=cfg.get("plugins"),
        plugin_kernel=cfg.get("plugin_kernel"),
        token_source=run.github_token_source(
            names=names, cache=credential.get("per_invocation_cache") is True
        ),
    )
    try:
        return adapters.for_store(entry, requested=frozenset({Operation.ISSUES_READ})).port
    except TrackerBindingError as exc:
        raise RepullUnavailable(f"tracker_sync.overlap_repull: {exc.message}") from None


def for_config(
    cfg: Mapping[str, Any] | None, entry: Mapping[str, Any], home: Path
) -> OverlapRepull | None:
    """The production :class:`OverlapRepull` when ``tracker_sync.overlap_repull`` is on, else
    None; ``home`` is bh's home directory (the mark lives under its ``state/``). A tracker that
    cannot be bound raises :class:`RepullUnavailable` before any request; callers refuse."""
    if not enabled(cfg):
        return None
    return OverlapRepull(
        reader=bound_reader(cfg or {}, entry), checkpoint_path=checkpoint_path(entry, home)
    )


__all__ = [
    "CHECKPOINT_SCHEMA",
    "FIRST_RUN_MARGIN",
    "REPULL_BATCH",
    "OverlapRepull",
    "RepullReport",
    "RepullUnavailable",
    "batches",
    "bound_reader",
    "checkpoint_path",
    "enabled",
    "first_run_floor",
    "for_config",
    "list_changed",
    "read_checkpoint",
    "write_checkpoint",
]
