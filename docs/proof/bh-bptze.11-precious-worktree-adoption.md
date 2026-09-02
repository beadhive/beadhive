# bh-bptze.11 precious-worktree adoption

## Provenance and approval boundary

This reconciliation starts from capability baseline
`90deb27bd24806e25f9a01a7e50d622acb5fb9de` (tree
`8339205d818e1c95705f749a8f7add583e64d0a0`) and merges approved source tip
`06246bd19198c8238bf5e3b6a5bbd4bdc3a2085c` (tree
`decf31b67cfa48f6ce3fc51d0d0f8da5b3586feb`) as its ordered second parent. The source range over
main contains exactly four Good-signed commits:

| Commit | Parents | Accepted behavior |
| --- | --- | --- |
| `aeb1d06921a1db5ac3a1a8be63404571c625a9db` | `739349806ead27c94282219b1befb796ba73b583` | precious-content scanner and taxonomy |
| `64c36341410ac73c334028f678613ca2a08216d9` | main, `aeb1d069...` | source merge for `bh-9yrn.1` |
| `e707fc767e9527d0471ae920794268d0ac70dd45` | `64c36341410ac73c334028f678613ca2a08216d9` | `WtStatus` safety overlay and inventory threading |
| `06246bd19198c8238bf5e3b6a5bbd4bdc3a2085c` | `64c36341410ac73c334028f678613ca2a08216d9`, `e707fc767e9527d0471ae920794268d0ac70dd45` | source merge for `bh-9yrn.2` |

`git verify-commit` accepted every commit with the same ED25519 signer key
`SHA256:246Vja2yiNhU8XVlz0203XXVk2miVvlsHNVWgBteDgA`. The resolved review gates are
`bh-w1gfm` and `bh-a189p` for `aeb1d069...`, and `bh-9qv77` for `e707fc76...`.

The source submit manifests are exact and green:

- `run-35b82b302f89a701c1ad13a783fadede`: `bh-9yrn.1`, SHA `aeb1d069...`, tree
  `df074905ab336e3be1021ad854fdf2fa52e18148`, `just check`, exit 0;
- `run-bf30ec9754c82929701e3c40aea9367e`: `bh-9yrn.2`, SHA `e707fc76...`, tree
  `decf31b67cfa48f6ce3fc51d0d0f8da5b3586feb`, `just check`, exit 0; and
- the capability baseline tree is independently covered by green submit run
  `run-3e70ef1481d3cc9cd7356465db8e800d` for tree `8339205d...`, `just check`, exit 0.

Protected refs were recorded before the merge: main
`739349806ead27c94282219b1befb796ba73b583`, root workstream
`287061f089764dac29b5f584ebcdbb5f25f86c26`, capability container `90deb27b...`, and source
container `06246bd1...`. The source epic remains `in_progress`. Later children `bh-9yrn.3`,
`bh-9yrn.4`, and `bh-9yrn.5` remain open, and none of their status presentation, destructive
override, retirement, lifecycle-warning, or documentation behavior is present in the four-commit
source range adopted here.

## North-star and conflict disposition

The boundary keeps the classifier pure and the scanner replaceable:

- canonical `modules/config/contracts.py` owns typed taxonomy defaults; the legacy schema facade
  forwards those identities and the generated JSON Schema records the fields;
- `config_work_settings.py` retains the established read-side facade accessors, and the narrow
  work settings port exposes only the three required names;
- `precious.py` owns bounded ignored/untracked discovery behind its patchable read-only git seam;
- `worktree_inventory.py` owns filesystem/git execution and threads `precious_by_path` into the
  pure classifier while preserving the current facade, callback, tuple, and monkeypatch seams;
- `wt_status.py` owns the orthogonal tuple overlay: base classification is unchanged and
  `safe = base_safe and not precious`; and
- prune retains its existing safe-only partition, so precious rows are withheld without a second
  deletion policy.

The two textual conflicts were resolved as follows. `config_schema.py` stays the current thin
compatibility facade; fields and defaults moved to the canonical contract instead of restoring
the retired flat implementation. `worktree_inventory.py` keeps the current narrow config port and
adds only the accepted scanner dependency. The source scanner's lazy dependency back on the
worktree facade was reconciled into a dedicated read-only git seam so the adopted behavior does
not introduce an unowned `precious -> worktree -> wt_status` import cycle. Ambient `GIT_*` values
remain scrubbed, and both `precious._run_git` and `worktree.precious.scan_precious` remain explicit
test patch points.

Non-goals are the three later source children, a persisted precious metadata cache, changes to
base classifications, a new prune policy, removal of compatibility facades, or finishing the
external source epic into main.

## Validation cadence and evidence

Balanced cadence applies because this is a behavior-preserving boundary reconciliation with a
destructive-safety consequence: focused scanner/config/classifier checks after conflict
resolution; generated-artifact, config-ledger, import-boundary, closure, Markdown, and static
checks after the canonical ownership adjustment; broader config/worktree/hive closures before
commit; and exactly one authoritative submit-owned `just check` from a clean checkout.

- The first sandboxed focused attempt reported 9 passed and 104 setup errors, all at the autouse
  shared-server fixture's local socket allocation with `PermissionError: [Errno 1]`. No product
  assertion failed.
- The permission-correct exact rerun passed 113 tests before the scanner seam cycle was removed.
- After the cycle-free scanner seam and its environment-scrub characterization, the exact focused
  suite passed 114 tests.
- The first broader config closure found two owned evidence drifts after 280 passing tests: the
  exact patch-point count increased from 257 to 266, and the exact AST test-file count increased
  from 138 to 139. Updating those checked expectations and the accompanying explanation produced
  a green 282-test config closure with the one expected invalid-plugin-default warning.
- The hives closure passed 156 tests. The precious/classifier/inventory/facade/full-worktree
  closure passed 291 tests.
- Closure registry check passed with 19 present and 4 absent.
- Ruff passed; all 656 Python files were already formatted. Markdown passed across 176 files with
  zero issues. Canonical schema, dependency ledger, structural metrics, capability map, closure
  registry, and wire-schema checks passed.
- Import boundaries passed at 242 Python files, 2,340 import edges, and the unchanged six owned
  legacy cycles / 278 cyclic edges.

The authoritative full gate remains submit-owned and is recorded after the signed merge commit.
