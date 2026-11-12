"""The ``github`` source reader: a snapshot's pull requests and issues as evidence artifacts.

A pure :class:`~beadlog_sources.readers.SourceReader` (design: ``backfill-history-pipeline-adr``,
D6). It reads the JSON snapshot that :func:`~beadhive_tracker_github.snapshot.fetch_snapshot`
wrote and never touches the network, so re-extraction is deterministic and offline.

Artifacts (provenance ``observed``):

- ``review`` keyed ``gh:pr/<N>``, kind ``pull-request``: ``number``, ``state`` (``open``,
  ``closed`` or ``merged``), ``opened_at``, ``merged_at``, ``closed_at``, ``draft``, ``commits``
  (the PR's shas) and ``closes`` (``gh:issue/<N>`` of each closing reference). The clock is
  ``merged_at``, else ``closed_at``, else ``opened_at``.
- ``tracker-item`` keyed ``gh:issue/<N>``, kind ``issue``: ``id`` (the key), ``number``,
  ``state`` (``open``/``closed``), ``labels`` (sorted), ``assignee`` (the first assignee),
  ``assignees``, ``opened_at``, ``closed_at``.

Timestamps are canonical BeadLog UTC (an unparsable one becomes ``None``, the written text kept in
``raw_attrs``). Un-interpreted refs: closing references and PR shas, plus bead-looking tokens of
the title and body (shape only, anchored by the ``namespaces`` option when given;
``header_options`` records ``token_mode``).

Options: ``snapshot`` (the snapshot file, relative to the request root; default
``github-snapshot.json``) and ``namespaces``. The request cursor is ignored: a snapshot is the
whole state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from beadlog import TimestampError, format_timestamp
from beadlog_sources import (
    Artifact,
    Category,
    ReadRequest,
    Ref,
    bead_token_pattern,
    find_bead_tokens,
    token_mode,
)

from .snapshot import load_snapshot

READER_NAME = "github"
READER_VERSION = "0.1.0"
DEFAULT_SNAPSHOT = "github-snapshot.json"


def _csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return format_timestamp(moment) if moment.tzinfo is not None else None
    except (ValueError, TimestampError):
        return None


def _refs(row: Mapping[str, Any], beads: Any) -> list[Ref]:
    refs: list[Ref] = []
    for where in ("title", "body"):
        for token in find_bead_tokens(str(row.get(where) or ""), beads):
            ref = Ref("bead-token", token, where)
            if ref not in refs:
                refs.append(ref)
    return refs


def _pull(row: Mapping[str, Any], beads: Any) -> Artifact:
    number = int(row["number"])
    opened, closed, merged = (_time(row[k]) for k in ("opened_at", "closed_at", "merged_at"))
    closes = [f"gh:issue/{n}" for n in row["closes"]]
    commits = list(row["commits"] or [])
    refs = [Ref("issue", raw, "closes") for raw in closes]
    refs += [Ref("sha", sha, "commits") for sha in commits]
    return Artifact(
        category=Category.REVIEW,
        kind="pull-request",
        key=f"gh:pr/{number}",
        title=str(row["title"]),
        occurred_at=merged or closed or opened,
        attrs={
            "number": number,
            "state": row["state"],
            "opened_at": opened,
            "merged_at": merged,
            "closed_at": closed,
            "draft": bool(row["draft"]),
            "commits": commits,
            "closes": closes,
        },
        raw_attrs={k: row[k] for k in ("opened_at", "closed_at", "merged_at") if row[k]},
        refs=tuple(refs + _refs(row, beads)),
        reader=READER_NAME,
        reader_version=READER_VERSION,
    )


def _issue(row: Mapping[str, Any], beads: Any) -> Artifact:
    number = int(row["number"])
    key = f"gh:issue/{number}"
    opened, closed = _time(row["opened_at"]), _time(row["closed_at"])
    assignees = list(row["assignees"])
    return Artifact(
        category=Category.TRACKER_ITEM,
        kind="issue",
        key=key,
        title=str(row["title"]),
        occurred_at=closed or opened,
        attrs={
            "id": key,
            "number": number,
            "state": row["state"],
            "state_reason": row["state_reason"],
            "labels": sorted(row["labels"]),
            "assignee": assignees[0] if assignees else None,
            "assignees": assignees,
            "opened_at": opened,
            "closed_at": closed,
        },
        raw_attrs={k: row[k] for k in ("opened_at", "closed_at") if row[k]},
        refs=tuple(_refs(row, beads)),
        reader=READER_NAME,
        reader_version=READER_VERSION,
    )


def read_snapshot(snapshot: Mapping[str, Any], namespaces: Iterable[str] = ()) -> list[Artifact]:
    """The artifacts of a validated snapshot dict: pull requests then issues, by number."""
    beads = bead_token_pattern(list(namespaces))
    return [_pull(r, beads) for r in snapshot["pulls"]] + [
        _issue(r, beads) for r in snapshot["issues"]
    ]


class GitHubReader:
    """The ``github`` :class:`~beadlog_sources.readers.SourceReader`."""

    name = READER_NAME
    version = READER_VERSION
    categories: tuple[Category, ...] = (Category.REVIEW, Category.TRACKER_ITEM)

    @staticmethod
    def header_options(options: Mapping[str, Any]) -> dict[str, Any]:
        """The options recorded in the run's ReaderHeader: the given ones plus ``token_mode``."""
        return {**options, "token_mode": token_mode(_csv(options.get("namespaces", "")))}

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        """The pull requests and issues of the snapshot file named by ``request``."""
        path = Path(request.root) / str(request.options.get("snapshot") or DEFAULT_SNAPSHOT)
        return read_snapshot(load_snapshot(path), _csv(request.options.get("namespaces", "")))


__all__ = ["DEFAULT_SNAPSHOT", "READER_NAME", "READER_VERSION", "GitHubReader", "read_snapshot"]
