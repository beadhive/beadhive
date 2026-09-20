# Spike `bh-wl4jh.2` — native sync versus Beadhive federation and epoch fencing

**Bead:** `bh-wl4jh.2` · **Seat:** `dev/codex-wl4jh-2` · **Type:** research-only
(no product code)

**Feeds decision on:** `bh-wl4jh.4`, and through it `bh-6p2j`

## Question

Can Beads v1.3.0's `bd sync` replace Beadhive's remote-state choreography without weakening
the multi-host primary and epoch-fence contract?

This spike is correctness-weighted. In the molecule's impact order it ranks below the service
API and typed schema, payload/token reduction, and atomic lifecycle operations. Its iteration
speed opportunity is still concrete: for a pull-and-push run, one native call replaces two
remote-transfer subprocess calls and owns the retry loop, but only if Beadhive can preserve its
publication fence.

## Method and evidence boundary

The upstream source and tests were inspected at the exact release commit
[`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`](https://github.com/gastownhall/beads/tree/f45b249ce6b40ba62aecc03949e6371e8f7c79d8).
The installed release binary reports `1.3.0 (f45b249ce: HEAD@f45b249ce6b4)`. Its help contract
and the pinned `cmd/bd/sync.go`, `cmd/bd/sync_test.go`,
`internal/storage/issueops/blocked_consistency.go`, and
`docs/multi-agent/bucket-federation.md` were compared with:

- `src/beadhive/sync_remote.py` and `src/beadhive/engine.py`;
- `src/beadhive/host_fence.py`, `src/beadhive/guard.py`, and the claim-epoch record;
- `docs/BEADS-SYNC.md` and `docs/design/multi-host-model-adr.md`.

An isolated throwaway database exercised the installed binary's benign and hard-error edges:

| probe | observed result |
|---|---|
| default remote absent, `bd sync --json --no-adopt` | exit 0; `status=no-remote`, `attempts=0`, `rows_corrected=0`, `pushed=false` |
| explicit nonexistent remote, `bd sync --remote missing --json` | exit 1; standard JSON error envelope naming the failed pull |

The pinned upstream fault-injection harness supplies deterministic pull, conflict, recompute,
and push functions to `runSyncLoop`. The exact cases used below are
`TestRunSyncLoopConflictDespiteSuccessfulPull`, `TestRunSyncLoopMergeCapturedConflicts`,
`TestRunSyncLoopPushRaceRetriesAndSucceeds`, `TestRunSyncLoopRetriesExhausted`,
`TestRunSyncLoopDirtyGraphRecomputeRetriesAndSucceeds`,
`TestRunSyncLoopConstraintViolationEscalatesOnAttemptOne`, and `TestClassifyDirtyProgress`.
This avoids manufacturing a live conflict in any managed hive.

## Impact ranking

| rank | opportunity | finding from this spike |
|---|---|---|
| **1 — service API + typed schema** | remove process and contract drift | owned by `bh-ie41e` and `bh-wl4jh.3`; `bd sync` is deliberately not an HTTP operation, so this path remains a CLI fallback |
| **2 — token/payload reduction** | brief projections and bounded rows | owned by `bh-wl4jh.3`; sync's structured outcome prevents stderr parsing but does not materially reduce agent context by itself |
| **3 — atomic lifecycle operations** | collapse read/claim/release windows | owned primarily by `bh-wl4jh.1`; native sync similarly collapses pull/check/recompute/push, but it is a workflow atomicity improvement, not a transaction across the remote |
| **4 — sync/federation correctness** | positive conflict detection, derived-state repair, bounded race retries | **strong native correctness gain**, but adoption is blocked on interposing Beadhive's epoch fence at every internal push |

For a live `--pull --push` hive, Beadhive currently invokes `bd dolt pull` and `bd dolt push`
separately. Native `bd sync` makes that **2 remote-state CLI operations -> 1** (50% fewer) and
keeps up to three push-race retries inside that operation. The default push-only fleet sweep
does not get that reduction. Target discovery, code-branch pushes, safety assessment, dry-run,
and epoch-fence calls are outside this count and remain necessary.

## Exact native outcome contract

`bd sync [--remote NAME] [--attempts N]` performs, on every attempt:

1. pull;
2. positively inspect both conflicts captured by the merge and live `dolt_conflicts` rows;
3. run the full `RecomputeAllBlocked` repair unconditionally;
4. push, retrying only typed/recognized fast-forward races and transient dirty-graph cases.

It does not infer conflicts from pull success or failure. A successful pull can leave live
conflicts; a failed SQL-route merge can capture conflicts and then abort back to a clean working
set. The two sources are unioned. An unresolved conflict stops before recompute and push, and
there is no `ours`/`theirs` switch on this command.

| exit | JSON `status` | exact meaning | automation response |
|---:|---|---|---|
| 0 | `ok`, `no-remote`, or `disabled` | synced, benign no-op, no remote, local-only, or `no-push` mirror completed its pull/repair | continue; inspect `pushed` / `push_skipped`, not exit alone |
| 1 | standard `{"error": ...}` envelope | transport, auth, storage, conflict-inspection, non-race push, or non-dirty recompute failure | retry with normal incident policy; repeated failures alert |
| 2 | `conflict` | conflict positively observed; recompute and push did not run | stop and require an operator |
| 3 | `retries-exhausted` | bounded attempts lost to push/pull race or a concurrently dirty graph | retry on the next scheduled tick |
| 4 | `dirty-stuck` | dirty graph is proven non-transient by constraint violations, or the same fingerprint survived three exhausted runs | page an operator; later ticks cannot publish until repaired |

Non-error JSON also carries `attempts`, `conflicts`, `conflicts_preexisting`, `conflicts_live`,
`rows_corrected`, `pulled`, `pushed`, `push_skipped`, final-step errors, every transient attempt,
and dirty-graph evidence. The installed binary additionally emits `schema_version: 1`.
Consumers must branch on exit plus `status`; exit 0 alone includes intentionally unpushed states.

### Fault-injection matrix

| injected condition | source evidence | invariant demonstrated |
|---|---|---|
| live conflict exists before pull | `TestRunSyncLoopPreflightConflictHaltsBeforePull` | zero pull, recompute, or push; exit 2 path |
| pull returns success while `dolt_conflicts` names `issues` / `dependencies` | `TestRunSyncLoopConflictDespiteSuccessfulPull` | conflict is positive data, not an exit-code guess |
| SQL settle pass captures conflict then aborts, leaving no live rows | `TestRunSyncLoopMergeCapturedConflicts` | captured rows still produce exit 2; `conflicts_live=false` is meaningful |
| transport error with no conflict evidence | `TestRunSyncLoopPullErrorWithoutConflicts` | hard exit 1; no misleading conflict classification or retry |
| first push loses non-fast-forward, second succeeds | `TestRunSyncLoopPushRaceRetriesAndSucceeds` | re-pull, recompute again, then publish; no shell retry choreography |
| every push loses the race | `TestRunSyncLoopRetriesExhausted` | bounded attempts and exit 3; never an unbounded busy loop |
| recompute sees a concurrent dirty graph, then clears | `TestRunSyncLoopDirtyGraphRecomputeRetriesAndSucceeds` | dirty writer is transient; next attempt recomputes and pushes |
| dirty graph has a constraint violation | `TestRunSyncLoopConstraintViolationEscalatesOnAttemptOne` | immediate exit 4 based on positive evidence |
| identical dirty fingerprint survives consecutive runs | `TestClassifyDirtyProgress` | third exhausted run promotes 3 -> 4; changed evidence resets the counter |

`is_blocked` repair is load-bearing. It is denormalized state used by readiness queries. It must
run even when pull advances nothing because a human may have resolved a prior conflict between
ticks, and gating repair on HEAD movement would leave that resolved database permanently stale.

The exit-4 marker is local scratch at `.beads/sync-state.json`, not replicated state. That is
correct for diagnosing whether this replica's working set is stuck. It is not a fleet-wide
lease, fence, or consensus record.

## Conflict inspection and resolution

The safe boundary is narrower than upstream's generic operator examples:

- treat `conflicts`, `conflicts_live`, `discarded_pull_error`, and the exit code as the
  machine contract;
- surface which tables conflict and whether the merge is still live;
- never auto-pick a side for configured-remote sync;
- resolve through a managed Beadhive operator flow, then rerun sync so unconditional
  `is_blocked` repair occurs;
- never copy upstream examples that invoke raw `dolt push`, `dolt gc`, compaction, or flattening
  into a managed hive. Those bypass Beadhive's publication fence or violate fleet policy.

Beads still auto-settles its convergent classes beneath the loop: machine-local metadata,
audit-only dependency rows, and last-write-wins issue cells. Beadhive currently extracts
`auto-merged` notices from `bd dolt pull`. The `syncOutcome` JSON has no typed field for those
notices. The notice parser cannot be deleted until a probe proves `bd sync --json` preserves an
equivalent signal or upstream adds one; silently losing LWW visibility is not acceptable.

`bd federation sync` is a different feature: it exchanges data with named peer towns and allows
`--strategy ours|theirs`. Beadhive's `Engine.sync_state` and `bh hive sync peers` use that peer
surface. Configured-remote `bd sync` must not replace or be confused with it.

## Compatibility with the multi-host ADR

Native sync solves convergence inside one Dolt history. The ADR solves authority to publish
that history. They compose; neither replaces the other.

`bd sync` performs its push internally. A plain subprocess therefore bypasses
`host_fence.managed_push`, whose preflight CAS-reserves `refs/bh/epoch` and whose postflight
verifies that exact ticket. Wrapping the whole sync call with one preflight/postflight pair is
not equivalent to fencing each internal push: pull, recompute, and up to three retries enlarge
the CAS-to-data window, and another host can take over during it. Postflight can report
`DATA MAY HAVE LANDED`, but cannot prevent the stale publication.

Therefore native sync does **not** yet satisfy the managed publisher contract. Safe adoption
needs one of:

1. an upstream push callback / compare-and-swap token that Beadhive can apply immediately before
   every sync push;
2. an upstream pull+conflict+recompute mode that returns before push, followed by the existing
   fenced Beadhive push (without forfeiting race retry semantics); or
3. a demonstrably atomic remote update of `refs/dolt/data` and `refs/bh/epoch` through the
   transport Beads actually uses.

Until then, invoking native sync is safe only on a follower or mirror whose `no-push` posture is
already enforced and verified. It is not the primary's managed publication path.

Other ADR guarantees remain Beadhive-owned:

- `guard_primary` decides which host may mutate a hive;
- the claim record's host id + adopt epoch invalidates workers surviving a host handoff;
- the remote epoch fence linearizes publication attempts;
- Git worktree/branch review state and exact reviewed SHA remain independent channels;
- target/HQ policy and code-branch pushes are outside Beads remote sync.

### Replica worker leases

The `node_id` stored with a Beads worker lease identifies the granting **store replica**, not a
physical Beadhive host. Cross-replica state is stale by up to the sync interval, and the replica
guard correctly skips reclaiming another replica's lease unless an operator uses
`--any-replica`. Consequently:

- worker lease TTL and reclaim grace must each exceed the configured sync interval;
- all clients of one shared SQL server use one replica identity, not one per client host;
- Beadhive must never use broad `--any-replica` as routine federation recovery;
- replica leases cannot replace the host lease, adopt epoch, or remote publication fence.

## Keep / replace / delete map

| current Beadhive operation | disposition | reason / prerequisite |
|---|---|---|
| hive target resolution, HQ exclusion, remote-only classification | **keep** | fleet policy is outside one database's sync loop |
| read-only dirty-tree and unpushed-code assessment | **keep** | native sync has no dry-run and does not publish code branches |
| code-branch `git push` | **keep** | separate handoff channel from `refs/dolt/data` |
| `guard_primary`, claim-epoch check, and host lease | **keep** | authority and stale-worker fencing are orthogonal to convergence |
| `host_fence.managed_push` preflight/postflight | **keep** | native internal push cannot currently accept the fence token |
| configured-remote pull -> conflict check -> recompute -> retry loop | **replace narrowly** | delegate to `bd sync` only when the push boundary is fence-compatible, or on a verified no-push follower |
| separate `_pull_dolt_state` + `_push_dolt_state` on a live pull/push run | **delete after prerequisite** | native call gives 2 -> 1 subprocess and correct bounded retries |
| optimistic `_DOLT_PUSHABLE` treatment of `diverged` followed by a push attempt | **delete after prerequisite** | native loop re-pulls and distinguishes retryable race from hard divergence |
| stderr-only conflict/race classification | **replace** | consume exits 0–4 plus structured JSON; retain stderr only as diagnostic detail |
| `auto-merged` notice extraction | **keep pending evidence** | no equivalent typed member exists in `syncOutcome` |
| Beadhive dry-run preview and bounded recent-touch context | **keep** | `bd sync` mutates and has no read-only remote-diff mode |
| explicit `--force` break-glass push | **keep separate** | native configured-remote sync intentionally offers no choose-a-side/force switch |
| `Engine.sync_state` / `bh hive sync peers` | **keep** | peer-town federation is a distinct topology and policy |
| ad-hoc shell retry around push races | **delete with adoption** | native `--attempts` owns bounded re-pull/recompute/push retries |

## Recommendation for `bh-wl4jh.4`

**Narrow GO for the native outcome and reconciliation contract; NO-GO for replacing the current
primary publication path yet.**

Adopt in this order:

1. add a typed configured-remote sync outcome to the `Engine` boundary, preserving all five
   exits and the full non-error JSON shape;
2. use it first for verified `no-push` followers/mirrors and measure whether auto-merge notices
   survive JSON mode;
3. specify or obtain a fence-interposition seam at every internal push;
4. only then replace the primary's separate pull/push calls and delete the divergence/retry
   shell machinery.

This gives Beadhive the strongest v1.3 correctness improvements—positive conflict detection,
unconditional blocked-state repair, and bounded race handling—without mistaking convergence for
publication authority. The resulting speed win is real but secondary: one call instead of two
on pull+push paths, fewer process round trips during races, and fewer bespoke failure branches.
