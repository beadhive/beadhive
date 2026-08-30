# Lifecycle-owned Agent and Herdr Space implementation decision

> Status: **NO-GO, refreshed** (2026-08-30). This decision incorporates the `bh-4bhs7.4`
> managed-seat proof run in addition to spike epic `bh-2m1yw`.
> Core launch transactions, worktree-scoped Herdr allocation/recovery, and authoritative teardown
> are feasible. The required Claude-and-Codex managed transport is not yet proved, so no product
> implementation molecule is authorized or filed.

## Decision

The overall implementation verdict is **NO-GO**.

The exact-seat implementation now supplies canonical Claude and Codex authority, but the release
gate remains conjunctive. The refreshed installed matrix completed only the Codex developer row;
see [`herdr-managed-seat-matrix-2026-08-30.md`](../proof/herdr-managed-seat-matrix-2026-08-30.md).
The other five rows, full descendant cancellation, and restart recovery remain unproved, so the
capability is still unreleased.

Three of the four spike boundaries are implementation-ready: the Herdr-independent core launch
transaction is GO, exact-worktree Herdr Space allocation and recovery is GO, and authoritative
completion plus safe exact teardown is GO. The transport spike is NO-GO. Its failure is at the
authority boundary, not at receipt propagation or workspace selection:

1. Codex accepts model, reasoning effort, cwd, and `BH_AGENT_LAUNCH_RECEIPT`, but the current
   launcher supplies no provider-native developer, dispatcher, or planner instructions. A receipt
   and `BH_ROLE` are evidence and attribution; neither may become seat authority.
2. Claude exposes the intended scoped `--agent` transport and the beadless planner was exercised,
   but disclosure-approved external developer and dispatcher executions were not completed. The
   argv shape is plausible evidence, not an empirical lifecycle proof.
3. Native Claude Task and Codex collaboration children expose neither the complete launch inputs
   nor a launcher-owned lifecycle handle. They cannot be intercepted with the same guarantees.

The third finding does not independently block a managed implementation: native fanout may remain
explicitly unmanaged. The first two findings do block the promised supported transport matrix,
which requires exact launcher-owned Claude and Codex seats rather than a Claude-only subset or
seat claims inferred from receipts.

Do **not** run `bh:replan` on `bh-2m1yw` yet. Do not file the coordinated core or Herdr-plugin
implementation molecules until the transport prerequisites below land and this decision is
revisited with their evidence.

## Accepted architecture, held pending transport proof

The NO-GO does not reject the three successful contracts. They are the provisional input to the
later replan and must not be weakened while transport proof is completed.

### Ownership and authority

- Core owns launch identity, workspace binding, operation idempotency, authoritative completion,
  and teardown sequencing. It imports no Herdr implementation and contains no Space/tab/pane
  fields in its base schemas.
- A presentation adapter owns bounded allocation effects only. Herdr IDs and metadata are
  staleable pointers, never bead, checkout, completion, or cleanup authority.
- V1 presentation ownership is exactly one Space per stable exact worktree and one named tab per
  independently managed Agent. An explicit cooperative split may add panes inside that Agent's
  tab; it is not the default placement.
- Local durable state and generation fences remain authoritative across retries and cold restart.
  Labels and cwd are correlation evidence. Foreign, partial, ambiguous, stale-generation, or
  unresolvable inventory is a refusal without mutation.
- Presentation state, process exit, idle, `done`, a missing pane, labels, cwd, and heartbeats never
  mint lifecycle completion. Managed completion comes from a fresh exact closed-bead record;
  beadless completion comes from an explicit launcher/controller `complete` or `cancel` command.

### Core launch and workspace contract

The accepted core boundary is prepare -> adapter commit -> core commit/abort. It uses caller-minted
`launch_id` and retry-scoped `operation_id`, persists local transaction state under git-common
private state, and makes retries byte-equivalent by `(launch_id, phase, operation_id)` plus a
canonical input digest. Conflicting reuse is `operation_conflict`; a conflicting terminal
transition is `terminal_state_conflict`.

The later implementation must carry forward the five strict, frozen, versioned schema shapes
specified by `bh-2m1yw.1`: `WorkspaceBindingV1`, `PreparedLaunchV1`,
`AdapterCommitResultV1`, `LaunchReceiptV1`, and `AbortReceiptV1`. Workspace bindings distinguish
`managed_bead`, `beadless_seat`, `batch`, `epic_container`, and `shared_checkout`; retain the
requested child bead separately from the resolved batch or epic checkout. Portable records use an
opaque stable `worktree_id` and exact branch, never host paths, credentials, raw argv, provider
continuation IDs, or cleanup capabilities. `parent_launch_id` is core lineage and is never
replaced by provider identity.

These schema names and semantics are accepted evidence, but this NO-GO does not publish them as
an implementation baseline. The replan must freeze their exact serialized JSON fixtures only
after the transport prerequisites establish the final supported harness projection.

### Herdr placement and recovery contract

The later Herdr adapter must derive stable `scope_key` and `agent_key` values from hive identity
and stable Git worktree identity; persist fsynced `creating | active | failed | lost | closed`
intent and a monotonic lifecycle generation in git-common private state; and serialize allocate,
reconcile, compensate, and close beneath a per-scope cross-process lock.

Each mutation requires an exact reread of Space, tab, pane, cwd, ownership token, and generation.
A duplicate operation returns its stored result. Failed start compensates only objects recorded in
`created_by_this_operation`, newest child first. Restart reconciliation may adopt an empty
same-cwd create-window Space only when the durable intent proves that exact operation could have
created it. It never adopts or deletes arbitrary same-label or same-cwd content. Exact close is
generation-fenced; empty-Space close requires a final ownership and emptiness reread.

### Completion and teardown contract

The accepted teardown saga is durable and retryable:

```text
awaiting_authoritative_completion -> completion_accepted -> stopping_agents
  -> agents_stopped -> lease_verified -> space_closed -> cleanup_classified -> complete
effect/refusal failure -> cleanup_blocked (exact retained IDs, resumable)
```

The physical order is fixed: stop every Agent and live descendant bound to the exact worktree;
perform a final generation-fenced lease read; close only the exact Space; then refresh and classify
only that exact worktree and run one-target cleanup if it is `SAFE` or `LANDED_REBASED`. Main or
shared checkout, dirty, unmerged, unknown, foreign, stale-generation, or reference-bearing cases
remain retained. The lifecycle path must never invoke broad prune and must never offer automatic
force cleanup.

## Compatibility and transport policy

Until a later GO supersedes this decision:

| Route | Policy | Evidence status |
| --- | --- | --- |
| Launcher-owned external Claude planner | Candidate managed route | Exact beadless seat and receipt observed. |
| Launcher-owned external Claude developer/dispatcher | Unsupported pending proof | Scoped argv exists; real approved executions are missing. |
| Launcher-owned external Codex seats | Unsupported pending capability and proof | Model, effort, cwd, and receipt transport; no seat-authoritative instructions. |
| Native Claude Task children | Unmanaged compatibility only | No callable bridge with cwd, environment/receipt, exact identity, and lifecycle handle. |
| Native Codex collaboration children | Unmanaged compatibility only | Spawn surface lacks cwd, environment, Beadhive identity, and launcher-owned process handle. |

Native fanout must scrub inherited managed receipts at its boundary and must not be adopted into a
managed Space, operation ledger, or teardown saga. Provider UI attribution and transcript labels
remain observational. A future public bridge may be evaluated separately; native interception is
not required to lift this NO-GO if it remains explicitly unsupported.

## Proof matrix

| Contract | Verdict | Evidence | Required before implementation filing |
| --- | --- | --- | --- |
| Core prepare/commit/abort and all workspace kinds | GO | 48 focused tests; strict profile, worktree, parent, batch/epic, and private-path probes | Preserve the five provisional schema semantics and add conformance fixtures during replan. |
| Claude managed external transport | NO-GO | Planner invocation and receipt observed; developer/dispatcher argv only | Approved hermetic developer and dispatcher executions proving exact scoped agent, cwd, profile/receipt, completion handle, and cancellation/reap. |
| Codex managed external transport | NO-GO | Model, effort, cwd, and receipt observed; exact seat rejected | Provider-native allowlisted seat-instruction transport plus developer, dispatcher, and beadless planner execution proofs. |
| Native child interception | NO-GO as managed; accepted unmanaged | Callable surfaces lack the required controls/handle | No proof required while policy stays unmanaged; a future managed claim requires all four controls. |
| Herdr Space/tab allocation and recovery | GO | Herdr 0.8.2 live probe plus 8 recovery cases | Preserve worktree Space, named Agent tab, local intent/lock/generation, exact rereads, and foreign-content refusal. |
| Authoritative completion and safe teardown | GO | 343 focused tests plus the frozen saga scenario matrix | Preserve authoritative producers, post-stop final lease verification, exact close, and one-target safe cleanup. |

## Deferred prerequisites and re-entry gate

No existing backlog bead was found for either blocking transport item. Planning must first create
and land narrowly scoped deferred proof work for:

1. a Codex seat-authority capability, using an allowlisted generated instruction/profile artifact
   or another provider-native mechanism that bakes the developer, dispatcher, and planner duties
   independently of environment receipt contents;
2. a Codex empirical matrix exercising all three seats with exact cwd, model, effort, redacted
   receipt observation, launcher-owned completion, and cancellation/reap;
3. disclosure-approved hermetic Claude developer and dispatcher probes exercising the scoped
   `--agent` load, exact worktree, redacted receipt, lifecycle handle, and cancellation/reap; and
4. a refreshed cross-provider transport decision showing both external harnesses meet the same
   authority and lifecycle threshold while native routes remain explicitly unmanaged.

Only after those proofs land with GO verdicts should the planning seat run `bh:replan` on epic
`bh-2m1yw`. That replan must file two coordinated implementation molecules: one for core
workspace/launch/completion/teardown schemas and ledgers, and one for the Herdr plugin's
Space/tab allocation, recovery, generation-fenced stop/close, and conformance adapter. The core
molecule must remain independently testable with a fake adapter; the plugin molecule consumes the
versioned port and may not become lifecycle authority.

## Sources

- [`bh-2m1yw.1`](../spikes/bh-2m1yw.1-core-launch-transaction.md): core transaction **GO**.
- [`bh-2m1yw.2`](../spikes/bh-2m1yw.2-managed-claude-codex-transport.md): managed transport
  **NO-GO**.
- [`bh-2m1yw.3`](../spikes/bh-2m1yw.3-herdr-space-tab-recovery.md): Herdr allocation/recovery
  **GO**.
- [`bh-2m1yw.4`](../spikes/bh-2m1yw.4-authoritative-completion-safe-teardown.md): teardown
  **GO**.
