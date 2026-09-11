# Modularization closeout review packet

This packet originated with
`bh-j5uyb.1 — Assemble final modularization validation and operator review
packet` and is refreshed by
`bh-uvotu.4 — Refresh modularization closeout and generated architecture
evidence`. It is a local-only operator handoff for
`bh-j5uyb — Workstream: modular Beadhive core, plugins, contracts,
transports, and test closures`; it is not an approval or merge decision.

The machine-readable companion
[`bh-j5uyb.1-modularization-closeout.json`](bh-j5uyb.1-modularization-closeout.json)
is the canonical inventory. It records every relevant direct child, the nested
foundation prerequisites, all review-transition event IDs, the active exception
IDs, and the current-versus-historical evidence classification summarized
below.

## Exact remediated candidate lineage

- Current remediated source candidate:
  `5e14f142e4fa7610514e87fceb0d179601c4c23f`
- Current remediated source tree:
  `0447133176cee14dbb0ffb5c91ac32c367c36fe0`
- Main and rollback anchor:
  `739349806ead27c94282219b1befb796ba73b583`
- Main tree at measurement:
  `8fb6984bb1a4cf1ed598e71d6a3f9da41fbc8b88`

The remediated candidate descends through the original closeout merge
`071ebdf2`, exact-tree receipt-authority repair `0b5749d0`, standalone process
proof `a886eefc`, and Beadhive Frame Bridge replay `5e14f142`. This packet changes
that tree again. Its `bh-uvotu.4` check and submit receipts, and the later final
`bh-j5uyb` review receipt, must each resolve their own exact tree. No SHA or
receipt written into this self-changing packet can prove the later
closeout-inclusive tree.

The earlier `6ba52a9c` / `1c2dfd72` implementation assembly and its receipts are
immutable **historical evidence only**. They establish the original modularized
baseline, not the receipt repair, process proof, Frame Bridge replay, this
closeout, or the final review tree.

## First-tier epic topology

Every first-tier epic is closed as `molecule landed`, and every listed merge is
a two-parent, no-fast-forward bubble.

| Closed epic | Bubble merge | Direct children |
| --- | --- | ---: |
| `bh-inqwc — Modular foundation: enforce dependency boundaries and independent test closures` | `d112526d` | 8 |
| `bh-qw9oi — Plugin kernel: versioned manifests, typed capabilities, and lifecycle hooks` | `5e4a7670` | 6 |
| `bh-18hud — Configuration module: canonical models, stores, and plugin schema fragments` | `1a156d46` | 6 |
| `bh-5wuc0 — Agents module: extract provider-neutral launch policy and the Herdr adapter` | `287061f0` | 6 |
| `bh-bptze — Capability modules: extract hives, worktrees, work, planning, and state behind ports` | `d68cefad` | 13 |
| `bh-3qkmk — Transport composition: finish catalog-derived CLI, MCP, API, and gateway adapters` | `2ca3703e` | 6 |
| `bh-id9pp — Telemetry readiness and official v1 contract-schema release` | `071521b0` | 7 |
| `bh-ck1t6 — Selective CI graduation: certify module and plugin test closures` | `6ba52a9c` | 5 |

The companion JSON enumerates all 57 direct children and eight nested children,
with their titles, types, closed states, submissions, and bounces. The assembled
history also includes these reviewed supporting bubbles:

- `bh-aoaej — Make epic review history policy preserve reviewed merge topology`
- `bh-j5uyb.4 — Audit root-first epic composition wrappers without flattening reviewed topology`
- `bh-q0lol — Unified host daemon: authoritative MCP/operator ingress and supervised runtime`
- `bh-1owpi — Make local validation verdict lookup sub-second without weakening attestation safety`
- `bh-xaymo — Preserve reviewed epic wrapper prefixes after subsequent child merges`

Across the recorded relevant bead set, the lifecycle ledger contains 190
submissions and 94 changes-requested transitions: 89 reviewer bounces and five
integration or other bounces. The companion JSON carries all 94 event IDs so an
operator can reconcile the summary without relying on prose.

## Validation evidence

The historical implementation assembly tree has a green `just check-all` receipt:
`run-0f98b0032d26017943d65bad457e2d19`. It ran for 601.899 seconds from
2026-09-10 21:37:40 UTC through 21:47:42 UTC and reported:

- unit: 8,881 passed, 12 skipped, one warning in 259.55 seconds;
- integration land: 63 passed and two skipped in 138.81 seconds;
- local-loop demo: passed in 104.3 seconds with clean tripwires;
- deterministic live-ingress matrix: all four cells passed.

Each first-tier epic also has an individual green full-gate receipt. Their wall
times were 429.863, 431.169, 452.427, 456.617, 455.029, 531.643, 597.623,
and 601.899 seconds respectively; exact run IDs and pytest summaries are in the
companion JSON.

Closeout-focused verification is green:

- architecture: 327 Python modules, 2,912 import edges, eight owned SCCs,
  155 cyclic edges, and zero unowned architecture errors;
- plugin conformance: 74 passed in 1.01 seconds;
- transport artifacts: operation catalog, projection inventory, OpenAPI,
  Gateway wire contract, and Frame Bridge composition evidence are current;
- schema compatibility: telemetry, official v1 bundle, release evidence, and
  generators are current; v1.0.0 validates as the initial release relative to
  main and the proof compares five immutable historical releases;
- selective CI: all 24 closures remain uncertified, activation is fail-closed,
  and there are zero production routes;
- package and entry point: an offline wheel contains the Frame Bridge bootstrap,
  application, and runtime modules, exports
  `beadhive-frame-bridge = beadhive.bootstrap.frame_bridge:main`, and contains
  neither the old core command nor old modules.

The closeout gate exposed a downstream receipt-bootstrap defect after formatting
lint was corrected: a live exact-tree validation manifest was rejected solely
because its bead identity differed from the historical certification bead. The
repair does not treat that old bead identity as authority. A completed receipt
is reusable only when the exact tree, canonical full-gate command and hash,
lifecycle shape, phase, managed bead/branch binding, exit code, and signal state
all match. An in-flight receipt additionally must name this exact worktree and a
verified live host, PID, non-zombie state, and process-start token. The
standalone process test proves both paths and rejects stale, dead, wrong-tree,
wrong-command, and malformed receipts. The two original RED diagnostics remain
private historical records: `run-42e5b4142657e17a70709f50a0a5ec28` and
`run-5bea33e53bd6d131cfa7ec4511c19ee1`.

## Current generated evidence and immutable history

The companion JSON records reproducible SHA-256 values for every current
transport, import-boundary ledger, schema/release, selective-CI, package, and
entry-point input claimed here. `tests/test_modularization_closeout.py`
recomputes those values from repository bytes. The canonical checks are:

```console
just architecture-check
just transport-artifact-check
just wire-schema-compat
uv run pytest -q tests/test_modularization_closeout.py tests/test_frame_bridge_handoff.py
```

`docs/design/capability-module-dependency-map.json`,
`docs/proof/bh-bptze.7-capability-closeout.json`, the original Frame Bridge
handoff candidate, and all receipt IDs printed above remain explicitly
classified as immutable historical evidence. Their pre-rename paths and old
trees are facts about those snapshots; they are not regenerated or presented as
current-candidate proof.

The core owns one per-frame **Beadhive Frame Bridge**. It preserves the Gateway
wire contract and audience but does not aggregate frames. The multi-frame
**Beadhive Gateway** is owned by the sibling `beadhive-gateway` repository; core
does not install a `beadhive-gateway` process command.

## Structural before and after

The foundation baseline is revision `adf182bc4fc9c628a23b9b76c55f5291ec337b7a`.
Repository growth increased the module and import-edge totals, while cyclic
coupling fell materially.

| Measure | Foundation | Current candidate | Change |
| --- | ---: | ---: | ---: |
| Python modules | 182 | 327 | +145 |
| Import edges | 2,029 | 2,912 | +883 |
| Cyclic modules | 85 | 59 | -26 |
| Largest SCC | 65 | 35 | -30 |
| Cyclic edges | 288 | 155 | -133 |
| Cyclic symbols | 318 | 172 | -146 |

The SCC count increased from six to eight because a large component split into
multiple smaller, explicitly owned components. The current digest is
`f60b8e61436c0a28909f9d20ce9c3d1e500fe6961c76c54caae1c1e9969f7b09`.

## Remaining facade and exception ledger

No compatibility debt is silently retired. The active ledger has 38 cycle
exceptions, six outward-boundary exceptions, and eight compatibility facades.
Their exact IDs and the five authoritative ledger paths are in the companion
JSON. The current boundary set includes
`boundary-bootstrap-frame-bridge-compatibility`; pre-rename Gateway terminology
inside explicitly immutable historical evidence does not name current active
debt. Removal requires separate reviewed changes.

## RepoWise freshness

RepoWise 0.45.0 was refreshed at the historical `6ba52a9c` assembly tip in index-only mode,
without a workspace, cost tracking, managed-agent generation, or model tokens.
It indexed 1,203 files into 159,405,396 bytes and reported average health 7.85,
hotspot health 5.91, maintainability 8.73, performance 9.74, and 4,872 open
findings. The lowest score was `src/beadhive/cli.py` at 1.65.

Before that historical refresh, the wrapper claimed zero commits behind while
its native state was still pinned to main. The retained values are static
architecture signals for `6ba52a9c`, not correctness, per-test coverage, or
evidence for the current remediated or final tree.

## Known risks and rollback

- The `5e14f142` source candidate predates this closeout. This bead's check and
  submit and the final workstream review must each use an authoritative receipt
  for their exact closeout-inclusive tree.
- Selective CI claims no production savings: all 24 closures are uncertified and
  every production boundary still takes the full lane.
- The integration-land recipe quarantines the known `bh-tfapu` host-fence test;
  an ordinary unquarantined integration run still exposes that upstream `bd`
  limitation.
- `bh-hxbln.1 — Certify the unified host daemon on real OS supervisors` remains
  deferred, so Darwin LaunchAgent and persistent Linux systemd-user
  certification are release-gated.
- Genuine provider smoke remains externally blocked by the current
  non-runnable, nonconformant baml-harness Codex artifact, although all four
  deterministic live-ingress cells pass.
- The active compatibility ledgers and 4,872 untriaged RepoWise findings remain
  follow-up work, not proof failures.
- `bh-9ghuh.1 — Exercise a real host daemon through the Beadhive Frame Bridge`
  owns the remaining real-process core integration check.
- `bh-gw-ywh.1 — Refresh the Gateway source lock for Beadhive Frame Bridge`,
  `bh-gw-ywh.2 — Certify exact Gateway sources against the renamed Frame Bridge`,
  and `bh-gw-ywh.3 — Exercise live Gateway aggregation across multiple Frame
  Bridges` own the sibling Gateway integration sequence.

The workstream rollback anchor remains
`739349806ead27c94282219b1befb796ba73b583`; unrelated later commits on main do
not change the workstream's ancestry. Before merge, rollback means declining the
submitted workstream. After merge, revert its single top-level no-ff bubble with
mainline parent 1. Do not independently replay or revert child bubbles.

## Reproducible operator review

From the hive repository, the operator-owned final gate is:

```console
bh work review bh-j5uyb --run --view stat
just demo-local-loop
bh work review bh-j5uyb --view diff
```

The explicit `just demo-local-loop` step is load-bearing: its recipe runs the
demo through `scripts/hermetic.sh` with a private `HOME`. Do not replace it with
the review command's `--demo` flag while this hive's managed demo command points
to the bare script. The independent reviewer did that once and recorded
`run-4146531c0d2a8c7c5ec79dcbf02ffdc7` as RED after the functional scenario
completed but ambient `~/.beadhive` writers tripped the isolation assertion.
That is an expected ambient-write diagnostic, not candidate evidence. The
separate fenced `just demo-local-loop` run is the authoritative green demo.

The expected next state is a human review gate opened by the root dispatcher
after this reviewed closeout bead is integrated. Approval and final merge remain
operator decisions; this closeout bead performs neither.
