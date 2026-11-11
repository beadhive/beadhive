"""One-line builders of synthetic evidence for tests (``bdlg.evidence/1``).

Each category has a builder with valid defaults for every required attr, so one artifact of
any category is one call::

    from beadlog_sources.testing import evidence as ev

    corpus = ev.corpus(
        ev.change("a1b2c3d", refs=[ev.ref("bh-12x")]),
        ev.review(143, closes=["gh:issue/7"]),
        ev.tracker_item("bh-12x"),
        ev.design_record("docs/design/x.md", declared_status="accepted"),
        ev.plan_item("phase-1/plan-01"),
        ev.release_marker("v1.2.0"),
    )

Extra keyword arguments override or extend the normalized ``attrs``; the record fields
(``title``, ``occurred_at``, ``refs``, ``raw_attrs``, ``provenance``, ``reader``,
``reader_version``, ``kind``) are keyword arguments of every builder. Builders validate exactly
like production code, so an override that breaks a category shape raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..evidence import (
    OBSERVED,
    Artifact,
    Category,
    EvidenceCorpus,
    ReaderHeader,
    Ref,
    build_corpus,
)

READER = "fixture"
READER_VERSION = "1"
WHEN = "2026-01-01T00:00:00Z"


def ref(raw: str, kind: str = "bead-token", where: str = "subject") -> Ref:
    """A :class:`Ref`; by default a bead token found in a subject line."""
    return Ref(kind, raw, where)


def artifact(
    category: Category | str,
    key: str,
    attrs: Mapping[str, Any],
    *,
    kind: str,
    title: str | None = None,
    occurred_at: str | None = WHEN,
    refs: Iterable[Ref] = (),
    raw_attrs: Mapping[str, Any] | None = None,
    provenance: str = OBSERVED,
    reader: str = READER,
    reader_version: str = READER_VERSION,
) -> Artifact:
    """An :class:`Artifact` with fixture defaults for the record fields."""
    return Artifact(
        category=Category(category),
        kind=kind,
        key=key,
        title=key if title is None else title,
        occurred_at=occurred_at,
        attrs=dict(attrs),
        raw_attrs={} if raw_attrs is None else dict(raw_attrs),
        refs=tuple(refs),
        provenance=provenance,
        reader=reader,
        reader_version=reader_version,
    )


def change(sha: str = "a1b2c3d", *, kind: str = "commit", **fields: Any) -> Artifact:
    """A ``change`` (a git commit) keyed ``git:commit/<sha>``."""
    record, attrs = _split(fields)
    defaults = {
        "sha": sha,
        "parents": [],
        "author": "Fixture Author <fixture@example.invalid>",
        "committed_at": record.get("occurred_at") or WHEN,
        "cc_type": "feat",
        "cc_scope": None,
        "paths": [],
        "trailers": {},
    }
    return artifact(
        Category.CHANGE, f"git:commit/{sha}", {**defaults, **attrs}, kind=kind, **record
    )


def review(number: int = 1, *, kind: str = "pr", **fields: Any) -> Artifact:
    """A ``review`` (a merged pull request) keyed ``gh:pr/<number>``."""
    record, attrs = _split(fields)
    when = record.get("occurred_at") or WHEN
    defaults = {
        "number": number,
        "state": "merged",
        "opened_at": when,
        "merged_at": when,
        "commits": [],
        "closes": [],
    }
    return artifact(Category.REVIEW, f"gh:pr/{number}", {**defaults, **attrs}, kind=kind, **record)


def tracker_item(item_id: str = "1", *, kind: str = "issue", **fields: Any) -> Artifact:
    """A ``tracker-item`` (an open issue) keyed ``gh:issue/<item_id>``."""
    record, attrs = _split(fields)
    defaults = {
        "id": item_id,
        "state": "open",
        "labels": [],
        "assignee": None,
        "opened_at": record.get("occurred_at") or WHEN,
        "closed_at": None,
    }
    return artifact(
        Category.TRACKER_ITEM, f"gh:issue/{item_id}", {**defaults, **attrs}, kind=kind, **record
    )


def design_record(
    path: str = "docs/design/fixture.md", *, kind: str = "doc", **fields: Any
) -> Artifact:
    """A ``design-record`` (a doc with no declared status) keyed ``doc:<path>``."""
    record, attrs = _split(fields)
    defaults = {"declared_status": None, "declared_date": None, "owner": None, "supersedes": []}
    return artifact(
        Category.DESIGN_RECORD, f"doc:{path}", {**defaults, **attrs}, kind=kind, **record
    )


def plan_item(item: str = "plan-1", *, kind: str = "plan-item", **fields: Any) -> Artifact:
    """A ``plan-item`` (a top-level, not-done item) keyed ``plan:<item>``."""
    record, attrs = _split(fields)
    defaults = {"parent": None, "depends_on": [], "done_marker": False, "order": 0}
    return artifact(Category.PLAN_ITEM, f"plan:{item}", {**defaults, **attrs}, kind=kind, **record)


def release_marker(version: str = "v0.1.0", *, kind: str = "tag", **fields: Any) -> Artifact:
    """A ``release-marker`` (a tag) keyed ``git:tag/<version>``."""
    record, attrs = _split(fields)
    defaults = {
        "version": version,
        "released_at": record.get("occurred_at") or WHEN,
        "commit_range": None,
    }
    return artifact(
        Category.RELEASE_MARKER, f"git:tag/{version}", {**defaults, **attrs}, kind=kind, **record
    )


BUILDERS = {
    Category.CHANGE: change,
    Category.REVIEW: review,
    Category.TRACKER_ITEM: tracker_item,
    Category.DESIGN_RECORD: design_record,
    Category.PLAN_ITEM: plan_item,
    Category.RELEASE_MARKER: release_marker,
}


def one_of_each(**fields: Any) -> tuple[Artifact, ...]:
    """One default artifact of every category, in :class:`Category` order."""
    return tuple(BUILDERS[category](**fields) for category in Category)


def corpus(*artifacts: Artifact, headers: Iterable[ReaderHeader] = ()) -> EvidenceCorpus:
    """A corpus of ``artifacts`` with a default header for every reader not in ``headers``."""
    return build_corpus(artifacts, headers)


_RECORD_FIELDS = frozenset(
    {"title", "occurred_at", "refs", "raw_attrs", "provenance", "reader", "reader_version"}
)


def _split(fields: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    record = {name: value for name, value in fields.items() if name in _RECORD_FIELDS}
    attrs = {name: value for name, value in fields.items() if name not in _RECORD_FIELDS}
    return record, attrs


__all__ = [
    "BUILDERS",
    "READER",
    "READER_VERSION",
    "WHEN",
    "artifact",
    "change",
    "corpus",
    "design_record",
    "one_of_each",
    "plan_item",
    "ref",
    "release_marker",
    "review",
    "tracker_item",
]
