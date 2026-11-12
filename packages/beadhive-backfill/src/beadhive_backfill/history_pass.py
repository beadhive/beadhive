"""The ``history`` hive pass: collate an evidence corpus, project the units, upsert via the engine.

Phase C of ``docs/design/backfill-history-pipeline-adr.md``. The pass runs B1 to B6
(:func:`beadlog_sources.collate`) over the corpus against the live baseline and projects the units
(:func:`beadlog_sources.project`); the engine then reconciles, plans, verifies and applies it like
any other pass. DRIFT (stamps and ``git.commits`` appends on beads that already exist) is always
applicable; what ``create`` decides is the NEW units.

**create mode** (decision D1, #143.1):

- ``create=False`` (the default, ``backfill.collate.create = "off"``): link only. Only the events
  that land on a bead the baseline already holds are proposed; every unit the baseline has no bead
  for is left alone, exactly as the link-only passes do.
- ``create=True`` (``"propose"``): NEW units are also proposed as closed beads, labelled by
  ``project_options.creation_labels`` (the caller passes ``origin:backfill`` and the hive identity
  triplet, see ``beadhive.backfill_create``) plus their ``source:`` label. They are reviewed through
  the unchanged plan, verify and apply envelope.

**Empty hive.** A hive with no beads gives the link-only mode nothing to link to, so it is refused
there (``BackfillError``). Create mode is the only mode that allows it, and only with an explicit
``prefix`` (never derived, bh-ear6v): a missing prefix is refused at construction.

The pass is neutral: the hive facts (prefix, identity labels, the ledger directory, the create
choice from config) are arguments. Nothing here imports ``beadhive``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from beadlog import BeadEvent, BeadRef, BeadState
from beadlog_sources import (
    AttachOptions,
    ClusterOptions,
    EvidenceCorpus,
    IdentifyOptions,
    JudgmentPort,
    LedgerDirectory,
    ProjectError,
    ProjectOptions,
    StatusOptions,
    collate,
    project,
)

from ._errors import BackfillError

PASS_HISTORY = "history"
HISTORY_SCHEMA_VERSION = "1"
# Creates are discarded in link-only mode, so the minted ids never reach a store; the projector
# still insists on a syntactically valid prefix, and one is never derived from the hive.
_LINK_ONLY_PREFIX = "link-only"


class HistoryPass:
    """Collate and project an evidence corpus (module doc).

    ``corpus`` is the evidence read from the hive's sources, ``directory`` the ledger directory
    its refs resolve against, ``local`` the hive's ledger name, ``group`` the BeadLog group of
    the hive. ``prefix`` is the hive's explicit id prefix (required with ``create``).
    """

    name: ClassVar[str] = PASS_HISTORY
    schema_version = HISTORY_SCHEMA_VERSION
    drift_applicable = True
    invalidating_fields: ClassVar[frozenset[str]] = frozenset(
        {"exists", "phase", "labels", "edges", "fields.external_ref", "fields.metadata.git.commits"}
    )

    def __init__(
        self,
        corpus: EvidenceCorpus,
        directory: LedgerDirectory,
        *,
        local: str,
        group: str,
        prefix: str | None = None,
        create: bool = False,
        project_options: ProjectOptions | None = None,
        cluster_options: ClusterOptions | None = None,
        attach_options: AttachOptions | None = None,
        identify_options: IdentifyOptions | None = None,
        status_options: StatusOptions | None = None,
    ) -> None:
        if create and not prefix:
            raise BackfillError(
                "create mode needs the hive's explicit bead id prefix (prefixes are never "
                "derived); set the hive prefix in the registry"
            )
        self.corpus = corpus
        self.directory = directory
        self.local = local
        self.group = group
        self.prefix = prefix or _LINK_ONLY_PREFIX
        self.create = bool(create)
        self.project_options = project_options if create else None
        self.cluster_options = cluster_options
        self.attach_options = attach_options
        self.identify_options = identify_options
        self.status_options = status_options
        self._report: dict[str, Any] = {}

    def propose(
        self,
        baseline: Sequence[BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> tuple[BeadEvent, ...]:
        if not baseline and not self.create:
            raise BackfillError(
                "the hive has no beads to link to; an empty hive is only allowed in create mode "
                "(backfill.collate.create = propose, with an explicit hive prefix)"
            )
        collation = collate(
            self.corpus,
            baseline,
            self.directory,
            local=self.local,
            options=self.cluster_options,
            attach_options=self.attach_options,
            identify_options=self.identify_options,
            status_options=self.status_options,
        )
        try:
            projection = project(
                collation,
                self.corpus,
                baseline,
                prefix=self.prefix,
                group=self.group,
                extracted_at=extracted_at,
                options=self.project_options,
            )
        except ProjectError as exc:
            raise BackfillError(f"history: {exc}") from exc
        events = tuple(projection.events)
        skipped = 0
        if not self.create:
            held = {event.bead for event in baseline}
            kept = tuple(event for event in events if event.bead in held)
            skipped = len({event.bead for event in events} - {event.bead for event in kept})
            events = kept
        self._report = {
            "create": self.create,
            "units": projection.counts(),
            "residual": list(projection.residual),
            "creates_skipped": skipped,
        }
        return events

    def report(
        self, states: Mapping[BeadRef, BeadState], proposed: Sequence[BeadEvent]
    ) -> dict[str, Any]:
        return {**self._report, "beads": len({event.bead for event in proposed})}


__all__ = ["HISTORY_SCHEMA_VERSION", "PASS_HISTORY", "HistoryPass"]
