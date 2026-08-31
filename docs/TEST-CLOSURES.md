# Module-local test closures

Beadhive's module-local commands are advisory developer feedback. They do not replace the
authoritative `just check` submit gate or the `just check-all` land/release gate. A green local
closure must not be reported as either full-gate verdict.

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
