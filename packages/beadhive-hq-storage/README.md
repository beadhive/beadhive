# beadhive-hq-storage

The backend-neutral HQ storage **port**: protocols, value and outcome types, errors, in-memory
fakes with an injected clock, and the reusable contract suite. A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md): no
`plugin.json`, stdlib only, and it never imports the root `beadhive` distribution. The design is
[hq-storage-port-adr.md](../../docs/design/hq-storage-port-adr.md).

## Public surface

| Module | Holds |
|---|---|
| `beadhive_hq_storage` | `VersionedRecords`, `AtomicRecords` + `RecordWrite`, `AppendLog`, `Liveness`, `CommittedHistory`, `HiveFence`, `EpochRefStore`, `PlacementStore`, `CapabilityHost` + `require_capability`, `StorageClock`; `Applied` / `Conflict` / `Unknown`, `Confirmed` / `NotConfirmed`, `OperationId`, `ABSENT`; `BumpRequest` / `ReclaimSet` / `ReclaimNote` and the fence records; the `StorageError` family; `InMemoryHqStorage`, `InMemoryHiveFence`, `InMemoryEpochRef`, `FaultPlan` |
| `beadhive_hq_storage.contract` | `SCENARIOS`, `ContractAdapter` + harness protocols, `fake_adapter`, `adapter_fixture`, `parametrize_scenarios`, `run_scenario`, `build_state_machine`, the hypothesis profiles, `ManualClock` |

The fakes own no wall time. `InMemoryHqStorage(clock)` stamps renewals, appends and commits
from `clock.now()`, so a test advances the clock to cross staleness and retention thresholds.
Root's `tests/harness/clock.FakeClock` satisfies `StorageClock` structurally. `FaultPlan` makes
the next matching mutation return `Unknown` (applied or not) or raise `StorageUnavailable`.

## Running the contract against an adapter

```python
from beadhive_hq_storage.contract import adapter_fixture, fake_adapter, parametrize_scenarios, run_scenario

adapter = adapter_fixture({"fake": fake_adapter, "dolt": dolt_adapter})


@parametrize_scenarios()
def test_contract(scenario, adapter):
    run_scenario(scenario, adapter)
```

An adapter supplies a `ContractAdapter`: an id, a fresh `HqHarness` factory and a fresh
`FenceHarness` factory. A control the adapter cannot provide raises `ScenarioSkipped`. Cases that
need a controllable clock skip when `clock_controlled` is false; the others assert ordering only,
never elapsed time.

`build_state_machine(a, b)` returns a hypothesis `RuleBasedStateMachine` that drives both adapters
with the same commands. After every step it compares outcome kinds, bodies, the revision-equality
pattern, witness lookups and head movement.

## Hypothesis profiles

`tests/conftest.py` loads the profile named by `BH_HYPOTHESIS_PROFILE`. When the variable is
unset, it loads `gate`, so every run is reproducible from the tree alone.

| Profile | Budget | Notes |
|---|---|---|
| `gate` | 25 examples x 20 steps | `derandomize=True`, `database=None`, `deadline=None`, `too_slow` suppressed; a real-Dolt machine adds `phases=no_shrink_phases()` |
| `nightly` | 500 examples x 40 steps | derandomized, shrinking on |
| `dev` | hypothesis defaults | opt-in for local exploration |

`hypothesis` (MPL-2.0) is a dev-only dependency (root `dev` group). It is never a runtime
dependency, and the contract module imports it lazily.

## Contract coverage

Each port operation maps to the scenarios that exercise it. The table must match
`contract.SCENARIOS` exactly; `tests/test_hq_storage_contract.py` enforces this.

| Operation | Contract scenarios |
|---|---|
| `AtomicRecords.compare_and_set_all` | `atomic_cas_applies_every_write_or_none`, `atomic_cas_unknown_resolves_by_any_member`, `atomic_cas_refuses_malformed_writes_before_io` |
| `VersionedRecords.compare_and_set` | `cas_from_absent_creates_and_reads_back`, `cas_issues_a_fresh_revision_every_write`, `cas_stale_expectation_conflicts_with_current`, `cas_absent_expectation_conflicts_when_present`, `cas_expected_revision_on_a_missing_record_conflicts`, `update_only_kind_refuses_cas_from_absent`, `cas_unknown_applied_resolves_confirmed`, `cas_unknown_not_applied_keeps_original_expectation`, `cas_unavailable_means_nothing_applied`, `malformed_requests_are_refused_before_io` |
| `VersionedRecords.lookup` | `atomic_cas_applies_every_write_or_none`, `atomic_cas_unknown_resolves_by_any_member`, `cas_unknown_applied_resolves_confirmed`, `cas_unknown_not_applied_keeps_original_expectation`, `lookup_reads_not_confirmed_once_superseded` |
| `VersionedRecords.read` | `cas_from_absent_creates_and_reads_back` |
| `VersionedRecords.read_many` | `read_many_filters_by_kind_and_keys` |
| `PlacementStore.compare_and_set` | `placement_view_moves_frame_and_epoch_under_cas` |
| `PlacementStore.read` | `placement_view_moves_frame_and_epoch_under_cas` |
| `AppendLog.append` | `malformed_requests_are_refused_before_io`, `append_is_idempotent_for_the_same_operation`, `append_replay_of_a_key_conflicts`, `append_unknown_resolves_by_natural_key` |
| `AppendLog.lookup_append` | `append_unknown_resolves_by_natural_key` |
| `AppendLog.prune` | `prune_keeps_everything_younger_than_the_window`, `prune_measures_age_by_the_store_clock` |
| `AppendLog.scan` | `scan_orders_filters_and_pages` |
| `Liveness.ages` | `renew_is_stamped_by_the_store_clock`, `ages_follow_the_injected_clock` |
| `Liveness.now` | `renew_is_stamped_by_the_store_clock` |
| `Liveness.renew` | `renew_is_stamped_by_the_store_clock`, `ages_follow_the_injected_clock` |
| `Liveness.stamped_at` | `renew_is_stamped_by_the_store_clock` |
| `StorageClock` | `prune_measures_age_by_the_store_clock`, `ages_follow_the_injected_clock` |
| `CommittedHistory.consistent_read` | `consistent_read_is_one_token_or_refused` |
| `CommittedHistory.fetch` | `fetch_and_push_report_divergence`, `unreachable_remote_is_unavailable` |
| `CommittedHistory.head` | `publish_returns_the_exact_committed_head` |
| `CommittedHistory.lookup_publication` | `publish_unknown_resolves_by_publication_witness` |
| `CommittedHistory.publish` | `publish_returns_the_exact_committed_head`, `publish_conflicts_on_a_moved_or_absent_head`, `publish_unknown_resolves_by_publication_witness`, `publish_refuses_malformed_change_sets` |
| `CommittedHistory.push` | `fetch_and_push_report_divergence`, `unreachable_remote_is_unavailable`, `push_unknown_keeps_the_prior_remote_as_expectation` |
| `CommittedHistory.read_at` | `publish_returns_the_exact_committed_head` |
| `CapabilityHost.capability` | `missing_capability_is_none_and_required_raises` |
| `require_capability` | `missing_capability_is_none_and_required_raises` |
| `HiveFence.commit_bump` | `bump_is_one_commit_with_sentinel_and_reclaim`, `bump_reclaim_marks_resumable_and_leaves_the_audit_note`, `bump_refuses_a_malformed_reclaim`, `bump_never_regresses_below_the_floor`, `bump_refused_when_not_cut_over`, `bump_failure_leaves_nothing_applied`, `bump_unknown_resolves_through_lookup_bump` |
| `HiveFence.epoch_floor` | `bump_never_regresses_below_the_floor`, `bump_refused_when_not_cut_over` |
| `HiveFence.guard_conformance` | `guard_conformance_reports_counts` |
| `HiveFence.lookup_bump` | `push_then_lookup_bump_finds_the_sentinel`, `push_unknown_resolves_through_lookup_bump`, `bump_unknown_resolves_through_lookup_bump` |
| `HiveFence.push` | `push_then_lookup_bump_finds_the_sentinel`, `non_fast_forward_push_conflicts_and_sync_reports_divergence`, `push_unknown_resolves_through_lookup_bump`, `bump_unknown_resolves_through_lookup_bump` |
| `HiveFence.remote_writer` | `push_then_lookup_bump_finds_the_sentinel`, `non_fast_forward_push_conflicts_and_sync_reports_divergence` |
| `HiveFence.sync_to_remote` | `non_fast_forward_push_conflicts_and_sync_reports_divergence` |
| `HiveFence.writer` | `bump_is_one_commit_with_sentinel_and_reclaim`, `bump_refused_when_not_cut_over` |
| `ReclaimSet` | `bump_is_one_commit_with_sentinel_and_reclaim`, `bump_reclaim_marks_resumable_and_leaves_the_audit_note`, `bump_refuses_a_malformed_reclaim` |
| `ReclaimNote` | `bump_reclaim_marks_resumable_and_leaves_the_audit_note` |
| `EpochRefStore.compare_and_set` | `epoch_ref_cas_issues_a_fresh_sha` |
| `EpochRefStore.read` | `epoch_ref_cas_issues_a_fresh_sha` |
| `provisioning` | `update_only_kind_refuses_cas_from_absent` |
| `Unknown` | `atomic_cas_unknown_resolves_by_any_member`, `cas_unknown_applied_resolves_confirmed`, `cas_unknown_not_applied_keeps_original_expectation`, `append_unknown_resolves_by_natural_key`, `publish_unknown_resolves_by_publication_witness`, `push_unknown_keeps_the_prior_remote_as_expectation`, `push_unknown_resolves_through_lookup_bump`, `bump_unknown_resolves_through_lookup_bump` |
| `StorageUnavailable` | `cas_unavailable_means_nothing_applied`, `unreachable_remote_is_unavailable`, `bump_failure_leaves_nothing_applied` |
| `NotProvisioned` | `bump_refused_when_not_cut_over` |
| `InvalidRequest` | `atomic_cas_refuses_malformed_writes_before_io`, `malformed_requests_are_refused_before_io`, `publish_refuses_malformed_change_sets`, `bump_refuses_a_malformed_reclaim` |
| `IntegrityViolation` | `bump_never_regresses_below_the_floor`, `bump_unknown_resolves_through_lookup_bump` |
| `CapabilityUnsupported` | `consistent_read_is_one_token_or_refused`, `missing_capability_is_none_and_required_raises` |

## Import rules

- Imports nothing from `beadhive` and has no third-party runtime dependency.
- Consumers import it through `beadhive_hq_storage.__all__` only (`scripts/check_package_imports.py`).
- The Dolt adapter is [`beadhive-hq-storage-dolt`](../beadhive-hq-storage-dolt/README.md).

## Gate

Both HQ storage packages share the `hq-storage` attest key (`attest:hq-storage`, recipe
`just hq-storage-check`, opaque command `just attest-hq-storage`). Run the tests with:

```sh
just hq-storage-check
```
