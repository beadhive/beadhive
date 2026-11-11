# Sharding the integration and stateful keys under nested broker leases

**Status:** proposed (design for `bh-0pjeb`; implementation waits for the operator decisions
below) · **Date:** 2026-10-14 · **Bead:** `bh-0pjeb`
**Related:** [validation-broker-adr.md](validation-broker-adr.md) (accepted; this note works
inside it), [attest-keys-single-gate-model-adr.md](attest-keys-single-gate-model-adr.md),
[../ATTEST-KEYS.md](../ATTEST-KEYS.md), [../TEST_PROCESS_POLICY.md](../TEST_PROCESS_POLICY.md)
(worksteal packing and the collection pre-filter), `bh-fmuqq.4` (broker admission with nested
sub-allocation), `bh-fmuqq.2` (broker as a service with a socket API) and `bh-itn9m` (separate
CPU grants for integration and stateful under engine `local`, in progress in parallel).

## Problem

After the 0.25.1 profile change the `integration` key runs only at `release` and `push-main`, and
a push usually has to run only `integration` and `demos`, because carry and exact-tree reuse
supply the eight land keys. Integration takes 467-612 s, so it sets how long a push takes. The
target is a push gate of about 7-8 minutes (420-480 s) under engine `local`. `stateful` (about
596 s, run at `submit`, `merge` and land) is the second long key.

The bead asks for each long key to run as duration-balanced shards under nested leases. Verdicts
stay per key, and the attest catalog and profiles do not change.

## What exists today

Survey of `main` at `96188a28`:

| Piece | State |
|---|---|
| Nested sub-allocation in the engine | **Exists.** `LocalEngine` serves a `JobSpec` with `parent_lease` only from that parent's grant (`_serve_nested`). It never queues one host-wide and never brakes it. A child that can never fit is refused with `GrantRefused`. Releasing or reclaiming a parent ends its children. `FakeBroker` does the same, `FailoverBroker` keeps a nested request on the engine that holds its parent, and the conformance kit checks it (`nested_request_draws_only_from_the_parent_grant`). |
| Grant environment | **Exists.** `Grant.env()` exports `BH_VALIDATION_LEASE`, `BH_GRANT_CPUS`, `BH_GRANT_MEMORY_MB`, `BH_GRANT_DOLT_SERVERS` and the root variables. `key_environ` passes them to a key and never puts them in `cmd_hash`. |
| A nested request from a child process | **Missing.** `LocalEngine` runs inside the `bh` process that runs the gate. A key is a subprocess (`just attest-integration`), and it has no way to reach that engine. Reaching it needs the broker socket of `bh-fmuqq.2`, which is open. |
| Admission through the broker (`bh-fmuqq.4`) | **Not started.** The `validation_admission.host_slot` call sites still take host slots directly. This bead does not need that part. It needs only the clause "a request made under `BH_VALIDATION_LEASE` sub-allocates from its parent", from a child process. |
| Grant reaching the lanes | `stateful-native` and `test-integration-land` already use `-n ${BH_GRANT_CPUS:-16}`. `BH_GRANT_DOLT_SERVERS` is exported but no recipe reads it: the Dolt bound is `BH_DOLT_SLOTS` (8 by default, 12 in gate runs). `bh-itn9m` owns whether the grant reaches the lane and how large it is. |
| Worksteal packing | `tests/harness/worksteal_pack.py` (`trylast`) lays out the **selected** items as balanced worksteal chunks for the run's `-n`, Dolt-slot aware against `BH_DOLT_SLOTS`. |
| Collection pre-filter | `tests/harness/collection_prefilter.py` skips, in `pytest_ignore_collect`, files whose indexed marker sets the `-m` expression rejects. It cut per-worker collection on integration from 18.6-19.5 s to 7.1-7.2 s. On stateful the gain was within noise, because that lane keeps 530 of 581 files. |
| Durations | `tests/harness/test_durations.json` (JUnit `classname::name` keys, a per-module median table and a global median) is read by `duration_order.predicted_seconds`. |
| Report aggregation | `test_report.ingest` already merges every JUnit file in the run's `BH_TEST_REPORT_DIR`. `rc` decides the verdict and the report never changes it. |

## What sharding can and cannot buy

This section is the honest ceiling. The implementation has to measure against it.

- **The integration lane is slot-bound.** Packed runs keep 11.4-11.5 of 12 Dolt slots busy, and
  hold/12 plus collection (475-482 s) accounts for the 503 s measured (TEST_PROCESS_POLICY,
  bh-xqtp8). The CPU is also oversubscribed at 16 workers on a shared host (load median 26-30 on
  32 cores). That is why `BH_DOLT_SLOTS=12` stays.
- **Sharding at equal resources only moves the work around.** Two shards of 8 workers and 6
  slots hold the same 12 slots as one run of 16 workers and 12 slots. Integration gets faster
  only if the shards together hold more concurrent Dolt slots and more CPU than today's single
  run. On a host the broker is not oversubscribing, that is something engine `local` can grant
  safely.
- **Wins that come from sharding itself:**
  1. *Collection.* File-granular shards let each shard's workers import only that shard's files.
     The pre-filter already cut integration to about 7 s per worker. On stateful, where it did
     not help, file-granular shards would cut the 18-25 s per worker to roughly 1/N.
  2. *Independent fences.* Each shard has its own `bh-hermetic-*` scratch, its own Dolt slot lock
     files (the slots are `flock` files under the fence's `TMPDIR`) and its own xdist controller.
     A fault or a stall stays inside one shard.
  3. *Accounting the broker can see.* Each shard is a lease, with its own grant, run record
     detail and orphan reclaim, once the nested path crosses the process boundary.
- **Floors.** The longest single integration test is 155 s. The integration file set is about
  5,750 test-seconds in the 53 files that only hold integration items, plus part of 24 mixed
  files. With file-granular longest-processing-time bins, N = 2, 3 and 4 balance to within
  0.1 % (2,878/2,876, 1,919/1,917/1,918, 1,438×4), because the largest file (1,188 s,
  `test_epoch_fencing_int`) is under every bin. Stateful has 537 files and about 6,390
  test-seconds. Its largest file (`test_hq_authority_backend`, 1,539 s) is larger than a
  quarter of the total, so at N ≥ 4 that file has to be split by item (see the plan below).

**Result:** sharding is a means of giving one key more concurrent Dolt slots and CPU without one
oversized xdist run. It is not free speed. The implementation therefore starts with an A/B
measurement and a stop rule (Measurement and stop rule, below).

## Design

### Where sharding lives: a lane-level driver, behind the opaque key command

The key commands stay `just attest-integration` and `just attest-stateful`. `bh` keeps treating
them as opaque, so `cmd_hash` and the catalog do not change. Sharding lives inside the lane
recipes (`test-integration-land`, `stateful-native`), which hand their pytest argv to a driver,
`scripts/shard_lane.py`, in place of the single `hermetic.sh … pytest_with_report.py -n …` call:

```text
test-watchdog.py --timeout T --            (unchanged: one key-level watchdog)
  shard_lane.py --lane integration -- <the lane's pytest argv, unchanged>
    shard 1/N: hermetic.sh pytest_with_report.py -n c1 <argv>   env BH_TEST_SHARD=1/N, BH_DOLT_SLOTS=s1
    shard 2/N: hermetic.sh pytest_with_report.py -n c2 <argv>   env BH_TEST_SHARD=2/N, BH_DOLT_SLOTS=s2
    …
```

With sharding off (`N = 1`, the default) the driver is not on the path at all. The recipe line is
selected by a `just` conditional, so the unsharded command runs exactly as it does today.

Rejected placement: having `bh`'s key runner run the key command N times with a shard index.
That gives real in-process nested leases today, but it writes N run records and N ledger verdicts
for one `cmd_hash`. A person running `just attest-integration` would also get no sharding.

### Shard count from the grant

```text
G      = BH_GRANT_CPUS if set (engine local), else the lane's worker count (16)
c      = BH_TEST_SHARD_CPUS, default 8 (integration's measured knee, bh-5mdar.2)
N_max  = BH_TEST_SHARDS_MAX, default 4
N      = BH_TEST_SHARDS = 1 (default, off) | <n> | auto
auto  -> N = clamp(floor(G / c), 1, N_max), and at most the number of planned files
cpus   = G split into N near-equal parts, with the remainder going to the heaviest shards
slots  = S = BH_DOLT_SLOTS (default 8) split the same way, at least 1 per shard
memory = BH_GRANT_MEMORY_MB split evenly (reported only; no cgroups yet)
```

Total Dolt concurrency stays at most `S` across all shards, so the host bound the cap protects
still holds. Raising `S` for the push gate is a separate operator decision (D4).

### The shard plan: deterministic, file-granular, from `test_durations.json`

A pure module, `tests/harness/shard_plan.py`, builds on `duration_order`. The driver, every
shard's controller and every xdist worker compute the same plan independently from the same
inputs, so nothing has to be passed between them:

1. **Candidate files.** These are the files the run would import: files in
   `collection_index.json` that the lane's `-m` expression can select (the pre-filter's own test),
   plus every file the index cannot vouch for (new, changed, or covered by a global digest
   mismatch).
2. **Weights.** A file's weight is the sum of `predicted_seconds` over its indexed tests. A test
   the durations file does not list takes its module median, then the global median, as today.
3. **Bins.** Files are placed longest first onto the least-loaded of N bins (LPT). The tie-break
   is the shard index, so the plan is stable.
4. **Oversized files.** A file heavier than `total / N` is split by item. Its items go to the bins
   in LPT order and are deselected in the other shards. Every shard that owns any of its items
   imports it. This is the stateful case at N ≥ 4.
5. **Total by construction.** Assignment is a function of the path, with the item nodeid for
   split files: `plan.get(path)` if the plan lists it, else `stable_hash(path) mod N`. A file
   created after the index was built still lands in exactly one shard. The shards are disjoint,
   and together they select exactly the unsharded selection.
6. **Digest.** The plan's digest is a hash of its inputs and N. Each shard prints it in its
   pytest header. The driver refuses to aggregate shards whose digests differ.

Adopting rather than building: **`pytest-split`** (MIT) does duration-based `--splits N
--group i`, and **`pytest-shard`** (MIT) does hash-based sharding. Neither is a good fit as it
stands. `pytest-split` splits at item level, so every shard imports every file and the collection
win is lost. Its durations file is keyed by nodeid, with no per-module median for unknown tests.
It does not know our Dolt-slot accounting. `pytest-shard` is not duration-balanced. We borrow
`pytest-split`'s interface (`splits` and `group`, least-duration bins) and keep the
implementation in the harness next to `duration_order`, which already has LPT, the medians and
slot awareness. The `pytest-split` decision is recorded so it can be revisited if the harness
module grows beyond about 150 lines (D5).

### Per shard: selection, the pre-filter and worksteal packing

A new plugin, `tests/harness/shard_select.py`, is registered next to the pre-filter and is
inert unless `BH_TEST_SHARD=i/N` is set:

- `pytest_ignore_collect` skips files the plan assigns wholly to another shard. It works
  alongside `collection_prefilter`, which skips files the `-m` expression rejects. Either one
  skipping is enough. Both keep the skipped set a subset of what a full run deselects.
- A plain (not `trylast`) `pytest_collection_modifyitems` deselects the items of split files
  that belong to other shards and reports them through `pytest_deselected`.
- `worksteal_pack` stays `trylast`, so it packs what is left after the shard's selection, for
  that shard's own `-n` and `BH_DOLT_SLOTS`. No change is needed there.
- The plugin adds a header line naming the shard, the plan digest, the predicted seconds and the
  file and item counts.

### Per-key verdict rule

- **One key run, one verdict.** The driver is one process under the key's watchdog. It exits 0
  only when **every** shard exits 0. Otherwise it exits with the first non-zero shard's code, in
  shard order: 124 for a watchdog timeout and 75 for a broker kill are kept, and anything else
  becomes 1. `bh` records one run record and one ledger verdict for the key under its unchanged
  `cmd_hash`. The catalog, the profiles, carry and attested green do not change.
- **Same tree.** All shards run in the key's one checkout (the gate's clean checkout or the run
  directory in its root). They are started by one driver, which refuses to aggregate mismatched
  plan digests, so a green key means every shard was green on that tree.
- **No shard-level reuse.** A red key reruns every shard. Recording and reusing a green shard
  needs per-shard ledger identity, which would be a catalog change. It is out of scope here.
- **Empty shard.** Exit 5 (no tests collected) is red when the plan assigned files to that shard,
  as a vacuity guard. It is green only when the plan predicted an empty shard. `require-bd` stays
  the key-level non-vacuity probe.
- **Failure policy: the shards finish.** This matches the gate group's policy, so a red key
  names every failing test in one run. `BH_TEST_SHARD_FAIL_FAST=1` (off by default) terminates
  the sibling shards after the first red to free cores sooner.

### Reporting a red shard

- Each shard's output is line-prefixed `[integration 2/3]`.
- The driver records each shard's JUnit file: `pytest_with_report.py` already writes a
  collision-free file per pytest process into `BH_TEST_REPORT_DIR`. The driver gives each shard
  its own file-name prefix (from `BH_TEST_SHARD`) so it can find that shard's file. At the end it
  prints a summary:

  ```text
  shard 1/3  green   912 s·cpu  41 passed
  shard 2/3  RED     rc=1       2 failed: tests/test_fence_orphan_int.py::test_x, …::test_y
  shard 3/3  green   …
  integration: RED (1 of 3 shards red)
  ```

- `test_report.ingest` already merges every shard's report into the key's run record, so the
  failing node ids reach the ledger detail and `bh work review` without any new plumbing. The
  verdict still comes from `rc`.

### Broker interaction and nested leases

The driver talks to a small `NestedLeases` port with two adapters:

| Adapter | When | Behaviour |
|---|---|---|
| `LocalCarve` | no broker endpoint reachable (engine `slots`, or engine `local` before the endpoint exists) | Splits `BH_GRANT_*` (or the lane defaults) by the arithmetic above. No broker round trip. The driver says once which mode it used. |
| `BrokerNested` | `BH_VALIDATION_BROKER_SOCKET` names a live endpoint and `BH_VALIDATION_LEASE` is set | Per shard: `submit(JobSpec(parent_lease=$BH_VALIDATION_LEASE, reservation=(c_i, c_i, mem_i, dolt-servers: s_i), group=<gate>, key=<key>))`, then `wait_grant`, start the shard with that grant's env, and `report` the outcome. The engine's existing `_serve_nested` carves the children out of the key's grant. A refusal or lost lease falls back to `LocalCarve` for the shards not yet started, loudly. |

How this fits the accepted ADR:

- **Lease mode.** The driver stays the process parent of its shards. Nested requests carry
  `BH_VALIDATION_LEASE`, draw only from the parent's grant and are never braked. A shard cannot
  re-enter the host queue, so there is no hq-vbha style hold-and-wait.
- **The broker packs the key, and the key's lease bounds the shards.** The packer's laggard-first
  rule and `laggard_flex` give the laggard key (integration at push) spare CPU up to its
  `cpu_max`. The shards then split exactly that grant. They do not get more cores than the key
  was granted. That is what makes `bh-itn9m`'s grant sizing the main input here (next section).
- **Orphan reclaim** already covers nested leases: a dead driver's shards end with it.
- **The reservation contract does not change** (`cpu_min`/`cpu_max`, memory, pools, parent
  lease). It stays inside the ADR's HyperQueue and Kubernetes intersection.

**What this needs from `bh-fmuqq.4`:** only the client half of "a request made under
`BH_VALIDATION_LEASE` sub-allocates from its parent", reachable from a child process. The
engine half already exists. Moving the `host_slot` call sites onto the broker is not needed. How
the child reaches the engine is decision D1.

### How it composes with `bh-itn9m` (separate CPU grants)

The two beads split the work along a clean line:

- **`bh-itn9m` owns the key-level grant.** It decides whether integration and stateful get
  disjoint grants that sum to at most 32, whether `BH_GRANT_CPUS` really caps `-n` in
  `test-integration-land`, and whether integration starts first.
- **This bead consumes the grant and never sizes it.** `G = BH_GRANT_CPUS`. Under the slots
  engine (no grant) it uses the lane's worker count. A disjoint grant from `bh-itn9m` simply
  arrives as a smaller or larger `G`, and `auto` picks N from it.
- **Touch points to coordinate.** Both beads edit the `test-integration-land` and
  `stateful-native` recipe lines, and possibly the `integration` and `stateful` rows of
  `DEFAULT_RESERVATIONS`. This bead lands after `bh-itn9m` and rebases onto its recipe lines.
  `BH_GRANT_DOLT_SERVERS` is exported, but the lanes bound Dolt with `BH_DOLT_SLOTS`. Whether a
  lane takes `S` from the grant's pool is a shared question (D4), and only one bead should
  answer it.
- **Reservation size.** For sharding to speed up a push, integration's grant must be larger than
  today's `cpu_max` of 8 when the host is free. That is a change to `DEFAULT_RESERVATIONS`, a
  code default that `work.attest.keys[].resources` still overrides, not a catalog change. It
  belongs in `bh-itn9m` or here (D3).

### Configuration (every default off and every knob configurable)

All knobs are environment variables read by the recipe shell or the driver. They never enter a
key's command text, so they never reach `cmd_hash`. They pass through `key_environ`'s existing
`BH_*` passthrough.

| Knob | Default | Meaning |
|---|---|---|
| `BH_TEST_SHARDS_INTEGRATION` / `BH_TEST_SHARDS_STATEFUL` (justfile vars `integration_shards`, `stateful_shards`) | `1` (off) | `1`, `<n>` or `auto` for that lane |
| `BH_TEST_SHARD_CPUS` | `8` | Target CPUs per shard for `auto` |
| `BH_TEST_SHARDS_MAX` | `4` | Upper bound for `auto` |
| `BH_TEST_SHARD_FAIL_FAST` | `0` | Stop sibling shards after the first red |
| `BH_TEST_SHARD` | unset | Internal: `i/N`, set by the driver for each shard |
| `BH_VALIDATION_BROKER_SOCKET` | unset | Nested-lease endpoint; when unset, `LocalCarve` |

Turning sharding on for this hive is a one-line justfile default change, made after measurement
(D2). It is not an HQ catalog or profile edit.

## Implementation plan (phase 2, after the decisions)

1. `tests/harness/shard_plan.py` and its unit tests: totality and disjointness over random path
   and nodeid sets, stability, oversized-file item splits, unknown files, and the plan digest.
2. `tests/harness/shard_select.py`: the plugin, with tests that the union of the N selections
   equals the unsharded selection on a fixture tree, with the pre-filter on and off.
3. `scripts/shard_lane.py`: count, carve, launch, prefix, aggregate, the red-shard summary and
   the exit-code rule, with tests through a fake shard runner. It also needs the `NestedLeases`
   port and `LocalCarve`.
4. Justfile: `integration_shards` and `stateful_shards` conditionals in `test-integration-land`
   and `stateful-native`, defaulting to `1` (the byte-identical path), and a
   `tests/test_workspace_native_gate.py` check that the complete integration collection rule
   still holds when sharded.
5. `BrokerNested`, depending on D1.
6. Measurement, recorded in TEST_PROCESS_POLICY.md and in this note.

### Measurement and stop rule

Run on the factory host, quiet apart from the gate, under engine `local`, with
`BH_DOLT_SLOTS=12 BH_TEST_TIMEOUT_SECONDS=2400`. Do at least two runs per arm.

- **A/B at equal resources.** One run at `-n G` with `BH_DOLT_SLOTS=S` against N shards splitting
  the same `G` and `S`, for N = 2 and 3.
- **A/B at a push-sized grant.** Integration with demos alongside, at the grant `bh-itn9m`
  settles on, then with sharding at that grant.
- **Report** wall, busy/workers, collection, tail, slot wait, peak load, minimum MemAvailable and
  peak `dolt` processes, as in the existing tables.
- **Stop rule.** If sharded is not at least 30 s (or 5 %) faster than unsharded at the same grant,
  sharding stays off. The bead then closes with that result, and the push-gate speed-up is
  pursued through grant size alone (`bh-itn9m`).
- **Target check.** The push gate wall (integration plus demos) is recorded against 420-480 s.

## Open decisions for the operator

- **D1 — how a shard reaches the engine.**
  - (a) Use `LocalCarve` only now, with real nested leases waiting on `bh-fmuqq.2`'s socket.
  - (b) Add a small per-gate endpoint: the `bh` process running the gate serves the nested subset
    of the broker API (submit, wait, report and cancel for children of its own leases) on a Unix
    socket in the run directory. `key_environ` exports it, and `bh-fmuqq.2` later absorbs it.
  - (c) Implement that part of `bh-fmuqq.2` first.

  *Recommendation:* (a) to measure, then (b) only if the stop rule passes. The acceptance says
  "under nested leases", and (a) alone carves the grant without broker-visible leases.
- **D2 — default on for this hive.** Both lanes ship with `1` (off). Turn `auto` on for
  integration only after the stop rule passes. Stateful is a separate decision, because it also
  runs at `submit`.
- **D3 — who raises integration's `cpu_max`.** Without a larger grant on a free host, sharding
  cannot speed up a push. *Recommendation:* `bh-itn9m` owns the reservation and this bead reads
  it.
- **D4 — Dolt concurrency at push.** Options are to keep the total at `S = 12` split across
  shards (the safe default), or to derive `S` from `BH_GRANT_DOLT_SERVERS` and raise integration's
  `dolt-servers` reservation so the broker, not a fixed cap, bounds host Dolt. The second is the
  only way past the slot-bound floor of about 455 s, and it should be decided together with
  `bh-itn9m`.
- **D5 — `pytest-split`.** Confirm the borrow-not-adopt call above, or prefer adopting it at item
  level and giving up the per-shard collection win.
