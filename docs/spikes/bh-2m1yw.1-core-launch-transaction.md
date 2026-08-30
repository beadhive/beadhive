# Spike `bh-2m1yw.1` — core launch prepare, commit, and abort

**Baseline:** `1dc09c7` · **Run date:** 2026-08-30 · **Scope:** evidence only

## Question

Can Beadhive core freeze one Herdr-independent transaction that validates an
`AgentLaunchProfile` v1, resolves or claims exactly one checkout, gives a presentation adapter a
bounded allocation plan, and then records either commit or abort? The answer must hold for a
managed bead, a beadless seat, a batch checkout, an epic container, and the main/shared checkout;
it must preserve parent launch identity, make retries idempotent, compensate a failed adapter, and
keep private paths out of portable receipts.

## Method

The spike inspected the landed contracts rather than designing around the current Herdr calls:

- `src/beadhive/agent_launch_profile.py` owns strict profile validation, allowlisted harness argv,
  a frozen resolved profile, and a redacted v1 receipt. Its fresh-import test actively refuses any
  `beadhive.herdr*` import.
- `src/beadhive/cli.py` resolves a profile before `_apply_role_workspace()` claims or changes the
  checkout. This establishes the required validation-before-mutation ordering.
- `src/beadhive/worktree.py` provides side-effect-free `locate`/`preview` and idempotent `ensure`.
  Bead branches, epic containers, batch branches, and session branches have distinct identities.
- `src/beadhive/worktree_git.py` derives the exact bead and integration parent from the real branch,
  including dotted child IDs and the nearest epic container.
- `src/beadhive/private_paths.py` distinguishes git-common control state, shared repo artifacts, and
  worktree-local overlays. Resolution is read-only and explicit `ensure_*` calls are the write seam.

The executable evidence command was:

```sh
uv run pytest -q \
  tests/test_agent_launch_profile.py \
  tests/test_role.py::test_cli_role_profile_resolves_before_workspace_and_is_passed_to_route \
  tests/test_work.py::test_claim_group_provisions_one_shared_worktree_and_claims_all \
  tests/test_work.py::test_claim_collapse_lands_commits_on_batch_worktree_not_coordinator_seat \
  tests/test_worktree.py::test_branch_and_leaf_bead_epic_kind \
  tests/test_worktree.py::test_bead_and_parent_resolves_container_branch_when_present \
  tests/test_worktree.py::test_bead_and_parent_returns_none_for_non_bead_worktree \
  tests/test_private_paths.py::test_repo_private_root_is_shared_and_worktree_overlay_is_distinct \
  tests/test_private_paths.py::test_explicit_worktree_overlay_creation_does_not_create_shared_root \
  tests/test_private_paths.py::test_invalid_git_metadata_is_a_non_destructive_miss
```

Recorded result: **48 passed in 5.73s**.

### Frozen transaction contract

The feasible contract is an orchestration protocol, not a Herdr data model. All shapes use
`extra="forbid"`, are frozen, and carry a literal version and namespaced type discriminator.

```text
LaunchId = caller-minted UUID/ULID
OperationId = caller-minted retry key, unique within LaunchId

WorkspaceBindingV1 (portable)
  type = "beadhive.workspace-binding"; version = "1"
  hive_id                         # provider/org/repo
  binding_kind                    # managed_bead | beadless_seat | batch |
                                  # epic_container | shared_checkout
  requested_bead?                 # child identity survives batch resolution
  batch_id?; epic_id?
  branch                          # exact ref, or integration branch for shared_checkout
  worktree_id                     # stable opaque ID derived from Git worktree identity
  parent_launch_id?               # launch lineage, never provider session identity

PreparedLaunchV1 (portable envelope plus a local capability)
  type = "beadhive.prepared-launch"; version = "1"
  launch_id; prepare_operation_id; profile_receipt; workspace_binding
  binding_digest; adapter_kind; adapter_action; adapter_plan_digest
  state = "prepared"
  local_capability                # LOCAL ONLY: root path, secrets, adapter payload/handle

AdapterCommitResultV1 (adapter -> core, portable projection)
  type = "beadhive.adapter-commit-result"; version = "1"
  launch_id; commit_operation_id; binding_digest; adapter_kind
  outcome = "committed" | "refused" | "failed"
  allocation_id?; generation?; portable_receipt?; error_code?

LaunchReceiptV1 (portable terminal success)
  type = "beadhive.launch"; version = "1"
  launch_id; prepare_operation_id; commit_operation_id
  profile_receipt; workspace_binding; adapter_kind
  allocation_id?; generation?; state = "committed"

AbortReceiptV1 (portable terminal failure)
  type = "beadhive.launch-abort"; version = "1"
  launch_id; abort_operation_id; failed_operation_id?; workspace_binding?
  reason_code; compensation = not_needed | completed | failed
  compensated_allocation_id?; state = "aborted"
```

`prepare(profile, target, launch_id, operation_id)` validates first, resolves/claims one exact
binding second, persists the prepared record in git-common private state, and returns only one
bounded adapter action: `allocate`, `reuse`, or `none`. The adapter is not imported by core; it is
selected through an injected port and receives the local capability plus the portable projection.

`commit(prepared, adapter_result, operation_id)` verifies launch ID and both digests. A committed
result freezes the portable terminal receipt. A refusal/failure never becomes committed and must
flow to `abort`. `abort(prepared, reason, operation_id, compensate)` records the original failure,
invokes at most the compensation capability returned by prepare/adapter commit, and records the
compensation result without masking the original error.

The operation ledger is keyed by `(launch_id, phase, operation_id)`. Repeating the same key and
canonical input digest returns the byte-equivalent stored result and performs no mutation.
Reusing the key with a different digest is `operation_conflict`. Once terminal, commit-after-abort
and abort-after-commit are `terminal_state_conflict`; repeating the matching terminal operation is
a read. `parent_launch_id` is copied from the incoming core receipt and is never replaced by a
harness continuation, Herdr pane ID, or other provider identity.

### Portable versus local data

Portable receipts may contain the version/type, launch and operation IDs, parent launch ID,
`hive_id`, binding kind, exact bead/batch/epic IDs, exact Git branch, opaque `worktree_id`, profile
receipt fields, adapter kind/action, allocation ID, generation/fence, digests, outcomes, and stable
error/reason codes. These values are correlation or policy facts and do not reveal the host layout.

Local records may additionally contain the absolute main/worktree/git-common/repo-private paths,
the adapter executable/socket, environment, credentials, raw argv, provider continuation IDs,
unredacted adapter response, cleanup callable/capability, logs, and exception text. None may appear
in a portable receipt. In particular, portable binding uses `worktree_id` plus the exact branch,
not `/home/...`, `$BH_WORKTREES`, `.git/worktrees/...`, or a `.bh` path. Local transaction state
belongs under `<git-common-dir>/bh/launches/<launch-id>/`; adapter artifacts use the shared
repo-private root, while a genuinely checkout-specific overlay uses `<worktree>/.bh/` only.

## Evidence

| Contract probe | Executable/recorded evidence | Result |
| --- | --- | --- |
| Managed bead | `test_managed_profile_defaults_seats_and_round_trips_without_host_fields`, exact-bead parametrization, and the CLI ordering test | Strict bead identity; profile resolves before claim; host fields are absent. |
| Beadless seat | `test_unmanaged_optional_profile_is_valid_but_unmanaged_is_not_a_value` and fresh-import probe | Optional planner/analyst launch works with no bead and no Herdr import; required seats fail closed. Its binding kind is `beadless_seat` and `locate/ensure(branch=session/...)` supplies the checkout. |
| Batch | `test_claim_group_provisions_one_shared_worktree_and_claims_all` | Every child retains its requested ID while one `wt/batch/<group>` checkout and identity are claimed. |
| Epic container | `test_branch_and_leaf_bead_epic_kind` and `test_bead_and_parent_resolves_container_branch_when_present` | Epic binds to `wt/bead/epic/<id>`; a child resolves that exact container as integration parent. |
| Batch vs epic collision | `test_claim_collapse_lands_commits_on_batch_worktree_not_coordinator_seat` | `batch-<group>` directory and `wt/batch/<group>` ref remain distinct from the epic seat/container. |
| Main/shared checkout | `test_bead_and_parent_returns_none_for_non_bead_worktree` | A non-bead checkout yields no bead and the integration branch. The frozen contract names this explicitly `shared_checkout`; prepare must refuse mutation unless the profile's seat policy permits beadless/shared use. |
| Parent launch identity | versioned core receipt tests plus the separate `seat_process_id` / provider continuation construction in `cli.py` | Core identity can be propagated independently of provider/session identities. The proposed `parent_launch_id` makes that separation explicit. |
| Private paths | three selected `test_private_paths.py` probes | Shared repo state and worktree overlay are distinct; resolution is non-destructive; absolute/private roots need not be portable. |
| Failed adapter | `test_another_harness_and_refused_capability`, `test_no_arbitrary_argv_or_unknown_fields`, and the conformance probe below | Unsupported capability and unsafe values refuse before mutation. A post-prepare adapter error must produce an abort receipt and exactly-once compensation. |
| Duplicate operation | conformance probe below | The proposed ledger rule is deterministic and implementable with the existing git-private ownership/fencing pattern. |

The two transaction-specific probes are specifications for the implementation molecule (there is
intentionally no product implementation in this spike), and are executable pytest pseudocode:

```python
def test_duplicate_operation_is_a_read(core, adapter, request):
    first = core.prepare(request, launch_id="L1", operation_id="P1")
    again = core.prepare(request, launch_id="L1", operation_id="P1")
    assert again.portable == first.portable
    assert adapter.calls == []                 # prepare exposes a plan; it does not allocate
    with pytest.raises(OperationConflict):
        core.prepare(request.changed(), launch_id="L1", operation_id="P1")

def test_failed_adapter_aborts_and_compensates_once(core, failing_adapter, request):
    prepared = core.prepare(request, launch_id="L2", operation_id="P1")
    failed = failing_adapter.commit(prepared.local_capability)
    receipt = core.abort(prepared, failed, operation_id="A1")
    replay = core.abort(prepared, failed, operation_id="A1")
    assert receipt == replay and receipt.compensation == "completed"
    assert failing_adapter.compensations == 1
    assert core.lookup("L2").state == "aborted"
```

No evidence requires a pre-existing Herdr Space. Existing eager `_workspace()` behavior is an
adapter implementation detail and must not leak into prepare. No product source was changed.

## Verdict

**GO.** One frozen core prepare/commit/abort contract is feasible without importing Herdr.
The landed v1 profile already proves strict, host-neutral validation and redacted receipts;
worktree `locate`/`preview`/`ensure` already separates read-only resolution from idempotent
provisioning across managed, batch, epic, session, and integration shapes; and private-path APIs
already provide the correct local persistence boundaries.

The GO is conditional on the contract preserving two separations: requested identity versus
resolved checkout identity (especially a child bead in a batch), and portable facts versus local
capabilities/paths. Herdr may implement the adapter port, but core must neither import it nor name
Space/tab/pane fields in its base schemas.

## Recommendation

Replan the implementation molecule around the five v1 schemas and three state transitions above.
Implement the core protocol and git-common operation ledger first with a fake adapter conformance
suite covering every row in the evidence matrix. Then adapt Herdr in its own repository/molecule,
projecting only stable allocation ID and generation into the portable receipt. Keep native
in-process child bridging out of this contract unless a separately supported lifecycle bridge is
proved; it is not needed to establish this GO.
