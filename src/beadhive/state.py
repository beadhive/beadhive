"""Intake + outbound state vocabulary — the single owner of the cross-hive report
state dimensions (epic).

The lifecycle states are modelled via native `bd set-state <bead> <dim>=<value>`
(event-sourced, with the `<dim>:<value>` label cache) — NOT ad-hoc labels and NOT a
re-implemented state store. This module owns only the *vocabulary*: the closed set of
dimensions/values, plus the queue predicates the triage and
contributor seats resolve against.

States
------
- ``intake:untriaged`` — untriaged inbound; set when a report lands, cleared on triage.
- ``intake:accepted`` / ``rejected`` / ``rerouted`` / ``promoted`` — the terminal value a triage
  disposition transitions the intake dimension to. Each clears untriaged
  (so the bead leaves the triage queue) while recording *which* disposition fielded the report as
  an event-sourced audit trail. ``intake:promoted`` is also the queue key the planner's adopt path
   reads.
- ``outbound:pending`` — a staged outbound candidate (captured with ZERO public exposure).
- ``publish:approved`` — the contributor filed it upstream (behind the human publish gate).
- ``origin:report|github|import`` — the intake CHANNEL a bead entered through. A CLOSED
  provenance dimension (queryable, validates clean), orthogonal to the intake *queue* state:
  ``intake`` is queue membership (cleared on triage); ``origin`` is a durable source tag.

Provenance — THREE orthogonal facets (operator-approved, epic)
--------------------------------------------------------------------------------
1. **System-of-record** = the NATIVE ``source_system`` + ``external_ref`` pair — bd's
   "mirrors an external system of record" coupling, settable only at import. Reserved for
   external mirrors (github / legacy import), NOT overloaded for born-native reports.
2. **Intake channel** = the CLOSED ``origin`` dimension here (``origin_of`` / ``is_*``). A
   cross-hive report is born-native with no ``external_ref``, so its channel rides ``origin``
   (set via ``bd set-state``, like ``intake``) instead of overloading ``source_system``.
3. **Reporter identity** = ``bd --actor`` (unchanged) — never a closed label (``reported-by``
   is open-ended and would fail ``bh label validate``). Do not add a reporter label dimension.

Imported beads (github / legacy import) carry a native ``source_system`` but NO ``origin``
label; ``origin_from_source_system`` derives their channel on READ so the triage queue
 sees a uniform channel WITHOUT double-stamping an origin label.
"""

from __future__ import annotations

# Built-in CLOSED state dimensions: {dimension: {allowed values}}. Owned by ws (not
# per-hive config) so intake/outbound beads validate clean fleet-wide and downstream beads
# (/ r7s7 / uxam.3) share ONE vocabulary instead of each inventing it.
# `registry.closed_dimensions` merges these into the set `bh label validate` reads, so an
# unknown value (e.g. `outbound:bogus`) is rejected.
STATE_DIMENSIONS: dict[str, frozenset[str]] = {
    # untriaged inbound; a triage disposition moves it to a terminal value (below)
    "intake": frozenset({"untriaged", "accepted", "rejected", "rerouted", "promoted"}),
    "outbound": frozenset({"pending"}),  # staged outbound candidate (no public exposure)
    "publish": frozenset({"approved"}),  # contributor filed upstream (behind the human gate)
    # intake CHANNEL — the closed provenance dimension a report/import rides instead of
    # overloading the sync-coupled native `source_system` (see the module docstring).
    # `escalation` is the fire-and-forget HQ channel: an agent that
    # hits a tool problem names the tool, hands it up to HQ, and never blocks.
    # `factory-seed` is the synthetic-identity channel the HQ factory (local/factory/hq) stamps
    # on the beads it seeds; registering it keeps those beads validate-clean fleet-wide so they
    # never trip the intake gate.
    "origin": frozenset({"report", "github", "import", "escalation", "factory-seed"}),
}

# Canonical `<dim>:<value>` label cache entries (what `bd set-state` writes).
INTAKE_UNTRIAGED = "intake:untriaged"
INTAKE_PROMOTED = "intake:promoted" # handed to the planner (adopt queue key)
OUTBOUND_PENDING = "outbound:pending"
PUBLISH_APPROVED = "publish:approved"
ORIGIN_REPORT = "origin:report"
ORIGIN_GITHUB = "origin:github"
ORIGIN_IMPORT = "origin:import"
ORIGIN_ESCALATION = "origin:escalation"
ORIGIN_FACTORY_SEED = "origin:factory-seed"  # HQ factory synthetic-identity seed (akyd)

# Dimension name for the intake channel — the single spelling report.py / triage derive from.
ORIGIN_DIM = "origin"

# Triage disposition -> the terminal `intake` value it transitions to. Setting
# any of these via `bd set-state` clears `untriaged` (leaving the triage queue) while recording the
# outcome as an event-sourced state transition — NOT a silently-yanked label.
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


def is_outbound_candidate(labels) -> bool:
    """True for a staged outbound candidate (`outbound:pending`) not yet filed upstream
    (`publish:approved`). Drives the contributor queue."""
    labels = labels or []
    return OUTBOUND_PENDING in labels and PUBLISH_APPROVED not in labels


def origin_of(labels):
    """The intake channel (`report` | `github` | `import`) stamped on a bead via the
    `origin:<value>` label cache, or ``None`` when no valid origin label is present.

    Reports carry an explicit `origin:report` label (set by `ws report`); imported beads do
    NOT — for those, derive the channel from the native `source_system` via
    ``origin_from_source_system`` (or use ``channel_of`` to resolve both in one call)."""
    for label in labels or []:
        if label.startswith(f"{ORIGIN_DIM}:"):
            value = label.split(":", 1)[1]
            if value in STATE_DIMENSIONS[ORIGIN_DIM]:
                return value
    return None


def is_report_origin(labels) -> bool:
    """True for a bead that entered through the cross-hive `ws report` channel
    (`origin:report`). The triage queue keys on this channel."""
    return ORIGIN_REPORT in (labels or [])


def is_escalation_origin(labels) -> bool:
    """True for a bead that entered through the fire-and-forget `ws escalate` channel
    (`origin:escalation`). Always lands in HQ; the triage queue
     will key on this channel once routing is wired."""
    return ORIGIN_ESCALATION in (labels or [])


def origin_from_source_system(source_system):
    """Derive the intake channel from a bead's NATIVE `source_system` — a READ-side map for
    imported beads (github / legacy import) that carry a `source_system` but no `origin:`
    label. Returns the channel (`github` | `import` | `report`) or ``None`` for an unknown /
    empty value. This does NOT re-stamp an origin label; it maps `source_system` → channel on
    read so the triage queue is uniform WITHOUT double-stamping imports."""
    value = (source_system or "").strip().lower()
    return value if value in STATE_DIMENSIONS[ORIGIN_DIM] else None


def channel_of(labels, source_system=None):
    """Uniform intake channel for the triage queue: the explicit
    `origin:` label if present (reports), else derived from the native `source_system`
    (imported beads). Returns the channel or ``None``."""
    return origin_of(labels) or origin_from_source_system(source_system)
