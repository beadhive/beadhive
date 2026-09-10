# Module-local test closures

Beadhive's module-local commands are advisory developer feedback. They do not replace the
authoritative `just check` submit gate or the `just check-all` land/release gate. A green local
closure must not be reported as either full-gate verdict.

The checked prerequisite certification is
[`docs/proof/bh-ck1t6.1-test-closure-certification.json`](proof/bh-ck1t6.1-test-closure-certification.json).
It binds every closure to the content of its owned implementation, public port, mandatory tests,
shared-contract inputs, and certification tooling. Each record carries the current pytest
collection count and digest, the best available selector-to-source map, known reverse dependents,
relationship classes, historical timing where one exists, confidence, and fail-closed triggers.
The artifact explicitly does **not** activate selective validation. `bh-ck1t6.2` owns deterministic
selection and `bh-ck1t6.3` owns shadow validation and any provisional local activation.

The checked impact map is [`tests/closures.toml`](../tests/closures.toml). Each present closure
declares three kinds of evidence:

- `tests`: tests owned directly by the kernel, adapter, plugin, contract, integration, or system
  surface;
- `shared_contracts` and `shared_contract_tests`: shared ports, lifecycle, plugin, and schema
  contracts that the surface consumes; and
- `reverse_dependencies` and `reverse_dependency_tests`: known consumers whose behavior can be
  affected even when their paths did not change.

The runner de-duplicates and executes the union. A closure with marker-specific `pytest_args`
runs its direct selection first and its shared/reverse-dependent tests in a second invocation, so
the marker cannot silently filter those supplemental tests. The integration row mirrors the
existing land integration selection, including its two documented `bh-tfapu` deselections.

## Commands

| Scope | Command |
| --- | --- |
| Any registered closure | `just test-closure <closure-id>` |
| Registry and drift validation | `just test-closure-check` |
| Advisory impact plan | `just test-impact-plan <base> [head]` |
| Kernel | `just test-kernel` |
| Future or migrated module | `just test-module <module>` |
| Adapters | `just test-adapters` |
| Registered plugin | `just test-plugin <plugin>` |
| Shared contracts | `just test-contracts` |
| Integration | `just test-integration` |
| System smoke | `just test-system-smoke` |

`uv run python scripts/test_closures.py list` lists every supported ID. `show`, `collect`, and
`run` expose the same checked selection for tooling. An expected future module is an explicit
`absent` row, not an omitted command: it returns success with zero collected tests until its
production directory exists.

## Drift behavior

The drift check parses source with the standard library and does not import product code. It
discovers migrated modules under `src/beadhive/modules/` and optional plugin registrations from
top-level `PLUGIN` assignments. Validation fails when:

- a discovered module or registered plugin has no closure;
- a module directory exists while its row is still `absent`;
- an expected future module loses its explicit row;
- a present closure has no direct tests, selects a missing test file, or references an unknown
  shared contract or reverse dependency; or
- the registry attempts to rename the authoritative full gates.

Every pytest selector is a repository-relative Python path under `tests/` (optionally with a
safe node ID), never an option or arbitrary path. Pytest arguments use a narrow structured
allowlist. Source globs are repository-relative and stay under `src/beadhive/`, `tests/`, or
`docs/`; absolute paths and traversal fail validation. Declaring a shared contract or reverse
dependency without its corresponding test selectors also fails. Pytest's no-tests-collected exit
is reported explicitly rather than being mistaken for a successful closure.

`tests/test_test_closures.py` keeps the drift failures executable. The existing import-boundary
checker remains part of `just check`; this registry complements it with test-impact ownership.
`just test-closure-certification-check` verifies the digest-bound evidence and is also composed
into `architecture-check`. It verifies the immutable historical snapshot against the Git objects
that produced it and that snapshot tree's authoritative completed-green receipt. Current
applicability is checked separately per closure, so a later unrelated commit cannot make the
historical proof pretend to be corrupt.

## Certification semantics

The current evidence uses an explicitly labelled `declared-best-available` mapping. It is finer
than a directory-only path filter because each record unions owned source, public ports, shared
contracts, boundary tests, real-adapter tests, and known reverse-dependent tests. It is not fresh
dynamic per-test coverage, so every record remains `uncertified` with its prerequisites recorded
and is ineligible for selective activation. The oracle is bound to the historical certifying
commit's checkout identity, recomputed from immutable Git blobs for every tracked input except the
generated evidence JSON itself. `--check` resolves that commit's Git tree and `just check` command
hash against the authoritative git-private Beadhive validation ledger. A live exact-tree check may
bootstrap its own receipt only while that certifying checkout owns the exact live, non-zombie
process identity; later descendants require the recorded tree's completed-green receipt. Unknown,
foreign-host, dead, zombie, or recycled-PID owners fail closed. Any non-ignored untracked path also
blocks receipt use because it could influence validation without entering Git history. Git-ignored
caches and environments remain irrelevant. The checked JSON must still match its committed
snapshot field-for-field, including policy/oracle claims, closure eligibility, confidence, timing,
boundary and coverage mappings, collection counts and node-ID digests, and canonical row sequence.
This is historical evidence, not a committed cache or permission to skip the gate.

## Advisory impact selection

`scripts/test_impact_selector.py` takes one Git base/head range and emits canonical JSON. Its pure
policy core combines exact declared ownership, import and reverse-dependency relationships, shared
contracts, per-closure applicability digests, and fresh per-test coverage when present. The plan
names changed closures, dependency paths, exact closure commands and tests, contracts, content
digests, confidence, digest-stable exclusions, and every fallback reason. The selector's own source
digest participates in the plan digest, so changing policy invalidates an earlier plan.

The Git adapter performs only local read queries (`rev-parse`, `merge-base`, `diff`, and `show`).
The selector never executes a test, writes an artifact, contacts a network, or changes the
configured gate. Its source-only dependency loaders suppress Python bytecode writes and restore
both the caller's bytecode flag and any prior module-registry entry, including when loading fails.
Deleted or renamed paths, an ambiguous merge base or ownership relation, multiple
closures, kernel/schema/build/bootstrap/validation/config surfaces, shared contracts, unknown or
unowned paths, stale closure or coverage digests, dynamic imports, subprocess edges, compatibility
facades, generated artifacts, and certification-infrastructure changes all select `just check`.
Path matching alone is never sufficient: a closure plan additionally requires an enforceable port,
complete relationship evidence, digest-current inputs, fresh exact per-test contexts, and a
certified activation-eligible record.

Current applicability binds two independently verified layers. The historical certification row
is compared in full with the selector material presented to the plan, while a canonical registry
definition is derived from both the historical Git snapshot and the current checkout. That
registry digest includes schema and gate metadata plus every closure field: identity, kind,
status, owner and source patterns, pytest arguments, direct selectors, shared contracts and their
tests, and reverse dependents and their tests. Thus a relationship change that preserves a
closure ID—including adding a plugin reverse dependent—fails closed with
`registry-definition-drift`; forged ports, relationship evidence, fallback triggers, coverage, or
other certification material fails with `certification-material-drift`. Both recorded and
observed digests are emitted in the candidate closure's applicability evidence.

Selective planning requires that same canonical applicability proof for every registry row,
including rows excluded as unaffected. The proof must explicitly say `applicable: true`, carry an
empty reason list, and provide equal non-empty recorded/observed pairs for the current input,
registry definition, and certification material digests. Missing, partial, malformed, false, or
digest-mismatched proofs force `just check`. Exclusions report their current status and complete
applicability evidence alongside the historical input digest; an inapplicable closure is therefore
never described as unaffected or stable.

This bead does not provide those final eligibility records: all 23 checked rows remain
`uncertified`, and activation remains disabled. Consequently current repository plans are
auditable shadow inputs that still fall back to `just check`. `bh-ck1t6.3` owns shadow comparison,
escape thresholds, rollback, and any provisional local leaf activation.

## Shadow policy and current activation state

The checked shadow-policy evidence is
[`docs/proof/bh-ck1t6.3-shadow-activation.json`](proof/bh-ck1t6.3-shadow-activation.json).
It binds the prerequisite artifact, selector source/version, and pure policy source. The policy
does not execute tests, write observations, mutate configuration, or replace a lifecycle command.
It evaluates already-recorded, exact-tree selected/full observations and resolves uncertainty to
`just check`.

Production observations must use exact sample, selector-plan, selected-receipt, full-receipt, and
authority schemas. Every sample is bound to a unique merged commit and tree, the current closure
and dynamic-trace revision, a canonically recomputed plan digest, and independently validated
completed receipt digests. Selected tests must be a subset of the full inventory, and exit codes,
failure inventories, relevant failures, and replayed escapes must agree. Selective routing also
requires an exact binding to the current plan range/head/tree, candidate closure row and digest,
source revision, decision digest, and authoritative evidence set; any mismatch runs `just check`.
Caller-provided authority records remain untrusted. The pure policy router is simulation-only and
requires an explicit test opt-in; it has no production capability constructor or caller-supplied
binding input. Production routing is owned end to end by the digest-bound read-only verifier. Its
public entrypoint constructs the repository Git and receipt adapters and derives the symbolic live
checkout ref, exact HEAD, and tree itself; callers cannot choose the ancestry ref. The selected
plan's HEAD and computed tree must match that live snapshot, which is also the ancestry boundary
for every qualifying commit. The verifier reloads both completed receipts from the Git-private
local store, repeats the complete verification, and finally re-reads the live ref/HEAD/tree after
all authority reads. Any stale plan, cross-process replay, or snapshot drift returns `just check`.
No reusable attestation crosses that boundary, and qualifying production evidence sent directly
to the pure router falls back to `just check`.

The live snapshot also requires an empty tracked/index state and no non-ignored untracked paths;
Git-ignored caches remain outside the validation input. Git runs through the pinned absolute
`/usr/bin/git` executable with a fixed system command path; ambient `PATH`, repository override
variables, and global/system configuration cannot select another executable or redirect the
repository authority. Replacement objects are disabled and optional index writes are suppressed.
A non-empty legacy graft or alternate-object file makes the repository unverifiable. The
Git-private receipt directory is opened from the filesystem root one directory component at a
time using no-follow directory descriptors, and every receipt is opened relative to that
descriptor with no-follow, regular-file, size, unique-binding, and duplicate-JSON-key checks. The
full repository, all 60 receipt bindings, and checkout cleanliness are re-read before selection; a
final live snapshot must be identical.

No real closure is currently a shadow candidate. All 23 prerequisite rows are `uncertified`, have
no fresh dynamic per-test contexts, have no qualifying same-tree shadow samples, and therefore
publish unavailable—not estimated—timing, compute, queue, flake, miss, and fallback measurements.
The checked activation set is empty. Simulation-labelled fixtures exercise the positive policy
path without claiming that a repository closure qualifies.

The predeclared eligibility bar is per closure: at least 30 qualifying merged changes spanning at
least 60 days; exact selector and closure-input digests; complete selected and omitted node IDs;
same-tree selected/full commands and outcomes; zero selected-green/full-red relevant escapes; and
a measured median selected wall time no more than half the full median with at least 30 seconds of
median savings. Any escape, selector/input drift, ownership or confidence degradation, unknown or
unowned path, shared/global/build/config/validation-lifecycle change, multi-module ambiguity, or
other full-plan reason restores the full command immediately. The local policy switch provides a
one-change rollback to full and is tested only as pure policy; it is not wired into configuration.

Even after the evidence bar is met, the only provisioned selective scope is local leaf `check`,
`submit`, and pristine-review feedback. Leaf merge, epic finish, final workstream submit/review,
scheduled validation, and release remain full-only. `bh-ck1t6.4` owns any later promotion after
real 30-change/60-day evidence; this bead enables none. For the current workstream, the immediate
benefit is auditable shadow collection and fast focused developer/reviewer feedback. Existing
exact-tree receipt reuse avoids duplicate full executions, while every configured submit/review
gate still runs or reuses the authoritative full gate because the candidate set is empty.

Any affected digest mismatch, unavailable coverage, unenforceable port, unknown ownership,
shared contract/schema change, dynamic plugin or subprocess ambiguity, compatibility facade,
generated artifact, or test-infrastructure change falls back to `just check`. Invalidation is
closure-local: changing one module's owned inputs does not expire an unrelated module's digest.
Changing the certifier or shared test infrastructure in the selected change range forces the full
gate. It does not permanently corrupt unrelated historical closure evidence after that change has
landed and passed its own full gate. A record can become activation-eligible only after the later
shadow-validation bead adds a fresh exact per-test trace, zero unexplained escapes, and a matching
same-tree oracle.

## Certification execution evidence

The prerequisite refresh began from clean source revision `6025df2248e1cd00ab46325587df52460a2ad740`
after the updated lifecycle CLI product-natively refreshed the zero-delta leaf/container from
`7055fec` before developer edits. Those hashes describe provenance only; they are not the
candidate identity. The checked artifact derives its historical identity from the certifying
commit, excluding only its own generated JSON path to avoid self-reference. Later applicability is
computed from closure-local current inputs rather than by pretending the historical identity is
current. The prior refs remain in reflogs and no manual reset or rebase occurred.

The selected cadence was **economical**: one shared pytest collection universe plus the
marker-specific integration collection, focused certification regressions, and the named module
isolation/real-adapter closure before the mandatory full `bh work check` and clean-submit gates.
The final focused boundary run produced 323 passed, 1 skipped, and 1 known Pydantic warning. The
single required `bh work check` produces the external authoritative receipt; exact-tree submit
may then reuse it instead of paying for a duplicate full-suite pass.

## Initial evidence

The following measurements were taken on 2026-08-31 in the hermetic test wrapper from the
candidate tree based on foundation commit `dd6c75e1a847c1d3f9886c1343f52f1277807e57`. Counts are
the tests selected by each closure; wall time is the complete `just` command measured with shell
`time -p`. They are initial evidence, not graduation evidence under the 30-change/60-day rule in
the modular dependency ADR.

| Closure | Selected result | Pytest time | Wall time |
| --- | ---: | ---: | ---: |
| `kernel` | 76 passed | 29.63s | 32.14s |
| `adapters` | 129 passed | 30.23s | 33.00s |
| `plugin.herdr` | 188 passed | 21.47s | 23.58s |
| `plugin.hitch` | 127 passed | 5.67s | 7.68s |
| `plugin.observaloop` | 73 passed, 1 skipped | 4.83s | 7.10s |
| `plugin.orca` | 106 passed | 4.03s | 6.20s |
| `plugin.repowise` | 58 passed, 1 skipped | 3.83s | 6.18s |
| `contracts` | 154 passed | 30.64s | 33.15s |
| `integration` | 84 passed, 1 skipped | 135.73s + 0.16s | 138.84s |
| `system-smoke` | 26 passed | 4.40s | 6.98s |
| `module.agents` (`absent`) | 0 collected | n/a | 1.02s |
| `module.config` (`absent`) | 0 collected | n/a | 1.39s |
| `module.hives` (`absent`) | 0 collected | n/a | 1.04s |
| `module.planning` (`absent`) | 0 collected | n/a | 0.99s |
| `module.state` (`absent`) | 0 collected | n/a | 1.07s |
| `module.work` (`absent`) | 0 collected | n/a | 0.97s |
| `module.worktrees` (`absent`) | 0 collected | n/a | 1.08s |

The unchanged full submit gate on the exact foundation commit collected 7,119 tests and produced
7,108 passed and 11 skipped in 145.29s pytest time (163.64s wall). It also checked 182 Python
files and 2,029 import edges. Candidate closures intentionally remain narrower; `just check` is
still the correctness decision.
