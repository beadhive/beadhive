"""The backfill plan: a ``.bdlg`` archive with a manifest ``plan`` section (format ADR §1).

A plan's events are the NEW and DRIFT events reconcile found missing from the baseline; its
manifest ``plan`` section names the pass, the pass schema version, the corpus (projection)
hash, the judge identity and the plan hash; its annotations carry the reconcile
classification (``bdlg.reconcile``, readable by :func:`beadlog_sources.plan_entries`) and the
engine's own record (:data:`ENGINE_ANNOTATION`: pass settings, scope and totals, applicable
event ids, the recorded pass judgments and the pass report).

The plan hash covers the plan section, every event and both annotations, so any edit to a
saved plan is refused. Plans written by the neutral ``beadlog reconcile`` (no engine
annotation) are also accepted: their own plan hash is verified, and only their NEW proposals
are applicable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

from beadlog import (
    ArchiveError,
    BeadEvent,
    BeadRef,
    Generator,
    Group,
    Manifest,
    ManifestError,
    read_archive,
    sort_events,
    union_merge,
    write_archive,
)
from beadlog_sources import PlanEntry, ReconcileClass, ReconcilePlan, Verdict, prior_verdicts

from ._errors import BackfillError
from ._judgments import verdicts_from_json
from ._projection import json_hash

ENGINE_ANNOTATION = "beadhive.backfill"
ENGINE_SCHEMA = "beadhive.backfill/1"
# The reconcile classification annotation and pass name written by beadlog_sources
# (``beadlog_sources.reconcile.PLAN_ANNOTATION`` / ``PASS``; not in its package ``__all__``).
RECONCILE_ANNOTATION = "bdlg.reconcile"
RECONCILE_PASS = "reconcile"
GENERATOR = Generator("beadhive-backfill", "0.1.0")


def _applicable_ids(entries: Sequence[PlanEntry], drift_applicable: bool) -> set[str]:
    ids: set[str] = set()
    for entry in entries:
        if entry.cls is ReconcileClass.NEW or (
            entry.cls is ReconcileClass.DRIFT and drift_applicable
        ):
            ids.update(entry.events)
    return ids


@dataclass(frozen=True)
class Plan:
    """A verified-or-sealable backfill plan. Build one with ``BackfillEngine.plan`` or
    :meth:`read` a saved one; :meth:`verify` refuses an edited or corrupt plan."""

    section: Mapping[str, Any]
    events: tuple[BeadEvent, ...]
    reconcile: Mapping[str, Any]
    engine: Mapping[str, Any] | None = None
    groups: tuple[Group, ...] = field(default=())

    # -- identity ------------------------------------------------------------------------

    @property
    def pass_name(self) -> str:
        return str(self.section["pass"])

    @property
    def schema_version(self) -> str:
        return str(self.section["schema_version"])

    @property
    def corpus_hash(self) -> str:
        return str(self.section["corpus_hash"])

    @property
    def plan_hash(self) -> str:
        return str(self.section.get("plan_hash", ""))

    @property
    def neutral(self) -> bool:
        """True for a plan from the neutral ``beadlog reconcile`` (no engine annotation)."""
        return self.engine is None

    def _hash_body(self) -> dict[str, Any]:
        return {
            "plan": {key: value for key, value in self.section.items() if key != "plan_hash"},
            "events": [event.to_json() for event in sort_events(self.events)],
            "annotations": {
                RECONCILE_ANNOTATION: self.reconcile,
                ENGINE_ANNOTATION: self.engine,
            },
        }

    def expected_hash(self) -> str:
        """The plan hash this plan's content implies."""
        if self.neutral:
            rebuilt = ReconcilePlan(
                events=self.events,
                entries=self.entries,
                corpus_hash=self.corpus_hash,
                proposed_hash=str(self.reconcile.get("proposed_hash", "")),
            )
            return rebuilt.plan_hash()
        return json_hash(self._hash_body())

    def sealed(self) -> Plan:
        """This plan with its plan hash computed and recorded."""
        events = union_merge([self.events])  # the archive's normal form, so a read round-trips
        draft = Plan(dict(self.section), events, self.reconcile, self.engine, self.groups)
        return Plan(
            {**self.section, "plan_hash": draft.expected_hash()},
            events,
            self.reconcile,
            self.engine,
            self.groups,
        )

    def verify(self) -> None:
        """Raise :class:`BackfillError` unless the plan is supported and its hash matches."""
        for key in ("pass", "schema_version", "corpus_hash", "plan_hash"):
            if not isinstance(self.section.get(key), str):
                raise BackfillError(f"plan section lacks {key!r}")
        if self.neutral and self.pass_name != RECONCILE_PASS:
            raise BackfillError(f"unsupported plan pass {self.pass_name!r} without engine record")
        if self.engine is not None and self.engine.get("schema_version") != ENGINE_SCHEMA:
            raise BackfillError("unsupported beadhive-backfill plan schema")
        try:
            expected = self.expected_hash()
        except (KeyError, TypeError, ValueError) as exc:
            raise BackfillError(f"plan is malformed: {exc}") from exc
        if self.plan_hash != expected:
            raise BackfillError("plan hash mismatch — refusing an edited or corrupt plan")
        if self.engine is not None:
            recorded = set(self.engine.get("applicable") or ())
            if recorded != _applicable_ids(self.entries, self.drift_applicable):
                raise BackfillError("plan applicable events disagree with its classification")

    # -- content -------------------------------------------------------------------------

    @property
    def entries(self) -> tuple[PlanEntry, ...]:
        return tuple(PlanEntry.from_json(item) for item in self.reconcile.get("entries") or ())

    @property
    def drift_applicable(self) -> bool:
        if self.engine is None:
            return False
        return bool(self.engine.get("pass", {}).get("drift_applicable"))

    @property
    def totals(self) -> Mapping[str, Any]:
        return dict((self.engine or {}).get("totals") or {})

    @property
    def report(self) -> Mapping[str, Any]:
        return dict((self.engine or {}).get("report") or {})

    def applicable_events(self) -> tuple[BeadEvent, ...]:
        """NEW events, plus DRIFT events when the pass marks DRIFT applicable, in part order."""
        ids = _applicable_ids(self.entries, self.drift_applicable)
        return tuple(event for event in sort_events(self.events) if event.id in ids)

    def units(self) -> tuple[tuple[BeadRef, tuple[BeadEvent, ...]], ...]:
        """Applicable events grouped per bead, in bead order (the apply's unit of work)."""
        grouped: dict[BeadRef, list[BeadEvent]] = {}
        for event in self.applicable_events():
            grouped.setdefault(event.bead, []).append(event)
        return tuple((bead, tuple(events)) for bead, events in grouped.items())

    @property
    def changes(self) -> int:
        """How many beads the apply would write."""
        return len(self.units())

    def drift_reported(self) -> tuple[PlanEntry, ...]:
        """DRIFT entries that are reported but not applied."""
        if self.drift_applicable:
            return ()
        return tuple(entry for entry in self.entries if entry.cls is ReconcileClass.DRIFT)

    def reconcile_verdicts(self) -> dict[str, Verdict]:
        return prior_verdicts(self.entries)

    def pass_verdicts(self) -> dict[str, Verdict]:
        return verdicts_from_json((self.engine or {}).get("judgments") or ())

    # -- archive I/O ---------------------------------------------------------------------

    def write(
        self,
        destination: str | os.PathLike[str] | IO[bytes],
        *,
        groups: Sequence[Group] | None = None,
        generator: Generator = GENERATOR,
        description: str | None = None,
    ) -> Manifest:
        """Write the plan as a ``.bdlg`` archive (capability ``plan``)."""
        self.verify()
        annotations: dict[str, Any] = {RECONCILE_ANNOTATION: self.reconcile}
        if self.engine is not None:
            annotations[ENGINE_ANNOTATION] = self.engine
        return write_archive(
            destination,
            self.events,
            groups=list(groups if groups is not None else self.groups),
            generator=generator,
            plan=self.section,
            annotations=annotations,
            description=description,
        )

    @classmethod
    def from_archive(cls, manifest: Manifest, events: Sequence[BeadEvent]) -> Plan:
        raw = manifest.to_json()
        section = raw.get("plan")
        if not isinstance(section, Mapping):
            raise BackfillError("not a plan archive (manifest has no plan section)")
        annotations = raw.get("annotations") or {}
        engine = annotations.get(ENGINE_ANNOTATION)
        return cls(
            section=dict(section),
            events=tuple(events),
            reconcile=dict(annotations.get(RECONCILE_ANNOTATION) or {}),
            engine=dict(engine) if isinstance(engine, Mapping) else None,
            groups=tuple(manifest.groups),
        )

    @classmethod
    def read(cls, source: str | os.PathLike[str] | IO[bytes]) -> Plan:
        """Open, verify the archive and the plan hash; refuse anything edited."""
        try:
            manifest, events = read_archive(source)
        except (ArchiveError, ManifestError, OSError) as exc:
            raise BackfillError(f"could not read plan {source!r}: {exc}") from exc
        plan = cls.from_archive(manifest, events)
        plan.verify()
        return plan
