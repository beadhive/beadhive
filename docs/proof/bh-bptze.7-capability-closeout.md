# `bh-bptze.7` capability-boundary closeout

## Decision and provenance

The seven capability modules are independently runnable through registered direct,
shared-contract, and reverse-dependent closures. The architecture checker is green with no
domain/application outward-direction violation. The historical largest 65-module SCC is now 35
modules, so the required reduction is material: 30 modules (46.2%). This bead changes no product
source or behavior.

All current structural and dynamic measurements below belong to immutable source commit
`ad4077d7ee5ae40c966f089920ca794006538090`, tree
`a12ad7e213228a591b4076ab1f3720e47c67f6c4`. The later proof commit contains only the checked
generator, JSON, test, ledger verification date, and this document; it cannot contain its own
digest without circularity. Measurements were taken on 2026-09-02 UTC with Python 3.13.5,
pytest 9.1.1, pytest-xdist 3.8.0, pytest-cov 7.1.0, and Coverage.py 7.1.0.

`scripts/capability_closeout.py` reads exact Git blobs, reuses the checked AST import/SCC
collector, reads the closure and exception ledgers, and generates
`bh-bptze.7-capability-closeout.json`. `tests/test_capability_closeout.py` rejects drift, missing
modules/dependents, unowned cycles, stale exception counts, and regression of the acceptance
totals.

## Repository structural result

| Point | Modules / edges | SCCs / cyclic modules | Largest | Cyclic edges / symbols |
| --- | ---: | ---: | ---: | ---: |
| Modular foundation `adf182bc` | 182 / 2,029 | 6 / 85 | 65 | 288 / 318 |
| Pre-migration `287061f0` | 232 / 2,282 | 6 / 85 | 65 | 278 / 308 |
| Pre-registry-cut `9dc7c105` | 281 / 2,463 | 5 / 82 | 64 | 272 / 298 |
| Closeout source `ad4077d7` | 281 / 2,460 | 8 / 59 | 35 | 155 / 172 |

The current SCC sizes are `35, 8, 6, 2, 2, 2, 2, 2`. The increase from five to eight SCCs is the
expected split of one large component, not new coupling: cyclic modules fell 82 to 59 and cyclic
edges fell 272 to 155 across the registry cut. Against the foundation, cyclic modules fell 26,
cyclic edges fell 133, and cyclic imported symbols fell 146. The exact current cycle digest is
`f60b8e61436c0a28909f9d20ce9c3d1e500fe6961c76c54caae1c1e9969f7b09`.

## Per-capability shape, coupling, and churn

Complexity is the checked AST decision-node count. Fan-in is distinct external source importers.
Churn is Git numstat from 2026-06-03 UTC through each exact revision. The before rows measure the
characterized legacy candidate family (or the exact agents/config baseline); the after rows
measure only the owned `modules/<name>` package. They are boundary-size evidence, not a claim that
all compatibility code was deleted.

| Module | Lines before → after | Decisions before → after | Fan-in before → after | Commits / + / - before → after |
| --- | ---: | ---: | ---: | ---: |
| Agents | 4,690 → 1,711 | 1,043 → 167 | 3 → 5 | 49/5,181/485 → 2/1,711/0 |
| Config | 2,790 → 2,931 | 211 → 217 | 6 → 15 | 3/2,790/0 → 7/2,865/9 |
| Hives | 5,351 → 555 | 755 → 27 | 60 → 6 | 76/6,537/1,187 → 2/733/178 |
| Worktrees | 5,922 → 573 | 785 → 29 | 27 → 3 | 89/8,483/2,561 → 1/573/0 |
| Work | 7,255 → 536 | 1,080 → 5 | 10 → 4 | 136/11,275/4,020 → 1/536/0 |
| Planning | 3,023 → 358 | 532 → 26 | 10 → 2 | 39/3,459/436 → 1/358/0 |
| State | 4,736 → 1,263 | 763 → 98 | 29 → 10 | 52/5,063/327 → 1/1,263/0 |

Config fan-in grows because typed contracts are now the canonical dependency. Agents similarly
collects explicit typed imports that previously targeted large facades. Those increases are
intentional inward dependencies; the direction checker confirms neither package imports a
forbidden runtime owner.

## Closure and coverage result

The registry contains 23 present and zero absent closures. Each module row declares direct tests,
shared contract tests where applicable, and adapter/compatibility/system reverse dependents.
Counts overlap by design and must not be summed as a repository total.

| Module | Direct / shared / reverse selectors | Result | Pytest / wall | Coverage |
| --- | ---: | ---: | ---: | ---: |
| Agents | 3 / 1 / 2 | 292 passed | 28.62s / 31.350s | 776/848 (91.51%) |
| Config | 11 / 2 / 6 | 282 passed, 1 warning | 49.30s / 51.495s | 1,033/1,072 (96.36%) |
| Hives | 3 / 2 / 8 | 159 passed | 30.15s / 35.169s | 276/285 (96.84%) |
| Worktrees | 4 / 2 / 8 | 449 passed | 54.76s / 57.824s | 234/243 (96.30%) |
| Work | 2 / 2 / 7 | 465 passed | 224.07s / 227.467s | 211/212 (99.53%) |
| Planning | 2 / 1 / 5 | 173 passed | 124.93s / 127.998s | 152/160 (95.00%) |
| State | 2 / 1 / 6 | 88 passed | 14.84s / 17.102s | 535/554 (96.57%) |

Historical comparison is preserved exactly rather than normalized after the fact:

| Module | Historical selection | Historical timing | Historical coverage |
| --- | ---: | ---: | ---: |
| Agents | 322 passed | 14.28s / 15.455s | 82% rounded; ratio was not recorded |
| Config | 102 passed | 36.79s / 38.36s | 964/1,059 (91.03%) |
| Hives | 690 collected | not recorded | 2,097/2,234 (93.87%) |
| Worktrees | 372 collected | not recorded | 2,141/2,495 (85.81%) |
| Work | 931 collected | not recorded | 2,759/3,203 (86.14%) |
| Planning | 231 collected | not recorded | 1,186/1,336 (88.77%) |
| State | 109 collected | not recorded | 1,948/2,175 (89.56%) |

The five unrecorded historical timings are named as absent rather than fabricated. Their counts
and coverage come from the exact `287061f0` capability map. Agents comes from the exact
`5e4a7670` characterization, and config from the exact `5e47c611` evidence.

Repository coverage moved from 38,592/43,477 (88.7642%) with 7,436 passed and 12 skipped in
269.12s at `287061f0`, to 40,679/45,826 (88.7684%) with 7,645 passed, 12 skipped, and the one
known invalid-config-default Pydantic warning in 304.25s pytest / 307.011s wall at `ad4077d7`.
Coverage is non-integration statement coverage without branch data or per-test contexts.

## Remaining cycle ownership

| Size / edges | Members (short form) | Owner | Follow-up and retention rationale |
| --- | --- | --- | --- |
| 35 / 123 | flat core from `bd` through `worktree_merge` | Legacy core and capability-compatibility maintainers | `bh-8kn42` owns the raw-bd seam; then plan a consumer-zero cycle cut. This proof bead cannot delete live composition/facade edges. |
| 8 / 12 | alerts, doctor, host CLI/daemon/provision/retire, MCP, operator SSE | `bh-q0lol` unified-host-daemon owners | Retire host transport feedback while consuming module contracts; transport composition stays outward. |
| 6 / 10 | backup, hive, HQ, hub, onboard, storage migration | Hive/storage adapter maintainers | Follow `bh-8kn42`, then remove storage-adapter feedback at consumer zero. |
| 2 / 2 | Herdr plugin / views | `bh-5wuc0` compatibility maintainers | Retire `cycle-edge-004` only after the documented public facade consumer-zero gate. |
| 2 / 2 | local loop / runtime | Local runtime maintainers | Separate tier policy from its adapter before `cycle-edge-026` expires. |
| 2 / 2 | report / triage | Planning/report compatibility maintainers | Put report projection behind the planning read port before `cycle-edge-036` expires. |
| 2 / 2 | epic schedule / polling | State adapter maintainers | Inject schedule projection into polling before `cycle-edge-033` expires. |
| 2 / 2 | work / work-show | Work compatibility maintainers | Put review presentation behind a work read port before `cycle-edge-037` expires. |

The machine artifact contains every full member list, edge/symbol total, and active exception ID.
All 38 active feedback exceptions map to one of these eight components.

## Exception disposition and validation

The ledger contains 38 active cycle exceptions, two active config boundary exceptions, and seven
active facades. Six resolved cycle entries—`cycle-edge-012` through `016`, plus `022`—are excluded
from active exceptions and retained with `status = "removed"` as immutable audit records. This is
the ADR's historical-ledger convention; physically deleting them would erase their removal commit,
replacement path, and reason. No active exception is stale or unused. The ledger verification
commit is advanced to the measured source revision.

Executed evidence:

```text
time just test-module agents|config|hives|worktrees|work|planning|state
time env COVERAGE_FILE=/tmp/bh-bptze-7.coverage just cov
env COVERAGE_FILE=/tmp/bh-bptze-7.coverage uv run coverage json \
  -o /tmp/bh-bptze-7-coverage.json
python3 scripts/capability_closeout.py --check
just architecture-check
just test-closure-check
uv run pytest -q tests/test_capability_closeout.py \
  tests/test_import_boundaries.py tests/test_test_closures.py
```

The focused/static terminal results and the Good-signed proof commit are recorded in the bead
attestation. The dispatcher retains authority for the final full gate and submission.
