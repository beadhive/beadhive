"""The ``intake.source`` metadata document, the ``intake.thread`` slot, and the bead rows they
ride on.

One flat top-level metadata key, ``intake.source`` (the dot is part of the key, never a JSON
path), holds the binding facts and the last issue snapshot of a bound report; import and discovery
are its sole writer (external-intake-module-adr.md section 4). The issue URL is not repeated here:
it is the bead's native ``external_ref``. The document is schema-versioned and rewritten whole.
``intake.thread`` (the conversation) is a separate key with its own writer; its codec is the
module's (``thread_to_document`` / ``thread_from_document``), so every store shares one format.
:func:`bead_view` never decodes it: a queue row carries it, but nothing on that path reads it.
Pure functions over plain mappings; no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from beadhive.modules.intake.contracts import (
    THREAD_KEY,
    BeadView,
    IssueBinding,
    IssueStatus,
    SourceSnapshot,
)

SOURCE_KEY = "intake.source"
SCHEMA = 1


def source_document(binding: IssueBinding | None, snapshot: SourceSnapshot | None) -> dict:
    """The ``intake.source`` value for a binding and/or snapshot (the URL stays on the bead)."""
    document: dict[str, Any] = {"schema": SCHEMA}
    if binding is not None:
        document["binding"] = {
            "tracker": binding.tracker,
            "project": binding.project,
            "identifier": binding.identifier,
        }
    if snapshot is not None:
        document["snapshot"] = {
            "title": snapshot.title,
            "body": snapshot.body,
            "state": snapshot.state.value,
            "revision": snapshot.revision,
            "closed_reason": snapshot.closed_reason,
            "labels": list(snapshot.labels),
            "author_id": snapshot.author_id,
            "author_kind": snapshot.author_kind,
            "source_missing": snapshot.source_missing,
        }
    return document


def _documents(row: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    return metadata if isinstance(metadata, Mapping) else {}


def stored_thread(row: Mapping[str, Any]) -> Any | None:
    """The row's raw ``intake.thread`` value (None when the bead has none). Not decoded here."""
    return _documents(row).get(THREAD_KEY)


def current_source(row: Mapping[str, Any]) -> dict:
    """The row's ``intake.source`` document as a plain dict ({} when absent or malformed)."""
    document = _documents(row).get(SOURCE_KEY)
    return dict(document) if isinstance(document, Mapping) else {}


def _snapshot(document: Mapping[str, Any]) -> SourceSnapshot | None:
    raw = document.get("snapshot")
    if not isinstance(raw, Mapping):
        return None
    try:
        state = IssueStatus(str(raw.get("state", "open")))
    except ValueError:
        state = IssueStatus.OPEN
    labels = raw.get("labels")
    return SourceSnapshot(
        title=str(raw.get("title", "")),
        body=str(raw.get("body", "")),
        state=state,
        revision=str(raw.get("revision", "")),
        closed_reason=str(raw.get("closed_reason", "")),
        labels=tuple(str(x) for x in labels) if isinstance(labels, list) else (),
        author_id=str(raw.get("author_id", "")),
        author_kind=str(raw.get("author_kind", "unknown")),
        source_missing=bool(raw.get("source_missing", False)),
    )


def _binding(row: Mapping[str, Any], document: Mapping[str, Any]) -> IssueBinding | None:
    url = str(row.get("external_ref") or "")
    if not url:
        return None
    facts = document.get("binding")
    facts = facts if isinstance(facts, Mapping) else {}
    return IssueBinding(
        report=str(row.get("id", "")),
        tracker=str(facts.get("tracker") or row.get("source_system") or ""),
        project=str(facts.get("project", "")),
        url=url,
        identifier=str(facts.get("identifier", "")),
    )


def bead_view(row: Mapping[str, Any]) -> BeadView:
    """A store row (``bd list`` / ``bd show`` JSON) as the fields intake decides on."""
    document = current_source(row)
    labels = row.get("labels")
    priority = row.get("priority")
    return BeadView(
        id=str(row.get("id", "")),
        title=str(row.get("title", "")),
        issue_type=str(row.get("issue_type", "")),
        status=str(row.get("status", "open")),
        priority="" if priority is None else str(priority),
        labels=tuple(str(x) for x in labels) if isinstance(labels, list) else (),
        source_system=str(row.get("source_system") or ""),
        external_ref=str(row.get("external_ref") or ""),
        assignee=str(row.get("assignee") or ""),
        binding=_binding(row, document),
        snapshot=_snapshot(document),
        raw=dict(row),
    )


__all__ = [
    "SCHEMA",
    "SOURCE_KEY",
    "THREAD_KEY",
    "bead_view",
    "current_source",
    "source_document",
    "stored_thread",
]
