# Spike `bh-6wj9y`: outbox, milestone triggers and report dimensions with field ownership

**Bead:** `bh-6wj9y` · **Seat:** `dev/6wj9y` · **Type:** research-only (no product code)
**Feeds decision on:** epic `bh-wjvq9` (external-tracker intake module, prerequisite 3 of the
public GitHub issue to internal work loop `bh-33d5a`), and its implementation beads for the
outbox, the supervised sender, the merge and release milestones and the report dimensions.
**Builds on:** the sibling spikes
[`bh-mj7cl-external-intake-boundary.md`](bh-mj7cl-external-intake-boundary.md) (module
`beadhive.modules.intake`, `IntakePort`, `ReportStore`, the `tracks` link, import refusals) and
[`bh-fptob-conversation-model.md`](bh-fptob-conversation-model.md) (thread document on the
report bead, own-message layers, tombstones, checkpoints), the accepted
[`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md),
[`external-issue-intake.md`](../design/external-issue-intake.md) sections 1, 2, 4, 5 and 7,
[`github-issue-projection.md`](../design/github-issue-projection.md),
[`two-store-scope-adr.md`](../design/two-store-scope-adr.md) and [`TWO-STORES.md`](../TWO-STORES.md).

Names from the siblings are used unchanged. Where this design diverges from them, the
divergence is called out in place and collected in Recommendation R12.

## Question

Can the **outward half** of external intake be designed so that every one of these holds?

1. **Records and vocabulary.** A communication record (target binding, semantic event, source
   revision, public message, owned-label changes, delivery state) with explicit publish intent,
   plus closed report dimensions that can be registered in `state.py` beside
   `STATE_DIMENSIONS`, **without** reusing the contributor's `outbound:` / `publish:`
   vocabulary.
2. **A complete outbox state machine.** Every transition is named with its from-state, event,
   guard, to-state and trigger, including the D2 supervised confirmation after a dry-run
   render, `sent`, the unknown outcome after `WriteOutcomeUnknown`, reconciliation,
   `failed`, `abandoned` and `superseded`.
3. **Merged but unreleased.** A report can stay open after its work merges without making
   completed work dispatchable. The dimensions are triage, derived work progress, delivery and
   notified, and every field has a named owner.
4. **No duplicate posts.** Reconciliation after an ambiguous timeout cannot post a message
   twice, and the argument is testable on the offline, clock-free `MemoryTracker` /
   `MemoryWorld` fake. The own-message marker carries an opaque `outbox_id`, never a `bh-*` id.
5. **Writes and gates.** Every tracker write is named, and each is behind its own
   operator-approved gate that issues a single-use `WriteGrant`.
6. **Triggers and leaks.** Merge and release triggers **draft** records and never send them.
   A leak check enforces the `bh-o2mjz` policy: no `bh-*` id or internal content leaves unless
   it is deliberately published.

The binding operator decisions are those of epic `bh-wjvq9`:

- **D1.** One internal report bead per external issue, `external_ref` = issue URL,
  `origin:github`, `intake:untriaged`, keyed on the URL. No edge crosses stores.
- **D2.** The first outbox slice is supervised-only: an explicit per-record operator
  confirmation after a dry-run render.
- **D3.** Import precedes `bh-elkv`. Wake-on-reply waits for `bh-elkv`'s needs-info state.

The constraints are also binding. Everything is configurable and off by default. Every tracker
write is an operator-approved gate. Nothing writes into the public store except the pull.
Closed issues are in scope. Tests run offline and never depend on timing.

Explicitly **not** asked: the intake module boundary and import (`bh-mj7cl`), the thread and
comment model (`bh-fptob`), the intake state-machine redesign and needs-info (`bh-elkv`),
plugin wiring and config namespace (`bh-mobxo`), webhooks, unattended publication, and revert
detection.

This spike sent no request to GitHub, wrote nothing to any hive bead store and changed no
product code.

## Method

1. **Read the binding inputs.** These were the sibling spikes `bh-mj7cl` and `bh-fptob` in
   full, the tracker port ADR (sections 3, 4 and 6 and the inconsistencies), the prototype
   `docs/spikes/artifacts/bh-gj1hb/tracker_contracts.py` and `memory_tracker.py`, the intake
   design (sections 1, 2, 4, 5 and 7), the projection design, the two-store ADR and
   `TWO-STORES.md`, and the `bh-o2mjz` epic (`bh bd show bh-o2mjz`).
2. **Read the vocabulary and every reader of readiness as code.** This covered:
   - `state.py` (`STATE_DIMENSIONS`, the `outbound` / `publish` entries and the docstring);
   - `registry.closed_dimensions`;
   - `report.py`, `triage.py`, `work_intake.py`, `adopt.py` and `plan_filing.py`;
   - `dispatch_hive_run.kicked_off_ready_epics`, `work_next._ready`, `work_logic._gate_kind`,
     the gate markers in `guard.py`, `beadhive_core.review`, and
     `operator_contract.py` / `operator_work_items.py` (`_NON_BLOCKING_DEPENDENCIES`,
     `_readiness`);
   - `guard.guard_primary`, `guard.live_epoch` and `checkpoint._checkpoint_lock`;
   - the release and merge surfaces (`release.py`, `work.merge`, `work.finish`).
3. **Counted the live label vocabulary.** I ran one read-only `bh bd label list-all --json` on
   this hive and extracted the dimension prefixes in use (E2).
4. **Probed bd's readiness semantics.** This was a throwaway experiment against bd 1.3.0
   (`f45b249ce`) in an **embedded** scratch store: `bd init -p obx` under the session
   scratchpad, no shared server, no hive store touched and no network. I probed:
   - whether a `tracks` edge, an ad-hoc gate and the built-in `blocked` status remove a bead
     from `bd ready`;
   - whether `bd close` refuses a gate-blocked bead, and whether `--force` overrides it;
   - whether resolving the gate puts the bead back in `bd ready`;
   - whether metadata can be written on a closed bead, and whether
     `bd list --all --has-metadata-key` finds it;
   - `bd ready --help`, `bd statuses --help` and `bd gate create --help`.

   The scratch store was discarded (E4, E5).

## Evidence

### Vocabulary and its registration path

- **E1. `state.py` is the single registration point, and `outbound` / `publish` mean
  something else.**
  - `STATE_DIMENSIONS` is a module-level `dict[str, frozenset[str]]` literal
    (`src/beadhive/state.py:100-124`). The `dispatch` entry is spliced in after the literal
    (`:182`).
  - `registry.closed_dimensions` seeds the validator from it (`src/beadhive/registry.py:341-353`).
    A value outside the set fails `bh label validate`.
  - `outbound:pending` is "a staged outbound candidate (captured with ZERO public exposure)".
    `publish:approved` means "the contributor filed it upstream (behind the human publish
    gate)" (`state.py:18-19`, `:103-104`). `is_outbound_candidate` drives the contributor
    queue (`:266-270`).
  - So both describe **one bead being filed upstream as a new external issue**: the
    contributor terminal, explicit enrollment. They are single-valued per bead.
  - A report bead instead carries **many** messages on an **existing** bound issue. Reusing
    either dimension would merge the contributor queue with the report outbox and break
    `is_outbound_candidate`.
  - `bh-mj7cl` §8 moves the intake vocabulary into `modules/intake/domain/vocabulary.py`
    (`INTAKE_DIMENSIONS`), re-exported through the `state.py` facade. New intake dimensions
    are registered there and reach `STATE_DIMENSIONS` through that facade.
- **E2. The names `delivery` and `notified` are free, and `release` is taken.**
  - The live hive carries 461 distinct labels over 35 dimension prefixes: `area`, `batch`,
    `bug`, `complexity`, `component`, `discovered-during`, `dispatch`, `export`, `found-by`,
    `gt`, `harness`, `incident`, `intake`, `kickoff`, `model`, `org`, `origin`, `phase`,
    `platform`, `provider`, `provides`, `release`, `repo`, `review`, `role`, `route`, `scope`,
    `size`, `source`, `tag`, `tool`, `topic`, `upstream`, `wave`.
  - None of them is `delivery`, `notified` or `outbox` (one `bh bd label list-all --json`
    read).
  - `release:` is a code-owned closed dimension (`breaking|feature|fix`,
    `registry.py:335-339`), so release inclusion must not use a `release` dimension.
  - A grep of `src/` finds no `delivery:` or `notified:` label spelling.
- **E3. A `set-state` write is permanent, so dimensions suit rare transitions only.**
  - `bd set-state` "creates event + updates label" (`bd help`). Event beads are permanent,
    and this hive has no compaction tier (`state.py:80-82`, `CLAUDE.md`).
  - A dimension is also single-valued per bead.
  - Per-message delivery states would therefore add a permanent event bead per transition,
    and one bead's several records could not share one dimension. Record states belong in a
    document. Report-level milestones, which happen a few times per report, fit dimensions.

### Readiness: what makes a bead dispatchable

- **E4. Scratch-store readiness probe (bd 1.3.0).** Three beads were created: W (work), R
  (report, `--external-ref https://github.com/o/r/issues/1`) and R2.

  | Step | `bd ready` afterwards | Finding |
  |---|---|---|
  | `bd dep add R W -t tracks` | W, R, R2 | `tracks` leaves the report **ready**; it does not affect readiness |
  | `bd gate create --blocks R` | W, R2 | an open gate removes R from `bd ready` |
  | `bd update R2 -s blocked` | W | the built-in `blocked` status removes R2 |
  | `bd close R` (gate open) | (refused) | `cannot close blocked issue: … blocked by [<gate>] (use --force to override)`; R stays open |
  | `bd close R2` (status `blocked`) | W | closes directly |
  | `bd close W` | (none) | closed work is never ready |
  | `bd gate resolve <gate>` while R is open | **R** | resolving the hold first re-admits R: a window |
  | `bd close R3 --force` with its own gate open, then `bd gate resolve` | unchanged | no window; the gate closes afterwards as bookkeeping |
  | `bd update R3 --metadata …` after close | — | metadata writes succeed on a closed bead |
  | `bd list --all --has-metadata-key external_outbox` | — | finds the closed R3 with its document |
  | `bd list --external-ref <url> --all` | — | finds R by URL |

- **E5. Native readiness has three levers, and a label is not one of them.**
  - `bd ready` "excludes in_progress, blocked, deferred, and hooked issues" and applies
    blocker-aware semantics (`bd ready --help`).
  - `bd statuses --help`: custom statuses come from store config (`status.custom`), with
    categories `active`, `wip`, `done` and `frozen`. Only `active` appears in `bd ready`.
  - `bd gate create --help`: "The blocked issue will not appear in 'bd ready' until the gate
    is resolved."
  - The intake design says the close-on-release policy "needs separate execution and
    delivery readiness; a label alone is insufficient" (`external-issue-intake.md` §4).
- **E6. Today an accepted report is backlog work, and untriaged reports are open.**
  - `triage.accept` clears intake and "the bead stays open as normal backlog"
    (`src/beadhive/triage.py:138-159`).
  - `list_intake` keys on `intake:untriaged` **and** `status="open"` (`triage.py:65-77`).
  - So every report, untriaged or accepted, is open with no blocker, and is listed by
    `bd ready` and `bh work ready`.
  - The unattended dispatcher picks only kicked-off epics
    (`dispatch_hive_run.kicked_off_ready_epics`, `src/beadhive/dispatch_hive_run.py:80-100`).
    `work_next._ready` (`work_next.py:564-576`) picks molecule children and defers to
    `bd ready` for blockers outside the molecule.
  - A standalone report is therefore not auto-dispatched today. It is still advertised as
    ready work to every seat that reads `bd ready`, and nothing stops a claim.
  - For an imported GitHub issue that means attacker-controllable public text, advertised as
    ready work before triage.
- **E7. Gate markers are the existing way to type a hold.**
  - `guard.RELEASE_HOLD_GATE_MARKER = "release-hold:"` is a human gate that holds a
    `release:breaking` bead, matched on `reason: release-hold:` in the description
    (`src/beadhive/guard.py:59-75`).
  - `work_logic._gate_kind` classifies `review | security | release-hold | kickoff | other`
    (`src/beadhive/work_logic.py:1150-1166`).
  - Seat policy lives in `beadhive_core.review`: only a `releaser/` seat may resolve a
    release-hold (`packages/beadhive-core/src/beadhive_core/review.py:77`, `:235`, `:327-352`).
  - "The generic open-gate check already refuses a merge while ANY gate naming the bead is
    open" (`guard.py:50-53`).
- **E8. Operator views count `tracks` as a blocker until the work closes.** This is the
  finding from `bh-mj7cl` E6, traced one step further.
  - `_NON_BLOCKING_DEPENDENCIES = {"parent-child", "related", "discovered-from"}` sits at
    `operator_contract.py:44` and `operator_work_items.py:43`.
  - `_queue_indexes` counts any other edge whose prerequisite is not closed
    (`operator_work_items.py:223-249`).
  - `_readiness` returns `ready` for an open bead with no blocker and no open gate
    (`:252-266`).
  - So a report that tracks W shows as **blocked** while W is open. It flips to **ready** at
    the moment W closes, which is exactly the merged-but-unreleased moment, even though
    `bd ready` already listed it as ready all along (E4).
  - bd's own readiness is `AffectsReadyWork` for `blocks`, `parent-child`,
    `conditional-blocks` and `waits-for` only (`bh-mj7cl` E5).
  - The views and bd disagree in **both** directions: views count `tracks`, and they ignore
    `parent-child`.
- **E9. The adopt path conflicts with D1's URL key and with a held report.**
  - `adopt.py:12-20` links an adopted report as `parent-child` of the epic.
  - `adopt.provenance_of` / `plan_filing.py:174-194` copy the report's `external_ref` onto the
    filed epic.
  - The bd index on `external_ref` is not unique (`bh-mj7cl` E4). After adopt, the
    `ReportStore.find_by_external_ref(url)` of `bh-mj7cl` §4 returns **two** beads: the report
    and the epic. Rule R8 then refuses every later import and discovery of that issue with
    `DUPLICATE_BINDING`.
  - A report that is a `parent-child` molecule step is also something `work_next._ready` will
    try to dispatch.

### The write side of the port

- **E10. The port has exactly two writes, each needing a grant.**
  - `Operation` has two write values, `comment.create` and `labels.change`
    (`artifacts/bh-gj1hb/tracker_contracts.py:258-266`).
  - `CommentWriter.post_comment(issue, body, *, marker, grant) -> WriteReceipt` and
    `LabelWriter.change_labels(issue, *, add, remove, ownership, grant) -> WriteReceipt` are
    the only write operations (`:429-445`).
  - `WriteGrant(grant_id, operation, target)` is "one operator-approved write. Issued by the
    gate (the intake outbox), checked here" (`:305-311`).
  - `WriteReceipt` returns `comment` (with its `CommentKey`) or `labels_after` (`:324-330`).
  - `LabelOwnership(prefixes)` is checked before any request (`:314-321`).
  - The port has no edit, delete, close, reopen, title, body or assignee operation.
  - Writes are refused before any request when the operation is not enabled, the grant
    names another operation or issue, a grant is reused, or a label is not owned (ADR §3,
    "Capability flags"; ADR §6).
- **E11. Error classes split "nothing was written" from "unknown".**
  - "A write answered `5xx` or cut off in transit raises `WriteOutcomeUnknown` and is never
    retried: the comment may exist exactly once and is found again by its decoded marker,
    which is the outbox's reconcile path."
  - Writes refused by a rate limit are "proof that nothing was written" (ADR §3, "Rate-limit
    semantics").
  - `PayloadRejected` means "authorised, content refused" (ADR §3, "Errors").
  - Per `bh-mqvdq`'s `422` row, it is raised only after the server refused the content.
- **E12. The fake already scripts an ambiguous write, and needs one more variant.**
  - `MemoryTracker.post_comment` checks the grant, counts a write, **stores** the comment and
    then raises `WriteOutcomeUnknown` when `fail_next_write_ambiguously()` was armed
    (`artifacts/bh-gj1hb/memory_tracker.py:328-341`). That is "applied, but the caller cannot
    know".
  - Grant reuse is tracked in an in-process set, `_used_grants` (`:195`). It does not
    survive a restart.
  - `MemoryWorld` has no wall clock: timestamps are explicit `at=` values,
    `set_write_time(at)` sets write time, and `sleep` is recorded (`:84-160`). It exposes
    `write_requests()` and `comment_count(issue)`.
  - There is no "timed out **before** being applied" variant.
- **E13. One writer per hive exists, and it is clock-free.**
  - `guard.guard_primary` (`src/beadhive/guard.py:479`) refuses a write verb unless this host
    holds the hive's primary lease. Since `bh-12hev`, "`held_by` names the holder and
    `expires_at` is only a failover hint" (`:537-540`).
  - `guard.live_epoch` (`:583`) is the writer generation in force. A record minted under an
    older epoch is stale (`guard_claim_epoch`, `:655`).
  - `checkpoint._checkpoint_lock` (`src/beadhive/checkpoint.py:64-86`) is an `fcntl.flock`
    precedent for a per-key, process-exclusive lock.
  - `bh-fptob` R4 requires the same one-writer discipline on metadata documents, because bd
    has no compare-and-swap.
- **E14. There is a merge seam, and no single release-publish seam.**
  - `work.merge` / `work.finish` merge `--no-ff` and close the bead
    (`src/beadhive/work.py:1366`, `:1434`).
  - `release.py` exposes `preflight`, `attest`, `await_cmd`, `pending`, `recover` and
    `preview`, but no one "release published" function. Its ancestry precedent is
    `_remote_refs_containing(main, remote, bump_sha)` (`release.py:832`), which
    `recovery_decision` (`:886`) uses.
  - The intake design requires that "a version bump or release event triggers verification
    against the release commit/artifact; it is not automatic proof of inclusion" (§4).
- **E15. The sanitizer is a seam, not yet a component.**
  - `bh-o2mjz` is an open spike epic (P2). Its policy is "egress is an explicit per-hive
    opt-in, and EVERY outbound payload passes a local secret sanitizer first".
  - Existing mechanisms are the ADR's `scrub(text, secrets)` and `github_projection._scrub`
    (`src/beadhive/github_projection.py:149`).
  - `bh-fptob` R4 already requires that the sender render only an explicit communication
    record, passed through this sanitizer.
- **E16. The siblings' contracts this design must fit.**
  - `bh-mj7cl` §2: further operations, "for example `draft_milestone`", are added to
    `IntakePort`, not beside it. `reports_for_work(work)` serves the merge and release hooks,
    wrapped so that "an intake failure is reported and never fails the merge".
  - `bh-mj7cl` §6: import, link and discover hold no `WriteGrant`.
  - `bh-fptob` R4: one module-owned top-level metadata key per document, top-level keys merge
    (`bh-fptob` E11), and the outbox linkage is internal.
  - `bh-fptob` R7: row 1 is `receipt_ids()`; row 2 is `pending(outbox_id)` plus a self
    marker and a known actor, binding the remote id to the pending record. Its T14 is the
    ambiguous-write case.
  - `bh-fptob` E13: rewriting a large document costs roughly its changed chunks after the
    first write.
  - Two names already disagree between the siblings. The source snapshot key is
    `bh.intake.source` (`bh-mj7cl` §4), and the thread document key is `external_intake`
    (`bh-fptob` R4).
- **E17. The requirements the intake design places on communication.** From §5: a
  communication record has a "target binding, semantic event, source revision, public message,
  permitted label/state changes, and delivery state". Also:
  - "Use a stable identity for each binding, semantic event, and source revision; persist
    remote comment IDs and delivery receipts."
  - "After an ambiguous timeout, reconcile a message marker or receipt before posting again.
    Do not promise exactly-once remote writes where the API lacks that guarantee."
  - "Only one publisher owns a pending delivery at a time, stale queued updates are
    superseded when appropriate, and partial label/state/comment updates remain pending until
    reconciled."
  - "Conflicting human label/state edits need field ownership and reconciliation policy."

  §4 sets out the close-on-merge and close-on-release policies, and says that PR closing
  keywords "must not close the public report prematurely".

## Verdict: **GO**

The outward half fits the accepted port and both sibling designs with one structural choice
and a handful of additive changes.

The structural choice is this: **a bound report bead is never work.** It is held out of
`bd ready` for its whole life by its own typed `intake-hold:` gate, and the work it describes
is a separate bead that the report `tracks`. With that choice:

- **Merged but unreleased.** Completed work closes at merge as it does today, so it is never
  dispatchable. The report stays open, carrying `delivery:merged`. The probe shows that the
  hold gate keeps it out of `bd ready` while the `tracks` edge alone would not (E4). The
  procedure that closes the report has no window in which the report is ready (E4, last
  rows).
- **Vocabulary.** Report-level milestones are two new closed dimensions, `delivery` and
  `notified`, registered beside `intake` (E1 to E3). Per-message states live in one
  module-owned metadata document, `external_outbox`, on the report bead. That is the
  `bh-fptob` storage decision applied to the outbound side.
- **No duplicate posts.** A post is reachable only from `approved`. `approved` is reachable
  only through a fresh operator grant. After an ambiguous outcome, the record can return to a
  postable state only through **confirmed absence**: complete snapshots taken after the
  attempt ended, none carrying the record's marker. Under one stated visibility assumption,
  this rules out a second post, and every step runs on `MemoryTracker` with one small kit
  extension (E12).
- **Writes and gates.** The port has exactly two writes (E10). Each operation in a record has
  its own approval and its own single-use grant.

Nothing here writes to the public store. Nothing sends unattended. No `bh-*` id can enter a
marker or a rendered payload without an explicit, recorded override, and secrets cannot enter
one at all.

The residual risks are named in R13, each with a mitigation, and none is a blocker:

- the visibility assumption behind reconciliation;
- a short creation window before the hold gate exists;
- PR closing keywords under close-on-release;
- operator-view lanes for held reports.

## Recommendation

R1 to R3 give the vocabulary, the dimensions and the merged-but-unreleased mechanism. R4 to R7
cover the outbox: records, state machine, sender contract and the no-duplicate argument. R8
covers writes and gates, R9 the milestone triggers and R10 the leak check. R11 lists the port
additions, R12 the divergences from the siblings, R13 the risks, R14 the test plan, R15 the
configuration and R16 the follow-up work.

### R1. Vocabulary to register

**Closed state dimensions.** They live in `modules/intake/domain/vocabulary.py`
(`INTAKE_DIMENSIONS`) and reach `state.STATE_DIMENSIONS` through the `bh-mj7cl` facade. If the
module bead has not landed yet, they go directly into the `state.py` literal with the same
names; the facade move is then a no-op.

```python
DELIVERY_DIM = "delivery"
NOTIFIED_DIM = "notified"
INTAKE_DIMENSIONS[DELIVERY_DIM] = frozenset({"pending", "merged", "released", "reverted"})
INTAKE_DIMENSIONS[NOTIFIED_DIM] = frozenset({"replied", "merged", "released", "corrected"})

DELIVERY_PENDING = "delivery:pending"     # tracked work exists and has not merged
DELIVERY_MERGED = "delivery:merged"       # merged on canonical main; not verified in a release
DELIVERY_RELEASED = "delivery:released"   # verified contained in a published release
DELIVERY_REVERTED = "delivery:reverted"   # the merge was reverted (operator-recorded in v1)
NOTIFIED_REPLIED = "notified:replied"     # last delivered message was a triage reply
NOTIFIED_MERGED = "notified:merged"
NOTIFIED_RELEASED = "notified:released"
NOTIFIED_CORRECTED = "notified:corrected"

def delivery_of(labels) -> str | None: ...        # same shape as state.origin_of
def notified_of(labels) -> str | None: ...
def notification_behind(labels) -> bool: ...      # delivery milestone not yet notified
```

- **Why these names.** They are free in code and in the live vocabulary (E2). They are not
  `outbound` or `publish` (E1), and they are not `release`, which is taken (E2).
- **Why only these two.** Each value is a report-level milestone written a few times per
  report, so a permanent event bead per transition is acceptable (E3). Both are written by
  `bd set-state <report> <dim>=<value> --reason …` through `ReportStore.set_state`.
- **Deliberately not dimensions:**
  - **Per-operation outbox states.** A report has many records, a dimension is single-valued,
    and every transition would add a permanent event bead (E3). They live in
    `external_outbox`.
  - **Work progress.** It is **derived on read** from the tracked work bead and never stored on
    the report (R2).
  - **Triage.** It is the existing `intake` dimension, extended only by `bh-elkv`.

**Gate marker.** `INTAKE_HOLD_GATE_MARKER = "intake-hold:"` goes in `guard.py` beside
`RELEASE_HOLD_GATE_MARKER`, with `guard.is_intake_hold_gate(gate)`. `work_logic._gate_kind`
returns `"intake-hold"` for it. The seat policy in `beadhive_core.review` refuses to let
`bh work approve`, or any seat, resolve it. Only `ReportStore.close` resolves it (R3).

**Metadata key.** `external_outbox` is one module-owned, schema-versioned top-level key on the
report bead, holding the delivery facts and the outbox records (R4).

It is a separate key from `bh-fptob`'s `external_intake` thread document for two reasons:

- Outbox transitions are frequent, and a top-level key is rewritten wholesale. Sharing one key
  would rewrite a possibly multi-megabyte thread on every transition (`bh-fptob` E11, E13).
- The two documents have different writers (the sender and the ingest pass), so they need
  different sequence counters.

Both keys sit under the same per-binding lease (R6).

**Domain records** go in `modules/intake/domain/outbox.py`, using stdlib dataclasses and
`StrEnum`:

| Record | Purpose |
|---|---|
| `SemanticEvent` | `reply`, `merged`, `released`, `corrected` |
| `DraftKey` | `(binding_url, event, source_revision)`; the stable identity of R4 |
| `PublicFacts` | the **only** values a template may interpolate (R10) |
| `OutboxRecord` | one communication intent: `outbox_id`, `draft_key`, `binding`, `event`, `source_revision`, `group`, `facts`, `message`, `operations`, `created_seq`, `supersedes` |
| `OutboxOperation` | one write: `kind` (`comment.create` or `labels.change`), `payload`, `state` (`OpState`), `findings`, `approval`, `attempts`, `receipt`, `absent_confirmations`, `history` |
| `OpState` | `drafted`, `held`, `approved`, `sending`, `sent`, `unknown`, `failed`, `superseded`, `abandoned` |
| `LabelDiff` | `add`, `remove` (owned labels only), and `base_revision`, the snapshot revision the diff was computed against |
| `RenderedOperation` | the exact target URL, operation, bytes (body plus encoded marker, or the label diff) and `digest` |
| `LeakFinding` | `kind`, a redacted excerpt, `overridable` |
| `Approval` | `actor`, `digest`, `grant_id`, `overrides` (finding ids deliberately published), `seq` |
| `Attempt` | `n`, `grant_id`, `epoch`, `holder`, `start_seq`, `end_seq`, `outcome` |
| `Receipt` | `comment` (`CommentKey`) or `labels_after`, plus `reconciled: bool` |
| `DeliveryFacts` | `merged {work, commit}` and `released {tag, commit, verified}`; internal only |
| `MergeFact`, `ReleaseFact` | the inputs of the milestone triggers (R9) |

### R2. Report dimensions and field ownership

A report has four dimensions. Each has **one** writer.

| Dimension | Stored as | Values | Sole writer | Read by |
|---|---|---|---|---|
| **Triage** | `intake` (existing) | `untriaged`, `accepted`, `rejected`, `rerouted`, `promoted` (plus `bh-elkv`'s later values) | `IntakeService.dispose`, operator-invoked; import stamps `untriaged` once | the queue, the planner's adopt path |
| **Work progress** | **derived**, never stored | `unlinked`, `open`, `in_progress`, `in_review`, `closed` | nobody; computed by `IntakeService.work_progress(report)` from the tracked bead's status and `review:` label | status views, templates (as a public fact only when chosen) |
| **Delivery** | `delivery` (new) | `pending`, `merged`, `released`, `reverted` | the intake module only: link (`pending`), the merge trigger (`merged`), the release trigger (`released`), the operator correction verb (`reverted`) | the release trigger, close policy, `notification_behind` |
| **Notified** | `notified` (new) | `replied`, `merged`, `released`, `corrected` | the outbox sender only, after a comment operation reaches `sent` | `notification_behind`, the report close rule |

The delivery transitions:

| From | Event | Guard | To | Trigger |
|---|---|---|---|---|
| (unset) | `link` / `accept --work` / `accept --new-work` | report bound; work in this operational store (`bh-mj7cl` §5 link rules) | `pending` | operator through `bh work link` / `bh work accept` |
| `pending` | `unlink` | none | cleared (see the note below) | operator through `bh work link --remove` |
| `pending` or `reverted` | `on_work_merged(W)` | report tracks W; W merged on canonical main | `merged` | the merge trigger (R9) |
| `merged` | `on_release(tag)` | `ReleaseVerifier.contains(tag_commit, merge_commit)` is `YES` | `released` | the release trigger (R9) |
| `merged` or `released` | `revert` | operator names the reverting commit | `reverted` | operator through `bh work delivery revert` (detection is out of scope) |

Whether `bd set-state` can clear a dimension value on unlink is an open item for the first
dimension bead (R16). If it cannot, the safe fallback keeps `pending` and records `unlinked`
in `external_outbox.facts`, and `work_progress` then reports `unlinked`.

**Field ownership, internal store.** "Writer" means the only layer that may change the field.

| Field (report bead R unless stated) | Writer | Never written by |
|---|---|---|
| `title`, `description`, `issue_type` at birth | import (`ReportStore.create`), once | triage, the outbox, triggers. Re-import updates only the snapshot (`bh-mj7cl` §4) |
| `issue_type`, `priority` after birth | triage (`dispose(accept)`) | everyone else |
| `external_ref` | import, at creation only | everyone; never copied onto other beads (R12, D-c) |
| `origin` | import | everyone else |
| `intake` | import (`untriaged`), then triage dispositions | the outbox, triggers |
| `delivery` | the intake module's link, trigger and correction operations (table above) | anyone by hand through `bd set-state`. `bh label validate` cannot prevent that, so `bh doctor` reports a `delivery` value with no matching `external_outbox.facts` |
| `notified` | the outbox sender | everyone else |
| `bh.intake.source` snapshot | import and discovery | everyone else |
| `external_intake` thread document | the thread ingest pass (`bh-fptob`) | the outbox; it reads the thread only through the R7 view |
| `external_outbox` document | the outbox service: drafting through `IntakePort`, transitions by the approve, send and reconcile operations | everyone else |
| the `intake-hold:` gate | `ReportStore.create` opens it; `ReportStore.close` resolves it | every seat (seat policy, R1) |
| the `tracks` edge | `IntakeService.link` / `unlink` | everyone else |
| R's status (open or closed) | `IntakeService` only (dispositions and the R3 close rule) | the work layer: `bh work merge` never closes a report, because a report is never work (R3) |
| the work bead W (every field) | the work layer | the intake module, which only reads W |

**Field ownership, tracker side.** These fields are written only through the port, so only the
two operations of E10 are possible at all.

| Tracker field | Owner | Beadhive may |
|---|---|---|
| issue title, body | the reporter and the repository's maintainers | never write; there is no port operation |
| issue state (open or closed) | humans and GitHub's own automation | never write in v1; there is no port operation. A future `issue.close` is its own operation behind its own gate |
| human comments | their authors | never write; there is no edit or delete operation |
| Beadhive's marked comments | the outbox | append only (`comment.create`) |
| labels under `write.labels.owned_prefixes` | the outbox | add or remove through `labels.change`, inside `LabelOwnership` |
| every other label | humans | never touch; `LabelNotOwned` is raised before any request |
| assignees, milestones, projects, reactions | humans | never write; there is no port operation |

**Conflict policy for owned labels.** Humans win until the next milestone.

1. A `LabelDiff` is computed at draft time against the snapshot labels and stamped with
   `base_revision`.
2. At approval and again at send, the sender compares the latest `SourceSnapshot.revision`.
   If it moved **and** an owned label changed since `base_revision`, the operation drops back
   to `drafted`. It is re-rendered against the new labels, and the operator re-approves the
   new diff (transition O7).
3. Beadhive never re-asserts a label that a human removed, except as part of a newly approved
   diff.

### R3. Merged but unreleased: the report stays open, completed work is never dispatchable

**Invariant H.** A bound report bead (`origin:github`, with an `external_ref` binding) is never
in `bd ready`. Completed work is closed. These two facts together keep completed work out of
dispatch.

The mechanism has five parts.

1. **A bound report is never work.** This is a divergence (R12, D-a).
   - One-to-one is represented as the report **tracking** a work bead that triage creates:
     `bh work accept <report> --new-work [--type T --priority P]`. The new bead is born
     internal, with no `external_ref` and no `origin:github`. Its description is written by
     triage, not copied from the issue body.
   - Many-to-one is `bh work link` (`bh-mj7cl` §5), or `accept --work <id>`.
   - Two reasons drive this:
     - Untrusted public text never becomes dispatchable work directly (E6).
     - Delivery tracking must outlive the closure of the work.
2. **The hold gate.** `ReportStore.create` opens one human gate per report:
   `bd gate create --blocks <report> --reason "intake-hold: external report, never work"`.
   - It opens immediately after `bd create` and before the `origin` and `intake` stamps.
   - It uses `blocks`, so it is native readiness (E4, E5), not a label.
   - It is visible in `bd show`, and typed by its marker (E7).
   - The `bh-mj7cl` "missing stamp" repair also repairs a missing hold.
   - `bh work claim` additionally refuses a bead with an open `intake-hold:` gate, as defence
     in depth.
3. **Work closes at merge as it does today.** `bh work merge` / `finish` close W. Nothing
   reopens it or keeps it open for delivery tracking, so completed work is closed and never
   ready (E4, `bd close W`).
4. **Delivery lives on the report.**
   - The merge trigger sets `delivery:merged` on every report that tracks W, open or closed
     (R9).
   - Under **close-on-release**, the report stays **open**, so the reporter's issue is still
     tracked in the internal store and listed as an open report. The gate keeps it out of
     `bd ready`, and the `tracks` edge is irrelevant to readiness.
   - Under **close-on-merge**, the report closes by the rule below once the merged message is
     notified.
5. **The close rule.** `ReportStore.close(report, reason)` is the **only** way a report
   closes. Every closing disposition uses it: reject, reroute, `bh-elkv`'s dedupe, and
   delivery completion. In order:
   1. Read the report's open blockers. Refuse unless every one is one of its own
      `intake-hold:` gates.
   2. Run `bd close <report> --force --reason …`, through a new named route
      `issue_close_held(report, reason, actor)`. `--force` overrides only the live-blocker
      refusal (E4).
   3. Run `bd gate resolve <hold>` as bookkeeping.

   Closing first means there is **no instant** at which the report is open and unblocked
   (E4: resolving first re-admits it). A crash between steps 2 and 3 leaves a closed report
   with an open hold. That is harmless, because a closed bead is never ready. A `bh doctor`
   sweep, idempotently, resolves hold gates whose blocked report is closed.

**When delivery completes**, depending on the hive's completion policy:

| Policy | At merge | At verified release | Report closes when |
|---|---|---|---|
| `close-on-merge` (default) | `delivery:merged`; a `merged` record is drafted | optional append (`milestones.append_release_to_closed`, off): `delivery:released` and a `released` record drafted on the closed report | `delivery:merged` **and** the `merged` record's comment is `sent`, `abandoned` or `superseded`. With the outbox off, at merge |
| `close-on-release` | `delivery:merged`; a `merged` record says "on main, not yet released"; the report stays open and held | `delivery:released`; a `released` record is drafted | `delivery:released` **and** the `released` record's comment is `sent`, `abandoned` or `superseded`. With the outbox off, at release |

Tying closure to `notified` makes "the report is done" mean "the reporter has been told, or
the operator decided not to tell them". Because sending is supervised (D2), a report can wait
open for days with no cost to dispatch, since it is held.

Duplicates closed early keep their `tracks` edge (`bh-mj7cl` §5). They still receive milestone
drafts on the closed bead. That matches intake design §1: "even if a duplicate report has
already been closed".

**Operator views (the `bh-mj7cl` finding, E8).**

- **Replace the allow-list with bd's blocking set.** Replace `_NON_BLOCKING_DEPENDENCIES` in
  `operator_contract.py:44` and `operator_work_items.py:43` with
  `_BLOCKING_DEPENDENCIES = {"blocks", "conditional-blocks", "waits-for"}`. This is bd's
  `AffectsReadyWork` set without `parent-child`, which bh already treats as non-blocking.
  - With it, `tracks`, `relates-to`, `supersedes`, `caused-by`, `validates`, `until` and
    `duplicates` stop counting as blockers.
  - A report then reads blocked only for the right reason, its open hold gate. It no longer
    flips to ready when W closes.
  - Adding `"tracks"` to the existing allow-list is the minimal alternative. It leaves every
    future edge type miscounted.
- **Classify held reports in their own lane.** In `_readiness`, a bead whose **only** open
  gates are `intake-hold:` gates is classified as `report` (reason `external report; tracked
  work STATE, delivery VALUE`), not `blocked`. Until that lane lands, held reports show as
  "blocked: 1 open gate". That is accurate but noisy (R13).

**Alternatives rejected:**

| Mechanism | Why not |
|---|---|
| Keep the work bead (or a one-to-one report-as-work) open until release | Completed work stays open and unblocked, so it is in `bd ready`: the exact hazard |
| A `delivery:merged` label plus a filter in bh readers | "A label alone is insufficient" (E5). `bd ready` and every other reader would still list it |
| The `tracks` edge as the hold | Not a readiness edge (E4, row 1) |
| The built-in `blocked` status | Works in bd (E4), but a hand `bd update -s open` silently undoes it, and the views show "status is blocked" with no stated cause |
| A custom `wip` status (`status.custom`) | Needs store config on every internal store. `list_intake` filters `status="open"` (`triage.py:69`), and the views' status set is fixed (`operator_contract.py:41`), so every reader changes |
| `bd defer` | Time-based, and auto-wakes to `open` |
| One shared, never-resolved hold gate for all reports | Saves a bead per report, but resolving it re-admits every report at once, and per-bead gate listings cannot name the report |
| Close the report at link time, delivery on the closed bead only | No dispatch hazard, but every open external report would be indistinguishable from finished ones, and intake design §4 asks for "an open report or delivery-tracking bead" |

**Cost.** One permanent gate bead per bound report, plus one work bead per one-to-one
acceptance. That is bounded per report, not per comment or message (contrast `bh-fptob` E14).

### R4. The communication record and its document

`external_outbox` on the report bead:

```text
external_outbox = {
  schema: 1,
  seq: <int>,                         # local monotone counter; every applied change bumps it once
  facts: DeliveryFacts,               # merged {work, commit}, released {tag, commit, verified}
  records: { <outbox_id>: OutboxRecord, ... },
}
OutboxRecord = {
  outbox_id,                          # opaque, minted by IdSource; never a bead id (R10)
  draft_key: {binding_url, event, source_revision},
  binding: IssueBinding,              # bh-mj7cl's record; the target
  event, group,                       # group: "milestone" or "reply:<n>" (supersession scope)
  facts: PublicFacts,                 # explicit publish intent: the only renderable values
  message: {template, text},          # text without marker; operator-authored for replies
  operations: [OutboxOperation, ...], # at most one comment.create and one labels.change
  created_seq, supersedes?,
}
```

**Identity and idempotence.** `DraftKey` is the stable identity that intake design §5 asks
for: binding, semantic event and source revision.

| Event | `source_revision` |
|---|---|
| `merged` | the merge commit on canonical main |
| `released` | the tag plus the release commit |
| `corrected` | the reverting commit |
| `reply` | a digest of the composed text plus the thread `seq` it answers |

Drafting with a key that already exists is a no-op that returns the existing record. A
re-run merge hook, a retried release verb or a replayed composer therefore never drafts twice.
The `outbox_id` is minted once, at first draft.

**Explicit publish intent.** A record renders only `PublicFacts` and `message.text`.

- `PublicFacts` holds the version and tag, and the public commit or PR URL only when
  `outbox.public_evidence.commit_links` is on (the operator declares the code origin public).
  It also holds the derived work progress word when a template asks for it.
- A template has **no** read path to the report bead, the work bead, notes or the thread
  document.
- Being bound does not authorise publishing any bead field (intake design §5).

**Record-level state is derived from its operations:**

- `open` while any operation is non-terminal;
- `delivered` when every operation is `sent`;
- `partial` when some are `sent` and some are not. This is intake design §5's "partial
  label/state/comment updates remain pending until reconciled".

### R5. The outbox state machine, every transition

The state machine is **per operation**, because each write is its own gate (R8).

- Terminal states: `sent`, `superseded`, `abandoned`.
- Non-terminal states: `drafted`, `held`, `approved`, `sending`, `unknown`, `failed`.

Every row is one durable write of `external_outbox`, guarded by the R6 lock and `seq`
re-check. Each write appends `(seq, from, to, event, actor)` to `history`.

| # | From | Event | Guard | To | Trigger |
|---|---|---|---|---|---|
| O1 | (none) | `draft` | outbox enabled for the hive; report bound; `DraftKey` absent (else no-op, O2); for labels, every label in `owned_prefixes` | `drafted` | the merge or release trigger (R9), or the operator through `bh work outbox draft` (a reply) |
| O2 | any | `draft` with an existing `DraftKey` | none | unchanged (no write) | same as O1 |
| O3 | `drafted` | `check` finds a leak, a sanitizer finding, or a thread `missing` (`bh-fptob` R6) | the check runs at draft and again at every render | `held` | the outbox service (automatic) |
| O4 | `held` | `edit` (new reply text or template facts), or `override <finding>` | the operator seat; a `secret` finding is never overridable | `drafted` (re-checked by O3) | the operator through `bh work outbox edit` / `approve --publish` |
| O5 | `drafted` | `approve(op, digest)` | operator seat, not an agent seat (`dev/`, `disp/`); `digest` equals a fresh render's digest; no unresolved finding; the operation `usable` in `probe_capabilities` (enabled, not `NO`); **`comments.read` usable**, so that an unknown outcome can be reconciled; the target equals the binding | `approved`; mints a single-use `WriteGrant(grant_id, operation, target)` recorded in `Approval` | the operator through `bh work outbox approve <id> --op comment:<digest>` (D2) |
| O6 | `drafted`, `held`, `approved` | `supersede` | a newer record in the same `group` of the same report reaches O1; the state is before `sending` | `superseded`; any grant is voided | the trigger that drafts the newer record (automatic) |
| O7 | `approved` | `invalidate` | the fresh render digest differs from the approved one (edited text, moved label base), or the binding no longer matches | `drafted`; grant voided | the sender's pre-send check, or the operator's edit |
| O8 | `drafted`, `held`, `approved`, `failed` | `discard(reason)` | operator seat | `abandoned`; grant voided | the operator through `bh work outbox discard` |
| O9 | `approved` | `send` | `guard_primary` passes; the per-binding send lock is held; `seq` re-read is unchanged; the re-render digest equals the approved digest; the send-time leak check on the exact bytes is clean; the grant is unused | `sending`, with `Attempt{n, grant_id, epoch, holder, start_seq}` written **before** the request | the operator through `bh work outbox send` (or `approve --send`); never a timer (D2) |
| O10 | `sending` | `WriteReceipt` returned | none | `sent`, with `Receipt` (the comment's `CommentKey` or `labels_after`) | the sender |
| O11 | `sending` | a refusal that proves nothing was written: `OperationNotEnabled`, `LabelNotOwned`, `AuthenticationFailed`, `PermissionDenied`, `NotVisible`, `TargetStateRefused`, `PayloadRejected`, `RateLimited` (E11) | none | `failed(reason)`; grant consumed | the sender |
| O12 | `sending` | `WriteOutcomeUnknown`, `TransientFailure`, or any other exception | none | `unknown`, with `Attempt.end_seq` | the sender |
| O13 | `sending` | `recover`: found in `sending` by a sender that now holds the lock, so the earlier holder is gone (flock released), or found with `Attempt.epoch` < `live_epoch` | none | `unknown`, with `end_seq` set at detection | the next sender, or `bh work outbox reconcile` (automatic on lock acquisition) |
| O14 | `unknown` | `observe` own comment with a self marker naming `outbox_id` by a known actor (`bh-fptob` R7 row 2); for labels, observed labels already satisfy the diff | none | `sent`, with `Receipt{reconciled: true}` | the reconciler, or the thread ingest pass through the R7 row 2 binding |
| O15 | `unknown` | `observe` a **complete** comment snapshot without the marker | `snapshot_start_seq > Attempt.end_seq` | `unknown`, `absent_confirmations += 1` | the reconciler (read-only on the tracker) |
| O16 | `unknown` | `confirm_absent` | `absent_confirmations >= outbox.reconcile.absent_confirmations` (default 2) | `failed(absent)`; grant consumed | the reconciler |
| O17 | `failed` | `retry` | operator seat | `drafted` (a new O5 is required: a new render, a new digest, a new grant) | the operator through `bh work outbox retry` |
| O18 | `unknown` | `discard(reason, accept_unknown=True)` | operator seat; explicit flag | `abandoned`, with `outcome_unknown: true`; stays in the ledger (R7) | the operator |
| O19 | `abandoned` (from `unknown`) or `failed(absent)` | `observe` the marker late | none | `sent`, with `Receipt{reconciled: true, late: true}`. If another attempt of the same `outbox_id` is also `sent`, flag `duplicate_observed` for the operator | the reconciler or the ingest pass |

Transitions that are absent **on purpose**:

- `unknown → approved` and `unknown → drafted`: an ambiguous outcome is never re-sent without
  confirmed absence.
- `sending → superseded` and `unknown → superseded`: a message that may exist is never hidden
  behind a newer one.
- Any automatic `→ approved`: only an operator mints grants.
- Any `sent →` transition: Beadhive has no edit or delete operation (E10). `sent` is final. O19
  is a late receipt into `sent`, not out of it.

**Report-level effects, each in the same write as the operation transition:**

- On O10 or O14 of a record's `comment.create` operation, the sender writes
  `notified=<event>` through `set_state`.
- If R3's close rule is now satisfied, the sender calls `ReportStore.close`.
- On O6, O8 or O18 of the completion milestone's comment, the close rule is re-evaluated.

`notified` is **not** advanced for `abandoned` or `superseded`. The report closes, but
`notification_behind` stays true, so the gap is visible.

### R6. The supervised sender contract (D2)

Sending is an operator-run sequence. No daemon or timer ever runs O5 or O9. Reconciliation
(O13 to O16, O19) is tracker-read-only plus internal writes, and may also run from the
discovery pass.

1. **Dry run.** `bh work outbox show <outbox_id> [--json]` renders every operation exactly as
   it would be sent:
   - the target URL;
   - the operation;
   - the full body including the adapter-encoded marker line;
   - the label add and remove sets against the current snapshot;
   - the findings;
   - a `digest`: sha256 over the canonical serialisation of the target, operation, bytes and,
     for labels, `base_revision`. The first 12 hex characters are shown.

   It performs no tracker request and no store write. The marker is encoded through the
   adapter's codec, so the bytes shown are the bytes posted.
2. **Confirmation.** `bh work outbox approve <outbox_id> --op comment:<digest> [--op
   labels:<digest>] [--publish <finding-id>]... [--send]`.
   - Each `--op` is a **separate gate resolution** that mints its own grant (R8). Approving
     the comment does not approve the label change.
   - The digest the operator types proves that they approved what they saw. A changed render
     is refused (O5's guard).
   - The actor must resolve to a human operator seat. Agent seats are refused, which is the
     same seat-typed policy style as `beadhive_core.review`.
3. **Idempotency marker.** Every `comment.create` carries
   `OwnMarker(hive=<owner/repo>, install=<install uuid>, outbox_id=<opaque>)`. The adapter
   encodes it as the last non-empty line (ADR inconsistency 2).
   - The same `outbox_id` is used for **every** attempt of one operation, so any attempt that
     landed is findable by the same marker.
   - Labels carry no marker. Their reconciliation compares observed labels with the diff,
     because set operations are idempotent in effect.
4. **Send lease.** One publisher owns a pending delivery at a time. The holder needs:
   - **host**: `guard.guard_primary(hive)`, so only the hive's primary writer sends (E13);
   - **process**: an `fcntl.flock` on a per-binding lock file in the hive's private `.bh/`
     state, following the `checkpoint._checkpoint_lock` pattern. This is the same lock as the
     `bh-fptob` ingest lease, so a send and an ingest of one thread never interleave their
     read-modify-writes;
   - **record**: re-read `external_outbox.seq` under the lock and abort if it moved (bd has no
     compare-and-swap).

   `Attempt.holder` records `(host, pid, lock token)` and `Attempt.epoch` records
   `live_epoch`. Detecting a dead holder is clock-free, as O13 describes: the flock was
   released, or the epoch moved. No lease expires by time.
5. **Write-ahead, then request.** O9 is durable before `post_comment` or `change_labels` is
   called. The grant is passed to the adapter exactly once.
6. **Receipts.** O10 stores the remote `CommentKey` (id and global id) or `labels_after`. The
   returned `TrackerComment` is handed to the thread ingest as an `own` record (`bh-fptob` R7
   row 1).
7. **Reconciliation.** `bh work outbox reconcile [<outbox_id>]` performs O13 to O16 and O19
   through `list_comments(CommentQuery(issue=K))` and `snapshot_ids(IdQuery(COMMENTS,
   issue=K))`, or a labels read through `IssueResolver.get` (`bh-mj7cl` A1) or `list_issues`.
   A snapshot counts only when `complete=True`.
8. **Unavailable tracker.** Every failure leaves the internal work intact and the record
   visible in `bh work outbox`. That is the "observable communication backlog" of intake
   design §5.

### R7. Why timeout reconciliation never posts twice

**Setting.** O is one `comment.create` operation with `outbox_id` X on issue K. An *attempt*
is a durable `sending` entry followed by one `post_comment` call.

**Invariants**, each enforced by a guard in R5 or R6:

- **I1. At most one attempt is in flight.** An attempt starts only by O9, from `approved`, and
  O9 is written before the request.
  - Two senders cannot both pass O9's guard: one host by `guard_primary`, one process by the
    flock, and one record version by the `seq` re-check under the lock.
  - A crashed holder's record stays in `sending` until O13 moves it to `unknown`. It never
    goes back to `approved`.
- **I2. Every attempt has a fresh operator grant.** O9 requires `approved`, and only O5 (the
  operator) produces `approved`. O5 is reachable only from `drafted`. After an attempt ends,
  `drafted` is reachable only through `failed` (O17).
- **I3. `failed` after an attempt implies evidence that nothing was written.** An attempt can
  reach `failed` in only two ways:
  - **O11**, a refusal class the port defines as proof that nothing was written (E11);
  - **O16**, k ≥ 2 complete per-issue snapshots, each begun after the attempt ended
    (`snapshot_start_seq > end_seq`, ordered by local `seq`, never by time), none containing a
    comment whose decoded marker names X from a known actor.

**Assumptions:**

- **A1 (visibility).** If the tracker committed an attempt's comment, then every complete
  per-issue listing that begins after the client observed the attempt's request terminate
  contains it, unless someone deleted it.
- **A2 (classification).** The adapter raises O11's classes only when the request provably
  did not create the comment.

**Claim.** Under A1 and A2, Beadhive never causes two comments carrying marker X to exist at
the same time.

**Proof sketch.**

1. Suppose attempt `a_i` committed a comment and a later attempt `a_j` is
   issued.
2. By I2, O passed through `failed` between `a_i` and `a_j`.
3. By I3, that `failed` came either from O11 for `a_i`, which contradicts A2, or from
   O16. O16 needs k complete snapshots begun after `a_i` ended with no X marker, which
   contradicts A1.
4. By I1, `a_j` cannot overlap `a_i`.
5. Hence no `a_j` exists. ∎

What happens in each case:

- **The comment exists** (the timeout fired after the commit): reconciliation finds X and
  takes O14 to `sent`, with no second request.
- **The comment does not exist**: after k complete absences, O16 then O17 then a fresh O5
  lead to exactly one new attempt.
- **A crash anywhere between O9 and O10** looks like an unknown outcome (O13) and is resolved
  the same way.
- **A human deleted Beadhive's comment**: it is absent from the snapshots, so a re-approved
  post is not a visible duplicate.

**When A1 fails** (eventual consistency longer than the confirmation passes), a late original
can appear after a re-post. O19 then flags `duplicate_observed` for the operator. The port has
no delete, so Beadhive never "fixes" it automatically. This is the honest form of intake design
§5's "do not promise exactly-once remote writes".

- k = 2 follows `bh-fptob` R6, which guards against page-shift false absences on page-number
  pagination.
- A1 is documented GitHub behaviour for a single repository endpoint. It is **not verified**
  here (R13).

**Offline, clock-free test.** It uses the ADR conformance kit, `MemoryTracker` plus
`MemoryWorld` (E12), an in-memory `external_outbox` store, a deterministic `IdSource` and an
in-process lock and epoch.

1. Draft, then approve, then send with `world.fail_next_write_ambiguously()` armed. The fake
   stores the comment and raises. The operation is `unknown`.
2. Send again: refused, because it is not `approved`.
3. Reconcile: the comment with marker X is found. The operation is `sent` (reconciled), and
   `write_requests() == 1` and `comment_count(K) == 1`.
4. **Kit extension K1:** `fail_next_write_ambiguously(applied=False)` raises before storing.
   - Reconciling once gives `absent_confirmations == 1`, still `unknown`.
   - A second complete snapshot gives `failed(absent)`.
   - `retry`, a fresh `approve` and `send` follow.
   - `write_requests() == 2` and `comment_count(K) == 1`.
5. Crash simulation: after O9 is persisted, raise in the sender before the adapter call. The
   next sender acquires the lock and takes O13. The rest is as step 4, with
   `write_requests() == 1`.
6. A snapshot that began **before** `end_seq` is ignored by O15's guard. The test drives a
   stale snapshot by ordering, not by time.
7. A scripted `TrackerError` mid-pagination gives `complete=False`, so nothing changes.

No step reads the host clock, sleeps or opens a socket. Ordering is asserted on `seq`,
`Attempt.n` and `write_requests()` only.

### R8. Which operations are writes, and their gates

**Tracker writes.** There are exactly two. Each needs its own approval and its own single-use
grant.

| Write | Port call | Gate | Grant scope | Also required |
|---|---|---|---|---|
| post a comment | `CommentWriter.post_comment(issue, body, marker=, grant=)` | O5 for that operation: a human seat, the digest of the dry-run render | `WriteGrant(grant_id, COMMENT_CREATE, issue)`, used once by O9 | `write.comment.enabled`; `comments.read` usable (for reconciliation); a clean leak check |
| change owned labels | `LabelWriter.change_labels(issue, add=, remove=, ownership=, grant=)` | O5 for that operation, separately confirmed | `WriteGrant(grant_id, LABELS_CHANGE, issue)`, used once | `write.labels.enabled`; a non-empty `write.labels.owned_prefixes`; every label owned |

**Single use** is enforced twice:

- **across processes** by the state machine: a grant is recorded in `Approval`, consumed by
  O9, and voided by O6, O7 and O8. Re-entering `approved` mints a new `grant_id`;
- **in-process** by the adapter's reused-grant refusal (E10, E12).

Enabling a write in config is necessary and never sufficient (ADR §6).

**Why a recorded approval and not a `bd gate` bead per write.** The ADR asks for an
"operator-approved gate" that issues a single-use grant. It does not prescribe a bd gate
bead. A bead per approval would add a permanent bead per message operation (E3, `bh-fptob`
E14), and it could not commit atomically with the operation's transition.

The approval is therefore a durable, attributed entry (`Approval{actor, digest, grant_id,
overrides, seq}`), written in the same document write as O5. This is a deliberate
interpretation (R12, D-g).

**Not writes to the tracker (listed for completeness):**

- All reads: render, reconcile, ingest and probe.
- Every internal-store write. These are `set-state delivery|notified`, the `external_outbox`
  document, the hold gate, the `tracks` edge and the close of a report. They are internal
  bookkeeping under the intake module's ownership (R2), written only through
  `store_target.operational_store`.

**Public store writes: none.** The module never resolves `SCOPE_PUBLIC` (`bh-mj7cl` §3). The
next projection pull imports Beadhive's label changes as GitHub-authoritative data, and that
is the pull, not a write by the outbox.

**Not possible in v1, because the port has no such operation:** closing or reopening an issue,
editing or deleting any comment, editing a title or body, assigning, or creating an issue. Any
of them would be a new port operation behind its own new gate.

### R9. Merge and release triggers: draft, never send

Both triggers are `IntakePort` operations (`bh-mj7cl` §2: added to the port, not beside it).
They are called from the composition root and wrapped so that an intake failure is reported
and never fails the merge or the release. With intake or the outbox off, `NullIntake` makes
them free.

```python
def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome: ...
    # MergeFact(work, commit, actor); for each r in reports_for_work(work), open or closed:
    #   delivery: pending|reverted -> merged (set_state, reason names the commit)
    #   facts.merged = {work, commit}
    #   if milestones.draft_on_merge: draft(DraftKey(url, merged, commit)) -> O1 (supersedes, O6)
    #   if close-on-merge and the outbox is off: ReportStore.close(r)
def on_release(self, fact: ReleaseFact) -> MilestoneOutcome: ...
    # ReleaseFact(tag, commit, actor); candidates: reports with delivery:merged (all statuses)
    #   verified = ReleaseVerifier.contains(fact.commit, r.facts.merged.commit)
    #   YES -> delivery: merged -> released; facts.released; draft if policy says so
    #   NO / UNKNOWN -> unchanged; reported as "not verified" (never assumed)
```

- **The merge seam** is `work.merge` / `work.finish` after the bead closes (E14). It is the
  `reports_for_work` call site that `bh-mj7cl` §8 already lists.
  - `commit` is the merge commit on the integration branch. That is the identity the intake
    design asks for: "the final merge … commit, not only a branch-head hash".
  - For a molecule, the trigger runs for the epic and for every closed child that a report
    tracks.
- **The release seam.** bh has no single publish function (E14), so v1 adds an operator verb,
  `bh work delivery release <tag>`.
  - It resolves the tag's commit and calls `on_release`.
  - `ReleaseVerifier` is a port implemented in the root with
    `git merge-base --is-ancestor <merge> <tag-commit>`, following the
    `release._remote_refs_containing` precedent.
  - A later bead may call the same function from the release flow once one exists. That is
    the same "one function, two triggers" shape as `release_catalog_auto_import`
    (`bh-mj7cl` E12).
- **Never send.** Both triggers end at O1 or O6. Neither holds a grant or a writer. A test
  asserts `write_requests() == 0` across every trigger case.
- **Supersession.** Milestone records share `group = "milestone"`. A `released` draft
  supersedes an unsent `merged` draft (O6), so the reporter receives one current message
  rather than a stale backlog. A `merged` record already `sending`, `unknown` or `sent` is
  untouched. Reply records each have their own group and are never superseded automatically.
- **Out of scope for v1:** started, blocked and deprioritised milestones (intake design §4),
  and revert detection (only operator-recorded `reverted`, which drafts `corrected`).
- **Closing keywords.** Under close-on-release, a `Fixes #12` in a commit message pushed to
  the default branch would close the public issue on GitHub's side, out of Beadhive's control.
  The leak check flags closing keywords in rendered text (they are harmless in comments). A
  merge-time commit-message lint is follow-up work (R13).

### R10. The leak check and the sanitizer seam

`domain.leaks.check(rendered, policy) -> tuple[LeakFinding, ...]` is a pure function. It runs
at O1, at every render, and finally on the exact bytes at O9.

| Finding kind | Detects | Overridable at O5 |
|---|---|---|
| `secret` | `Sanitizer.scan` hits (the `bh-o2mjz` seam), plus the ADR's token-shape regex | **never**; the operation stays `held` |
| `bead_id` | `<prefix>-<id>` for every prefix in the fleet (operational and public, from the registry) | yes, deliberately published and recorded |
| `branch` | `wt/bead/…`, `wt/batch/…`, `mol/…` and other internal branch shapes | yes |
| `private_label` | any `dimension:value` of a closed or configured dimension (`intake:`, `dispatch:`, `review:`, …) | yes |
| `seat` | seat identities (`dev/…`, `disp/…`, `releaser/…`, …) | yes |
| `path` | host paths: the worktree root, `.bh/`, home directories | yes |
| `label_not_owned` | a label operation outside `owned_prefixes` | **never** (also `LabelNotOwned` in the port) |
| `closing_keyword` | `fixes #N`, `closes #N`, … | warning only |

- **`LeakPolicy`** carries the fleet prefixes, the branch and seat patterns, the dimension
  names (from `registry.closed_dimensions`) and the path roots. It is built in the composition
  root and passed in, so the domain stays standard-library only.
- **Sanitizer seam.** `Sanitizer` is a `Protocol` in the intake contracts with
  `scan(text) -> tuple[Finding, ...]` and `scrub(text) -> str`.
  - Until `bh-o2mjz` lands, the root binds the ADR's `scrub()` plus the token-shape regex.
  - If no sanitizer is bound, every operation is `held` (fail closed).
  - Egress itself is the per-hive opt-in `intake.external.outbox.enabled`.
- **Overrides** are named per finding (`--publish bead_id:3`) and recorded in `Approval.overrides`
  with the actor. That is what "unless deliberately published" means here.
- **Marker privacy.**
  - `outbox_id` is minted by `IdSource` as `ob1_` plus 26 base32 characters of random bytes.
  - The underscore means it can never match the `<prefix>-<id>` bead shape.
  - It is never derived from a bead id.
  - `hive` is the repository identity and `install` is a UUID (`bh-fptob` R4).
  - A contract test asserts that an encoded marker and a rendered payload with no overrides
    match no fleet bead-id pattern.

### R11. Port additions

These are added to `IntakePort` (`bh-mj7cl` §2). Their records go in `domain/outbox.py`.

```python
def on_work_merged(self, fact: MergeFact) -> MilestoneOutcome: ...
def on_release(self, fact: ReleaseFact) -> MilestoneOutcome: ...
def draft_reply(self, request: ReplyDraft) -> OutboxRecordRef: ...   # O1 for a reply
def outbox(self, query: OutboxQuery) -> OutboxView: ...              # list; read-only
def render(self, outbox_id: str) -> RenderedRecord: ...              # dry run; read-only
def approve(self, request: ApprovalRequest) -> ApprovalOutcome: ...  # O5 / O4 overrides
def send(self, outbox_id: str, *, actor: str) -> SendOutcome: ...    # O9 to O13
def reconcile(self, outbox_id: str | None = None) -> ReconcileOutcome: ...  # O13 to O16, O19
def discard(self, outbox_id: str, *, reason: str, actor: str, accept_unknown: bool = False): ...
def retry(self, outbox_id: str, *, actor: str) -> OutboxRecordRef: ...      # O17
def work_progress(self, report: str) -> WorkProgress: ...            # derived dimension
```

If the port grows past review comfort, the module's contracts may split it into `IntakePort`
and `OutboxPort` with one `IntakeService` implementing both. That is an implementation-bead
choice, and either shape keeps one module.

**Outbound ports**, in the contracts:

- **`ReportStore` additions** (`bh-mj7cl` §3):
  - `read_outbox(report)`;
  - `write_outbox(report, doc, *, expect_seq)`;
  - `reports_with_outbox()` (`bd list --all --has-metadata-key external_outbox`, E4);
  - `create` now opens the hold;
  - `close` is now R3's held close.
- **`Sanitizer`**, as in R10.
- **`ReleaseVerifier.contains(release_commit, merge_commit) -> Tri`.**
- **`SendLock`**: a context manager plus `epoch()` and `holder()`, implemented in the root
  with `guard_primary`, `flock` and `live_epoch`.
- **`IdSource`**: `outbox_id()` and `grant_id()`.

The tracker's `CommentWriter`, `LabelWriter` and `TrackerReader` come from
`beadhive.modules.tracker.contracts`. No new tracker amendment is needed beyond `bh-mj7cl` A1,
which reconciliation of labels uses when present.

**New named routes in `beadhive-bd-cli`:**

- `issue_close_held` (`bd close --force`, for R3 only);
- `gate_create` with the `intake-hold:` reason (the existing `gate_create`, `routes.py:123`);
- `issue_set_metadata(report, key, doc)` (`bd update --metadata @file`);
- `issue_list(has_metadata_key=, all_=True)`.

### R12. Divergences from the sibling spikes, and why

| # | Sibling statement | This design | Reason |
|---|---|---|---|
| D-a | `bh-mj7cl` §5: "An *accepted* report may act as its own one-to-one work" | A bound report is never work. One-to-one is the report tracking a work bead created by `accept --new-work` | Untrusted public text must not become dispatchable directly (E6), and delivery must outlive work closure (R3) |
| D-b | `bh-mj7cl` §3 `ReportStore.create` / `close`; `DispositionRequest` | `create` opens the hold gate; `close` is the held close; `DispositionRequest` gains `work` and `new_work` | R3 |
| D-c | `adopt.py` and `plan_filing.py` copy `external_ref` onto the epic and link the report as `parent-child` | For bound reports, adopt links the report as **`tracks` → epic** and does **not** copy `external_ref` | A second bead with the URL makes `find_by_external_ref` return two rows, and R8 `DUPLICATE_BINDING` then refuses every re-import (E9). A held report as a `parent-child` step would be offered to `work_next` |
| D-d | `bh-fptob` R7's outbox view: `receipt_ids()`, `pending(outbox_id)`, `known_actors()` | `pending` covers `sending` and `unknown`. A new `issued(outbox_id)` covers every operation that ever had an attempt, including `abandoned` and `failed` ones | A late arrival (O19) must classify as `own` (flagged), not `external` new evidence. Additive |
| D-e | `bh-mj7cl`'s `bh.intake.source` vs `bh-fptob`'s `external_intake` | This design uses `external_outbox`, following `bh-fptob`'s shape | Two conventions already exist. The implementation molecule should pick one spelling for all three keys before any lands |
| D-f | `bh-mj7cl` §5: add `"tracks"` to `_NON_BLOCKING_DEPENDENCIES` | Invert to `_BLOCKING_DEPENDENCIES = {"blocks", "conditional-blocks", "waits-for"}` | The views disagree with bd in both directions (E8). The allow-list goes stale with every edge type |
| D-g | ADR §6 "operator-approved gate" | Realised as a recorded `Approval` in the outbox document, not a bd gate bead per write | Atomic with the transition, and no permanent bead per message (R8) |

### R13. Residual risks

| Risk | Mitigation |
|---|---|
| A1 (read-after-write on comment listings) is not verified for GitHub | k = 2 confirmations; O19 detects and flags a late duplicate; re-verify on the first gated write bead with recorded responses |
| Creation window: R exists for one bd call before its hold gate | Open the gate immediately after `bd create`; the `bh-mj7cl` stamp repair also repairs a missing hold; `bh work claim` refuses held beads; untriaged reports are not auto-dispatched today (E6) |
| Held reports read as "blocked: 1 open gate" in operator views until a `report` lane lands | Accurate meanwhile; the lane is a small `_readiness` change (R3) |
| A human resolves or deletes a hold gate by hand | The seat policy refuses it through bh; `bh doctor` reports an open bound report without an open hold and re-opens one |
| PR or commit closing keywords close the public issue early under close-on-release | The leak check warns in rendered text; a commit-message lint on merge is follow-up work |
| `bd set-state` may not be able to clear `delivery` on unlink | Verify in the first dimension bead; the fallback keeps `pending` with an `unlinked` fact |
| Port width (about 20 operations on `IntakePort`) | The split into `OutboxPort` stays available (R11) |
| Outbox scan cost: `reports_with_outbox()` lists every report that ever had a record | Bounded by reports, not messages; add an index key only if measured slow |

### R14. Offline, clock-free test plan

Every case uses `MemoryTracker` and `MemoryWorld` with explicit `at=`, an in-memory
`ReportStore` with `external_outbox`, a deterministic `IdSource`, an in-process `SendLock` with
a settable epoch, and a recording `Sanitizer`. No case reads the clock, sleeps or opens a
socket.

| # | Case | Asserts |
|---|---|---|
| X1 | Merge trigger on W tracked by two open reports and one closed duplicate | three `delivery:merged`; three `merged` drafts; `write_requests() == 0` |
| X2 | The merge trigger re-run | no new record (O2); no new `set-state` |
| X3 | Release trigger with a verifier answering YES, NO and UNKNOWN | only YES moves to `released`; NO and UNKNOWN are reported unchanged |
| X4 | `released` drafted while `merged` is `drafted` or `approved` | `merged` is `superseded`, its grant voided |
| X5 | `released` drafted while `merged` is `unknown` | `merged` untouched (no supersede of a possibly-sent message) |
| X6 | Approve with a stale digest, by an agent seat, with `comments.read` disabled, or with an unresolved finding | refused; no state change |
| X7 | Comment and label operations in one record | two approvals, two distinct `grant_id`s; approving one does not approve the other |
| X8 | Send twice after `sent` | the second is refused; `write_requests() == 1` |
| X9 | R7 steps 1 to 7 (ambiguous applied, ambiguous not applied with K1, crash after write-ahead, stale snapshot, incomplete snapshot) | as stated in R7 |
| X10 | O11 for each proof-of-no-write class (scripted refusals, throttle past budget) | `failed`; a retry requires a new approval |
| X11 | Label diff when a human changed an owned label after the draft | O7 back to `drafted`; the re-rendered diff excludes the human's change |
| X12 | Leak matrix: each finding kind in R10, with and without override | `secret` and `label_not_owned` never pass; overrides recorded with the actor |
| X13 | Marker privacy | the encoded marker and rendered payloads match no fleet bead-id pattern |
| X14 | Readiness (against the in-memory store's model of E4): import, accept `--new-work`, merge, release under both policies | the report is never in the ready set; W is ready until claimed and closed at merge; under close-on-release the report stays open and held between merge and release |
| X15 | Held close: crash injected between `close --force` and `gate resolve` | the report is closed; the doctor sweep resolves the gate; it is never ready at any step |
| X16 | Notified and close rule: the `released` comment is `sent`, then the same scenario with it `abandoned` | `notified:released` and closed; then closed with `notification_behind` true |
| X17 | Isolation | the public-store fixture is byte-identical; other metadata keys (`external_intake`, `bh.intake.source`) survive every outbox write |
| X18 | The outbox off (`NullIntake`) | merge and release are unaffected; no drafts, no dimension writes |

Kit extension **K1** (to `beadhive.testing.tracker`): add
`MemoryWorld.fail_next_write_ambiguously(applied: bool = True)`. The existing behaviour stays
the default.

**Integration (bd binary, scratch store, `legacy-stateful` or integration key):** one test
replays E4 against bd. It covers the gate hold, the refused plain close, the forced close with
no window, metadata on a closed bead and `--has-metadata-key`. It pins the bd semantics the
design relies on.

### R15. Configuration (proposals; off by default)

The keys sit under the external-intake namespace that `bh-mj7cl` §6 and `bh-mobxo` settle. The
spelling is a proposal; the defaults are the decision.

| Proposed key | Default | Meaning |
|---|---|---|
| `intake.external.outbox.enabled` | `false` | The per-hive egress opt-in (`bh-o2mjz`); off means `NullIntake` for every outbox operation |
| `intake.external.outbox.mode` | `supervised` | The only accepted value in v1 (D2); anything else fails schema validation |
| `intake.external.completion_policy` | `close-on-merge` | Or `close-on-release` (R3) |
| `intake.external.milestones.draft_on_merge` | `false` | Draft `merged` records |
| `intake.external.milestones.draft_on_release` | `false` | Draft `released` records (close-on-release) |
| `intake.external.milestones.append_release_to_closed` | `false` | The release append on closed reports (close-on-merge) |
| `intake.external.outbox.labels.on_delivery` | none | Map from a `delivery` value to the owned labels to add and remove; must lie inside `write.labels.owned_prefixes` |
| `intake.external.outbox.public_evidence.commit_links` | `false` | Allow public commit or PR URLs in `PublicFacts` |
| `intake.external.outbox.reconcile.absent_confirmations` | `2` | k in O16 |

Tracker-side flags stay the adapter's (ADR §6): `write.comment.enabled`,
`write.labels.enabled` and `write.labels.owned_prefixes` (empty: no label write possible), plus
`read.comments.enabled`, which O5 requires.

No key holds a token, a grant or an outbox id. With the defaults, the merge and release paths
behave exactly as today.

### R16. Follow-up work for the implementation molecule (not filed here)

1. **Vocabulary bead.** Register `delivery` and `notified` (R1), the `intake-hold:` marker in
   `guard.py`, `work_logic._gate_kind` and the `beadhive_core.review` seat policy. It also
   inverts `_NON_BLOCKING_DEPENDENCIES` (D-f) and adds the `report` lane in `_readiness`.
   Verify whether `bd set-state` can clear a value.
2. **Hold bead**, which depends on `bh-mj7cl`'s `BdReportStore`. It adds the hold at create,
   the held close and `issue_close_held`, the doctor sweep, and the `bh work claim` refusal.
   It also adds `accept --new-work` / `--work` (D-a, D-b) and the adopt change (D-c). It
   carries X14, X15 and the bd integration test.
3. **Outbox domain bead.** It covers the records, the pure state machine (O1 to O19), the
   leak check, `DraftKey` idempotence and supersession, with X2, X4 to X7, X10 to X13 and
   X16. It depends only on the tracker contracts and the testing kit, plus K1.
4. **Sender bead.** It adds the `SendLock` root implementation, write-ahead, receipts,
   reconciliation, the `bh-fptob` R7 view with `issued()` (D-d), and the CLI verbs
   (`bh work outbox show|approve|send|reconcile|discard|retry|draft`), with X8, X9 and X17.
   Each write operation (`comment.create`, then `labels.change`) lands behind its own gate, as
   ADR next-steps item 3 requires.
5. **Triggers bead.** It adds `on_work_merged` from `work.merge` / `finish`,
   `bh work delivery release <tag>` with `ReleaseVerifier`, and the policies, with X1, X3 and
   X18.
6. **Open items:**
   - one metadata key convention (D-e);
   - A1 verification against recorded GitHub responses;
   - a closing-keyword lint on merge commits;
   - the `started`, `blocked` and deprioritised milestones;
   - revert detection;
   - after `bh-elkv`, a `needs-info` reply event that wakes on reply (D3).
