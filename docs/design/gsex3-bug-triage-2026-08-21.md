# bh-gsex3.1 — triage of the 0.14.x bug-fix batch (2026-08-21)

Gate before any bundled bug in bh-gsex3 (Bug-fix batch: 0.14.x patch backlog) is picked up for
implementation. Each of the six bundled bugs was re-checked against this checkout of `main`
(subsystems: dispatch loop / escalation, release attestation, image-drift, host provisioning,
Dolt sync, HQ aggregate). Full evidence for each verdict is recorded as a comment on the bead
itself (`bd comment <id>` / `bd show <id>`); this note is the roll-up.

All six are **STILL VALID** — none is fixed, stale, or superseded. Nothing was closed.

## bh-7679k — attempt count spans failures and successes, never resets

Partially mitigated, not fixed. `work_next.decide()`'s row-priority table (row 7 `review`
strictly ahead of row 10 `dispatch-up-to-budget`) now prevents the *specific* trace in the bug
report — a bead already `review:pending` is never escalated by the dispatch loop-breaker.
Verified live against the current `Molecule`/`decide` API.

But the general defect the title names is still present: `attempt_count()` has no registered
failure signature for the `dispatch` action, so it falls back to counting *every* event bead
ever recorded on the issue, with no reset on success. Two unrelated priority-bump events are
enough to escalate a bead as "stuck" on its first real dispatch attempt; a bead that failed
once, then succeeded, then got bounced and reopened still carries the success event in its
count forever. Acceptance criterion 2 (review-pending never escalated) is met; acceptance
criterion 1 (attempt counting resets/excludes pre-success events) is not.

## bh-d3u1o — dry-run says GO on a tree the real release refuses

Still two different predicates under one name. `just release` requires a background-gate
*marker* (`release await` → `_marker_for_tree`); `just release-preview`'s "green" line goes
through `prepush.check_push_main` → `validation_ledger.green_verdict`, which accepts any fresh
green verdict for the tree regardless of whether a marker exists. A tree proven green via
`just attest` in the foreground still reads GO from preview and is refused by the real release.

## bh-pee6m — nothing guards image-vs-release drift

`scripts/image-drift.sh` still only compares images against each other and against the
working-tree HEAD (informational, never fails). No check compares a built image's manifest
version against the released/checked-out beadhive version with a failing verdict. Grepped
image-drift.sh, the justfile, doctor.py, and hive_ready.py — no such check exists anywhere.

## bh-tx2hp — provisioned host cannot run an agent seat

`host_provision.PLAN` still has no step that runs `bh mcp install` or installs the claude-code
harness plugin (INSTALL.md's steps 2/3). `bh doctor` still does not report the plugin's absence.
`hitch_plugin.py` is a separate, opt-in, disabled-by-default integration for a third-party
`hitch` binary — it does not cover the default `bh@beadhive` plugin path this bug is about.

## bh-s9cdk — split-brain Dolt store, no common ancestor

Both enabling defects are still open: the epoch fence is still inert (bh-ban1j → bh-tfapu,
still P0/open) and the two-install-planes issue (bh-tp38g) is still open. No lineage/
merge-base check exists in dolt_health.py, hive_ready.py, or doctor.py — split-brain is still
only discoverable by hand-running `dolt merge-base` against both remotes.

## bh-eu2pp — missing integration test for HQ aggregate non-decreasing invariant

No test asserts per-prefix row counts are non-decreasing across a sync. The closest existing
test (`test_hub_bulk_int.py::test_bulk_copy_matches_a_real_bd_produced_aggregate`) checks a
single bulk copy's parity against a real bd-produced aggregate; it does not sync twice and
compare per-prefix counts before/after, so the bh-4o07n regression shape would still slip
through CI today.
