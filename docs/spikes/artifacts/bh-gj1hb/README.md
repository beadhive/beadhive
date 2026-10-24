# Spike `bh-gj1hb` artifacts — the bead tracker adapter port, runnable

Prototype code for [the spike](../../bh-gj1hb-tracker-port.md). Nothing under `src/` imports
any of it (`test_contract_shape.py` asserts that). No file here talks to GitHub: `conftest.py`
makes every real socket connection and every `time.sleep` raise.

| File | Stands in for |
|---|---|
| `tracker_contracts.py` | `src/beadhive/modules/tracker/contracts/` (records, errors, Protocols); standard library only |
| `conformance.py` | `beadhive.testing.tracker` conformance cases (22 named cases) and the test-only `TrackerWorld` |
| `memory_tracker.py` | `beadhive.testing.tracker.MemoryTracker`, the offline fake consumers substitute |
| `github_adapter.py` | `packages/beadhive-tracker-github/.../adapter.py` (plain `httpx` here; githubkit in production) |
| `fake_github_rest.py` | the GitHub REST stand-in the package's tests would own (reads and writes) |
| `test_port_conformance.py` | one suite, two implementations |
| `test_contract_shape.py` | stdlib-only, no GitHub vocabulary, writes need a keyword-only grant |
| `mutation_controls.py` | 13 negative controls; each must make a case fail |
| `probe_boundary_rules.py` | what today's two import checkers say about the proposed layout |

## Reproduce

Any interpreter with `httpx` and `pytest` (a throwaway venv; the project's `pyproject.toml` and
`uv.lock` are not involved):

```sh
uv venv /tmp/gj && uv pip install --python /tmp/gj/bin/python httpx pytest
cd docs/spikes/artifacts/bh-gj1hb
/tmp/gj/bin/python -m pytest -c /dev/null --rootdir=. -p no:cacheprovider . -q
/tmp/gj/bin/python mutation_controls.py /tmp/gj-mutants
cd - && python3 docs/spikes/artifacts/bh-gj1hb/probe_boundary_rules.py /tmp/gj-boundary
```

## Recorded output (2026-10-24, httpx 0.28.1, pytest 9.1.1)

```text
py3.11: 51 passed in 3.48s
py3.12: 51 passed in 3.78s
py3.13: 51 passed in 3.49s
py3.14: 51 passed in 3.89s
```

The 51 tests are 22 conformance cases for each of `memory` and `github` (44), four contract
shape tests and three GitHub-only probe and marker tests.

Mutation controls (every mutation killed):

```text
github: state=open: issues.checkpoint-incremental-github, issues.closed-included-github, issues.id-snapshot-github
github: keep PRs: issues.change-requests-excluded-github, issues.id-snapshot-github
github: GitHub's default issue ordering: issues.checkpoint-incremental-github, issues.checkpoint-mid-pull-resume-github
github: keep PR comments: issues.change-requests-excluded-github
github: retry an ambiguous write: write.ambiguous-not-retried-github
github: ignore the enabled set: write.refused-when-not-enabled-github
github: no single-wait cap: rate-limit.wait-cap-github
github: no label ownership: labels.only-owned-github
github: checkpoint mid-pull on an unordered listing: comments.checkpoint-safe-at-every-page-github
memory: reusable grants: write.grant-matches-and-single-use-memory
memory: exclusive since: issues.checkpoint-incremental-memory
memory: keep change-request comments: issues.change-requests-excluded-memory
snapshot: a failed listing counts as complete: ids.failed-snapshot-is-incomplete-github, ids.failed-snapshot-is-incomplete-memory
```

Boundary probes against the real `scripts/check_import_boundaries.py` and
`scripts/check_package_imports.py` on a `git archive` copy of the tree:

```text
== baseline: proposed layout
    import-boundary-check: OK (463 files, 4714 import edges, 8 owned legacy cycles/147 edges)
    package-imports: OK
== contracts import httpx
    import-boundary-check: OK (463 files, 4715 import edges, 8 owned legacy cycles/147 edges)
== contracts import githubkit
    import-boundary-check: OK (463 files, 4715 import edges, 8 owned legacy cycles/147 edges)
== tracker application imports githubkit
    import-boundary-check: OK (463 files, 4715 import edges, 8 owned legacy cycles/147 edges)
== tracker application imports httpx
    import-boundary-check: FAILED
    - src/beadhive/modules/tracker/application/__init__.py: forbidden application runtime import beadhive.modules.tracker.application -> httpx
== tracker contracts import work contracts (consumer-side module)
    import-boundary-check: OK (463 files, 4715 import edges, 8 owned legacy cycles/147 edges)
== tracker application imports intake application
    import-boundary-check: FAILED
    - src/beadhive/modules/tracker/application/__init__.py: forbidden dependency direction application/tracker -> application/intake: beadhive.modules.intake.application.pull
== tracker application imports the CLI
    import-boundary-check: FAILED
    - src/beadhive/modules/tracker/application/__init__.py: forbidden application runtime import beadhive.modules.tracker.application -> beadhive.cli
== intake application imports httpx
    import-boundary-check: FAILED
    - src/beadhive/modules/intake/application/pull.py: forbidden application runtime import beadhive.modules.intake.application.pull -> httpx
== plugin adapter imports root internals
    package-imports: FAILED
    packages/beadhive-tracker-github/src/beadhive_tracker_github/adapter.py:2: forbidden import beadhive: plugin packages may use only kernel/module contracts or beadhive.testing
== plugin adapter imports the plugin kernel contracts (wiring)
    package-imports: OK
== root imports the plugin package statically
    package-imports: FAILED
    src/beadhive/modules/intake/application/pull.py:2: forbidden import beadhive_tracker_github: src/beadhive must resolve a plugin package's implementation lazily
== package without plugin.json (library class)
    package-imports: FAILED
    packages/beadhive-tracker-github/src/beadhive_tracker_github/adapter.py:1: forbidden import beadhive.modules.tracker.contracts: library packages may not import beadhive under any path
```

The four `OK` probes after the baseline are the enforcement gaps the spike's recommendation
closes (G1 to G4 in the spike document).
