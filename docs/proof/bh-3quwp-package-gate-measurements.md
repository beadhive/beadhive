# bh-3quwp package gate measurements

Measured on 2026-09-28 UTC on the exact command implementation at
`f79080157a536f6220c555bfde9f00be6067b66c`, with the uncommitted catalog-parser dependency fix and
its regression applied. The later `ec76900b` commit only applied canonical Ruff formatting and did
not change these commands, their behavior, or their selected test counts. Each measurement ran
alone on CPUs 0-7 of the shared 32-core release host at nice +10, with Pants local parallelism
capped at four, warm dependency caches, and the normal host `TMPDIR`:

```text
taskset -c 0-7 nice -n 10 env PANTS_PROCESS_EXECUTION_LOCAL_PARALLELISM=4 \
  bash -c 'time -p just attest-packages'
taskset -c 0-7 nice -n 10 env PANTS_PROCESS_EXECUTION_LOCAL_PARALLELISM=4 \
  bash -c 'time -p just attest-root-composition'
```

The component timings used the same wrapper around `just root-composition-native` and
`just root-workspace-check`. No proof or gate workload overlapped these commands.

## Selection

The old live catalog expanded `@root-workspace-packages` into `stateful`, `integration`, and
`demos`. For a change under `packages/beadhive-worktrees`, the old selected set was:

```text
packages, stateful, integration, demos
```

The new catalog expands `@root-composition` only on the dedicated key. The same package-only
change selects:

```text
packages, root-composition
```

Attest commands are opaque strings and receive no changed-package argument. The shared
`root-composition` command intentionally runs the deduplicated union of registered root tests for
all root workspace dependencies; it does not claim per-package command narrowing. The measured
command bound is 27 root files and 700 root pytest items. The accompanying Pants/root-artifact
leaf adds 34 Pants-package items, for 734 test items across the complete key.

Impact resolution rejects any derived root dependency without a `PACKAGE_TESTS` mapping and falls
back to the full key set. Its dependency closure unions direct requirements, optional
requirements, dependency groups, and workspace source roots vendored into the root wheel. Direct
root test imports are checked against the same mapping so a newly added root consumer cannot be
silently omitted. The repository regression resolves all six current consumed distributions.

`tests/test_native_impact_map.py` also proves that a `src/beadhive` change selects every root key
(`unit`, `stateful`, `root-composition`, `integration`, `architecture-contracts`, and `demos`) and
that an unresolvable composition expansion falls back to all keys.

## Package-only before and after

The before total is the sum of successful native-key run manifests for the commands the old
catalog selected. The after total is the wall time of the two exact measured attest commands.
Key execution is serial in the selective consumer; neither total includes common resolver or
clean-checkout setup.

| Catalog | Component | Tests recorded | Wall seconds | Evidence |
| --- | --- | ---: | ---: | --- |
| before | `packages` | 367 | 32.153 | green manifest ending 2026-09-28 07:48:19 UTC |
| before | `stateful` | 9,504 | 340.341 | green manifest `run-e4759fed35f4c535bee9dc55d827f79c` |
| before | `integration` | 72 | 328.981 | green manifest ending 2026-09-27 06:01:16 UTC |
| before | `demos` | not reported | 180.069 | green manifest ending 2026-09-28 16:04:55 UTC |
| **before total** | four selected keys | at least 9,943 | **881.544** | successful components |
| after | `packages` | 411 collected; 392 passed, 16 skipped, 3 deselected; five builds green | **24.220** | exact measured command above |
| after | `root-composition` | 734 items; 733 passed, 1 skipped; Pants/root wheel smoke green | **49.530** | exact measured command above |
| **after total** | two selected keys | 1,145 items | **73.750** | successful measured commands |

The measured package-only selection removes 807.794 seconds (91.6%). This is a gate-command
comparison rather than an end-to-end submit latency claim.

## Root-change partition

A root change retains the existing root keys and adds `root-composition`; `stateful-native`
excludes exactly the registered files that `root-composition-native` owns. The optimization moves
those tests into a separately selectable partition for package-only changes. It does not omit
either partition for a root change.

The exact bh-3quwp package-only root-composition measurement breaks down as follows:

| Component | Selection and result | Wall seconds |
| --- | --- | ---: |
| `root-composition-native` | 27 files; 700 items; 699 passed, 1 skipped | **34.740** |
| `root-workspace-check` | 34 Pants-package items passed; Pants package and root wheel builds plus isolated smoke green | **16.510** |
| complete `attest-root-composition` | both components, including recipe overhead | **49.530** |

## Root change before and after

Before the split, a root change selected `unit`, `stateful`, `integration`,
`architecture-contracts`, and `demos`; the recorded successful component sum was 939.531 seconds
(2.451 + 340.341 + 328.981 + 87.689 + 180.069).

The green after measurement used the intended combined v0.20.0 wave tree. It contains bh-3quwp
plus the required cross-blocker fixes from bh-selwz, the corrected sweep proof, and bh-94qmw. The
combined head was `7f9d4ba6` and its Git tree was `89cc1ed8`.

| Selected key | Result | Wall seconds |
| --- | --- | ---: |
| `unit` | lint and licence policy green | 1.690 |
| `stateful` | 8,857 collected; 8,846 passed, 11 skipped | 461.010 |
| `root-composition` | root 699 passed/1 skipped of 700; Pants 34 passed; release smoke green | 51.370 |
| `integration` | 69 collected; 65 passed, 4 skipped | 375.750 |
| `architecture-contracts` | green | 83.490 |
| `demos` | both demos green | 158.410 |
| **after total** | all six selected keys green | **1,131.720** |

The unit and stateful logs are under
`/data/bees/beadhive/b94/e/20260929T002430Z/`; the remaining key logs are under
`/data/bees/beadhive/b94/e/20260929T003400Z/`.

The like-for-like root-change total increased from 939.531 to 1,131.720 seconds: 192.189 seconds,
or 20.46%, because the new selection adds the `root-composition` key. As a secondary comparison,
the five corresponding after keys excluding that addition total 1,080.350 seconds, 140.819 seconds
(14.99%) above the old five-key sample.

## Separate native profile evidence

The same combined tree also passed the narrower `check-native` profile. This profile result is not
the root-change comparison above because it does not execute the same selected key set. The exact
command, run from the managed bh-94qmw worktree, was:

```bash
export UV_CACHE_DIR=/data/bees/beadhive/b94/uv-cache
export UV_OFFLINE=1
export PANTS_PROCESS_EXECUTION_LOCAL_PARALLELISM=4
unset TMPDIR
taskset -c 0-7 nice -n 10 bash -c 'time -p just --time --timestamp check-native' \
  2>&1 | tee -a /data/bees/beadhive/b94/e/20260929T001030Z/check-native.log
```

| Native profile component | Result | Wall seconds |
| --- | --- | ---: |
| `lint` | Ruff check and format check green | 0.244 |
| `lint-md` | 210 Markdown files green | 7.797 |
| SBOM | green | 0.066 |
| licence policy | green | 0.950 |
| `architecture-structural-check` | green | 85.136 |
| `root-composition-validate` | green | 4.136 |
| `stateful-native` | 8,857 collected; 8,846 passed, 11 skipped | 455.617 |
| `root-composition-native` | 700 items; 699 passed, 1 skipped | 30.715 |
| `beads-client-check` | 30 collected; 27 passed, 3 deselected; regeneration, sync, and build green | 12.537 |
| **`just check-native` total** | all components green; user 112.10s, system 10.34s | **597.210** |

The package-only result remains 73.750 seconds because package impact selects `packages` and
`root-composition` rather than the complete root gate.
