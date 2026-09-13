# bh-1dj9x.1: Pants-first selective build graph spike

## Question

Can Pants materially reduce Beadhive's build/test cycle time and unnecessary test churn while
preserving the canonical uv dependency contract, pytest/xdist behavior, hermeticity, and
fail-closed selector correctness?

## Method

The prototype ran only in the disposable issue worktree at canonical commit
`b05143da3058cbf372b180e0b92415fc0c9f1133`. The canonical `uv.lock` SHA-256 remained
`3c1548a9feef8bc05510870f33762a40e33c3f6fdbb1b510f5e153c72d838818`.

Pants 2.32.0 and 2.32.1 were bootstrapped under `/tmp`. Temporary `pants.toml`, BUILD files,
Pants-generated lock/metadata, workdirs, caches, and traces were not committed. The viable 2.32.1
prototype used:

- `resolver = "uv"`, resolves enabled, and `run_against_entire_lockfile = true`;
- `[pytest] install_from_resolve = "beadhive"` with pytest, xdist, and coverage as separate
  requirement targets;
- recursive per-file `python_sources` and `python_tests` generators;
- explicit ownership for `conftest.py`, `stateful_fixtures.py`, and the string-imported
  `harness.watchdog_diagnostics` plugin; and
- explicit resources/dynamic imports where representative tests required them.

The same representative relationships came from `tests/closures.toml`: module ownership and
reverse dependents, shared contracts, compatibility facades, generated schemas, plugins,
subprocess behavior, and integration behavior. Native and Pants paths were timed, Pants was run
with and without pantsd, its test cache was observed, dependency and source mutations were
attempted, and selected results were compared with the native same-tree behavior.

## Evidence

### Experimental uv resolver and dependency fidelity

- Pants 2.32.0 explicitly reported `Generate uv lockfile for beadhive`; the cold command took
  9.195 seconds and warm resolver work about 0.35 seconds. It proved the experimental resolver
  can solve this repository's modeled inputs.
- PEP 621 core and optional `otel` dependencies were parsed. PEP 735
  `[dependency-groups].dev` was not automatically modeled; four explicit targets were required.
  Upstream Pants issue #22176 tracks dependency-group support.
- Canonical `uv export --all-groups --all-extras` resolves 114 packages, including
  pytest 9.1.1, pytest-xdist 3.8.0, pytest-cov 7.1.0, ruff 0.15.20, and commitizen 4.16.4.
  Pants generated a second uv lock from BUILD-modeled requirements rather than consuming the
  canonical `uv.lock` directly.
- Mixing canonical pytest 9 into tests while retaining Pants' bundled pytest tool crashed at
  `_pytest.config._console_main`. This was an invalid mixed-tool configuration, not the final
  compatibility verdict.
- The documented coherent tool configuration, `[pytest].install_from_resolve`, eliminated that
  collision. On 2.32.0, uv lock materialization omitted the explicitly modeled dev packages;
  `run_against_entire_lockfile` then failed with a missing cached venv executable.
- Pants 2.32.1 fixed that uv sync path. With the entire-lock option it ran the canonical pytest
  9.1.1, xdist 3.8.0, and coverage 7.1.0 environment successfully. This establishes uv resolver
  and relevant test-group compatibility, with two qualifications: Pants requires a second lock
  and every lock change conservatively invalidates every Python test because the entire lock is
  installed.

### Representative execution and correctness

Pants discovered about 430 per-file test targets. After narrow source/test utility modeling:

- module config: green, 58 tests;
- reverse-dependent config boundary: green;
- shared conformance contract: green;
- compatibility facade: green after explicitly modeling dynamic imports;
- generated/wire schema: green after explicitly including its script/schema inputs;
- subprocess state-stream path: green;
- plugin path: green after explicitly owning built-in manifest JSON; and
- integration stream demo: 10/11 green, with the remaining test expecting the installed `bh`
  console script next to `sys.executable`.

A `python_distribution` runtime-package dependency built successfully but did not put `bh` next
to the Pants pytest interpreter. Reproducing that native installed-console-script assumption
would require a different test contract or a dedicated executable fixture. The correct Pants
route is therefore a native/full fallback for that integration case, not weakening the test.

There was no selected-green/full-red relevant escape in the cases Pants could execute. Pants
failed closed on missing conftest/plugin/resource/dynamic-import ownership. However, the initially
minimal graph omitted relationships that native pytest obtains from the checkout layout; those
edges had to be discovered through failures. Production use would require completing and
auditing those edges before selection could be trusted.

### Timing, cache, and churn

| Path | State | Wall time | Outcome |
|---|---:|---:|---|
| Native module file | first measured | 1.605 s | 58 passed |
| Native module file | warm repeat | 1.772 s | 58 passed |
| Pants 2.32.1, no pantsd | first green | 15.791 s | 58 passed; test 1.85 s |
| Pants 2.32.1, no pantsd | local cache hit | 13.755 s | 58 cache-served |
| Pants 2.32.1, pantsd | first daemon request | 15.487 s | cache-served |
| Pants 2.32.1, pantsd | warm memoized | 0.429 s | 58 cache-served |
| Eight-file representative matrix | explicit broad repair | 79.550 s | 6 green, 2 setup failures |
| Plugin + integration repair | warm | 40.343 s | plugin green; integration 10/11 |

The steady-state no-edit Pants result is materially faster: 0.429 seconds versus 1.6–1.8 seconds,
and it demonstrably served 58 tests from cache. Pants output makes target success/cache state
auditable. Without pantsd, its 13–16 second graph/startup overhead is materially slower.

The more important edit loop did not improve. `conftest.py` unconditionally string-imports
`stateful_fixtures`; its inferred transitive closure includes `worktree.py`, `wt_status.py`, and a
large part of the product. After a content-only edit to `wt_status.py`, the otherwise unrelated
config module test reran instead of hitting cache (1.988 seconds wall). A deliberately broad
whole-source workaround also reran it (22.189 seconds wall). Thus Pants correctly reflects the
repository's present shared-fixture dependency, but cannot reduce that churn without first
splitting the global fixture topology. Tests avoided after a meaningful representative source
edit: zero for this leaf case. Cache-served tests: 58 only for a no-edit repeat.

A final disposable topology experiment removed `stateful_fixtures` from the root plugin list and
from the config leaf's test dependencies (all edits were reverted). This models the intended
follow-on: attach stateful scopes only to consumers and keep the pure collection guard in a small
plugin that does not import the stateful world. Results changed decisively:

- decomposed baseline: 58 green, 2.300 seconds wall;
- edit to unrelated `wt_status.py`: 58 cache-served/memoized, 0.358 seconds wall;
- edit to relevant `modules/config/application/resolution.py`: 58 executed, 1.437 seconds wall;
- native same-tree oracle for the relevant edit: 58 green, 1.145 seconds wall.

The Pants selector therefore avoided all 58 executions for the unrelated edit and invalidated all
58 for the relevant edit, with no selected-green/oracle-red escape. This proves Pants can deliver
the desired behavior after fixture decomposition. It does not make the current topology a GO:
simply dropping the plugin also drops collection policy and isolation fixtures, so the follow-on
must split and preserve those semantics, then validate the complete suite.

Pants' per-file processes provide parallelism and cache granularity; canonical xdist loaded and
worked, although `xdist_enabled` was not enabled because Pants already partitions per target.
Hermetic sandboxes correctly exposed undeclared scripts, manifests, dynamic imports, and console
scripts. Warm offline execution is plausible from the complete named cache but was not claimed as
fully proven: the entire-lock mode and runtime-package build had already filled several gigabytes
of disposable cache, and the integration console-script case remained a native fallback.

### Setup and maintenance cost

The working prototype grew from roughly 30 lines in four files to explicit dev requirements,
whole-lock behavior, custom pytest tool resolution, shared plugin utilities, resources, dynamic
imports, and a runtime distribution experiment. Remaining third-party mappings emitted warnings
for pydantic, pydantic-core, yaml, mcp, OpenTelemetry, platformdirs, and cryptography. These are
fixable mappings, but they add a second dependency graph and second lock beside the checked native
closure registry and canonical uv lock.

The highest-value obstacle is local rather than upstream: global `stateful_fixtures` makes many
apparently unrelated files genuine transitive test dependencies. Pants cannot safely avoid those
tests until that shared fixture is decomposed. Pants 2.32.1 also must be the minimum version;
2.32.0's uv lock sync failure is already fixed upstream, so no new patch should target it. A useful
upstream contribution would instead be PEP 735 dependency-group ingestion (issue #22176) or clearer
diagnostics around entire-lock/test-tool resolves.

## Verdict

**GO-Pants for implementation planning, conditional on fixture decomposition and conservative
fallbacks. No current activation.**

Pants 2.32.1 passes the experimental uv-resolver feasibility test, runs the canonical pytest 9
environment, has an excellent 0.429-second no-edit pantsd cache hit, and—after the disposable
fixture decomposition—correctly avoided 58 tests for an unrelated edit while rerunning them for a
relevant edit with a green native oracle. That is sufficient to choose Pants for the next
implementation-planning step and stop framework comparison.

This is not approval to activate Pants on the current tree. Implementation is gated on splitting
the global fixture closure without losing policy/isolation behavior, retaining native/full
fallbacks for installed-console-script and full-only boundaries, accepting or eliminating the
whole-lock invalidation tradeoff, completing the narrow resource/dynamic-import graph, and proving
the complete selector against the full-suite oracle. Until those prerequisites land, uv/pytest/
just remains the production execution path.

No Bazel command, file, target, or measurement was produced. Because Pants receives a conditional
GO for implementation planning, Bazel evaluation is deferred to a separate backlog spike as the
operator requested.

## Recommendation

Keep uv/pytest/just and the existing checked closure selector. First reduce the real dependency
closure by splitting `stateful_fixtures`/global conftest responsibilities and continue shadowing
selected closures against the same-tree full-suite oracle. Add executed, avoided, cache-served,
and fallback counts to its operational artifact.

File a bounded fixture-decomposition follow-on: separate pure collection policy from stateful
fixtures, attach stateful scopes only to inventoried consumers, and prove the full native suite is
unchanged. Then revisit Pants with Pants 2.32.1 or newer. Require one canonical dependency
source (or automated exact lock-fidelity proof), narrow explicit dynamic/resource edges, native
fallback for installed-console-script integration tests, pantsd warm measurements, meaningful-edit
cache wins, offline proof, and zero selected-green/full-red escapes before GO.
