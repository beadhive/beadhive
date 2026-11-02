"""The XTDB loader's loss report and its loader codes (format ADR section 8, amendment A1-A5).

Every load, dry run or apply, returns one report validating against
``docs/schemas/beadlog/v0.1/loss-report.schema.json``. The report is computed while the load is
*planned*, before anything is written, so a dry run and the apply of the same events against the
same target report the same losses.

Loader codes (``x-<name>``, allowed by the loss-report pattern):

========================== ============== =====================================================
code                       disposition    meaning
========================== ============== =====================================================
``x-xtdb.us-split``        approximated   a same-instant, same-entity write moved by ``k`` µs
                                          (A5), once for ``/occurred_at`` and once for
                                          ``/recorded_at``; ``stored_as`` is the shifted value
``x-xtdb.sub-microsecond`` approximated   a timestamp finer than XTDB's microsecond was
                                          truncated for the store's own times; the journal keeps
                                          the event as written
``x-xtdb.column-coerced``  degraded       a field value stored as its column's one type (``2``
                                          in a text column becomes ``"2"``), so the column never
                                          widens to a union (``bh-w56ts`` E4)
``x-xtdb.column-refused``  degraded       a field value that cannot take its column's type; the
                                          column is left without it, the journal keeps it
``x-xtdb.erased``          dropped        an event erased by a tombstone in the same load (or
                                          already in the store): it is never written
``x-xtdb.id-unrepresentable`` dropped     a bead id (or edge target) containing ``|`` (or
                                          ``:`` in a multi-group load), which the layout's
                                          composite ids cannot spell
========================== ============== =====================================================
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from beadlog import FORMAT_VERSION, BeadEvent

LOSS_REPORT_VERSION = "0.1.0"

US_SPLIT = "x-xtdb.us-split"
SUB_MICROSECOND = "x-xtdb.sub-microsecond"
COLUMN_COERCED = "x-xtdb.column-coerced"
COLUMN_REFUSED = "x-xtdb.column-refused"
ERASED = "x-xtdb.erased"
ID_UNREPRESENTABLE = "x-xtdb.id-unrepresentable"
LOADER_CODES = frozenset(
    {US_SPLIT, SUB_MICROSECOND, COLUMN_COERCED, COLUMN_REFUSED, ERASED, ID_UNREPRESENTABLE}
)

# Where a value the native tables cannot hold is still kept: the loader's event journal.
JOURNAL_STORED_AS = "bead_events.event"
ERASE_LOGICAL_DETAIL = (
    "erase-logical: XTDB ERASE removes the content from every valid-time and system-time read, "
    "but its bytes may survive in the transaction log and block files until the database is "
    "rebuilt from a purged BeadLog (format ADR A1); operator acknowledgement required"
)


def batch_digest(events: Iterable[BeadEvent]) -> str:
    """SHA-256 of the batch's sorted event ids: the report's ``archive`` digest for bare events.

    An archive driver (:func:`~beadlog_xtdb.load_archive`) replaces it with the manifest digest.
    """
    joined = "\n".join(sorted(event.id for event in events))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class LossReport:
    """Accumulates the losses of one planned load."""

    batch: tuple[BeadEvent, ...]
    losses: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        event: BeadEvent,
        code: str,
        disposition: str,
        *,
        member: str = "",
        stored_as: str | None = None,
        detail: str = "",
    ) -> None:
        loss: dict[str, Any] = {
            "event_id": event.id,
            "code": code,
            "member": member,
            "disposition": disposition,
            "stored_as": stored_as,
        }
        if detail:
            loss["detail"] = detail
        self.losses.append(loss)

    def drop(self, event: BeadEvent, code: str, detail: str, *, member: str = "") -> None:
        self.add(event, code, "dropped", member=member, detail=detail)

    def dropped(self) -> set[str]:
        return {loss["event_id"] for loss in self.losses if loss["disposition"] == "dropped"}

    def codes(self) -> Counter[str]:
        return Counter(loss["code"] for loss in self.losses)

    def to_json(
        self,
        *,
        name: str,
        version: str,
        capabilities: Iterable[str],
        target: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        dropped = self.dropped()
        degraded = {loss["event_id"] for loss in self.losses} - dropped
        ordered = sorted(
            self.losses, key=lambda loss: (loss["event_id"], loss["code"], loss["member"])
        )
        events = len(self.batch)
        return {
            "loss_report_version": LOSS_REPORT_VERSION,
            "archive": {
                "format_version": FORMAT_VERSION,
                "manifest_sha256": batch_digest(self.batch),
            },
            "loader": {"name": name, "version": version, "capabilities": sorted(capabilities)},
            "target": target,
            "mode": "dry-run" if dry_run else "apply",
            "summary": {
                "events": events,
                "loaded": events - len(dropped),
                "degraded": len(degraded),
                "dropped": len(dropped),
                "by_code": dict(sorted(self.codes().items())),
            },
            "losses": ordered,
        }


__all__ = [
    "COLUMN_COERCED",
    "COLUMN_REFUSED",
    "ERASED",
    "ERASE_LOGICAL_DETAIL",
    "ID_UNREPRESENTABLE",
    "JOURNAL_STORED_AS",
    "LOADER_CODES",
    "LOSS_REPORT_VERSION",
    "SUB_MICROSECOND",
    "US_SPLIT",
    "LossReport",
    "batch_digest",
]
