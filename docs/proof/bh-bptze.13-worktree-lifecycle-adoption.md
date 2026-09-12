# bh-bptze.13 worktree-lifecycle adoption

## Provenance and approval boundary

This reconciliation starts from capability baseline
`58352624bd1525f1d99cabb49736e7353351c91a` (tree
`fc33133b0af30ae1106c89d6ca7fa5f5c7ce95da`) and merges the independently reviewed
worktree-lifecycle source tip `e6f291d4870b39eed0c52a749496bed4751e6e47` (tree
`9bbad768ba833026e30bc09196f578e28e433221`) as its ordered second parent. The source range
over protected main contains these twelve Good-signed commits in ancestry order:

| Commit | Tree | Parents | Accepted boundary |
| --- | --- | --- | --- |
| `a6e8de98f9286a5f41bcb04c8a949617c6f4e620` | `15ce27663f708e6e88922798f4a736bee5af2474` | main | terminal-disposition vocabulary |
| `4311d21f1ec208a8148e257bcef86366b9602aec` | `15ce27663f708e6e88922798f4a736bee5af2474` | main, `a6e8de9...` | `bh-kx1x` merge bubble |
| `814907f1516dc7ee9239093789e8ecfdf834e74a` | `1e89f7dd89d40484566e0f9c2dda98ce2182ff41` | `4311d21...` | bounded recovery-ref lifecycle |
| `86a52e88c67af155381f830dd76fa1f2f0c650cb` | `1e89f7dd89d40484566e0f9c2dda98ce2182ff41` | `4311d21...`, `814907f...` | `bh-c5go4` merge bubble |
| `d25d382da974f66ab71d1330faf7134fd018adf7` | `72e9870d768c25a423add09de704754a68874a6e` | `86a52e8...` | superseded-cache reclaim |
| `ff71e17928038456ce276ab61db0d58037f7898b` | `72e9870d768c25a423add09de704754a68874a6e` | `86a52e8...`, `d25d382...` | `bh-1pspf` merge bubble |
| `54c3f7c2d5016087bf603d05187998dbcf06c5c7` | `8702e33add3c0b0abe7731cc5ee7aea9d22b5c0d` | `ff71e17...` | closed-batch safe proof |
| `f16d581bec7964eb513c9d62c9c33b53b880f81d` | `8702e33add3c0b0abe7731cc5ee7aea9d22b5c0d` | `ff71e17...`, `54c3f7c...` | `bh-dyk7` merge bubble |
| `b7ae0886e731dc17fb4f3d1702d974e45c7067b4` | `01168349e66ebc66e3b0a16cc4ed22887ef6a4df` | `f16d581...` | reattach init-rule drift warning |
| `60bf76f659f4a7ffd2da132bfa7ae1786d7d40a3` | `01168349e66ebc66e3b0a16cc4ed22887ef6a4df` | `f16d581...`, `b7ae088...` | `bh-s3dd` merge bubble |
| `fe73fb788751f050a9cf177efe6cebbf1f5b9661` | `9bbad768ba833026e30bc09196f578e28e433221` | `60bf76f...` | checked E5 wave proof |
| `e6f291d4870b39eed0c52a749496bed4751e6e47` | `9bbad768ba833026e30bc09196f578e28e433221` | `60bf76f...`, `fe73fb7...` | checked exit batch merge |

`git verify-commit` reported `%G? = G` for all twelve commits under the same accepted signer.
Exit review gate `bh-2isqb` is approved. The authoritative exact-source-tree aggregate manifest
is `run-a19144868e5851765a1937234ad76feb`: `just check`, 7,002 passed, 11 skipped, zero
failures. A fresh whole-molecule read-only review separately approved the source with no
findings. The exact capability first-parent tree is independently covered by accepted manifest
`run-d65b2e4e099c5f45dbc73cd83e31ecfe`: 7,543 passed, 11 skipped, zero failures.

Protected refs recorded before the mandatory merge were main
`739349806ead27c94282219b1befb796ba73b583`, root workstream
`287061f089764dac29b5f584ebcdbb5f25f86c26`, capability container
`58352624bd1525f1d99cabb49736e7353351c91a`, and source container
`e6f291d4870b39eed0c52a749496bed4751e6e47`. The source epic remains `in_progress` and
unfinished; this adoption does not land it to main.

## North-star and reconciliation

The worktree lifecycle remains a behavior-oriented capability behind the existing stable
facades. Its boundary score is 13/14: cohesion 2, coupling 1, state/side-effect ownership 2,
port narrowness 2, replaceability 2, dependency direction 2, and test-closure reduction 2. The
remaining coupling is the intentionally retained flat-package facade that the next capability
extraction owns; this adoption does not claim that physical modularization is complete.

The mandatory `--no-ff --no-commit` merge preserved exact `MERGE_HEAD` `e6f291d...`. It produced
five conflicted paths: `wt_status.py`, `test_doctor.py`, `test_worktree.py`,
`test_worktree_inventory_boundaries.py`, and `test_wt_status.py`. Reconciliation preserved both
sides of the accepted contracts:

- `WtStatus` carries the precious-content tuple together with terminal reason/citing data;
  `SAFE`, `LANDED_REBASED`, and equivalence-proven `SUPERSEDED` are eligible only when no precious
  local-only content is present. `RETAINED`, `STALE`, unknown, dirty, and incomplete batch rows
  remain unsafe.
- The pure classifier accepts both the existing precious scan and the source disposition-edge
  and closed-batch evidence. The inventory adapter returns before filesystem lookup when no
  relevant disposition codec parsed, retaining injected test seams and avoiding unnecessary I/O.
- `worktree.py`, `worktree_git.py`, `worktree_inventory.py`, and `worktree_verify.py` retain the
  current compatibility facade and monkeypatch boundaries while adding exact safety-ref APIs,
  fail-closed relation readback, batch evidence, init-rule stamping, and reattach warnings.
- Refinement retains only its latest successful backup, no-op refinement creates none, submit
  reaps accepted refine refs after its local gate, and successful merge close/reconcile reaps
  exact refine/premerge refs. Failures and CAS mismatches retain recovery state.
- Terminal mutation remains relation-first with rollback/compensation. `RETAINED` requires its
  matching consumer edge; `SUPERSEDED` requires its matching replacement edge and landed proof;
  malformed or unsupported evidence becomes `STALE`.
- Cache reclaim and hub hydration share one pure `cache_store.py` path/source predicate. Public
  `hub.cache_path` and `hub.local_checkout_source` wrappers remain available, while backup no
  longer imports hub. This removes the candidate `backup -> hub` cyclic edge instead of
  normalizing it into the exception ledger. Symlinks, ambiguous registrations, only-copy caches,
  and confirm-time evidence loss remain non-removable.
- Doctor diagnostics take one bounded issue snapshot per hive and do not grant deletion authority.
  CLI/MCP payloads, PID checks, worktree facades, config ports, and existing patch points remain
  compatible.

Non-goals are finishing `bh-zbht5`, broad worktree-module extraction, deletion-policy widening,
automatic safety-ref deletion on a failed lifecycle boundary, automatic init-rule re-execution,
or changes to protected main/root/capability/source refs.

## Validation cadence and evidence

Strict cadence applies because the adopted behavior owns deletion safety, recovery refs,
concurrency fences, and cache data retention.

- The exact first-parent and source trees use the accepted full manifests above. A pre-merge
  focused process exited without a terminal status from the session adapter and is classified
  inconclusive, not green; it was not used as evidence or duplicated.
- Classifier/inventory conflict closure passed 98 tests after fixing the no-disposition I/O seam.
- Doctor/worktree/facade conflict closure passed 366 tests.
- Work, refinement, merge, cache, host-retire, and init-boundary closure passed 540 tests.
- The pure cache seam transition passed 150 backup/hub/private-path tests.
- Architecture/import/facade tests passed 32 tests. The capability map is reproducible and the
  import checker reports 243 files, 2,350 edges, and the unchanged six owned legacy cycles / 278
  cyclic edges. No import-cycle snapshot or exception was widened.
- Ruff passed across every changed Python file; formatting reported all changed files formatted.
- The first submit-owned full run, `run-ec9d4a4912c03a33498e1f83a7407f72`, completed with
  7,592 passed, 11 skipped, and four failures. All four were checked-evidence drift owned by this
  adoption: the operation catalog lacked the new `worktree mark-abandoned` and hidden
  `wt mark-abandoned` projections, Convention 8 still expected 206 rather than 208 CLI leaves,
  and the config dependency ledger still recorded 266 rather than 270 patch points. No product
  behavior test failed, and the failed run did not open a review gate.
- The repository generators rebuilt the canonical operation catalog and config dependency ledger
  after adding those exact two projections and updating only the demonstrated exact-count
  assertions. The four failed nodes then passed 4/4; the full catalog, dependency-ledger, and wire
  schema affected selection passed 68/68. The ledger check, capability map, wire compatibility,
  Ruff, formatting, and import-boundary checks are green at the amended tree, with import ownership
  still exactly 243 files, 2,350 edges, and six owned legacy cycles / 278 cyclic edges.

One replacement authoritative full `just check` is authorized but has not started. It will attest
the amended signed ordered-parent reconciliation tree; fresh review is allowed only if that run is
green.
