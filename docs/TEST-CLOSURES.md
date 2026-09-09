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
into `architecture-check`, so an owned source, port, shared contract, selector, or certifier change
cannot silently leave a current-looking record behind.

## Certification semantics

The current evidence uses an explicitly labelled `declared-best-available` mapping. It is finer
than a directory-only path filter because each record unions owned source, public ports, shared
contracts, boundary tests, real-adapter tests, and known reverse-dependent tests. It is not fresh
dynamic per-test coverage, so every record remains `uncertified` with its prerequisites recorded
and is ineligible for selective activation. The oracle is bound to a checkout-derived identity of
every tracked input except the generated evidence JSON itself. Its source revision, source tree,
and full-gate lookup identity are independently recomputed rather than trusted as artifact
constants. `--check` resolves the candidate Git tree and `just check` command hash against the
authoritative git-private Beadhive validation ledger. A live exact-tree check may bootstrap its
own receipt only when its manifest names this host and its PID is live, non-zombie, and has the
exact recorded process-start token. Unknown, foreign-host, dead, zombie, or recycled-PID owners
fail closed. Any non-ignored untracked path also blocks both running and completed receipt use,
because a source or test outside `git ls-files` could affect execution without entering the
identity. Git-ignored caches and environments remain irrelevant. Every later check requires the
completed green receipt. Because the generated JSON is the identity's sole self-reference
exclusion, the checker re-derives its complete material schema: policy and oracle claims, closure
certification and eligibility, confidence and timing, boundary and coverage mappings, and the
current pytest collection count and node-ID digest. Collection wall time is intentionally not a
stored claim because it cannot be reproduced exactly. Closure rows must also match the registry
one-for-one in canonical order; duplicate, missing, reordered, or extra rows fail before any
ID-indexed comparison. This is evidence, not a committed cache or permission to skip the gate.

Any affected digest mismatch, unavailable coverage, unenforceable port, unknown ownership,
shared contract/schema change, dynamic plugin or subprocess ambiguity, compatibility facade,
generated artifact, or test-infrastructure change falls back to `just check`. Invalidation is
closure-local: changing one module's owned inputs does not expire an unrelated module's digest.
Changing the certifier or shared test infrastructure intentionally expires every record. A record
can become activation-eligible only after the later selector and shadow-validation beads add a
fresh exact per-test trace, zero unexplained escapes, and a matching same-tree oracle.

## Certification execution evidence

The prerequisite refresh began from clean source revision `6025df2248e1cd00ab46325587df52460a2ad740`
after the updated lifecycle CLI product-natively refreshed the zero-delta leaf/container from
`7055fec` before developer edits. Those hashes describe provenance only; they are not the
candidate identity. The checked artifact derives its current identity from the candidate
checkout, excluding only its own generated JSON path to avoid self-reference. The prior refs
remain in reflogs and no manual reset or rebase occurred.

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
