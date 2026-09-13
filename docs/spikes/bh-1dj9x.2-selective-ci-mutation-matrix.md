# bh-1dj9x.2: selective CI mutation matrix

## Question

Does the conditional Pants 2.32.1 design select every affected closure, avoid unrelated work,
and reuse a host-wide cache safely across independent Beadhive worktrees while remaining
equivalent to native validation?

## Method

All trials used signed baseline `3fd757827c29a48e6cab581bad0d9bd4b1b215e1`
(tree `01c56a7b77d7d0ccdbf1614eb6e99480b79a7cbd`) in detached disposable worktrees
`/tmp/bh-1dj9x.2-wt-a`, `-wt-b`, and `-wt-replay`. Canonical `uv.lock` stayed at SHA-256
`3c1548a9feef8bc05510870f33762a40e33c3f6fdbb1b510f5e153c72d838818`.

The prototype reproduced the predecessor's viable settings: Pants 2.32.1, experimental uv
resolver, `run_against_entire_lockfile`, and a coherent pytest 9.1.1/xdist 3.8.0/cov 7.1.0
environment installed from the `beadhive` resolve. Per-file Python generators, explicit harness
and resource ownership, and removal of the stateful plugin from the leaf conftest closure modeled
fixture decomposition. Each content mutation was reversed before the next case; `git diff`, status,
baseline tree, generated-file inventory, and lock digest were compared after reversal.

The qualified topology used one explicit immutable store and isolated mutable caches:

- local store: `/tmp/bh-1dj9x.2-host-cache/local-store`;
- named caches: distinct `named-a` and `named-b` directories per worktree;
- isolated workdirs: `/tmp/bh-1dj9x.2-work-a` and `/tmp/bh-1dj9x.2-work-b`.

Remote cache reads and writes were both false. No endpoint, credentials, networked cache, main
checkout, remote, canonical lock, or lifecycle state was mutated.

## Evidence

### Mutation trials

| Trial | Reversible diff and selector reason | Pants result | Same-tree native oracle / fallback | Invalidation result |
|---|---|---|---|---|
| M1 isolated relevant source | Comment in `modules/config/application/resolution.py`; direct owner of config leaf | 58 executed, 58 passed; 1.414 s wall, 1.01 s process | native config closure: 58 passed in 2.11 s | correct rerun; no escape |
| M2 unrelated source | Comment in `wt_status.py` with stateful plugin absent from pure-unit sandbox | 58 cache-served in 1.04 s | mixed native xdist oracle: 107 passed | unrelated closure avoided after topology split |
| M3 test source | Comment-only test-file mutation in the config leaf | leaf target selected; test input digest changed | native config closure was the required oracle | correct test invalidation |
| M4 shared contract / compatibility facade | reversible changes under config contracts/facade | graph required broader inferred product edges and emitted unresolved pydantic mappings | native full route required | conservative full fallback; not eligible for skipping |
| M5 generated/plugin-dynamic/multi-module | reversible schema/manifest ownership changes | explicit resource ownership was necessary; dynamic/generated completeness not proven | native full route required | conservative full fallback |
| M6 dependency/lock | second worktree regenerated the same uv solution with checkout-local Pants metadata | isolated-named-cache B served 58 memoized in 1.90 s from shared local store | native uv environment remained authoritative | whole lock invalidates all Python tests; identical locks reuse process results |
| M7 build configuration | remote read/write remained false while local store/named-cache/workdir settings parsed | configuration digest invalidated graph setup | native full route required for runner changes | correct conservative fallback |
| M8 test infrastructure / console script | conftest/plugin and installed-`bh` assumptions | stateful removal broke full collection; Pants cannot provide sibling `bh` beside `sys.executable` | native full route mandatory | fail closed, never skipped |

No affected-test mutation produced selected-green/native-red. More importantly, several required
cases never became eligible for selected-only success: they failed closed or routed to native
full, as the safety policy requires.

### Fixture-decomposition result

The initial removal-only and dynamic-registration experiments were rejected because they did not
preserve full xdist session semantics. The successful design registers plugins statically at root
conftest import time: `stateful_fixtures` is included iff its module exists in the sandbox. Native
and full Pants fallbacks contain it and therefore retain identical early registration. The pure
unit Pants target deliberately excludes its target/file, with a `pants: no-infer-dep` annotation
making the optional probe explicit. The broad harness dependency was also removed from every test;
legacy harness users require narrow edges or native fallback.

The mixed xdist oracle passed 107/107 tests (58 unit plus 49 flat/stateful) in 2.52 seconds. The
strict hermetic non-integration gate passed 8,975 tests with 12 skipped and one warning in 275.56
seconds. Pants then served all 58 leaf tests memoized after the unrelated `wt_status.py` edit
(1.04-second cached result) and reran all 58 after the relevant resolution edit (1.22 seconds).

### Cross-worktree cache result

Worktree A's cold resolver took 10.899 seconds. Its first leaf attempt encountered ENOSPC while
linking pytest, failed closed, and a retry succeeded: 58 tests passed in 22.750 seconds. That
discarded trial shared both cache classes. The qualified worktree B trial shared only the explicit
immutable local store and used a separate named-cache path and workdir. Copying the
generated lock proved invalid because Pants' metadata is checkout-sensitive; regenerating it
took 0.753 seconds and produced the same package solution.

Because that run began with only 1.6 GiB free and first failed ENOSPC, its mutable-cache result was
discarded as invalid qualification evidence. The corrected architecture used fresh capacity, one
shared immutable `local_store_dir`, separate `named_caches_dir` and `pants_workdir` per checkout.
Worktree A seeded 58 green; independently generated-lock worktree B then reported the same target
`succeeded ... (memoized)` in 1.90 seconds, without duplicate test execution. A concurrent A/B
read trial also completed green; B explicitly reported `cached locally` in 1.90 seconds while A's
different fixture-policy digest executed independently. Relevant content changes the process
digest and reruns the closure. This satisfies cross-worktree reuse and concurrent access while
avoiding an unsupported assumption that mutable uv/PEX venv directories are share-safe.

The shared directory was owned `bees:bees` with mode 0775. The qualification architecture isolates
mutable uv/PEX venv directories while sharing Pants' content-addressed local store, so concurrent
worktrees do not mutate the same venv. Capacity was operationally material: `/tmp` began at 94%
usage with 1.6 GiB free, the first cache reached about 272 MiB, and the first run failed ENOSPC.
Configure
`local_store_processes_max_size_bytes`/`local_store_files_max_size_bytes` with a monitored quota,
age-based cleanup only while no Pants process is active, and retain a native fail-closed route.
Corruption recovery must delete an exact content-addressed entry, never the whole host cache.

### Remote-cache seam

The replay config parses with `remote_cache_read = false` and `remote_cache_write = false` and
needs no provider, server, or secret. A later implementation can inject a supported REAPI store
through environment-specific Pants options for remote store address, instance name, TLS headers,
and read/write policy. Keep repository defaults disabled. Pants' uv resolver produces process
digests that can be stored remotely; the compatibility constraint is that Pants 2.32.1+ and the
generated-lock metadata plus entire-lock setting must be identical on writers and readers.

### Replayable implementation handoff

`docs/spikes/artifacts/bh-1dj9x.2/pants-prototype.patch` captures the proven non-active Pants
config, BUILD graph, uv/coherent-pytest setup, fixture-split seam, resource ownership, and disabled
remote-cache defaults. It is pinned to the baseline above. Both `git apply --check` and an actual
`git apply` succeeded in `/tmp/bh-1dj9x.2-wt-replay`; `git diff --check` was clean. Patch SHA-256:
`3571cd6e170dc185ab911f5114a652fea225f3a25977383b4b4d03fa9f2221fa`.
The generated secondary lock is deliberately excluded and must be regenerated with Pants' uv
resolver in each replay.

## Verdict

**GO-Pants for implementation planning; activation remains a separate reviewed change.**

The relevant leaf mutation was never missed, unrelated work was cache-served, the complete native
non-integration oracle remained green under xdist, and a second checkout reused the shared
immutable local-store result without duplicate execution. The rejected dynamic-registration and
ENOSPC/mutable-cache attempts define safe boundaries: use static early registration, narrow graph
edges, shared immutable local store, and isolated named caches/workdirs per checkout.
Native full validation remains mandatory for shared, unknown, generated, compatibility,
plugin-dynamic, multi-module, test-infrastructure, dependency/lock, build-configuration, and
installed-console-script changes until each boundary graduates with oracle evidence.

## Recommendation

Proceed with a bounded implementation behind shadow/oracle mode:

1. land and independently validate a real fixture decomposition that preserves root collection
   policy while attaching stateful fixtures only to inventoried consumers; and
2. share Pants' immutable local store across worktrees while isolating mutable named venv caches
   and workdirs; enforce capacity checks and exact-entry corruption recovery.

Replay the checked artifact, complete third-party mappings/resources, and retain the same-tree
native oracle while graduating closures.
Keep remote caching disabled until local correctness is proven. Bazel remains deferred; the
remaining activation work does not reverse the preceding Pants-first feasibility choice.
