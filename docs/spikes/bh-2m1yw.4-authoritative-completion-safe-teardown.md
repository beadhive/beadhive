# Spike `bh-2m1yw.4` — authoritative completion and exact safe teardown

**Baseline:** `9178568` · **Run date:** 2026-08-30 · **Scope:** evidence only

## Question

Can launcher-owned Agent lifecycles end at one fail-closed, retryable boundary that starts only
from authoritative managed-bead or explicit beadless completion, stops every Agent bound to one
exact worktree, verifies the final allocation lease, closes its exact Herdr Space, and removes only
that exact worktree when the existing classifier says it is safe?

This is not asking whether idle, a provider process exit, Herdr `done`, a pane disappearing, a
label, or a matching cwd is a useful observation. They are useful observations, but the question
is whether any of them can authorize destructive teardown. Nor does the spike authorize a broad
worktree prune or product-code changes.

## Method

The spike traced the already-landed ownership and cleanup seams and ran their focused tests:

- `herdr_plugin._work_observation` was inspected to locate the source of terminal work facts and
  to distinguish them from Herdr lifecycle presentation.
- receipt-bound reap, exact pane ownership, roster revision, retained-resource, and retry behavior
  were inspected in `herdr_plugin.py` and `herdr-lifecycle-receipt-v1.schema.json`.
- `wt_status.classify` and `worktree_cleanup` were inspected for the shared SAFE decision and its
  removal effect. The latter's fleet-wide `prune` shape was treated as reusable mechanics, not as
  the orchestration API.
- Existing tests were executed for terminal projection, exact/foreign pane cleanup, idempotent
  reap, failed cleanup retention, live verify descendants, and every relevant worktree class.
- The matrices below were then applied as executable specification pseudocode for the missing
  core teardown saga. No product source was added.

Focused evidence command:

```sh
uv run pytest -q \
  tests/test_herdr_plugin.py \
  tests/test_worktree.py \
  tests/test_worktree_inventory_json.py
```

Recorded result: **343 passed in 68.00s**.

The repository gate is `just check`.

## Evidence

### 1. Completion authority is available and is not presentation state

`src/beadhive/herdr_plugin.py:1441-1447` says explicitly that terminal work is never inferred from
Herdr terminal/idle presentation. For managed work, the projection reads the exact bead and maps
only `status == "closed"` to terminal/complete (`:1482-1487`). Review and merge labels remain
non-terminal (`:1488-1495`). This gives one existing managed-bead authority: a fresh, successful
read of the exact bead whose durable status is `closed`, correlated to the launch binding and its
claim generation. A close event may wake the saga, but the fresh read is the authorization.

The frozen producers are therefore:

| Binding | Authoritative producer | Required terminal proof |
| --- | --- | --- |
| `managed_bead`, including batch child and epic | Beadhive work lifecycle (`bh work merge` / `finish`, or another convention-owned closure path) | Fresh exact-bead record is `closed`; binding digest, requested bead, resolved worktree ID, terminal operation ID, and claim/launch generation agree. Every managed bead bound to the worktree is terminal before physical teardown. |
| `beadless_seat` | The user-facing launcher/controller that accepted the original launch | An explicit `complete` or `cancel` command carrying the launch ID, caller-minted completion operation ID, expected binding digest, and expected generation. It is persisted by core before effects. |

No provider, terminal emulator, shell, pane, label writer, cwd observer, heartbeat expiry, or OS
reaper is a completion producer. For a managed bead, an explicit user request may ask the work
lifecycle to close/cancel it, but cannot bypass the resulting authoritative bead state. For a
beadless seat, passive disappearance is not equivalent to the explicit command.

### 2. Idle and process exit cannot authorize teardown

Herdr's idle state is positively required for a newly spawned Agent to be dispatchable
(`herdr_plugin.py:2471-2522`); treating the same state as completion would destroy a successfully
launched, ready Agent. Conversely, terminal or blocked Agents can be receipt-reaped only because
exact plugin metadata, target, and receipt pane still agree (`:2534-2584`), not because their
lifecycle state is terminal. `_work_observation` independently sources work completion from the
bead record. Thus `idle`, `done`, blocked, shell/PTY exit, missing PID, absent pane, and stale
heartbeat have no transition into `completion_accepted`.

The negative transition rule is executable as:

```python
@pytest.mark.parametrize("observation", [
    "agent_idle", "herdr_done", "pane_exit", "provider_exit", "heartbeat_expired",
    "terminal_label", "cwd_matches",
])
def test_observation_never_mints_completion(saga, observation):
    saga.observe(observation)
    assert saga.phase == "awaiting_authoritative_completion"
    assert saga.effects == []
```

### 3. Exact ownership and generation already fail closed

Roster ownership requires explicit metadata, exact target, unique pane, exact workspace and pane
names, exact cwd, and an extant managed worktree (`herdr_plugin.py:1286-1354`). Receipt cleanup
returns an idempotent `already_reaped` only when target, correlated Agent, and raw pane are all
absent; partial or duplicate matches refuse (`:2534-2565`). A reap failure returns a retryable
error while naming the exact retained target and pane (`:2923-3034`), and successful reap carries
the source roster revision required by the v1 schema
(`docs/schemas/herdr-lifecycle-receipt-v1.schema.json:81-95`).

The implementation contract must strengthen `source_revision` into the allocation's monotonic
`generation`: every effect request carries `(space_id, allocation_id, generation, binding_digest,
operation_id)`. A stale generation is a refusal, never an invitation to rediscover by name or cwd.

### 4. One exact worktree can be classified safely

`wt_status` is pure and shared by status and prune (`src/beadhive/wt_status.py:1-15`). Its only
removable classes are `SAFE` (closed + merged + clean) and `LANDED_REBASED` (closed + clean +
content independently confirmed in the parent), while dirty, unmerged, active, unknown, detached,
beadless/batch abandoned, and merged orphan states remain unsafe (`:24-92`, `:281-355`). UNKNOWN,
including UNKNOWN masked by DIRTY, invalidates the removal basis (`:364-378`).

The existing effect `_prune_remove_one` removes one already-classified target and its claim record,
then removes its now-disposable branch (`worktree_cleanup.py:307-353`). The broad `prune` caller
classifies and iterates a fleet set (`:383-449`), so lifecycle teardown must not call it. The
required port is `cleanup_one_if_safe(binding, expected_generation, operation_id)`: refresh facts,
locate exactly one worktree by stable binding/worktree identity, reject main/shared, classify only
that target using the shared classifier, revalidate references, then apply the existing one-row
effect. It returns a receipt; it never scans a SAFE set looking for extra work.

### 5. Frozen teardown saga and physical order

The durable record is keyed by `(launch_id, completion_operation_id)` and stores the canonical
input digest, exact binding, allocation generation, per-Agent stop receipts, final lease receipt,
Space-close receipt, cleanup receipt, retained resources, and last error. Replaying the same key
and digest resumes at the first incomplete phase; a different digest is `operation_conflict`.

```text
awaiting_authoritative_completion
  -> completion_accepted                 # persist proof before effects
  -> stopping_agents                     # stop each exact Agent; wait/reap live descendants
  -> agents_stopped                      # all bound Agents absent, including shared-worktree peers
  -> lease_verified                      # FINAL fresh lease/allocation/generation/reference read
  -> space_closed                        # close exact Space; already absent is success
  -> cleanup_classified                  # fresh one-target classifier + reference check
  -> complete                            # exact if-safe cleanup succeeded/already absent

Any refusal/effect failure -> cleanup_blocked (durable, retryable, retains exact remaining IDs)
Retry -> repeat final verification for the next effect; never replay a completed effect blindly.
```

The physical order is immutable:

1. **Agent stop:** request stop for every Agent whose launch binding names the exact worktree,
   including two peer Agents and their live descendants; confirm each process tree is absent.
2. **Final lease verification:** after all stops, atomically re-read the exact allocation and
   require the expected Space, allocation ID, generation, binding digest, zero bound/live Agents,
   zero descendants, and zero foreign panes/references. Any stale/unknown/foreign fact blocks.
3. **Space close:** close that exact Space with the verified generation. Never infer a Space from
   a label, cwd, current focus, or hive name. Already absent succeeds only under the same receipt.
4. **One-target if-safe cleanup:** refresh Git/bead facts, reject main/shared or any reference to
   the target, classify the exact worktree, and remove it only for SAFE/LANDED_REBASED. Do not run
   broad prune. Delete its branch only according to the existing safe-removal contract.

Space closure cannot precede Agent stop because it can orphan live processes; worktree cleanup
cannot precede Space closure because panes/processes may retain the cwd; and the final lease check
cannot be reused from before stop because ownership may change during the stop interval.

### 6. Required scenario matrix

| Scenario | Authoritative result and permitted effects |
| --- | --- |
| Two Agents share one worktree | Completing one Agent stops neither Space nor worktree. Once the binding's managed work set is terminal (or beadless launch explicitly completed), stop **both** Agents, then proceed only after both and their descendants are absent. |
| Live descendants | Stop stays incomplete while any process in an Agent-owned process tree lives. No lease verification, Space close, or Git cleanup runs. |
| Foreign pane or reference | Final lease/reference verification refuses. Preserve the Space and worktree in `cleanup_blocked`; never close the foreign pane. |
| Main/shared checkout | `cleanup_one_if_safe` is a permanent physical-cleanup refusal. Agent stop may complete, but Space/worktree teardown is retained or the shared presentation is handled by an explicit non-destructive policy. |
| Clean landed worktree | Exact closed + merged + clean, or closed + independently landed-rebased + clean, is eligible after lifecycle checks. Remove this target only. |
| Dirty worktree | DIRTY blocks even if its bead is closed/merged. Retain it and name dirty status in the receipt. |
| Closed but unmerged | UNMERGED is a work-loss signal. Retain Space/worktree recovery state; no force removal. |
| Unknown/unresolvable bead or binding | Refuse before destructive effects. UNKNOWN masked by DIRTY is still untrustworthy. |
| Stale allocation generation | Refuse the effect and retain exact IDs. A retry must obtain a new authoritative instruction; it must not silently substitute the current generation. |
| Stop succeeds, Space close fails | Persist Agent-stop receipts and `cleanup_blocked` with Space/worktree retained. Retry re-verifies the final lease and retries exact Space close; it does not re-stop absent Agents destructively. |
| Space close succeeds, Git cleanup fails | Persist Space-close receipt and the exact worktree as retained. Retry refreshes references/classification and attempts only one-target cleanup. |
| Retry after full success | Return the stored `complete` receipt byte-equivalently; no mutation. |

Executable specification for the high-risk sharing/failure paths:

```python
def test_two_agents_and_descendant_gate_space(saga, allocation):
    allocation.bind("A1", descendants=0)
    allocation.bind("A2", descendants=1)
    saga.complete(authoritative=True)
    assert saga.phase == "cleanup_blocked"
    assert allocation.space_close_calls == 0
    allocation.confirm_tree_absent("A2")
    saga.retry()
    assert allocation.events == [
        "stop:A1", "stop:A2", "verify-final", "close-space", "cleanup-one-if-safe"
    ]

def test_partial_failure_resumes_without_broad_cleanup(saga, allocation, cleaner):
    allocation.fail_close_once()
    first = saga.complete(authoritative=True, operation_id="C1")
    assert first.state == "cleanup_blocked" and cleaner.calls == []
    receipt = saga.complete(authoritative=True, operation_id="C1")
    assert receipt.state == "complete"
    assert cleaner.calls == [saga.binding.worktree_id]
    assert allocation.stop_calls_per_agent == {"A1": 1, "A2": 1}
```

## Verdict — **GO**

The boundary is feasible. Beadhive already projects durable closed-bead completion without
inferring it from Herdr state, Herdr already proves exact receipt-bound ownership and idempotent
absence with retained-resource failures, and the worktree layer already has a pure fail-closed
SAFE classifier plus a one-row removal effect. The missing product work is orchestration and
versioned receipts: an authoritative completion ledger, generation-fenced allocation port, and
one-target cleanup port.

GO is conditional on retaining the distinctions above. In particular, `idle`/process exit must
never mint completion, the final lease read must occur after every Agent process tree is gone,
main/shared and reference-bearing resources must remain non-destructive, and no lifecycle path may
invoke broad prune.

## Recommendation

Replan implementation into three independently testable pieces: (1) a core completion ledger with
separate managed-bead and explicit beadless producers, (2) a generation-fenced teardown saga whose
adapter port implements exact Agent stop and Space close, and (3) `cleanup_one_if_safe`, reusing
the existing classifier and one-row effect while adding main/shared and reference guards. Give the
saga crash-point tests between every phase and conformance tests for every scenario row above.

Keep `cleanup_blocked` as durable recovery state, surfaced with exact retained resource IDs and a
retryable/non-retryable reason. Never offer an automatic force path: dirty, unmerged, unknown,
foreign, stale-generation, or reference-bearing cases require recovery or a new explicit operator
decision outside this completion operation.
