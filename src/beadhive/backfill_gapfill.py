"""Timeline gap fill scoped to the beads one backfill run created (#143.6).

``backfill.timeline.gapfill.on_created`` (off by default) runs the *existing* bounded
``dolt_history`` gap fill (``beadlog_sources.bd_gapfill``) over exactly the ids a create-mode
apply just made, instead of every bead that lacks a ``created`` row. Nothing here reimplements
the gap fill: the reader, its batch size / per-query timeout, the cursor and the event
derivation are the ones ``bh backfill export --history`` uses.

The pipeline calls :func:`gapfill_created` after apply, with the ids of the ``create`` events the
engine applied (:func:`created_ids`), only when :func:`on_created_enabled` says so. ``enabled``
(the export-wide gap fill) is independent: ``on_created`` alone is enough here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from beadhive_backfill import system_clock
from beadlog import BeadEvent, EventKind
from beadlog_sources import BdReadError, GapfillReader, JsonCursorFile, gap_events
from beadlog_sources.bd_gapfill import GapfillCursor, GapfillEvents

from .modules.config.contracts import BackfillGapfillConfig

ReaderFactory = Callable[[Path, BackfillGapfillConfig], GapfillReader]


def on_created_enabled(settings: BackfillGapfillConfig | None) -> bool:
    """Whether the post-apply gap fill over created beads is on (``on_created``; default off)."""
    return settings is not None and settings.on_created


def created_ids(events: Iterable[BeadEvent]) -> list[str]:
    """The sorted distinct ids of the ``create`` events (what an apply created)."""
    return sorted({e.bead.id for e in events if e.kind is EventKind.CREATE})


def _default_reader(cwd: Path, settings: BackfillGapfillConfig) -> GapfillReader:
    return GapfillReader(cwd, batch_size=settings.batch_size, timeout=settings.timeout_seconds)


def gapfill_created(
    cwd: Path,
    group: str,
    created: Iterable[str],
    settings: BackfillGapfillConfig | None,
    *,
    baseline: Sequence[BeadEvent] = (),
    state: Path | GapfillCursor | None = None,
    reader: ReaderFactory | None = None,
    extracted_at: str | None = None,
) -> GapfillEvents | None:
    """Gap-fill exactly the ``created`` ids; ``None`` when ``on_created`` is off or none exist.

    Bounded like the export gap fill: read-only ``SELECT`` batches of ``settings.batch_size``
    ids with ``settings.timeout_seconds`` per query, resumable through ``state`` (a cursor file
    path). Only the given ids are queried, and events for any other bead are dropped. ``baseline``
    is the created beads' events (their fold must stay unchanged). A stopped run raises
    :class:`BdReadError` naming the saved cursor so a re-run resumes.
    """
    ids = sorted(set(created))
    if not on_created_enabled(settings) or not ids:
        return None
    assert settings is not None
    cursor = JsonCursorFile(state) if isinstance(state, Path) else state
    try:
        read = (reader or _default_reader)(cwd, settings).read(ids, cursor)
    except BdReadError as exc:
        hint = f"; progress is saved in {state}, re-run to resume" if cursor is not None else ""
        raise BdReadError(f"gap fill of created beads stopped: {exc}{hint}") from exc
    result = gap_events(
        read,
        tuple(baseline),
        group=group,
        extracted_at=extracted_at or system_clock(),
        queried=ids,
    )
    return result


__all__ = ["created_ids", "gapfill_created", "on_created_enabled"]
