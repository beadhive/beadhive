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

#: Dimension names, the single spelling every consumer derives from.
INTAKE_DIM = "intake"
ORIGIN_DIM = "origin"

#: Built-in CLOSED dimensions owned by intake: ``{dimension: {allowed values}}``. ``state.py``
#: splices these into ``STATE_DIMENSIONS``, so ``registry.closed_dimensions`` validates them
#: fleet-wide. Later beads register the ``delivery`` and ``notified`` dimensions here.
INTAKE_DIMENSIONS: Mapping[str, frozenset[str]] = {
    INTAKE_DIM: frozenset({"untriaged", "accepted", "rejected", "rerouted", "promoted"}),
    # `escalation` is the fire-and-forget HQ channel; `factory-seed` is the synthetic-identity
    # channel the HQ factory stamps; `backfill` is the historical-reconstruction channel (bh-vfx9).
    # The companion `source:<kind>` facet stays intentionally OPEN: closed dimensions are reserved
    # for values code actually branches on.
    ORIGIN_DIM: frozenset({"report", "github", "import", "escalation", "factory-seed", "backfill"}),
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

# Triage disposition -> the terminal `intake` value it transitions to. Setting any of these via
# `bd set-state` clears `untriaged` (leaving the triage queue) while recording the outcome as an
# event-sourced state transition, not a silently-yanked label.
DISPOSITION_STATE: dict[str, str] = {
    "accept": "accepted",
    "reject": "rejected",
    "reroute": "rerouted",
    "promote": "promoted",
}


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
    "DISPOSITION_STATE",
    "INTAKE_DIM",
    "INTAKE_DIMENSIONS",
    "INTAKE_PROMOTED",
    "INTAKE_UNTRIAGED",
    "ORIGIN_BACKFILL",
    "ORIGIN_DIM",
    "ORIGIN_ESCALATION",
    "ORIGIN_FACTORY_SEED",
    "ORIGIN_GITHUB",
    "ORIGIN_IMPORT",
    "ORIGIN_REPORT",
    "TRACKS_EDGE",
    "channel_of",
    "disposition_state",
    "is_escalation_origin",
    "is_promoted",
    "is_report_origin",
    "is_untriaged_intake",
    "origin_from_source_system",
    "origin_of",
]
