# bh-3quwp package gate measurements

Measured on 2026-09-28 UTC on the final implementation tree based on
`f79080157a536f6220c555bfde9f00be6067b66c`, including the catalog-parser dependency fix and its
regression. Only this numeric proof record changed after the timed commands. Each measurement ran
alone on CPUs 0-7 of the shared 32-core release host at
nice +10, with Pants local parallelism capped at four, warm dependency caches, and the normal
host `TMPDIR`:

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
all root workspace dependencies; it does not claim per-package command narrowing. The final-tree
bound is 27 root files and 700 root pytest items. The accompanying Pants/root-artifact leaf adds
34 Pants-package items, for 734 test items across the complete key.

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
catalog selected. The after total is the wall time of the two exact final-tree attest commands.
Key execution is serial in the selective consumer; neither total includes common resolver or
clean-checkout setup.

| Catalog | Component | Tests recorded | Wall seconds | Evidence |
| --- | --- | ---: | ---: | --- |
| before | `packages` | 367 | 32.153 | green manifest ending 2026-09-28 07:48:19 UTC |
| before | `stateful` | 9,504 | 340.341 | green manifest `run-e4759fed35f4c535bee9dc55d827f79c` |
| before | `integration` | 72 | 328.981 | green manifest ending 2026-09-27 06:01:16 UTC |
| before | `demos` | not reported | 180.069 | green manifest ending 2026-09-28 16:04:55 UTC |
| **before total** | four selected keys | at least 9,943 | **881.544** | successful components |
| after | `packages` | 411 collected; 392 passed, 16 skipped, 3 deselected; five builds green | **24.220** | exact final-tree command above |
| after | `root-composition` | 734 items; 733 passed, 1 skipped; Pants/root wheel smoke green | **49.530** | exact final-tree command above |
| **after total** | two selected keys | 1,145 items | **73.750** | successful final-tree commands |

The measured package-only selection removes 807.794 seconds (91.6%). This is a gate-command
comparison rather than an end-to-end submit latency claim.

## Root-change partition

A root change retains the existing root keys and adds `root-composition`; `stateful-native`
excludes exactly the registered files that `root-composition-native` owns. The optimization moves
those tests into a separately selectable partition for package-only changes. It does not omit
either partition for a root change.

The exact final-tree root-composition measurement breaks down as follows:

| Component | Selection and result | Wall seconds |
| --- | --- | ---: |
| `root-composition-native` | 27 files; 700 items; 699 passed, 1 skipped | **34.740** |
| `root-workspace-check` | 34 Pants-package items passed; Pants package and root wheel builds plus isolated smoke green | **16.510** |
| complete `attest-root-composition` | both components, including recipe overhead | **49.530** |

Before the split, a root change selected `unit`, `stateful`, `integration`,
`architecture-contracts`, and `demos`; the recorded successful component sum was 939.531 seconds
(2.451 + 340.341 + 328.981 + 87.689 + 180.069). After the split, a root change selects those same
keys plus the measured 49.530-second root-composition key, while the 27-file root selection is
removed from `stateful`. The table reports the exact added partition cost and ownership boundary;
it does not combine that final-tree measurement with a stateful timing from a different tree.
