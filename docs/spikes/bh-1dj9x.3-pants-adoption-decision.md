# bh-1dj9x.3: Pants adoption decision

## Decision

**GO-Pants.** Re-enter planning for a reviewed Pants adoption molecule. This decision selects
Pants as Beadhive's build/test acceleration path; it does not activate Pants, alter CI, or permit
production test skipping. The existing uv/pytest/just path and fail-closed closure policy remain
authoritative until implementation lands normally on main with full native-oracle validation.

Bazel was not evaluated because Pants met the GO criteria. Bazel remains deferred backlog work,
not a fallback task inside the Pants implementation molecule.

## Evidence basis

The decision reads the two linked reports:

- `bh-1dj9x.1-selective-ci-build-graph.md` verified Pants 2.32.1's experimental uv resolver,
  coherent repository-controlled pytest 9.1.1/xdist 3.8.0/cov 7.1.0 execution, representative
  graph ownership, and a 0.429-second warm pantsd result versus 1.6--1.8 seconds native.
- `bh-1dj9x.2-selective-ci-mutation-matrix.md` proved the semantics-preserving static fixture
  boundary with 8,975 native tests passing (12 skipped), an unrelated edit serving 58 tests from
  cache in 1.04 seconds, a relevant edit rerunning all 58 green in 1.22 seconds, and compatible
  cross-worktree reuse through a shared immutable local store.

No affected-test mutation escaped the native oracle. Ambiguous cases failed closed or were routed
to native full validation. The setup cost is acceptable only as a staged implementation: Pants
adds BUILD ownership, a generated Pants lock beside canonical `uv.lock`, explicit dynamic/resource
edges, fixture topology, cache operations, and observability that must remain checked.

## Required implementation contract

### Version and dependency fidelity

- Pin Pants **2.32.1 or newer**. Pants 2.32.0's uv lock metadata/materialization path failed;
  2.32.1 fixed the tested path.
- Keep `resolver = "uv"`, resolves enabled, and initially
  `run_against_entire_lockfile = true`.
- Install the pytest tool coherently from the repository resolve with
  `[pytest].install_from_resolve = "beadhive"` and explicit pytest, xdist, and coverage targets.
  Never mix Pants' bundled pytest distribution with the repository pytest environment.
- Treat canonical `uv.lock` as product truth. Generate the secondary Pants lock mechanically and
  check exact core/extra/dev-group fidelity. PEP 735 dependency groups still require explicit
  modeling. A lock change conservatively invalidates every Python test while whole-lock mode is
  enabled.

### Fixture and target topology

- Preserve static, early pytest plugin registration. Root conftest may include
  `stateful_fixtures` only when that module is present in the sandbox; annotate the optional
  string/module probe so Pants does not infer the heavy fixture edge into pure unit targets.
- Native/full sandboxes and legacy consumers include `stateful_fixtures`; pure unit targets omit
  it. Preserve the complete collection policy, isolation fixtures, xdist lifecycle, and Dolt
  cleanup semantics. The full native suite is the acceptance oracle.
- Use recursive per-file Python source/test generators. Give conftest, watchdog, stateful
  fixtures, manifests, schemas, scripts, and dynamic imports narrow explicit ownership. Do not
  attach the entire harness or whole source tree to every test.

### Native and full-only boundaries

Keep native full validation for shared or unknown ownership, shared contracts, compatibility
facades, generated inputs, plugin-dynamic changes, multi-module edits, test infrastructure,
dependency/lock changes, runner/build configuration, and ambiguous integrations. Tests expecting
an installed `bh` console script beside `sys.executable` remain native because Pants runtime
package dependencies did not reproduce that contract. A selected-green/native-full-red result is
a hard fail-closed event and disables the selective route.

### Host cache topology

The acceptance phrase "one host-level Pants local store and named cache" is corrected by the
qualified evidence: **share only the immutable content-addressed `local_store_dir`; isolate
mutable `named_caches_dir` and `pants_workdir` per worktree.** The initial shared named-cache trial
followed ENOSPC and produced a partial uv/PEX venv, so it is discarded evidence and must not be
encoded as the architecture. With isolated named caches, a second checkout reused 58 tests from
the shared local store without duplicate execution, and concurrent reads succeeded.

- Run caches under one dedicated service identity with explicit UID/GID ownership and directory
  mode; reject mixed owners and unexpected writable principals.
- Configure Pants local process/file size limits, monitor bytes and inode headroom, and refuse
  selective execution below a documented reserve.
- Emit worktree, target, input digest, executed/avoided/local-cache-served/fallback counts,
  invalidation reason, latency, cache size, and recovery events.
- On corruption, fail closed to native validation and remove only the exact content-addressed or
  per-worktree mutable entry while Pants is stopped. Never clear an unresolved or whole host
  cache opportunistically.

### Remote cache seam

Repository defaults must keep `remote_cache_read = false` and `remote_cache_write = false`; the
initial adoption requires no server or credentials. Preserve environment-owned options for a
future supported REAPI store/provider: address, instance name, TLS/auth headers, and separate
read/write policy. A future enablement requires identical Pants version, generated-lock metadata,
whole-lock behavior, platform/interpreter constraints, security review, and its own oracle trial.

## Rollout and rollback

The implementation molecule must stage Pants behind shadow/oracle mode, record both Pants and
native plans/results, and graduate closures individually. Initial developer commands may expose
Pants explicitly, but canonical `just check` remains native until review approves a later switch.

Rollback is configuration-only: disable the Pants route/cache reads, restore native uv/pytest/just
as the sole executor, retain the evidence and cache telemetry for diagnosis, and remove only
generated Pants artifacts or isolated cache entries owned by the rollout. Never rewrite canonical
`uv.lock`, weaken tests, or keep a stale green verdict after a selector uncertainty.

## Planning instruction

Invoke `bh:replan` on `bh-1dj9x` and file a reviewed adoption molecule linked to all three spike
beads. Decompose work into fixture/target ownership, dependency-lock fidelity, host cache service
and observability, shadow selector/oracle integration, fallback routing, developer UX, and a final
activation decision. Each bead must land through normal review and full validation; no production
configuration is authorized by this decision artifact.
