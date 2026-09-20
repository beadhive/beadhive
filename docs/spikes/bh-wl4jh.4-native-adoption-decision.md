# `bh-wl4jh.4`: Beads v1.3 coordination and contract adoption decision

**Status:** accepted for implementation planning

**Evidence:** [`bh-wl4jh.1`](bh-wl4jh.1-native-coordination.md),
[`bh-wl4jh.2`](bh-wl4jh.2-native-sync.md), and
[`bh-wl4jh.3`](bh-wl4jh.3-native-contracts.md)

**Release examined:** Beads v1.3.0 at
`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`

## Decision

Adopt the independently useful v1.3 contract, projection, and coordination slices below. This is
not a blanket migration and it is not conditional on adopting `bd serve`. Each slice keeps its own
compatibility gate and rollback boundary. In particular, the canonical CLI/export schema is not
an OpenAPI schema, a worker lease is not a host epoch fence, and native sync convergence is not
authority to publish.

The impact order is binding for planning:

1. **API and schema contract safety.** Adopt the canonical `bd schema` contract narrowly. The
   separate `bh-ie41e` decision owns HTTP transport and OpenAPI; nothing here assumes its verdict.
2. **Measured token and payload reduction.** Adopt brief projections and count only where the
   caller's field/filter contract is proved.
3. **Iteration speed from fewer or atomic operations.** Delegate single-issue arbitration and
   guarded release to native claim/CAS; use native aggregates and idempotent provenance where they
   truly collapse operations. Do not advertise an unshipped CLI `claimNext` or batch transaction.
4. **Provenance.** Pilot native Git-SHA events behind dual-write compatibility.
5. **Federation correctness.** Adopt the typed sync outcome first on verified no-push replicas;
   retain the primary publisher until every internal push can carry Beadhive's epoch fence.

### Independent verdicts

| feature group | verdict | immediate boundary |
|---|---|---|
| canonical Issue/Dependency JSON Schema | **Narrow GO** | vendor the pinned schema and generate strict canonical-record models; compose command and projection contracts separately |
| `--brief` and `--brief-deps` | **GO at audited internal call sites** | request metadata must carry partialness; public/native JSON and text-dependent readers remain full |
| native count and `--max-rows` | **Narrow GO** | prove filter parity for count; surface row-cap exit 2 as a typed refusal, never truncation |
| single-issue claim, assignee/status CAS, guarded unclaim | **Narrow GO** | delegate mutation arbitration while retaining Beadhive authorization and holder/fence checks |
| heartbeat and replica-local reclaim | **Narrow GO** | make the existing wrappers an active supervisor contract; never perform routine broad `--any-replica` reclaim |
| claim pools | **NO-GO for operational adoption now** | reclaim loses pool routing, pools do not type seats, and they do not make a batch atomic |
| native `claimNext` and transactional batch mutation | **NO-GO as a claimed v1.3 CLI dependency** | the measured CLI does not expose these atomics; reconsider only behind a separately certified capability with a CLI fallback |
| native provenance | **Narrow GO pilot** | dual-write single-SHA commit/land events and `git.commits`; do not migrate authority or review audit |
| typed `bd sync` reconciliation on verified no-push replicas | **Narrow GO** | consume all exits 0--4 and structured evidence without granting publication authority |
| native sync as the primary pull/push publisher | **NO-GO until a fence seam exists** | its internal push/retries cannot carry the `refs/bh/epoch` token immediately before every data push |

These verdicts are intentionally separable. A failure or delay in provenance, leases, native
sync, or `bd serve` must not block schema/projection work, and adopting schema/projections must not
silently authorize a lifecycle mutation.

## 1. Contract-first schema adoption — Narrow GO

At toolchain upgrade time, capture `bd schema` from the exact pinned binary, require
`schema_version: 1`, validate both Draft 2020-12 documents, and vendor the byte-stable result.
Generate strict `BdIssueRecord` and `BdDependencyRecord` models for canonical import/export rows.
Keep `additionalProperties: false` so an upstream field or enum change fails visibly.

The generated models do not replace command contracts. `list`, `ready`, and `show` add fields and
decorations not described by the canonical schema; brief rows omit fields without a serialized
partial marker; provenance has no published schema; HTTP requests, pages, cursors, and errors have
no retrievable OpenAPI document in the certified server. Beadhive must therefore retain or add
small explicit `BdListRow`, `BdShowRow`, brief-row, command-envelope, and typed-error contracts.
The Dolt schema version, HTTP context schema version, and `bd schema` version remain distinct.

This makes hand-maintained canonical Issue/Dependency field and enum lists obsolete after fixture
parity is green. It does **not** make legacy aliases, timestamp normalization, origin policy,
dependency decorations, gate/state records, HTTP capability negotiation, or lifecycle
authorization obsolete.

### Implementation molecule: canonical Beads contracts

1. Add a pinned-schema capture/update command and check in the versioned artifact with binary
   commit and schema-version gates.
2. Generate strict canonical record models and prove representative export/import fixtures.
3. Compose and fixture-test list/show/ready, brief, error, and HTTP contracts around—not inside—the
   generated models.
4. Add an upgrade drift gate that regenerates/diffs only when the Beads pin changes.
5. Activate generated canonical models, then delete only the superseded canonical field/enum
   duplication. Rollback selects the previous vendored contract and models.

This molecule must not wait for or silently select the `bh-ie41e` transport verdict.

## 2. Projection, aggregate, and row-ceiling adoption — GO in audited seams

The live hive measurements justify this slice:

| operation | full -> narrow bytes | approximate tokens avoided | reduction |
|---|---:|---:|---:|
| `list --all --limit 0` -> `--brief` (4,456 rows) | 10,578,095 -> 4,366,054 | 1,553,010 | 58.7% |
| `ready --limit 0` -> `--brief` (588 rows) | 2,134,164 -> 424,170 | 427,498 | 80.1% |
| `show bh-wl4jh.3` -> `--brief-deps` | 5,479 -> 2,518 | 740 | 54.0% |
| matched full-list cardinality -> count | 10,578,095 -> 43 | 2,644,513 | 99.99959% |

The token figures are the stated bytes/4 planning estimate, not tokenizer-specific billing. The
corpus was live, so contract tests need stable fixtures rather than these changing row counts.

Projection intent must travel out of band in a wrapper such as `Projected[T]`; missing brief
fields can never be interpreted as empty values. Keep full rows for state-stream
export/reconciliation, complexity inference, work/plan briefs, review rendering, editorial and
triage surfaces, public/MCP passthroughs, and every caller consuming issue body text. Preserve
full outer issues where only nested dependencies become brief, and audit nested labels/metadata
before enabling `--brief-deps`.

Native count is approved only after exact list/count filter parity. The observed bare
`bd count --json` result differed unexpectedly from `--include-infra`, while the latter matched
the comparable list. No caller may replace materialization with count based on help text alone.

`--max-rows` is an explicit per-call circuit breaker: exit 2 with empty stdout means
`row_ceiling_exceeded`. It is neither pagination nor an empty/truncated result. Do not set a
process-wide `BEADS_MAX_ROWS`; direct and proxied routes differ, and full snapshots have different
safe ceilings. The adapter must preserve the exit code before any call site opts in.

### Implementation molecule: smaller bounded reads

1. Add projection-aware result types and a typed command failure retaining exit 2.
2. Add full-versus-brief field-parity fixtures, including absent-versus-empty assertions.
3. Enable `--brief` first in the audited existence, inventory, orphan-branch, stale-label,
   scheduling, `work next`, and local-loop reads from `.3`.
4. Add `--brief-deps` to lifecycle guard reads only after nested-field audits.
5. Add count/list parity fixtures for every proposed filter pair, then replace only
   cardinality-only materializations.
6. Add policy-derived, per-call row ceilings and fail-loud operator diagnostics; retain full
   exact reconciliation behavior. Rollback disables each call-site option independently.

This molecule delivers the highest measured token reduction and can land before any service API,
lease, provenance, or sync work.

## 3. Native coordination primitives — Narrow GO

Native `update --claim` is the single-issue arbitration point: one claimant succeeds and a racing
claimant exits 1 naming the holder. Native `unclaim --if-assignee` and guarded
`update --if-assignee` / `--if-status` make stale writes no-ops; pure stale-guard failures exit 13.
Use those contracts instead of comments and branches based on the pre-v1.3 assumption that two
claims can both succeed with last-writer-wins.

The existing `coordination.py` heartbeat and reclaim wrappers are useful groundwork, but their
documentation says it was verified against v1.1.0 and wrappers alone do not revoke a worker.
Adoption means certifying the v1.3 wire contract, scheduling heartbeats below TTL, treating a
failed heartbeat as loss of lifecycle authority, interrupting the supervised worker, and checking
ownership again before validation/review handoff. Reclaim remains replica-local, label-scoped,
observable, and delayed beyond both TTL and the sync interval.

The available atomics remove duplicate claim-race arbitration and harden provisioning rollback,
abandon, interruption, and reassignment. They do not make choose+claim one operation: the CLI
still performs ready/read then claim. A future certified `claimNext` could reduce that 2 -> 1 and
remove the selection window, but it is not a dependency of this decision. Claims for *N* batch
members remain *N* writes with compensation; no measured CLI transaction provides the projected
80% call reduction at the five-member cap.

### Machinery that remains authoritative

- Seat typing (`dev/`, `disp/`, reviewer, merger), kickoff approval, and open-gate policy.
- The exact reviewed branch/tree SHA, stale-review supersession, and review authority.
- Batch all-member preflight/rollback, the shared branch and gate, and group merge semantics.
- Molecule container topology and dependency ordering.
- `ClaimRecord` host/adopt epoch and any holder/run incarnation needed to distinguish two
  processes using the same actor name. Native leases return no incarnation token.
- `_guard_holds_claim` at submit and other privileged boundaries; a heartbeat is not an implicit
  guard on later commands.
- Branch/worktree recovery and the merge-slot's process/host/time cleanup semantics.
- The host lease, adopt epoch, and remote publication fence. A lease `node_id` identifies a store
  replica, not a Beadhive host.

### Implementation molecule: guarded native coordination

1. Re-certify claim, heartbeat, reclaim, guarded update, and unclaim envelopes/exits against the
   pinned release; correct v1.1-era docs and tests.
2. Make native claim return authoritative for single-issue contention; remove only the redundant
   last-writer-wins re-read/race classification after regression tests prove loser behavior.
3. Replace unconditional provisioning rollback, abandon, interruption, and reassignment writes
   with assignee/status-guarded mutations and expose stale exit 13 distinctly.
4. Add supervisor heartbeat ownership, failure-triggered interruption, and pre-validation plus
   pre-review ownership checks.
5. Add local-replica, label-scoped reclaim with metrics, grace above sync cadence, and an explicit
   operator-only path for one known-dead foreign replica. Never schedule broad `--any-replica`.
6. Exercise reclaim/reclaim-by-same-name, crash, submit-after-loss, and host-adopt races before
   activation. Rollback disables supervision/reclaim while retaining the native mutation guards.

Claim pools are excluded from this molecule. They are untyped aliases, reclaim returns an issue
unassigned instead of restoring its pool, and they do not provide batch ownership. Reopen only
after pool restoration semantics and a true atomic batch claim are proven. Likewise, do not route
`claimNext` through HTTP merely to claim this coordination GO; transport remains independently
decided.

## 4. Native provenance — Narrow GO pilot

Pilot deterministic, append-only `commit` and `land` events for the common one-SHA path. This can
turn `git_linkage.record_commits` from read-plus-metadata-update into one idempotent append and
adds reverse lookup by SHA. It does not improve every path: recording *N* rebased SHAs remains
*N* provenance commands because v1.3 has no batch record, while today's metadata can update the
array once after one read.

Use closed trusted producers such as `beadhive-submit`, `beadhive-merge`, and
`beadhive-backfill`; derive actor from the trusted seat rather than caller text. Provenance has no
published schema or certified HTTP capability, its payload is opaque, issue deletion cascades,
and concurrent cross-replica inserts are not auto-resolved. It cannot replace field-mutation
events, validation attestations, branch reachability, exact-SHA review gates, or actor
authorization.

### Implementation molecule: provenance compatibility pilot

1. Define and fixture-test Beadhive's closed source/kind/ref/payload profile and CLI envelope.
2. Dual-write one-SHA submit/merge bindings to provenance and `git.commits`; keep the current
   post-land non-fatal policy while surfacing discrepancies.
3. Design a privileged historical backfill path because ordinary recording rejects the reserved
   `ingest-backfill` source; compare forward and reverse readers over complete history.
4. Pilot on one local/single-writer hive and measure federation conflicts before broadening.
5. Move readers only after parity, retaining review-SHA and reachability checks. Delete
   `git.commits` writes no earlier than support-window and rollback reconstruction are proven.

Rollback stops provenance writes and continues reading the compatibility metadata. This pilot is
not blocked on `bd serve`; its initial transport is the retained CLI path.

## 5. Native sync outcome — Narrow GO for no-push replicas; primary NO-GO

Adopt a typed configured-remote sync result that preserves exits 0--4, `status`, attempt history,
positive conflict evidence, blocked-state repair counts, pull/push flags, and dirty-graph
evidence. Exit 0 includes `no-remote`, `disabled`, and intentionally unpushed outcomes, so callers
must inspect status and flags. Exit 2 requires operator conflict resolution, exit 3 is bounded
retry exhaustion, and exit 4 is a stuck/invalid dirty graph requiring intervention.

On a verified follower or mirror with no-push policy, native sync can replace bespoke pull,
conflict inspection, and blocked-state recomputation. On a future compatible primary it can
collapse the live pull-plus-push path from two remote-state CLI calls to one and own bounded race
retries. It may not publish from today's primary.

The blocker is structural: `bd sync` pushes internally and may retry three times. Wrapping the
whole call with one `host_fence.reserve_managed_push` / `verify_managed_push`
preflight/postflight pair does not put the
`refs/bh/epoch` token immediately before each data push. Another host can take authority during
the enlarged window; postflight can report stale publication but cannot prevent it. Primary GO
requires an upstream per-push fence callback/token, a pull/reconcile-without-push mode followed by
the existing fenced publisher without losing retry correctness, or an atomic remote update of
the data and epoch refs.

### Implementation molecule: typed follower reconciliation

1. Add a typed configured-remote sync outcome to the `Engine` boundary with fixtures for all five
   exits and every benign exit-0 status.
2. Prove and enforce no-push posture, then activate only for followers/mirrors.
3. Measure whether JSON mode preserves the current `auto-merged` notice; retain notice extraction
   until an equivalent typed signal is proven.
4. Preserve target/HQ selection, dry-run/recent-touch context, code-branch pushes, peer-town
   federation, force/break-glass policy, and operator conflict handling.
5. Specify/test an upstream fence-interposition seam. File a separate primary-activation decision
   only after the exact internal push boundary is fence-compatible.

The primary NO-GO keeps `_pull_dolt_state`, `_push_dolt_state`, `_DOLT_PUSHABLE`,
`host_fence.reserve_managed_push`, `host_fence.verify_managed_push`, and their retry/divergence
policy for now. Once the fence prerequisite is proven, the replacement may delete separate
configured-remote pull/push calls, optimistic diverged-then-push behavior, shell race retries,
and stderr-only classification. It must still retain `guard_primary`, the host lease/adopt epoch,
code-branch publication, dry-run, peer sync, and the managed epoch fence itself.

## Rollout order and compatibility gates

1. Keep the v1.3.0 binary commit and captured contracts pinned. Land strict canonical schema and
   typed command/error boundaries first.
2. Land brief/brief-deps, count, and row ceilings per audited call site. Measure payload, parse
   time, and token estimate; do not alter public payloads.
3. Land claim/CAS guards, then heartbeat supervision, then local reclaim. Preserve all Beadhive
   seat, review, batch, molecule, holder, and host fences during every stage.
4. Pilot provenance under dual write. Do not remove compatibility metadata while any supported
   reader depends on it.
5. Land typed sync outcomes and follower-only activation. Keep primary publication unchanged
   until a separate fence-compatibility proof.

Every stage is independently disableable. No stage may infer success from missing fields, empty
stdout, exit 0 alone, an assignee string alone, or a whole-call sync wrapper. Exact fixtures and
dual-read/dual-write comparisons gate removal of old machinery.

## Summarized close verdict

**Independent narrow GO:** contract-first canonical schema; audited brief/count/row-cap reads;
native single-claim/CAS plus supervised heartbeat and replica-local reclaim; dual-written
single-SHA provenance; and typed no-push sync reconciliation. **NO-GO for now:** operational claim
pools, unproven `claimNext`/batch atomics, deleting `git.commits` or review audit, and native sync
as the primary publisher without per-push epoch-fence interposition. All Beadhive seat, exact-SHA
review, batch/molecule, holder-incarnation, host epoch, and managed publication invariants remain.
