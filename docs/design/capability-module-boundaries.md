# Capability-module dependency map and compatibility ledgers

Status: exact-tip pre-migration evidence for `bh-bptze.1`

Evidence date: 2026-09-01 UTC

Measured source commit: `287061f089764dac29b5f584ebcdbb5f25f86c26`
(`chore(merge): molecule bh-5wuc0`)

This evidence freezes the source immediately before the hives, worktrees, work, planning, and
state migrations. The commit containing this document is necessarily newer than the measured
source commit; the bead changes no production file, so final exact-tree validation belongs in the
Beadhive check and review attestation. Numeric evidence is also published in
`capability-module-dependency-map.json`.

## North-star and invariants

Each slice owns one cohesive capability behind typed request/result contracts and narrow outbound
ports. Domain and application packages point inward; bootstrap and adapters provide filesystem,
Git, Dolt/beads, process, plugin, clock, notification, and transport implementations. Consumers
depend on public capability contracts, never concrete adapters or another capability's
application internals.

The target ownership is:

- `modules/hives`: hive identity plus onboard, readiness, registration, and retirement use cases;
- `modules/worktrees`: binding, branch/worktree policy, inventory, status, and lifecycle use cases;
- `modules/work`: assignment through abandonment, including execution and validation policy;
- `modules/planning`: molecule validation, DAG policy, filing, kickoff, verification, and repair;
- `modules/state`: durable validation facts, state/activity streams, and read projections.

Observable CLI/MCP JSON, text, help, exit, replay/cursor, validation, gate, branch/worktree, and
plugin-lifecycle behavior remains unchanged. Existing `beadhive.work` and `beadhive.worktree`
imports and executable monkeypatch seams remain compatibility facades until their recorded
consumer-zero gates are met. Every slice must provide pure application tests with fake ports,
real-adapter contract tests, compatibility tests, and registered reverse-dependent closure.

The evidence-derived sequence remains the filed DAG:

```text
map -> hives -> worktrees -> work -> planning ---\
           \                 \                  -> proof
            \-----------------> state ---------/
```

`worktrees` follows `hives` because workspace realization participates in hive lifecycle; `work`
follows `worktrees` because its assignment/claim/validation/merge services consume worktree
ports; `planning` follows `work` for stable work-read/request contracts; `state` follows `hives`
and `work` because its durable identities and validation facts are their outputs. No metric below
supports reversing an edge or adding a cycle.

Non-goals are a flag-day move, public behavior changes, deleting compatibility facades, changing
Beadflow authority, consolidating the raw `bd` transport, general CQRS/event sourcing, transport
rewrites, or selective-CI graduation. Live beads retain exclusive ownership of their named
symbols.

## Baseline and validation cadence

The worktree was clean at the measured commit on branch `wt/bead/issue/bh-bptze.1`. Python is the
only implementation language in scope. Repository `CLAUDE.md`, the modular architecture ADR, and
the bundled Python factoring guide apply; no host-wide Python convention file was present.

Balanced cadence applies: this bead changes evidence and repository tooling only, while the map
controls architecture-critical downstream sequencing. The untouched-base and final child SHA run
the full configured `just check`; the evidence generator, architecture collector, closure drift,
and focused evidence tests run after each coherent document/tool transition. A closure does not
replace the full gate.

The first untouched-base attempt used
`UV_CACHE_DIR=/tmp/bh-bptze-1-uv-cache just check`. Static checks reached 232 Python files,
2,282 import edges, six owned legacy SCCs, and 278 cyclic edges, then hermetic installation failed
before pytest because the isolated cache lacked `commitizen==4.16.4` and sandbox DNS could not
reach PyPI. This is an environmental limitation, not a source failure. The baseline was restarted
unchanged with the populated repository cache; its terminal result is recorded below.

## Exact structural and closure evidence

`scripts/capability_module_map.py` parses the exact Git blobs with the standard-library AST and
reuses the checked architecture collector's import/SCC definitions. It resolves call sites only
through explicit import aliases. Fan-in is the count of distinct source modules outside a slice
that statically import a selected module. Churn is Git numstat from 2026-06-03 through the
measured revision. The JSON records every edge, resolved call record, all 387 exact-revision
Python test/support paths, every slice-relevant dynamic test seam across that complete caller
scope, and the separately selected legacy characterization closure rather than only the totals
below.

Repo-wide results are 232 Python modules, 2,282 import edges, two nonliteral dynamic imports,
six owned legacy SCCs containing 85 modules, 278 cyclic edges, and 308 imported symbols. SCC
sizes remain 65, 8, 6, 2, 2, and 2. The dynamic imports are in `config_binding` and `mcp`; neither
is inside these five candidate slices.

| Slice | Shape: lines / funcs / classes / decisions | Fan-in | Calls in / out | 90-day commits / + / - | SCC intersections |
| --- | ---: | ---: | ---: | ---: | --- |
| Hives | 5,351 / 226 / 10 / 755 | 60 | 311 / 183 | 76 / 6,537 / 1,187 | 65, 6 |
| Worktrees | 5,922 / 378 / 3 / 785 | 27 | 115 / 189 | 89 / 8,483 / 2,561 | 65 |
| Work | 7,255 / 356 / 7 / 1,080 | 10 | 18 / 264 | 136 / 11,275 / 4,020 | 65, 2 |
| Planning | 3,023 / 116 / 4 / 532 | 10 | 32 / 136 | 39 / 3,459 / 436 | 65, 2, 2 |
| State | 4,736 / 168 / 48 / 763 | 29 | 89 / 55 | 52 / 5,063 / 327 | 65 |

These totals measure the candidate source family, not the future module's final size. High
outbound call counts and SCC membership are sequencing/risk evidence; they are not a reason to
copy dependencies inward or expose a broad service locator.

The closure registry is green with 18 present and five absent rows. Every candidate module is an
explicit absent row and collects zero today. For characterization, the exact selected legacy
files in the JSON collect 690 hives, 372 worktrees, 931 work, 231 planning, and 109 state tests.
The sets overlap through compatibility and shared behavior, so their counts must not be summed.
Those filename-selected sets describe current test closure only; they do not bound dynamic-caller
discovery. The compatibility inventory independently scans all 387 Python files under `tests/`
at the measured revision and finds 197 hives, 39 worktrees, 57 work, eight planning, and 20 state
patch/getattr/import seams.
Expected module closures below are deliberately narrower and retain explicit adapter,
compatibility, contract, and reverse-dependent additions.

Statement coverage was measured with `COVERAGE_FILE=/tmp/bh-bptze-1.coverage just cov` over the
non-integration selection: 7,436 passed, 12 skipped, and the existing config-fragment serializer
warning in 269.12 seconds. At that snapshot, the then-current two evidence tests added two
collected items relative to the untouched base gate; the coverage run observed one additional
capability-conditioned skip and no failure. The later comprehensive-scope currentness test is
covered by the focused and full R2 validation rather than retroactively changing this snapshot.
Coverage.py reports 38,592 of 43,477 repository statements covered (88.76417416105068%). The
exact per-slice values below aggregate its file JSON against the checked source path lists.

| Slice | Covered / statements | Exact statement coverage |
| --- | ---: | ---: |
| Hives | 2,097 / 2,234 | 93.867502238138% |
| Worktrees | 2,141 / 2,495 | 85.811623246493% |
| Work | 2,759 / 3,203 | 86.137995629098% |
| Planning | 1,186 / 1,336 | 88.772455089820% |
| State | 1,948 / 2,175 | 89.563218390805% |

## Per-slice boundary ledger

Scores use the modularization scale from zero to two for cohesion, coupling, state/effect
ownership, port narrowness, replaceable implementations, dependency direction, and test-closure
reduction. A zero for port narrowness or dependency direction would reject the candidate. The
scores guide the migration; they do not waive the explicit blockers in the overlap ledger.

### Hives — score 10/14 (`2,1,1,2,2,1,1`)

- Inbound use cases: identify, register, onboard, check readiness, repair lifecycle metadata, and
  retire a hive. Named application boundary: `HiveLifecycleService` with typed onboard,
  readiness, registration, and retirement requests/results.
- Outbound ports: `HiveRegistry`, `WorkspaceRealizer`, `DependencyProbe`, and
  `LifecyclePublisher`. Current concrete behavior is spread across `hive`, `hive_identity`,
  `hive_ready`, `onboard`, `registry`, and `retire`; filesystem/Git/Dolt/plugin operations become
  adapters supplied at bootstrap.
- State/effect ownership: the module owns identity and lifecycle policy, not fleet/config files,
  clone creation, Git, Dolt, process probes, terminal output, or plugin discovery. Dependencies
  point from adapters/bootstrap to hives contracts/application and from hives application to its
  ports.
- Compatibility surface: the six legacy modules, CLI/MCP call sites, lifecycle hook ordering,
  JSON/text/exit shapes, and 197 exact dynamic patch/getattr/import seams found across all 387
  Python test/support callers. Current selected closure: 43 legacy files / 690 tests. Expected
  isolated closure: pure identity and use-case tests with
  fake registry/workspace/probe/lifecycle ports, adapter contracts for the real registry and
  workspace realization, facade/transport compatibility, and CLI/MCP reverse dependents.
- Evidence: highest fan-in (60), two SCC intersections, 311 inbound call sites, and 76 recent
  commits justify the first policy extraction but require compatibility-first movement. Rejected:
  a directory move retaining registry/plugin imports, or a single facade that owns both hive
  policy and workspace/Dolt realization.

### Worktrees — score 11/14 (`2,1,2,2,2,1,1`)

- Inbound use cases: bind, preview, provision/ensure, locate, inventory, classify/status, verify,
  remove/prune, and merge-related worktree mechanics. Named port: `WorktreeProvisioner`, with
  `NativeGitWorktreeProvisioner` and `PluginWorktreeProvisioner` concrete adapters.
- Other outbound ports: `WorkspaceCatalog`, `GitExecutor`, `BeadStatusReader`, `ProcessProbe`,
  `ValidationCheckout`, and `LifecyclePublisher`. Branch/worktree identity and lifecycle policy
  point inward; Git/process/plugin operations point outward through these ports.
- State/effect ownership: the module owns binding, branch names, classifications and lifecycle
  decisions. Adapters own worktree directories, refs, verify directories/markers, PID reads,
  filesystem cleanup, Git commands, and plugin callbacks.
- Compatibility surface: `beadhive.worktree` tuple/payload contracts and its module-local
  collaborators; `gitworkspace`, `gitworkspace_plugin`, split `worktree_*` services,
  `wt_status.WtStatus`, `WtClassification`, `classify`, PID probing, and 39 exact dynamic test
  seams across the complete 387-file caller scope. Current selected closure: seven legacy files /
  372 tests. Expected isolated closure: pure
  binding/classification/lifecycle tests, one provisioner conformance suite for both adapters,
  real-Git adapter integration, worktree facade tests, and work/hive reverse dependents.
- Evidence: 27 importers, 115 inbound calls, the 65-file SCC, and 89 recent commits make state
  ownership strong but movement risk high. Rejected: treating native Git and plugin provision as
  separate capabilities, or moving cleanup before live classifier/safety owners land.

### Work — score 10/14 (`2,1,1,2,2,1,1`)

- Inbound use cases: assignment, claim, scheduling, check, submission, review, approve/bounce,
  merge, resume, abandonment, group flow, and reads. Named boundary: `WorkLifecycleService` with
  command-specific typed requests/results rather than one callback-shaped command.
- Outbound ports: `BeadStore`, `WorktreeProvisioner`, `ExecutionPort`,
  `ValidationEvidenceStore`, `IdentityProvider`, and `WorkNotifier`. Current concrete adapters are
  the beads/Dolt seam, native worktree/Git services, subprocess validation, validation ledger,
  identity, and telemetry/notification composition.
- State/effect ownership: the module owns Beadflow transition, authority, validation-reuse,
  history/gate and compensation policy. Adapters own Dolt/beads writes, worktree/Git/process I/O,
  evidence persistence, notification delivery, and CLI/MCP rendering.
- Compatibility surface: `beadhive.work` imports, text/JSON/gate behavior, `work_*` service patch
  seams, `work_show` cycle, and 57 exact dynamic seams across the complete 387-file caller scope.
  Current selected closure: 26 legacy files / 931 tests. Expected isolated closure:
  command-service policy against fake ports, outbound adapter
  contracts, real lifecycle compatibility, shared gate/validation contracts, and planning/CLI/MCP
  reverse dependents.
- Evidence: the largest surface and churn (7,255 lines, 136 commits), 264 outbound resolved calls,
  and 65+2 SCC intersections demand incremental use-case commits. Rejected: lifting the existing
  facade wholesale, changing work authority, or absorbing the separate raw-`bd` consolidation.

### Planning — score 10/14 (`2,1,1,2,2,1,1`)

- Inbound use cases: validate molecule specs/DAGs, check/dry-run/show/file, create kickoff gates,
  verify, approve, and repair. Named boundary: `PlanningService` with typed plan, filing,
  verification, and repair requests/results.
- Outbound ports: `PlanDocumentStore`, `BeadFiler`, `WorkReadPort`, and `KickoffGatePort`.
  Current concrete implementations are filesystem/YAML documents plus the beads/work-read
  adapters. Planning consumes work read contracts; neither capability imports the other's
  implementation.
- State/effect ownership: planning owns specification and dependency-DAG policy. Filesystem,
  bead filing/gate writes, CLI/MCP presentation, and report/triage storage stay in adapters.
- Compatibility surface: `plan`, `plan_repair`, `molecule`, `report`, `report_target`, `triage`,
  and `triage_store` command/file/check/dry-run/show/verify/approve behavior. The complete 387-file
  caller scope contains eight exact dynamic seams; current selected closure is ten legacy files /
  231 tests. Expected isolated closure:
  pure spec/DAG/repair tests, fake filer/work-reader/gate ports, real filing contract tests, and
  CLI/MCP/report/triage reverse dependents.
- Evidence: three SCC intersections are the exact `plan`/`plan_repair`, `report`/`triage`, and
  shared 65-core coupling named by acceptance. Rejected: a global planning/report module, direct
  work implementation imports, or a new generic workflow engine.

### State — score 11/14 (`2,1,2,2,2,1,1`)

- Inbound use cases: append/read durable validation facts, replay activity/state streams,
  project gate/epic schedules and public read models, and correlate run summaries/journals. Named
  boundaries: `ValidationRecordStore` and `StateProjectionReader`.
- Outbound ports: `StateStore`, `Clock`, and `StateNotifier`. Current concrete implementations
  are the validation ledger/record store, run journal, stream processors/pollers, and public
  reader adapters; transports consume projection contracts rather than storage internals.
- State/effect ownership: the module owns stable fact identity, cursor/replay rules, aggregation,
  and projection policy. Dolt/filesystem persistence, polling processes, notifications, and
  CLI/MCP/API/gateway rendering stay outward.
- Compatibility surface: validation record schema/replay/identity behavior, state-stream cursor
  and schedule/gate projections, public readers, run journal and summary readers. The complete
  387-file caller scope contains 20 exact dynamic seams; current selected closure is nine legacy
  files / 109 tests. Expected isolated closure: fixture-fed pure projections,
  store/clock/notifier fakes, persistence and replay
  contracts, schema compatibility, and operator API/gateway/MCP/CLI reverse dependents.
- Evidence: 29 importers, 89 inbound calls, 48 data classes, and one 65-core intersection support
  a stable fact/projection boundary. Rejected: moving command authority into state, a global
  event bus, generalized event sourcing, or CQRS beyond existing replay/aggregation needs.

## Compatibility and overlap ledger

Statuses were read from the shared store on 2026-09-01. `blocking` reserves a live bead's exact
symbols; `adopt` means a later slice consumes the landed behavior without reimplementation;
`non-overlapping` means the named work does not own selected symbols; `supersede` would require
proof that older work is wholly replaced. Nothing here supersedes an open bead.

| Bead | Disposition | Exact symbols/paths and downstream action |
| --- | --- | --- |
| `bh-zbht5` — E5 worktree lifecycle vocabulary | **blocking**, then adopt | Reserves `wt_status.WtClassification`, `wt_status.classify`, terminal close-reason semantics, and worktree landed/prune/reap/reclaim detection. `bh-bptze.3` must wait and adopt the landed vocabulary; it must not invent parallel states or broaden safe prune. |
| `bh-9yrn.2` — precious overlay in classifier | **blocking**, then adopt | Reserves `wt_status.WtStatus`, `wt_status.classify`, `worktree._classify_entry`, and `worktree_inventory.impl__classify_entry`, including `precious_by_path` and safe=false. This is an explicit blocker of `bh-bptze.3`. |
| `bh-bhsqp` — exit-127/parent-query leftovers | **blocking**, then adopt | Reserves `worktree.run_init` / `worktree_verify.impl_run_init`; `work_dispatch.impl__molecule_members`, `work_group.ready_children`, `work_metrics.flow_events`, `work_show._review_molecule_intent`, and the two `localloop` parent-list sites. Worktree/work slices wait on those symbols and retain the bead's chosen parent-edge/prefix semantics. |
| `bh-o6lsj` — reconcile landed bounced bead | **blocking**, then adopt | Its preserved branch commit `567ca343` moves the already-landed check ahead of review gating. Current refactoring has moved the equivalent owner to `work._merge_bead` / `work_merge.impl__merge_bead`; `bh-bptze.4` must port the accepted behavior after the live bead lands, never independently recreate or supersede it. |
| `bh-8kn42` — one raw-`bd` seam | **non-overlapping; defer and adopt** | It owns `bd._run`, `bd.err_line`, `bd.passthrough`, `engine.*._state_call`, `storage_migrate._significant_err_line`, and direct callers in onboard/registry/hub/hq/validate/backup/dolt_health/CLI. This molecule may declare capability ports and wrap the current adapter, but may not consolidate those symbols; later adapters adopt its landed seam. |
| `bh-q0lol` — unified host daemon | **non-overlapping consumer** | Owns host-daemon/FastMCP/operator HTTP/SSE composition. Its design explicitly consumes and does not replace `state_stream`, `validation_records`, `run_journal`, summaries, or `public_readers`; the state slice preserves it as a reverse-dependent transport consumer. |
| `bh-hx39m` — pinned Dolt S3 snapshot | **non-overlapping** | Owns `flake.nix` and toolchain metadata only. No selected capability symbol moves; future persistence adapters may rely on the resulting binary capability without claiming S3 behavior here. |
| `bh-p3lg5` — `BH_WORKTREES` parsing fix | **non-overlapping; adopt invariant** | Owns config model/settings parsing and `config.worktrees_root()` precedence. Hives/worktrees preserve the landed scalar runtime-override behavior but do not edit config ownership. |
| `bh-cx9me`, `bh-ab5e7` — hermetic fence | **non-overlapping production; defer harness** | Own `scripts/hermetic.sh`, justfile wiring, and fence tests, not selected production symbols. Capability closure work must run on and adopt whichever harness revision lands; it must not copy or weaken the fence. |
| `bh-0gpn.1` — bd/git-workspace install spike | **non-overlapping** | Evidence-only under `docs/spikes`; no product symbol. Its GO/NO-GO can inform concrete adapter provisioning later but does not block contracts or authorize tool installation here. |
| `bh-cgcg` — internal/external Git workspace | **blocking**, then adopt | Reserves `identity.workspace_root`, `identity.workspace_mode`, git-workspace config precedence, and provisioning behavior in `gitworkspace` / `gitworkspace_plugin`, which are selected by the worktrees slice and consumed by onboarding. `bh-bptze.2/.3` must adopt its landed contract before moving those callers. |

This reconciliation adds one sequencing constraint not visible in the original five-bead list:
`bh-cgcg` must resolve before the workspace-realization portions of hives/worktrees move. The
internal capability DAG itself is unchanged; external gates attach to its existing nodes and do
not introduce a capability cycle.

## Limitations and non-claims

Static imports and AST calls do not observe reflection, subprocess entry points, external
repositories, or downstream packages. RepoWise's fresh source index does not imply fresh dynamic
per-test coverage. Coverage is statement coverage without branch or per-test contexts. The five
module closures are currently registered as absent and remain advisory until their implementation
beads turn them present with owned tests. Full submit and land gates remain mandatory.
