# Modular-foundation exact-tip evidence ledger

Status: measured foundation baseline

Evidence date: 2026-08-31 UTC

Measured source commit: `adf182bc4fc9c628a23b9b76c55f5291ec337b7a`
(`chore(merge): bead bh-inqwc.5`)

This ledger closes `bh-inqwc.6`. Every numeric result below describes the named source commit,
measured from the dedicated `bh-inqwc.6` worktree while it was clean. The commit containing this
document is necessarily an evidence-only child of that source: a Git commit cannot contain its
own hash. Parent-tip results are not relabeled as child-tip results. The child is acceptable only
after the closeout matrix at the end of this document passes again at that one child SHA; its
exact SHA and command results belong in the immutable Beadhive check/review record.

The child adds this document only. It changes no production source, test selector, fixture,
contract, import rule, or gate command. That fact narrows review, but it is not a tree-identity
claim and does not permit reuse of a parent verdict.

## Tool and environment record

| Tool | Recorded version or setting |
| --- | --- |
| Beadhive | `0.15.1+local.gfbc4537` |
| Git | 2.47.3 |
| uv | 0.12.1 |
| just | 1.57.0 |
| Host coverage interpreter | CPython 3.11.15 |
| Hermetic and full-gate interpreter | CPython 3.13.5 |
| pytest | 9.1.1 |
| pytest-xdist | 3.8.0, 12 workers selected by `-n auto` |
| pytest-cov | 7.1.0 |
| coverage.py | 7.15.3, JSON format 3 |
| Ruff | 0.15.20 |
| markdownlint-cli2 | 0.23.2, markdownlint 0.41.1 |
| RepoWise | 0.45.0 |

The first hermetic closure found that the worktree's Python 3.11 virtual-environment symlink was
not resolvable inside the sandbox. uv replaced it with `/usr/bin/python3` 3.13.5. Coverage had
already completed under 3.11.15; the hermetic closures and `just check` then ran under 3.13.5.
Both contexts are recorded instead of presenting them as one interpreter.

Version commands:

```text
git --version
bh --version
uv --version
just --version
uv run python --version
uv run pytest --version
uv run ruff --version
uv run coverage --version
uv run python -c 'from importlib.metadata import version; print(version("pytest-xdist")); print(version("pytest-cov"))'
markdownlint-cli2 --version
repowise --version
```

## Import graph, cycles, and fan-in

The standard-library AST collector from `scripts/check_import_boundaries.py` found 182 Python
modules and 2,029 module import edges. It found two nonliteral dynamic-import call sites; the
architecture checker verifies their owning roles under its fail-closed rule. Fan-in here means
the number of distinct importing Python modules, not occurrences, runtime calls, or external
consumers.

The six owned legacy strongly connected components contain 85 modules, 288 edges, and 318
imported symbols. The exact cyclic-edge digest remains
`4a0d4a3a082713f054559ecfa8b3372744b3c4fc30d296ec35283e53a63f7821`, as checked against
`docs/design/import-boundary-exceptions.toml`. Removing the 44 exact feedback edges in that
ledger makes the graph acyclic; the normal checker rejects any new or altered cyclic edge.

| SCC | Size | Members |
| ---: | ---: | --- |
| 1 | 65 | `agent_run_summary_reader`, `bd`, `compose`, `config`, `converge`, `coordination`, `credentials`, `deps`, `dispatch_log`, `dolt_health`, `engine`, `fleet`, `ghpr`, `git_linkage`, `gitref`, `gitworkspace`, `guard`, `harness`, `herdr_plugin`, `herdr_views`, `hitch_plugin`, `hive_identity`, `hive_sync`, `host`, `host_adopt`, `host_fence`, `host_lease`, `hosts`, `identity`, `localloop`, `log`, `metadata`, `observaloop`, `observaloop_env`, `operator_contract`, `operator_sources`, `operator_work_items`, `orca`, `otel`, `plugins`, `private_paths`, `public_readers`, `registry`, `release_order`, `repowise_plugin`, `role`, `route`, `run`, `run_journal`, `runtime`, `safety`, `schedule`, `setup`, `state_stream_epic_schedule`, `state_stream_gate_projection`, `state_stream_polling`, `state_stream_process`, `triage_store`, `validate`, `validation_ledger`, `validation_records`, `work_group`, `work_logic`, `worktree`, `worktree_merge` |
| 2 | 8 | `alerts`, `doctor`, `host_cli`, `host_daemon`, `host_provision`, `host_retire`, `mcp`, `operator_sse` |
| 3 | 6 | `backup`, `hive`, `hq`, `hub`, `onboard`, `storage_migrate` |
| 4 | 2 | `plan`, `plan_repair` |
| 5 | 2 | `report`, `triage` |
| 6 | 2 | `work`, `work_show` |

Largest distinct-importer fan-in values:

| Module | Fan-in |
| --- | ---: |
| `beadhive.config` | 95 |
| `beadhive.registry` | 65 |
| `beadhive.run` | 51 |
| `beadhive.bd` | 37 |
| `beadhive.identity` | 37 |
| `beadhive.log` | 22 |
| `beadhive.worktree` | 22 |
| `beadhive.host` | 21 |
| `beadhive.otel` | 19 |
| `beadhive.guard` | 16 |

The three compatibility facades have fan-in `config=95`, `work=3`, and `worktree=22`. These are
static in-repository importers only. Dynamic imports, subprocess entry points, documentation,
other repositories, and downstream package consumers are outside this number.

The graph measurement used the collector's exact edge model:

```text
uv run python -c 'import json; from pathlib import Path; from scripts import check_import_boundaries as b; modules,edges,dynamic=b.collect_imports(Path("src")); components,cyclic=b._cyclic_edges(modules,edges); fan_in={module:sorted({edge.importer for edge in edges if edge.imported_module==module}) for module in modules}; print(json.dumps({"files":len(modules),"import_edges":len(edges),"dynamic":len(dynamic),"sccs":[sorted(component) for component in components],"cyclic_edges":len(cyclic),"cyclic_symbols":sum(len(edge.symbols) for edge in cyclic),"fan_in":{module:len(importers) for module,importers in fan_in.items()}},sort_keys=True))'
just architecture-check
```

## Static test blast radius

This is the checked `.5` closure registry's static selection, not a dynamic execution map. Each
present row is the de-duplicated union of its direct, shared-contract, and reverse-dependent
selectors. `collect` ran at the measured source commit; integration collection used its two
registered pytest invocations but did not execute the integration suite.

| Closure | Direct/shared/reverse selectors | Collected tests |
| --- | ---: | ---: |
| `kernel` | 2 / 1 / 2 | 76 |
| `adapters` | 4 / 2 / 2 | 129 |
| `plugin.herdr` | 1 / 1 / 2 | 188 |
| `plugin.hitch` | 1 / 1 / 2 | 127 |
| `plugin.observaloop` | 1 / 1 / 2 | 74 |
| `plugin.orca` | 1 / 1 / 2 | 106 |
| `plugin.repowise` | 1 / 1 / 2 | 59 |
| `contracts` | 5 / 0 / 5 | 154 |
| `integration` | 1 / 1 / 0 | 85 |
| `system-smoke` | 3 / 1 / 0 | 26 |
| `module.agents` | absent | 0 |
| `module.config` | absent | 0 |
| `module.hives` | absent | 0 |
| `module.planning` | absent | 0 |
| `module.state` | absent | 0 |
| `module.work` | absent | 0 |
| `module.worktrees` | absent | 0 |

Commands:

```text
uv run python scripts/test_closures.py check
uv run python scripts/test_closures.py list
uv run python scripts/test_closures.py collect <present-closure-id>
```

The counts overlap because shared contracts and reverse dependents intentionally appear in more
than one closure. Summing rows would overstate the repository's test count. The map records known
static impact; it does not prove that an unregistered runtime dependency cannot exist.

## Coverage and RepoWise freshness

Repository-native non-integration statement coverage:

| Measurement | Value |
| --- | ---: |
| Selected tests | 7,146 |
| Result | 7,134 passed, 12 skipped |
| Pytest / wall time | 239.36s / 241.22s |
| Covered / executable statements | 35,139 / 39,700 |
| Missing statements | 4,561 |
| Excluded lines | 130 |
| Exact statement coverage | 88.51133501259446% |
| Branch coverage | absent |
| Per-test contexts | absent |

Commands:

```text
time -p just cov
uv run coverage json -o /tmp/bh-inqwc-6-coverage.json
jq '{meta:.meta, totals:.totals}' /tmp/bh-inqwc-6-coverage.json
```

The temporary JSON report is generated evidence and is not committed. Its metadata records
coverage.py format 3, version 7.15.3, `branch_coverage=false`, and `show_contexts=false`.

RepoWise's Beadhive wrapper reported `ok`, zero commits behind, and 111.8 MiB. Native status from
the worktree named the exact source commit as `last_sync_commit`, with 830 analyzed files,
average health 7.3, hotspot health 5.78, and 3,586 open findings. This proves index freshness for
static structure/health questions only.

RepoWise has no coverage or per-test map for Beadhive at this commit. Canonical-repository
`coverage status` returned `coverage=null` and `test_map=null`. `impacted-tests
adf182bc^..adf182bc` returned `map_empty=true`, no impacted or inferred tests, and all six `.5`
files as unknown. A worktree-path coverage query first resolved the 34-repository workspace's
primary `qm` repository instead of Beadhive; the canonical Beadhive path was therefore used to
distinguish that resolution behavior from Beadhive's genuinely empty map.

Read-only probes:

```text
bh plugin repowise status
repowise status . --no-workspace --format json
repowise coverage status --path /home/bees/workspace/github/beadhive/beadhive --format json
repowise impacted-tests adf182bc^..adf182bc --path /home/bees/workspace/github/beadhive/beadhive --format json
```

The exact index was not refreshed and coverage was not ingested. No paid/model-backed operation
ran. RepoWise health is a static signal, not a correctness score; its open-finding count was not
triaged by this evidence bead.

## Measured-source gate matrix

All rows below passed at the named measured source commit. They establish the parent baseline;
they do not substitute for the evidence-child closeout matrix.

| Concern | Command | Result | Wall time |
| --- | --- | --- | ---: |
| Architecture | `just architecture-check` | 182 files, 2,029 edges, 6 owned SCCs / 288 cyclic edges | 1.78s |
| Closure drift | `just test-closure-check` | 10 present, 7 absent | 1.08s |
| Absent module | `just test-module agents` | explicit absence, zero collected | 1.23s |
| Contract/conformance | `just test-contracts` | 154 passed | 38.48s |
| Kernel/module-local | `just test-kernel` | 76 passed | 31.61s |
| Hermetic contract | `uv run pytest tests/test_hermetic_fence.py -q` | 12 passed, 4 skipped | 32.09s |
| Full submit gate | `just check` | 7,135 passed, 11 skipped; all static/policy gates green | 159.90s |

The four hermetic skips are platform/capability-conditioned cases reported by the existing
suite, not newly deselected tests. The full submit gate remains authoritative. `just check-all`
remains the land/release gate and is not replaced by any closure in this ledger.

## Downstream extraction exit criteria

Every downstream module-extraction epic must publish before/after values at named 40-character
commits and meet all applicable criteria:

1. **Clean provenance:** dirty paths are zero; tool versions, exact commands, skips,
   quarantines, collection counts, and wall times are recorded.
2. **Architecture:** unowned forbidden imports and unowned SCCs are both zero. Until an owned
   debt reduction lands, SCC count must be at most 6, cyclic modules at most 85, largest SCC at
   most 65, cyclic edges at most 288, and cyclic symbols at most 318. Any increase fails; a
   reviewed exact ledger amendment is not silently classified as improvement.
3. **Fan-in:** before/after distinct-importer fan-in is reported for every moved public contract,
   adapter, and retained facade. New imports of the legacy `config`, `work`, or `worktree` facade
   must not raise their 95, 3, and 22 baselines without an owned compatibility-ledger amendment.
4. **Contracts and isolation:** the module, shared-contract, reverse-dependent, required
   integration/system, import-boundary, closure-drift, and independence-sentinel commands all
   exit zero. Each present closure collects at least one test; each absent closure collects
   exactly zero and has no production directory.
5. **Coverage:** non-integration statement coverage is reported as covered/total statements and
   must remain at least 88.51133501259446% unless an explicitly reviewed waiver names the lost
   behavior and follow-up owner. The extracted module reports its own numerator/denominator;
   rounded display percentages are insufficient.
6. **Selective-test graduation:** a fresh dynamic per-test map at the candidate commit is
   mandatory, must cover every changed production file, and must agree with the registered
   closure. A shadow period must include at least 30 qualifying merged changes and 60 days, with
   zero selected-green/full-red relevant escapes. Any escape resets the window and restores the
   full gate.
7. **Final gates:** `just check` and the repository's applicable land/release gate both exit zero
   at the candidate commit. A closure result is never presented as either verdict.

Criteria 2, 4, 6, and 7 are binary pass/fail after applying their stated numeric limits. The
coverage threshold is an extraction-program evidence floor, not a change to pytest, CI, or the
repository's configured correctness gate.

## Limitations and non-claims

- There is no fresh dynamic per-test coverage map. The closure registry is static and
  conservatively includes known shared contracts and reverse dependents.
- Coverage is non-integration statement coverage only. It has no branch data or per-test
  contexts and no configured global fail-under.
- Static imports do not enumerate reflection, dynamic plugin/runtime loading, subprocess calls,
  generated code, other repositories, or external package consumers.
- The current 6-SCC legacy snapshot is owned debt, not an architectural target or proof of good
  modularity. Downstream work should reduce it without changing unowned edges.
- RepoWise's fresh source index does not make its absent coverage/test map current; those are
  separate data products.
- The evidence-only child still requires its own complete closeout matrix. Parent results are
  historical measurements, not attestations for a different commit.

Most importantly, this ledger makes **no claim that selective merge testing is safe**. `just
check` remains the submit correctness gate, and `just check-all` remains the land/release gate,
until every graduation criterion above is satisfied.

## Evidence-child closeout protocol

After committing this document, record the child SHA externally and run every command below at
that one clean SHA. All must exit zero. The Beadhive check/review packet supplies the exact-child
record without asking this file to self-reference.

```text
git status --short
git rev-parse HEAD
just architecture-check
just test-closure-check
just test-contracts
just test-kernel
just test-module agents
uv run pytest tests/test_hermetic_fence.py -q
bh work check bh-inqwc.6
```
