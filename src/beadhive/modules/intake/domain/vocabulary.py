"""The intake and origin vocabulary: the closed dimensions, label constants and queue predicates.

Moved out of ``beadhive.state``, which stays a facade that re-exports these names and builds its
``STATE_DIMENSIONS`` from :data:`INTAKE_DIMENSIONS` plus its own entries
(external-intake-module-adr.md section 3). Standard library only.

The lifecycle states are modelled through native ``bd set-state <bead> <dim>=<value>``
(event-sourced, with the ``<dim>:<value>`` label cache). This module owns only the *vocabulary*:

- ``intake:untriaged`` is untriaged inbound; a triage disposition moves it to a terminal value
  (``accepted``, ``rejected``, ``rerouted``, ``promoted``), which clears ``untriaged`` while
  recording which disposition fielded the report. ``intake:promoted`` is also the adopt queue key.
- ``origin:<channel>`` is the CLOSED intake channel a bead entered through. ``intake`` is queue
  membership (cleared on triage); ``origin`` is a durable source tag.

Provenance has three orthogonal facets: the native ``source_system`` + ``external_ref`` pair
(external mirrors only), the closed ``origin`` channel here, and the reporter identity on
``bd --actor``. Imported beads carry a ``source_system`` but no ``origin`` label, so
:func:`origin_from_source_system` derives their channel on read without double-stamping.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Dimension names, the single spelling every consumer derives from.
INTAKE_DIM = "intake"
ORIGIN_DIM = "origin"
DELIVERY_DIM = "delivery"
NOTIFIED_DIM = "notified"

#: Built-in CLOSED dimensions owned by intake: ``{dimension: {allowed values}}``. ``state.py``
#: splices these into ``STATE_DIMENSIONS``, so ``registry.closed_dimensions`` validates them
#: fleet-wide (``bh label validate`` accepts these values and refuses any other).
INTAKE_DIMENSIONS: Mapping[str, frozenset[str]] = {
    INTAKE_DIM: frozenset({"untriaged", "accepted", "rejected", "rerouted", "promoted"}),
    # `escalation` is the fire-and-forget HQ channel; `factory-seed` is the synthetic-identity
    # channel the HQ factory stamps; `backfill` is the historical-reconstruction channel (bh-vfx9).
    # The companion `source:<kind>` facet stays intentionally OPEN: closed dimensions are reserved
    # for values code actually branches on.
    ORIGIN_DIM: frozenset({"report", "github", "import", "escalation", "factory-seed", "backfill"}),
    # Where the work a report tracks has got to (external-intake-module-adr.md section 7). Written
    # only by the intake module: link (`pending`), the merge trigger (`merged`), the release
    # trigger (`released`) and the operator correction verb (`reverted`). Never `outbound` or
    # `publish` (the contributor's, single-valued per bead) and never `release` (code-owned).
    DELIVERY_DIM: frozenset({"pending", "merged", "released", "reverted"}),
    # Which milestone the reporter was last told about. Written only by the outbox sender, after a
    # comment operation reaches `sent`. Per-operation outbox states are NOT dimensions.
    NOTIFIED_DIM: frozenset({"replied", "merged", "released", "corrected"}),
}

#: The link type from a report to the work it tracks (reserved here; used by a later bead).
TRACKS_EDGE = "tracks"

# Canonical `<dim>:<value>` label-cache entries (what `bd set-state` writes).
INTAKE_UNTRIAGED = "intake:untriaged"
INTAKE_PROMOTED = "intake:promoted"  # handed to the planner (adopt queue key)
ORIGIN_REPORT = "origin:report"
ORIGIN_GITHUB = "origin:github"
ORIGIN_IMPORT = "origin:import"
ORIGIN_ESCALATION = "origin:escalation"
ORIGIN_FACTORY_SEED = "origin:factory-seed"  # HQ factory synthetic-identity seed (akyd)
ORIGIN_BACKFILL = "origin:backfill"  # backfill skill historical reconstruction (bh-vfx9)
DELIVERY_PENDING = "delivery:pending"  # linked to work that has not merged yet
DELIVERY_MERGED = "delivery:merged"
DELIVERY_RELEASED = "delivery:released"
DELIVERY_REVERTED = "delivery:reverted"
NOTIFIED_REPLIED = "notified:replied"
NOTIFIED_MERGED = "notified:merged"
NOTIFIED_RELEASED = "notified:released"
NOTIFIED_CORRECTED = "notified:corrected"

# Triage disposition -> the terminal `intake` value it transitions to. Setting any of these via
# `bd set-state` clears `untriaged` (leaving the triage queue) while recording the outcome as an
# event-sourced state transition, not a silently-yanked label.
DISPOSITION_STATE: dict[str, str] = {
    "accept": "accepted",
    "reject": "rejected",
    "reroute": "rerouted",
    "promote": "promoted",
}


@dataclass(frozen=True)
class DeliveryAfterUnlink:
    """What unlinking a report from its work leaves on the ``delivery`` dimension.

    ``bd set-state`` has no way to clear a dimension (it refuses an empty value and there is no
    unset flag; measured on bd 1.3.0, recorded in the "I2 measurement" section of
    ``docs/design/external-intake-module-adr.md``), and clearing the cache label with
    ``bd label remove`` would leave the event-sourced history saying otherwise. So the value is
    never cleared: it stays, and ``unlinked`` records that the work it described is no longer
    tracked."""

    value: str | None
    unlinked: bool = True


def delivery_of(labels: Iterable[str] | None) -> str | None:
    """The ``delivery`` value on a bead's ``delivery:<value>`` label cache, or ``None`` when no
    registered value is present."""
    for label in labels or []:
        if label.startswith(f"{DELIVERY_DIM}:"):
            value = label.split(":", 1)[1]
            if value in INTAKE_DIMENSIONS[DELIVERY_DIM]:
                return value
    return None


def delivery_after_unlink(labels: Iterable[str] | None) -> DeliveryAfterUnlink:
    """The pure rule for unlinking: keep the current ``delivery`` value (``pending`` stays
    ``pending``) and add the ``unlinked`` fact. Never a clear, because none is expressible."""
    return DeliveryAfterUnlink(value=delivery_of(labels))


def is_untriaged_intake(labels) -> bool:
    """True while a bead is untriaged inbound (`intake:untriaged`). Triage clears the
    intake dimension, so this predicate drives the triage queue."""
    return INTAKE_UNTRIAGED in (labels or [])


def is_promoted(labels) -> bool:
    """True once a report has been promoted to the planner (`intake:promoted`). Drives the
    planner's adopt queue, which builds on the triage `promote` verb."""
    return INTAKE_PROMOTED in (labels or [])


def disposition_state(disposition: str) -> str | None:
    """The terminal `intake` value a disposition transitions to (e.g. ``accept`` -> ``accepted``),
    or None for an unknown disposition."""
    return DISPOSITION_STATE.get(disposition)


def origin_of(labels: Iterable[str] | None):
    """The intake channel (`report` | `github` | `import` | ...) stamped on a bead via the
    `origin:<value>` label cache, or ``None`` when no valid origin label is present.

    Reports carry an explicit `origin:report` label; imported beads do NOT. For those, derive
    the channel from the native `source_system` via ``origin_from_source_system`` (or use
    ``channel_of`` to resolve both in one call)."""
    for label in labels or []:
        if label.startswith(f"{ORIGIN_DIM}:"):
            value = label.split(":", 1)[1]
            if value in INTAKE_DIMENSIONS[ORIGIN_DIM]:
                return value
    return None


def is_report_origin(labels) -> bool:
    """True for a bead that entered through the cross-hive `bh report` channel
    (`origin:report`). The triage queue keys on this channel."""
    return ORIGIN_REPORT in (labels or [])


def is_escalation_origin(labels) -> bool:
    """True for a bead that entered through the fire-and-forget `bh escalate` channel
    (`origin:escalation`). Always lands in HQ."""
    return ORIGIN_ESCALATION in (labels or [])


def origin_from_source_system(source_system):
    """Derive the intake channel from a bead's NATIVE `source_system`: a READ-side map for
    imported beads (github / legacy import) that carry a `source_system` but no `origin:`
    label. Returns the channel or ``None`` for an unknown / empty value. This does NOT re-stamp
    an origin label; it maps `source_system` -> channel on read so the triage queue is uniform
    WITHOUT double-stamping imports."""
    value = (source_system or "").strip().lower()
    return value if value in INTAKE_DIMENSIONS[ORIGIN_DIM] else None


def channel_of(labels, source_system=None):
    """Uniform intake channel for the triage queue: the explicit `origin:` label if present
    (reports), else derived from the native `source_system` (imported beads). Returns the
    channel or ``None``."""
    return origin_of(labels) or origin_from_source_system(source_system)


__all__ = [
    "DELIVERY_DIM",
    "DELIVERY_MERGED",
    "DELIVERY_PENDING",
    "DELIVERY_RELEASED",
    "DELIVERY_REVERTED",
    "DISPOSITION_STATE",
    "INTAKE_DIM",
    "INTAKE_DIMENSIONS",
    "INTAKE_PROMOTED",
    "INTAKE_UNTRIAGED",
    "NOTIFIED_CORRECTED",
    "NOTIFIED_DIM",
    "NOTIFIED_MERGED",
    "NOTIFIED_RELEASED",
    "NOTIFIED_REPLIED",
    "ORIGIN_BACKFILL",
    "ORIGIN_DIM",
    "ORIGIN_ESCALATION",
    "ORIGIN_FACTORY_SEED",
    "ORIGIN_GITHUB",
    "ORIGIN_IMPORT",
    "ORIGIN_REPORT",
    "TRACKS_EDGE",
    "DeliveryAfterUnlink",
    "channel_of",
    "delivery_after_unlink",
    "delivery_of",
    "disposition_state",
    "is_escalation_origin",
    "is_promoted",
    "is_report_origin",
    "is_untriaged_intake",
    "origin_from_source_system",
    "origin_of",
]
