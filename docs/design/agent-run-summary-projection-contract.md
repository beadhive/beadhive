# AgentRunSummary projection contract — deriving run state from `dispatch_log.py` (bh-6eu2c.1)

> **Status: decided.** This is the projection contract for `AgentRunSummary`: which
> `dispatch_log.py` record types drive which `state`, the two open questions bh-6eu2c's DESIGN
> notes flagged (resolved below, not deferred), and the join-key spellings a correlating
> consumer needs. The typed shape lives in
> [`src/beadhive/agent_run_summary.py`](../../src/beadhive/agent_run_summary.py); this document
> is the rationale a reader (bh-6eu2c.2) and a CLI (bh-6eu2c.3) implement against.
>
> **Scope guard.** This contract does not build the reader. It does not compute freshness (that
> needs a running reader observing file mtimes / colocation with the writer — bh-6eu2c.2's job,
> sketched only as a default below). It does not touch
> `beadhive-ui/packages/operator-contract/src/types.ts`, which is a read-only reference in this
> repo, not something this hive can edit.

## Producer boundary — host-local, never authoritative over bead lifecycle

`AgentRunSummary` is produced entirely from `src/beadhive/dispatch_log.py`'s per-hive JSONL
sink: one aggregate file per hive, written by every dispatcher loop that hive runs, read via
`tail_records`. That sink is exactly the "richer execution record" carve-out
[`work-runtime-tiers-adr.md`](work-runtime-tiers-adr.md) **Decision 1** describes and bounds:

> A runtime MAY keep a richer *execution* record (retry counts, timings, the parent chain, why
> something was retried) but is NEVER authoritative about whether a bead is claimed, blocked,
> approved, or done.

[`loop-ownership-and-execution-memory-adr.md`](loop-ownership-and-execution-memory-adr.md)
**Decision 2** draws the line for exactly this data: the in-flight `{bead_id → (proc, stdin,
pgid)}` map lives **in process only, and does not survive restart on purpose** — it is
execution memory, not lifecycle truth, which already lives in beads (`bd set-state`, gates,
leases, the merge slot).

Two consequences bind every consumer of `AgentRunSummary`, not just this contract's author:

1. **`AgentRunSummary.state` answers "what did one process do", never "what state is the bead
   in".** A `finished` run does not mean the bead is done — `seat_harvested` with
   `outcome=blocked` is a clean, finished run whose bead is still blocked in beads. A consumer
   that wants bead lifecycle reads bead state (bh-jksq's stream), not this projection.
2. **The sink is host-local and not git-synced.** A reader on a host other than the sink's
   writer has no way to positively confirm a "no records" gap means quiet vs. dead — hence
   `freshness.state` (see below) defaults to `unknown` unless the read path can confirm it is
   colocated with the writer.

## The record inventory (verified against `src/beadhive/localloop.py`, not assumed)

Five record types, all emitted through `beadhive.log` into the sink. Verified field lists,
cross-checked line-by-line against the emitting `_LOG.info(...)` call:

| record type | keys (as emitted) | emitted from |
|---|---|---|
| `seat_spawned` | `bead, role, action, pid, pgid, session_id` | `spawn_seat` |
| `seat_harvested` | `bead, outcome, exit_code, session_id` | `LocalLoop._harvest` |
| `seat_cancelled` | `bead, rung, exit_code, priced, session_id, group_gone, signals` | `cancel` |
| `dispatch_cause_recorded` | `bead, cause, reason` | `record_cause` |
| `dispatch_pass` | `pass, dry_run, claimable, gate_resolved, reclaimed, lease, heartbeats, decision, dispatched, routing, harvested, cancelled, denied, declined, orphans_reaped, causes, in_flight, halted, done` | `LocalLoop.run` (`PassReport.as_dict`) |

Two corrections against the epic's DESIGN-section sketch, found by reading the source rather
than trusting the sketch:

- `dispatch_pass` carries **no `session_id` anywhere** — not on the top-level record, not
  nested under `decision`. The "in-flight session" framing in bh-6eu2c's notes does not match
  the actual schema; `dispatch_pass` is bead-keyed (`in_flight`, `denied`, `harvested`,
  `cancelled`, `causes` are all `bead`-keyed tuples/lists), never session-keyed. The `waiting`
  derivation below is written against the real, bead-keyed schema.
- `dispatch_pass.in_flight` — "the beads whose seats are still running at the end of this
  pass" — is not listed in the epic's field inventory but exists in `PassReport` and is load
  bearing for this contract's `starting → active` and `waiting` rules below.

`seat_harvested.outcome` is one of `seatrun.RunOutcome`'s four wire values — `"done"`,
`"blocked"`, `"handoff"`, `"incomplete"` — not the two-value `success`/`failure` the epic's
DESIGN sketch assumed. The mapping below is written against the real four values.

## State enum

`AgentRunSummary.state` reuses `beadhive-ui`'s six values verbatim — `starting`, `active`,
`waiting`, `finished`, `failed`, `unknown` — with **no seventh value added**, because that enum
lives in a read-only reference this hive does not own. Where a dispatch_log record does not fit
cleanly (see `seat_cancelled` below), the decision is to reuse one of the six with an explicit,
documented rationale, not to widen the enum unilaterally.

## Mapping rules, and the two decisions

### `seat_spawned` → `starting`, promoted to `active` by confirmed presence

A `seat_spawned` record opens a run at `starting` for its `(bead, session_id)`. `dispatch_log`
has no separate "now actually running, past process-launch setup" signal, so the only positive
confirmation available is the seat's *bead* still being listed in a **later** `dispatch_pass`
record's `in_flight` — i.e. the pass ran, harvested whatever finished, and this bead is still
not among them. The first such confirmation after the spawn promotes `starting → active`. Absent
that confirmation (no `dispatch_pass` observed yet, or the sink is being read cold), a run stays
`starting` rather than being guessed into `active`.

### `seat_harvested` → terminal state, keyed off `outcome`

| `outcome` | exit shape | `AgentRunSummary.state` | why |
|---|---|---|---|
| `done` | `EXIT_DONE` (0), clean `SeatRun` | `finished` | completed as intended |
| `blocked` | `EXIT_BLOCKED` (10), clean `SeatRun` | `finished` | a controlled, intentional exit reporting a blocker — the **process** finished cleanly; whether the underlying bead is blocked is bead-lifecycle truth this projection does not carry (see Producer boundary above) |
| `handoff` | `EXIT_HANDOFF` (11), clean `SeatRun` | `finished` | also a controlled, intentional exit (e.g. a cooperative cancel's `wip: interrupted` handoff) |
| `incomplete` | no parseable `SeatRun` — killed, crashed, or no envelope | `failed` | the run did not come back attributable; this is the real failure case |
| anything else (defensive) | — | `unknown` | closed set today; do not silently coerce an outcome this contract has not seen |

The decision worth naming explicitly: `done` / `blocked` / `handoff` all share exit codes 0/10/11
— clean, self-terminated exits with a parsed envelope — and all three map to `finished`. Only
`incomplete` (no parseable `SeatRun`) maps to `failed`. `AgentRunSummary` has no field to carry
*why* a finished run ended (blocked vs. done vs. handoff) — a consumer that needs that detail
reads `dispatch_cause_recorded` or `bd` state directly; this projection intentionally only
answers "did the process finish".

### `seat_cancelled` → `failed` (decision, with a named upstream gap — not a deferral)

**Decision:** `seat_cancelled` maps to `failed`. It is the only one of the six enum values a
dispatcher-initiated termination (wall-time cap, priority preemption, any future admission
governor) can honestly land on: it is not `finished` (nothing about the CANCEL ladder is
"completed as intended" — even a `RUNG_COOPERATIVE` cancel that got a clean handoff envelope
back was still cut short by an external decision, not the seat's own completion), and mapping it
to `unknown` would hide a determinate outcome (`bh` chose to kill this run) behind an
indeterminate one.

**The named gap:** `failed` is an overload. A reader distinguishing "the agent errored" from
"the dispatcher pulled the plug" cannot do so from `state` alone today — it has to also read
`rung` / `priced` / `signals` off the raw record, which are execution-record fields, not part of
`AgentRunSummary`. That is consistent with bh-6eu2c's own notes flagging this as *possibly*
needing a value outside `finished`/`failed`. Since the enum is a read-only reference this hive
cannot widen unilaterally, the resolution is: use `failed` now, and **file a follow-up bead
against `beadhive-ui`** proposing a distinct `cancelled` state for `AgentRunSummary` (or an
adjacent field carrying rung/cause) once a real consumer needs to tell the two apart. Do not
silently treat this overload as settled — it is a known, documented lossy mapping, not an
oversight.

### `dispatch_cause_recorded` → does not independently drive `state`

`dispatch_cause_recorded` (`bead, cause, reason`) always co-occurs with a `seat_harvested` or a
cancel-path record explaining *why* — it never appears as the only signal for a transition.
`AgentRunSummary` has no free-text/reason field to hold it, so this contract does not give it a
dedicated mapping case: the state comes from the co-occurring `seat_harvested` /
`seat_cancelled` record, and `dispatch_cause_recorded` is left for a consumer that wants the
human-readable reason to read directly off the sink (e.g. `bh host dispatch logs`), not through
`AgentRunSummary`.

### `dispatch_pass` → `waiting`, derived from absence, not a direct record match (decision)

This was the epic notes' least-certain mapping, correctly — because, as the record inventory
above found, `dispatch_pass` carries no `session_id` to match a live run against at all. The
resolution:

**Decision:** `waiting` is a **bead-keyed** (not session-keyed) `AgentRunSummary` entry, derived
from `dispatch_pass.denied` — the per-pass list of beads an admission check turned away this
pass (host lease not held, per-run/concurrency caps full — i.e. exactly "awaiting a gate or
resource"), filtered to beads **not** also present in the same pass's `in_flight` (so a bead
that already has a running seat is `active`/`starting`, never `waiting`, even if some other
admission check also touched it). A `waiting` entry has `session_id = ""` and `owner_seat =
None` — no seat has been spawned for this attempt yet, so there is nothing to join a session on.
The moment a `seat_spawned` record appears for that bead, the entry transitions out of `waiting`
into `starting` under a real `session_id`, and it is that record — not another `dispatch_pass`
read — that ends the `waiting` state. This is the "absence of activity" derivation the epic
notes anticipated: no spawn activity yet, repeatedly named as denied, is what `waiting` means
here — not a literal field on any single record.

## Join keys — verified against `bh-jksq`'s epic notes, not a landed `bh-jksq.1` contract

`bh-jksq.1` ("Define Beadhive stream v1 contract") is **open**, not landed, as of this writing —
this contract does not depend on it and does not block on it landing (per bh-6eu2c's own
DESCRIPTION: "the dispatch sink must never become a backend for bh-jksq's stream... CORRELATION,
NOT MERGER"). The join-key spelling below is taken from `bh-jksq`'s own epic NOTES (dated
2026-08-11, written by whoever scoped that epic, specifically for this cross-epic correlation),
which state:

> `bead` and `session_id` are the join keys and both already exist in the dispatch records —
> worth making sure v1's records carry a bead id in a compatible spelling.

That note is itself a statement about `dispatch_log.py`'s existing spellings (verified above:
`seat_spawned` / `seat_harvested` / `seat_cancelled` all carry `bead` and `session_id` verbatim)
— i.e. `dispatch_log.py` already uses the spelling `bh-jksq.1` is committing to match. This
contract's `AgentRunSummary.bead` and `AgentRunSummary.session_id` fields carry those same
values unchanged (no renaming, no reformatting) so a consumer correlating both streams can join
on equality without a translation layer.

**Follow-up flag:** this is verified against `bh-jksq`'s epic notes, not against a landed
`bh-jksq.1` contract, because none exists yet. If `bh-jksq.1` lands with different field names
for its bead-id / session-id join keys than the epic notes promise, this contract's join-key
section needs a revisit (and `AgentRunSummary`'s consumers, if any exist by then, need to hear
about it) — this is a known, called-out risk, not something this contract can close on its own.

## Freshness (default only — computed by the reader, bh-6eu2c.2)

`AgentRunSummary.freshness` mirrors `beadhive-ui`'s `Freshness` shape (`state`, `asOf`,
`expiresAt`, `detail`). Per the epic's DESIGN notes and the host-local point above, there is no
remote authority to resync against — only a local file and whatever records are in it — so
`freshness.state` **defaults to `"unknown"`** and only a reader that can positively confirm it
is colocated with the writer (bh-6eu2c.2's read path) is entitled to report `"fresh"` /
`"stale"`. This contract fixes the default; computing the real value is out of scope here.
