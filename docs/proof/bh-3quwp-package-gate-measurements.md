# bh-3quwp package gate measurements

Measured on 2026-09-28 UTC on the shared 32-core release host. Native pytest recipes used their
configured 16-worker bound and warm dependency caches. The host was running other v0.20.0 wave
work concurrently, so the successful validation manifests below are the stable before samples;
the overloaded after-state observations are reported as red and are not presented as speedup
proof.

## Selection

The old live catalog expanded `@root-workspace-packages` into `stateful`, `integration`, and
`demos`. For a change under `packages/beadhive-worktrees`, the old selected set was therefore:

```text
packages, stateful, integration, demos
```

The new catalog expands `@root-composition` only on the dedicated key. The same package-only
change selects:

```text
packages, root-composition
```

`tests/test_native_impact_map.py` also proves that a `src/beadhive` change selects every root key
(`unit`, `stateful`, `root-composition`, `integration`, `architecture-contracts`, and `demos`) and
that an unresolvable composition expansion falls back to all keys.

## Package-only before and after

The before total is the sum of successful native-key run manifests for the commands the old
catalog selected. The after total combines the most recent successful `packages` component with
the directly measured new component. Key execution is serial in the selective consumer, so the
component sum is the gate execution time excluding common resolver and clean-checkout setup.

| Catalog | Component | Tests recorded | Wall seconds | Evidence |
| --- | --- | ---: | ---: | --- |
| before | `packages` | 367 | 32.153 | green manifest ending 2026-09-28 07:48:19 UTC |
| before | `stateful` | 9,504 | 340.341 | green manifest `run-e4759fed35f4c535bee9dc55d827f79c` |
| before | `integration` | 72 | 328.981 | green manifest ending 2026-09-27 06:01:16 UTC |
| before | `demos` | not reported | 180.069 | green manifest ending 2026-09-28 16:04:55 UTC |
| **before total** | four selected keys | at least 9,943 | **881.544** | successful components |
| after | `packages` | 367 | 32.153 | same unchanged component |
| after | `root-composition` | 682 collected; 681 passed, 1 skipped | 33.715 | green direct run at `e1348ed7` |
| **after total** | two selected keys | 1,049 collected | **65.868** | successful components |

This component comparison removes 815.676 seconds (92.5%) from the package-only selection. It is
not a claim about end-to-end submit latency because checkout, impact resolution, and carry lookup
were not included in either total.

## Root-change before and after

A root change retains the existing root keys and adds `root-composition`; `stateful-native`
excludes the same 25 registered files that `root-composition-native` owns. The pre-split successful
component sum was 939.531 seconds: unit 2.451, stateful 340.341, integration 328.981,
architecture-contracts 87.689, and demos 180.069 seconds.

The after-state reduced stateful partition collected 8,831 tests. Its release-wave observation
took 850.615 seconds and ended red (8,787 passed, 11 skipped, 27 failed, 6 setup errors). Failures
were dominated by host saturation: 10-second `git config` and `ps` subprocess timeouts, HTTP and
process fixtures that could not start, and concurrent validation receipt interference. Adding the
green 33.715-second root-composition component produces a measured after observation of 884.330
seconds for the two test partitions, or 1,483.520 seconds with the other root-key samples. It is
**not a valid green root-gate timing**. The
authoritative `just check-native` run must be taken after the concurrent certification-flake bead
lands and the release host is below saturation.

The count partition is deterministic despite that load: 8,831 stateful items plus 682 composition
items equals the pre-split 9,513-item collection. Package-only gates pay only the 682-item
composition side; root changes still pay both sides.
