# Lifecycle-owned Agent and Herdr Space implementation decision

> Status: **GO, refreshed** (2026-08-30). This decision incorporates the amended `bh-4bhs7.4`
> managed-seat proof run in addition to spike epic `bh-2m1yw`.
> Core launch transactions, worktree-scoped Herdr allocation/recovery, and authoritative teardown
> are feasible, and the required Claude-and-Codex managed transport is now proved. Installed Herdr
> `0.8.2` terminates live Agents across server restart; the amended contract treats that as
> authoritative process loss and requires a fresh fenced generation without completing work.

## Decision

The overall implementation verdict is **GO**.

The exact-seat implementation now supplies canonical Claude and Codex authority, and all six
installed transport rows plus both provider process-tree cancellation shapes pass; see
[`herdr-managed-seat-matrix-2026-08-30.md`](../proof/herdr-managed-seat-matrix-2026-08-30.md).
The release gate is satisfied because Herdr server restart is explicitly an Agent-termination
boundary, not a live-process adoption boundary. Durable Beadhive work remains in progress and a
new generation is launched in the same exact seat/worktree.

The transport authority boundary is now GO: Claude uses the exact scoped `--agent` seat, Codex uses
provider-native developer instructions, and receipts plus `BH_ROLE` remain evidence rather than
authority. Herdr launches and reaps both provider process trees. Two limits remain explicit:

1. native Claude Task and Codex collaboration children expose neither the complete launch inputs
   nor a launcher-owned lifecycle handle, so they remain unmanaged; and
2. installed Herdr `0.8.2` preserves topology but not the live provider process across either
   graceful server stop or server crash.

Both limits are accepted policy: native children remain unmanaged, and Herdr server/session loss
requires relaunch rather than process adoption. Neither blocks managed external Agents.

## Accepted architecture

The contracts below are the released baseline and must not be weakened.

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

Under this GO decision:

| Route | Policy | Evidence status |
| --- | --- | --- |
| Launcher-owned external Claude planner | Managed route | Exact beadless seat and receipt observed. |
| Launcher-owned external Claude developer/dispatcher | Managed route | Installed scoped-seat executions pass. |
| Launcher-owned external Codex seats | Managed route | Installed developer-instruction seat executions pass. |
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
| Claude managed external transport | GO | Hermetic plus installed developer/dispatcher/planner executions proved exact scoped seat, cwd, receipt, identity, and Herdr-owned cancellation | Preserve plugin-scoped `--agent` projection and disclosure-approved installed probes. |
| Codex managed external transport | GO | Hermetic plus installed developer/dispatcher/planner executions proved developer-level seat authority, cwd, receipt, identity, and Herdr-owned cancellation | Preserve allowlisted `developer_instructions` projection and disclosure-approved installed probes. |
| Native child interception | NO-GO as managed; accepted unmanaged | Callable surfaces lack the required controls/handle | No proof required while policy stays unmanaged; a future managed claim requires all four controls. |
| Herdr Space/tab allocation and recovery | GO | Exact installed server stop and crash probes prove authoritative Agent loss; hermetic recovery advances the fenced generation and relaunches without completing work | Preserve loss detection and fresh-generation relaunch; do not claim live-process survival. |
| Authoritative completion and safe teardown | GO | 343 focused tests plus the frozen saga scenario matrix | Preserve authoritative producers, post-stop final lease verification, exact close, and one-target safe cleanup. |

## Restart and release gate

All six external seat rows pass. Installed Herdr `0.8.2` terminates the managed Agent on both
graceful server shutdown and server crash. Beadhive treats restored topology as evidence only:
the old generation is missing, work remains in progress, and recovery creates generation `N+1`
with the same canonical seat and worktree. Beadhive/plugin/client restart may still adopt an exact
live matching generation. No upstream Herdr process-adoption enhancement is required.

## Sources

- [`bh-2m1yw.1`](../spikes/bh-2m1yw.1-core-launch-transaction.md): core transaction **GO**.
- [`bh-2m1yw.2`](../spikes/bh-2m1yw.2-managed-claude-codex-transport.md): managed transport
  **NO-GO**.
- [`bh-2m1yw.3`](../spikes/bh-2m1yw.3-herdr-space-tab-recovery.md): Herdr allocation/recovery
  **GO**.
- [`bh-2m1yw.4`](../spikes/bh-2m1yw.4-authoritative-completion-safe-teardown.md): teardown
  **GO**.
