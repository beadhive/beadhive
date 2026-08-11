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

Dispatcher failure dimensions (bh-e7r9q.2) — closed vocabulary, written on failure only
--------------------------------------------------------------------------------------
The unattended dispatch loop's execution-memory boundary
(``docs/design/loop-ownership-and-execution-memory-adr.md``, Decision 2) draws the line at
**zero** runtime state outside beads: failure cause / bounce history / stall reason live in
beads as closed state-dimension values written with ``bd set-state --reason``; retry /
bounce **counts** are never stored, only DERIVED by counting a bead's own ``issue_type='event'``
children at read time.

- ``escalation:raised`` / ``escalation:resolved`` — whether the loop has an open escalation
  against a bead/molecule (bh-bh2h.2.1's vocabulary, registered here so the write validates
  clean). ``raised`` clears on ``resolved``, same convention as ``intake``.
- ``dispatch:<cause>`` — the CLOSED reason-code vocabulary a dispatcher failure is filed under.
  ONE dimension, so ONE closed set (:data:`DISPATCH_CAUSES`), unioned from two legitimate
  facets that previously lived in two disjoint sets whose intersection was EMPTY (this module's
  and ``localloop.DISPATCH_CAUSES``'):

  * **decision verdicts** (:data:`DISPATCH_DECISION_CAUSES`, unprefixed) —
    ``not_dispatchable`` / ``deadlock`` / ``repeated_changes_requested`` /
    ``repeated_merge_failure`` / ``ambiguous_gate`` / ``stuck`` (bh-bh2h.2.2's closed
    reason-code set — the SAME six values ``work_next.py``'s ``REASONS`` enforces via
    ``Decision.__post_init__``; this module does not import that one, but the string values are
    identical on purpose), plus ``provisioning_failed`` and ``escalated``.
  * **run outcomes** (:data:`DISPATCH_RUN_CAUSES`, ``run_``-prefixed) — ``run_failed`` /
    ``run_blocked`` / ``run_handoff`` / ``run_cancelled`` / ``run_bead_mismatch`` /
    ``run_lease_lost``: how ONE seat process ended, written by ``localloop``'s harvest path.

  Both writers (``localloop.record_cause`` and ``work.record_dispatch_failure``) validate
  against this one set, so every value the loop can write is a value the read path
  (``work.dispatch_cause_count``) can count and ``bh label validate`` accepts.

  **``provisioning_failed`` decision (bh-qczj.2's NOTES field, settled here):** a worktree
  provisioning failure is an infrastructure failure on the dispatch path, not a review outcome,
  so it gets its OWN value in this dimension rather than riding ``review=abandoned --reason
  provisioning_failed`` (bh-qczj.2's current call site). Filing it under ``review`` would let
  ``attempt_count``-style text matching conflate "the reviewer bounced this" with "the disk was
  full" — two causes that must drive different dispatcher decisions. bh-qczj.2's
  ``work.py::_release_claim`` (or wherever its provisioning-failure recovery lands) should call
  ``bd set-state <bead> dispatch=provisioning_failed --reason "…"`` instead, once that bead's
  branch (still un-merged to ``main`` as of this bead) is retargeted — see this bead's report.

Write on failure, not on attempt: event beads are permanent (this hive has no compaction tier —
``bd compact`` / ``bd flatten`` are forbidden until bh-3vs6c lands), so only bounces/stalls/
escalations are recorded, never a per-pass or per-attempt heartbeat.

Read-path cost, measured (not assumed): the loop's read is ``bd list --parent <bead>
--include-infra --json`` (already ``work._flow_events``'s call, reused here) — NOT ``bd
history``, which wraps the noisy ``dolt_history_issues`` semantics (~6.1M rows for a 2321-row
table) and is the read the design record warns off. Measured on this hive, embedded mode,
2026-08-10: **~0.28s per call** (5-run mean over a real bead with 6 event children), paid once
per candidate bead per decision tick — cheap enough to be the loop-breaker's read path.
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
    # `backfill` is the historical-reconstruction channel the backfill skill stamps on beads it
    # reconstructs from a rig's own history (git log, decision docs, …); registering it keeps
    # backfilled beads validate-clean instead of guaranteed violations (bh-vfx9). The companion
    # `source:<kind>` facet stays intentionally OPEN (no registry entry) — closed dimensions are
    # reserved for values code actually branches on, which is true of `origin` but not `source`.
    "origin": frozenset({"report", "github", "import", "escalation", "factory-seed", "backfill"}),
    # the unattended dispatch loop's open/closed escalation flag (bh-bh2h.2.1's vocabulary) —
    # see the module docstring's "Dispatcher failure dimensions" section.
    "escalation": frozenset({"raised", "resolved"}),
    # the dispatcher's ONE closed reason-code set — both facets, one namespace. Defined below
    # (`DISPATCH_CAUSES`) and spliced in after this literal so there is exactly one definition.
    "dispatch": frozenset(),
}

# ---- the `dispatch` dimension: ONE closed set, two facets --------------------------------
#
# There is exactly ONE `dispatch:` dimension, so there can be exactly one closed set of values
# for it. Two disjoint sets were maintained here and in `localloop.DISPATCH_CAUSES` and their
# intersection was EMPTY: the loop wrote `dispatch=blocked` / `dispatch=cancelled` while the
# read path (`work.dispatch_cause_count`) and `bh label validate` only knew
# `not_dispatchable` / `deadlock` / ... — so the loop-breaker could not count a single cause the
# loop writes, and every label the loop emitted would have failed validation.
#
# They are two LEGITIMATE vocabularies answering different questions, so they are unioned with
# distinct shapes rather than mashed into one flat list:
#
#   facet 1 — DECISION VERDICTS: why the dispatcher's decision table stopped, refused, or
#             escalated. Unprefixed, because these are the names `work_next.REASONS` already
#             uses and the ones an operator reads in an escalation.
#   facet 2 — RUN OUTCOMES: how ONE seat run ended. `run_`-prefixed, because otherwise a bare
#             `failed`/`blocked`/`cancelled` reads as a property of the BEAD when it is a
#             property of one process's exit, and because `blocked` in particular would
#             otherwise be indistinguishable from a blocked-bead state.
#
# Spelling is normalised to snake_case across both facets (`run_bead_mismatch`, not
# `bead-mismatch`) — one dimension, one spelling rule.

#: facet 1 — the decision table's escalation reason codes. The six in `work_next.REASONS`, plus
#: `provisioning_failed` (an infra failure on the dispatch path, deliberately NOT filed under
#: `review`; see the module docstring) and `escalated` (the generic marker the loop writes when
#: the table escalates and the specific row reason rides the `--reason` text).
DISPATCH_DECISION_CAUSES: frozenset[str] = frozenset(
    {
        "not_dispatchable",
        "deadlock",
        "repeated_changes_requested",
        "repeated_merge_failure",
        "ambiguous_gate",
        "stuck",
        "provisioning_failed",
        "escalated",
    }
)

#: facet 2 — how one seat run ended, as harvested by `localloop`.
DISPATCH_RUN_CAUSES: frozenset[str] = frozenset(
    {
        "run_failed",  # the run did not complete and produced no usable outcome
        "run_blocked",  # the seat reported blocked — judgment, not failure; do not retry
        "run_handoff",  # the seat handed off (incl. a cooperative cancel)
        "run_cancelled",  # the loop cancelled it (wall-time cap, shutdown, orphan reap)
        "run_bead_mismatch",  # the seat reported a different bead than it was handed
        "run_lease_lost",  # the host lease went away mid-flight
    }
)

#: The ONE closed set `bd set-state <id> dispatch=<value>` may write, `bh label validate`
#: accepts, and `work.dispatch_cause_count` / `localloop.record_cause` both validate against.
DISPATCH_CAUSES: frozenset[str] = DISPATCH_DECISION_CAUSES | DISPATCH_RUN_CAUSES

STATE_DIMENSIONS["dispatch"] = DISPATCH_CAUSES


# Canonical `<dim>:<value>` label cache entries (what `bd set-state` writes).
INTAKE_UNTRIAGED = "intake:untriaged"
INTAKE_PROMOTED = "intake:promoted"  # handed to the planner (adopt queue key)
OUTBOUND_PENDING = "outbound:pending"
PUBLISH_APPROVED = "publish:approved"
ORIGIN_REPORT = "origin:report"
ORIGIN_GITHUB = "origin:github"
ORIGIN_IMPORT = "origin:import"
ORIGIN_ESCALATION = "origin:escalation"
ORIGIN_FACTORY_SEED = "origin:factory-seed"  # HQ factory synthetic-identity seed (akyd)
ORIGIN_BACKFILL = "origin:backfill"  # backfill skill historical reconstruction (bh-vfx9)

# Dimension name for the intake channel — the single spelling report.py / triage derive from.
ORIGIN_DIM = "origin"

# The unattended dispatch loop's escalation flag (bh-bh2h.2.1's vocabulary).
ESCALATION_DIM = "escalation"
ESCALATION_RAISED = "escalation:raised"
ESCALATION_RESOLVED = "escalation:resolved"

# The dispatcher's closed reason-code dimension (bh-e7r9q.2) — see the module docstring's
# "Dispatcher failure dimensions" section for why `provisioning_failed` lives here and not on
# `review`.
DISPATCH_DIM = "dispatch"

# facet 1 — decision verdicts (bare VALUES, for `bd set-state dispatch=<value>`)
CAUSE_NOT_DISPATCHABLE = "not_dispatchable"
CAUSE_DEADLOCK = "deadlock"
CAUSE_REPEATED_CHANGES_REQUESTED = "repeated_changes_requested"
CAUSE_REPEATED_MERGE_FAILURE = "repeated_merge_failure"
CAUSE_AMBIGUOUS_GATE = "ambiguous_gate"
CAUSE_STUCK = "stuck"
CAUSE_PROVISIONING_FAILED = "provisioning_failed"
CAUSE_ESCALATED = "escalated"

# facet 2 — run outcomes (bare VALUES); `localloop` re-exports these under its own CAUSE_* names
CAUSE_RUN_FAILED = "run_failed"
CAUSE_RUN_BLOCKED = "run_blocked"
CAUSE_RUN_HANDOFF = "run_handoff"
CAUSE_RUN_CANCELLED = "run_cancelled"
CAUSE_RUN_BEAD_MISMATCH = "run_bead_mismatch"
CAUSE_RUN_LEASE_LOST = "run_lease_lost"

# `<dim>:<value>` label-cache spellings
DISPATCH_NOT_DISPATCHABLE = "dispatch:not_dispatchable"
DISPATCH_DEADLOCK = "dispatch:deadlock"
DISPATCH_REPEATED_CHANGES_REQUESTED = "dispatch:repeated_changes_requested"
DISPATCH_REPEATED_MERGE_FAILURE = "dispatch:repeated_merge_failure"
DISPATCH_AMBIGUOUS_GATE = "dispatch:ambiguous_gate"
DISPATCH_STUCK = "dispatch:stuck"
DISPATCH_PROVISIONING_FAILED = "dispatch:provisioning_failed"

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


def is_escalation_raised(labels) -> bool:
    """True while the unattended dispatch loop has an open escalation against a bead/molecule
    (`escalation:raised`, bh-bh2h.2.1). Clears on `escalation:resolved`."""
    return ESCALATION_RAISED in (labels or [])


def is_escalation_resolved(labels) -> bool:
    """True once an escalation has been resolved (`escalation:resolved`)."""
    return ESCALATION_RESOLVED in (labels or [])


def dispatch_cause_of(labels):
    """The dispatcher's closed reason code currently stamped on a bead (the `dispatch:<value>`
    label cache), or ``None`` when unset / the value isn't a registered `dispatch` reason. This
    is the fast-lookup CURRENT value only — "how many times" is a derived count over event
    beads (see `beadhive.work`'s dispatch-cause helpers), never read from this label."""
    for label in labels or []:
        if label.startswith(f"{DISPATCH_DIM}:"):
            value = label.split(":", 1)[1]
            if value in STATE_DIMENSIONS[DISPATCH_DIM]:
                return value
    return None
